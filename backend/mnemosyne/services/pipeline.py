"""Pipeline stages, expressed as job runners.

Each stage takes a JobContext and the AppContext, reports progress through the
context, and updates session state through the session service. Routes only
submit these; they never run ML work inline.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from ..jobs import JobContext
from ..models.session import DEFAULT_SESSION_NAME, Session, SessionStatus, transcript_hash
from ..transcription.engine import AudioSource
from ..transcription.glossary import (
    apply_corrections,
    glossary_instructions,
    llm_correct,
    parse_glossary,
)
from .tasks import carry_over

if TYPE_CHECKING:
    from ..api.context import AppContext

logger = logging.getLogger(__name__)


def sources_for_session(
    session: Session, per_source: bool, local_speaker_name: str
) -> list[AudioSource]:
    """Decide what to feed the engine.

    With separate mic and system recordings present, transcribe each: the mic
    file is the local user (labelled, not diarized) and the system file holds
    everyone else (diarized). A lone mic recording may contain a whole room,
    so it is diarized. Otherwise fall back to the mixed file.
    """
    recordings = [r for r in session.recordings if Path(r.path).is_file()]
    kinds = {r.source for r in recordings}
    if per_source and recordings and (len(recordings) > 1 or "system" in kinds):
        label_mic = "system" in kinds
        return [
            AudioSource(
                path=r.path,
                kind=r.source,
                speaker_label=local_speaker_name if (r.source == "mic" and label_mic) else None,
            )
            for r in recordings
        ]
    if session.audio_file:
        return [AudioSource(path=session.audio_file, kind="mixed")]
    return []


def transcribe_session(app: AppContext, session_id: str):
    """Build the transcription job runner for a session."""

    async def run(ctx: JobContext) -> dict:
        session = app.sessions.get_session(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")
        settings = app.settings
        sources = sources_for_session(
            session, settings.per_source_transcription, settings.local_speaker_name
        )
        if not sources:
            raise ValueError(f"Session {session_id} has no audio to transcribe")

        app.sessions.set_status(session_id, SessionStatus.TRANSCRIBING)
        try:
            ctx.update("Loading models...")
            ctx.emit({"type": "status", "session_id": session_id, "message": "Loading models..."})
            engine = await app.models.ensure_loaded()

            ctx.update("Transcribing...")
            ctx.emit({"type": "status", "session_id": session_id, "message": "Transcribing..."})
            logger.info(
                "Transcribing session %s from %d source(s): %s",
                session_id,
                len(sources),
                [(s.kind, s.speaker_label) for s in sources],
            )

            last = {"stage": "", "frac": -1.0, "t": 0.0}

            def on_progress(stage: str, frac: float) -> None:
                # Throttle: stage changes, every 1 %, or at least once a second.
                now = time.monotonic()
                if stage == last["stage"] and frac - last["frac"] < 0.01 and now - last["t"] < 1:
                    return
                last.update(stage=stage, frac=frac, t=now)
                ctx.update(stage + "...", progress=round(min(max(frac, 0.0), 0.99), 3))

            segments = []
            async for segment in engine.transcribe_sources(sources, on_progress=on_progress):
                segments.append(segment)
                ctx.emit(
                    {
                        "type": "transcription",
                        "session_id": session_id,
                        "segment": segment.model_dump(),
                    }
                )

            embeddings = dict(getattr(engine, "last_speaker_embeddings", {}) or {})
            mapping = app.speakers.match(embeddings) if settings.auto_label_speakers else {}
            if embeddings:
                # Store under the final labels so a later rename can still enroll.
                app.repo.set_session_embeddings(
                    session_id, {mapping.get(k, k): v for k, v in embeddings.items()}
                )
            if mapping:
                segments = [
                    s.model_copy(update={"speaker": mapping.get(s.speaker, s.speaker)})
                    for s in segments
                ]
                ctx.emit(
                    {
                        "type": "status",
                        "session_id": session_id,
                        "message": "Recognized " + ", ".join(sorted(mapping.values())),
                    }
                )
            dropped = getattr(engine, "last_dropped_echo", 0)

            glossary = parse_glossary(settings.glossary)
            segments, fixed = apply_corrections(segments, glossary)
            if settings.glossary_llm_correct and glossary.terms and segments:
                ctx.update("Checking names and terms...", progress=0.99)

                async def complete(system: str, user: str) -> str:
                    return await app.summarizer.complete(
                        system, user, settings.default_provider, settings.default_model
                    )

                segments, llm_fixed = await llm_correct(segments, glossary, complete)
                fixed += llm_fixed

            app.sessions.set_transcript(session_id, segments)
            if settings.auto_summarize and segments:
                app.jobs.submit(
                    "summarize", summarize_session(app, session_id), session_id=session_id
                )
            ctx.update("Transcription complete")
            ctx.emit(
                {"type": "status", "session_id": session_id, "message": "Transcription complete"}
            )
            return {
                "segments": len(segments),
                "sources": len(sources),
                "echo_dropped": dropped,
                "glossary_fixes": fixed,
            }
        except Exception as e:
            app.sessions.set_status(session_id, SessionStatus.ERROR)
            ctx.emit({"type": "error", "session_id": session_id, "message": str(e)})
            raise

    return run


def _auto_export(app: AppContext, session_id: str) -> str | None:
    """Export to Obsidian after a summary; failures are logged, never raised."""
    from ..api.routes.export import build_exporter

    try:
        session = app.sessions.get_session(session_id)
        path = build_exporter(app, app.settings.obsidian_vault_path).export(session)
        logger.info("Auto-exported session %s to %s", session_id, path)
        return str(path)
    except Exception:
        logger.warning("Auto-export failed for session %s", session_id, exc_info=True)
        return None


def summarize_session(
    app: AppContext,
    session_id: str,
    provider: str = "",
    model: str = "",
    style: str = "",
    instructions: str | None = None,
):
    """Build the summarize job runner. Blank arguments fall back to settings."""

    async def run(ctx: JobContext) -> dict:
        session = app.sessions.get_session(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")
        if not session.transcript:
            raise ValueError("Session has no transcript")
        st = app.settings
        prov = provider or st.default_provider
        mdl = model or st.default_model
        sty = style or st.summary_style
        instr = st.summary_instructions if instructions is None else instructions
        spelling = glossary_instructions(parse_glossary(st.glossary))
        if spelling:
            instr = f"{instr}\n{spelling}".strip()
        if session.attendees:
            instr = (
                f'{instr}\nScheduled attendees of "{session.name}": '
                f"{', '.join(session.attendees)}. Speaker labels are not necessarily these "
                "people; only attribute to a name when the transcript makes it clear."
            ).strip()

        ctx.update(f"Summarizing with {prov}/{mdl or 'default model'}")
        ctx.emit({"type": "status", "session_id": session_id, "message": "Summarizing..."})
        try:
            result = await app.summarizer.summarize(
                segments=[s.model_dump() for s in session.transcript],
                provider_name=prov,
                model=mdl,
                style=sty,
                instructions=instr,
            )
        except Exception as e:
            ctx.emit({"type": "error", "session_id": session_id, "message": str(e)})
            raise
        result["data"].source_hash = transcript_hash(session.transcript)
        # Keep done flags and issue links of items that survive a re-summarize.
        current = app.sessions.get_session(session_id)
        carry_over(current.summary_data if current else None, result["data"])
        app.sessions.set_summary(session_id, result["summary"], result["data"])
        title = result["data"].title
        # Re-read: the user may have renamed the session while the LLM was running.
        current = app.sessions.get_session(session_id)
        if (
            st.auto_name_sessions
            and title
            and current is not None
            and current.name == DEFAULT_SESSION_NAME
        ):
            app.sessions.rename_session(session_id, title)
        exported = None
        if st.obsidian_auto_export and st.obsidian_vault_path:
            exported = _auto_export(app, session_id)
        ctx.update("Summary ready")
        return {
            "provider": result["provider"],
            "model": result["model"],
            "title": title,
            "exported": exported,
        }

    return run


def ask_question(app: AppContext, question: str, provider: str = "", model: str = ""):
    """Build the ask job runner; the saved Ask is the job result."""

    async def run(ctx: JobContext) -> dict:
        from .ask_service import answer_question

        st = app.settings
        prov = provider or st.default_provider
        ctx.update("Searching your meetings...")
        ask = await answer_question(
            app.repo,
            app.summarizer,
            question,
            prov,
            model or st.default_model,
            extra_instructions=glossary_instructions(parse_glossary(st.glossary)),
        )
        app.repo.save_ask(ask)
        ctx.update("Answer ready")
        return ask.model_dump(mode="json")

    return run


def make_digest(app: AppContext, start, end, provider: str = "", model: str = ""):
    """Build the digest job runner; the saved Digest is the job result."""

    async def run(ctx: JobContext) -> dict:
        from .digest_service import build_digest, write_to_vault

        st = app.settings
        prov = provider or st.default_provider
        mdl = model or st.default_model
        extra = glossary_instructions(parse_glossary(st.glossary))
        ctx.update("Gathering meetings...")
        sessions = await asyncio.to_thread(app.repo.sessions_between, start, end)
        if any(s.summary.strip() for s in sessions):
            mdl = await app.summarizer.resolve_model(prov, mdl)

        async def complete(system: str, user: str) -> str:
            system = f"{system}\n{extra}".strip() if extra else system
            return await app.summarizer.complete(system, user, prov, mdl)

        ctx.update(f"Writing the digest with {prov}/{mdl or 'default model'}")
        digest = await build_digest(sessions, start, end, complete, prov, mdl)
        if st.obsidian_vault_path:
            try:
                digest.path = str(
                    write_to_vault(digest, st.obsidian_vault_path, st.obsidian_subfolder)
                )
            except Exception:
                logger.warning(
                    "Could not write digest %s to the vault", digest.label, exc_info=True
                )
        app.repo.save_digest(digest)
        ctx.update("Digest ready")
        return digest.model_dump(mode="json")

    return run


def live_transcribe(app: AppContext, session_id: str, recording):
    """Build the live-transcription job runner for an active RecordingSession.

    Runs until cancelled (the stop-recording route cancels it before queuing
    the final transcription job).
    """
    from ..audio.capture import list_devices
    from ..transcription.live import LiveSource, LiveTranscriber

    settings = app.settings
    try:
        devices = {d.id: d for d in list_devices()}
    except Exception:
        devices = {}

    sources = []
    multi = len(recording.processes) > 1
    for proc in recording.processes:
        device = devices.get(proc.device_id)
        is_system = device is not None and device.is_output
        if multi:
            speaker = settings.remote_speaker_name if is_system else settings.local_speaker_name
        else:
            speaker = "Speaker"
        # With separate channels the mic is the local user; everything else may hold
        # several voices.
        diarize = settings.live_diarization and (is_system or not multi)
        sources.append(
            LiveSource(
                path=proc.output_path,
                speaker=speaker,
                kind="system" if is_system else "mic",
                diarize=diarize,
            )
        )

    embedder = clusterer = None
    if any(src.diarize for src in sources):
        from ..transcription.live_speakers import OnlineClusterer

        embedder = app.models.live_embedder
        if embedder is not None:
            clusterer = OnlineClusterer(
                threshold=settings.live_speaker_threshold,
                known_threshold=settings.speaker_match_threshold,
                known={p.name: p.embedding for p in app.repo.list_speakers()},
            )
            # Never hand the local user's name to a remote voice.
            if multi:
                clusterer.known.pop(settings.local_speaker_name, None)

    async def run(ctx: JobContext) -> dict:
        ctx.update("Live transcription")
        live = LiveTranscriber(
            transcriber=app.models.live_transcriber,
            sources=sources,
            emit=ctx.emit,
            session_id=session_id,
            interval=settings.live_interval_seconds,
            language=settings.language or None,
            embedder=embedder,
            clusterer=clusterer,
        )
        try:
            await live.run()
        except asyncio.CancelledError:
            return _live_result(live)
        return _live_result(live)

    return run


def _live_result(live) -> dict:
    speakers = [c.label for c in live.clusterer.clusters] if live.clusterer else []
    return {"segments": len(live.committed), "speakers": speakers}
