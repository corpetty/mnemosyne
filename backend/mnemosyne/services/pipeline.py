"""Pipeline stages, expressed as job runners.

Each stage takes a JobContext and the AppContext, reports progress through the
context, and updates session state through the session service. Routes only
submit these; they never run ML work inline.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from ..jobs import JobContext
from ..models.session import DEFAULT_SESSION_NAME, Session, SessionStatus, transcript_hash
from ..models.transcript import TranscriptSegment
from ..storage.crypto import plaintext_async
from ..summarization.privacy import LOCAL_ONLY_ERROR, is_cloud, redact_transcript
from ..summarization.prompts import meeting_date_instructions
from ..transcription.engine import AudioSource
from ..transcription.glossary import (
    apply_corrections,
    glossary_instructions,
    llm_correct,
    parse_glossary,
)
from . import history
from .copilot import agenda_hint, bookmark_hint, copilot_hint
from .tasks import add_live_todos, carry_over

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


def _part_sources(session, part: int, starts: dict[int, float], settings, stack) -> list:
    """What to transcribe for one part of a meeting. A single-part meeting is transcribed as
    before; for a part of a longer one, its own per-source files, else its stretch of the
    meeting's audio cut to a private temporary file."""
    from ..storage.crypto import _scratch_dir
    from .parts import cut

    if len(starts) == 1:
        return sources_for_session(
            session, settings.per_source_transcription, settings.local_speaker_name
        )
    own = session.model_copy(
        update={"recordings": [r for r in session.recordings if r.part == part], "audio_file": None}
    )
    sources = sources_for_session(
        own, settings.per_source_transcription, settings.local_speaker_name
    )
    if sources or not session.audio_file:
        return sources
    later = sorted(o for o in starts.values() if o > starts[part])
    out = _scratch_dir() / f"{session.id}-part{part}.ogg"
    stack.callback(out.unlink, missing_ok=True)
    cut(Path(session.audio_file), starts[part], later[0] if later else None, out, None)
    return [AudioSource(path=str(out), kind="mixed")]


def transcribe_session(app: AppContext, session_id: str, parts: list[int] | None = None):
    """Build the transcription job runner for a session: every part of it, or only `parts`
    (a part recorded after the rest was transcribed), keeping the other parts' lines."""

    async def run(ctx: JobContext) -> dict:
        from .parts import match_speakers, offsets, part_of, shift

        session = app.sessions.get_session(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")
        settings = app.settings
        starts = offsets(session)
        todo = sorted(starts) if parts is None else [p for p in sorted(starts) if p in parts]
        kept = [s for s in session.transcript if part_of(s.start, starts) not in todo]
        if not kept:
            todo = sorted(starts)  # nothing to keep: do the whole meeting
        if not session.audio_file and not session.recordings:
            raise ValueError(f"Session {session_id} has no audio to transcribe")

        before = session.status
        app.sessions.set_status(session_id, SessionStatus.TRANSCRIBING)
        try:
            ctx.update("Loading models...")
            ctx.emit({"type": "status", "session_id": session_id, "message": "Loading models..."})
            engine = await app.models.ensure_loaded()

            ctx.update("Transcribing...")
            ctx.emit({"type": "status", "session_id": session_id, "message": "Transcribing..."})
            for seg in kept:  # the parts not transcribed again, so the view shows the meeting
                ctx.emit(
                    {"type": "transcription", "session_id": session_id, "segment": seg.model_dump()}
                )

            last = {"stage": "", "frac": -1.0, "t": 0.0}

            def on_progress(stage: str, frac: float) -> None:
                # Throttle: stage changes, every 1 %, or at least once a second.
                now = time.monotonic()
                if stage == last["stage"] and frac - last["frac"] < 0.01 and now - last["t"] < 1:
                    return
                last.update(stage=stage, frac=frac, t=now)
                ctx.update(stage + "...", progress=round(min(max(frac, 0.0), 0.99), 3))

            segments = list(kept)
            embeddings = app.repo.get_session_embeddings(session_id) if kept else {}
            n_sources = 0
            for part in todo:
                async with contextlib.AsyncExitStack() as stack:
                    if len(starts) > 1 and session.audio_file:
                        # A multi-part meeting's audio may be encrypted; cut from a plain copy.
                        # Decrypting and cutting hours of audio: in a thread, not on the loop.
                        plain_meeting = await stack.enter_async_context(
                            plaintext_async(session.audio_file, app.file_key)
                        )
                        view = session.model_copy(update={"audio_file": str(plain_meeting)})
                    else:
                        view = session
                    sources = await asyncio.to_thread(
                        _part_sources, view, part, starts, settings, stack
                    )
                    if not sources:
                        continue
                    n_sources += len(sources)
                    logger.info(
                        "Transcribing session %s part %d from %d source(s): %s",
                        session_id,
                        part,
                        len(sources),
                        [(s.kind, s.speaker_label) for s in sources],
                    )
                    # Encrypted audio: the engine reads private plaintext copies, removed after.
                    plain = [
                        dataclasses.replace(
                            s,
                            path=str(
                                await stack.enter_async_context(
                                    plaintext_async(s.path, app.file_key)
                                )
                            ),
                        )
                        for s in sources
                    ]
                    new: list[TranscriptSegment] = []
                    async for segment in engine.transcribe_sources(plain, on_progress=on_progress):
                        new.append(segment)
                part_embeddings = dict(getattr(engine, "last_speaker_embeddings", {}) or {})
                if embeddings or segments:  # a later part: same voices, same labels
                    taken = {s.speaker for s in segments}
                    relabel = match_speakers(
                        part_embeddings, embeddings, taken, settings.speaker_match_threshold
                    )
                    new = [
                        s.model_copy(update={"speaker": relabel.get(s.speaker, s.speaker)})
                        for s in new
                    ]
                    part_embeddings = {relabel[k]: v for k, v in part_embeddings.items()}
                for k, v in part_embeddings.items():
                    embeddings.setdefault(k, v)
                new = shift(new, starts.get(part, 0.0))
                if settings.redact_stored_transcripts:  # before anything shows or keeps them
                    new = redact_transcript(new)
                for segment in new:
                    ctx.emit(
                        {
                            "type": "transcription",
                            "session_id": session_id,
                            "segment": segment.model_dump(),
                        }
                    )
                segments += new
            if not n_sources:
                raise ValueError(f"Session {session_id} has no audio to transcribe")
            segments.sort(key=lambda s: (s.start, s.end))
            if settings.redact_stored_transcripts:
                # Again over the whole meeting: parts kept from before the setting was on, and
                # a cue at the end of one part ("your social?") for a number in the next.
                segments = redact_transcript(segments)

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
            local_only = session is not None and session.local_only
            if (
                settings.glossary_llm_correct
                and glossary.terms
                and segments
                and not (local_only and is_cloud(settings.default_provider))
            ):
                ctx.update("Checking names and terms...", progress=0.99)

                async def complete(system: str, user: str) -> str:
                    return await app.summarizer.complete(
                        system, user, settings.default_provider, settings.default_model
                    )

                segments, llm_fixed = await llm_correct(segments, glossary, complete)
                fixed += llm_fixed

            app.sessions.set_transcript(session_id, segments)
            app.repo.update_fields(session_id, speakers_reviewed=False)  # new labels to name
            if settings.auto_summarize and segments:
                app.jobs.submit(
                    "summarize", summarize_session(app, session_id), session_id=session_id
                )
            ctx.update("Transcription complete")
            ctx.emit(
                {"type": "status", "session_id": session_id, "message": "Transcription complete"}
            )
            history.log(
                app,
                session_id,
                "transcribed",
                parts=todo,
                segments=len(segments),
                engine=getattr(engine, "name", ""),
            )
            return {
                "segments": len(segments),
                "sources": n_sources,
                "parts": todo,
                "echo_dropped": dropped,
                "glossary_fixes": fixed,
            }
        except asyncio.CancelledError:  # cancelled, or the backend shutting down
            app.sessions.set_status(session_id, before)
            raise
        except Exception as e:
            app.sessions.set_status(session_id, SessionStatus.ERROR)
            ctx.emit({"type": "error", "session_id": session_id, "message": str(e)})
            history.log(app, session_id, "transcribe_failed", error=str(e))
            raise

    return run


def _auto_export(app: AppContext, session_id: str) -> str | None:
    """Export to Obsidian after a summary; failures are logged, never raised."""
    from ..api.routes.export import export_session

    try:
        session = app.sessions.get_session(session_id)
        path = export_session(app, session, app.settings.obsidian_vault_path)
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
        from .external_notes import NOTES_ONLY, notes_as_transcript, notes_hint

        notes_only = not session.transcript and bool(session.external_notes)
        if not session.transcript and not notes_only:
            raise ValueError("Session has no transcript")
        segments = notes_as_transcript(session.external_notes) if notes_only else session.transcript
        st = app.settings
        prov = provider or st.default_provider
        mdl = model or st.default_model
        from .meeting_types import summary_instructions, summary_style

        sty = style or summary_style(st, session)
        if session.local_only and is_cloud(prov):
            raise ValueError(LOCAL_ONLY_ERROR.format(provider=prov))
        instr = summary_instructions(st, session) if instructions is None else instructions
        spelling = glossary_instructions(parse_glossary(st.glossary))
        if spelling:
            instr = f"{instr}\n{spelling}".strip()
        if session.attendees:
            instr = (
                f'{instr}\nScheduled attendees of "{session.name}": '
                f"{', '.join(session.attendees)}. Speaker labels are not necessarily these "
                "people; only attribute to a name when the transcript makes it clear."
            ).strip()
        instr = f"{instr}\n{meeting_date_instructions(session.created_at)}".strip()
        live_notes = copilot_hint(session.copilot_notes)
        if live_notes:
            instr = f"{instr}\n{live_notes}".strip()
        planned = agenda_hint(session.agenda)
        if planned:
            instr = f"{instr}\n{planned}".strip()
        others = NOTES_ONLY if notes_only else notes_hint(session.external_notes)
        if others:
            instr = f"{instr}\n{others}".strip()
        from .assets import resources_hint

        shared = resources_hint(app, session_id)
        if shared:
            instr = f"{instr}\n{shared}".strip()
        marked = bookmark_hint(session.bookmarks, session.transcript)
        if marked:
            instr = f"{instr}\n{marked}".strip()

        ctx.update(f"Summarizing with {prov}/{mdl or 'default model'}")
        ctx.emit({"type": "status", "session_id": session_id, "message": "Summarizing..."})
        try:
            result = await app.summarizer.summarize(
                segments=[s.model_dump() for s in segments],
                provider_name=prov,
                model=mdl,
                style=sty,
                instructions=instr,
                on_progress=ctx.update,
            )
        except Exception as e:
            ctx.emit({"type": "error", "session_id": session_id, "message": str(e)})
            history.log(app, session_id, "summarize_failed", provider=prov, error=str(e))
            raise
        result["data"].source_hash = transcript_hash(session.transcript)
        if notes_only:  # no timeline to point at
            data = result["data"]
            data.decision_at = [None] * len(data.decisions)
            data.question_at = [None] * len(data.open_questions)
            data.chapters = []
            for item in data.action_items:
                item.at = None
            for fact in data.client_facts:
                fact.at = None
        # To-dos the copilot heard that the summary missed; then keep done flags and issue
        # links of items that survive a re-summarize.
        add_live_todos(session.copilot_notes, result["data"])
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
            exported = await asyncio.to_thread(_auto_export, app, session_id)
        ctx.update("Summary ready")
        history.log(
            app, session_id, "summarized", provider=result["provider"], model=result["model"]
        )
        from .hubspot import auto_push

        crm = await auto_push(app, session_id)  # never raises
        return {
            "provider": result["provider"],
            "model": result["model"],
            "title": title,
            "exported": exported,
            "hubspot": crm,
        }

    return run


def ask_question(
    app: AppContext,
    question: str,
    provider: str = "",
    model: str = "",
    exclude_local_only: bool = False,
):
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
            index=app.index,
            exclude_local_only=exclude_local_only,
        )
        app.repo.save_ask(ask)
        ctx.update("Answer ready")
        return ask.model_dump(mode="json")

    return run


def draft_followup(app: AppContext, session_id: str, style: str = "email", provider="", model=""):
    """Build the follow-up job runner; the draft is saved on summary_data.followup."""

    async def run(ctx: JobContext) -> dict:
        from .followup import SYSTEM, clean, prompt_for

        session = app.sessions.get_session(session_id)
        if session is None or session.summary_data is None:
            raise ValueError("Summarize the meeting first")
        st = app.settings
        prov = provider or st.default_provider
        if session.local_only and is_cloud(prov):
            raise ValueError(LOCAL_ONLY_ERROR.format(provider=prov))
        mdl = await app.summarizer.resolve_model(prov, model or st.default_model)
        extra = glossary_instructions(parse_glossary(st.glossary))
        system = f"{SYSTEM[style]}\n{extra}".strip() if extra else SYSTEM[style]
        ctx.update(f"Drafting with {prov}/{mdl}")
        text = clean(await app.summarizer.complete(system, prompt_for(session), prov, mdl))
        # Re-read so a concurrent edit (e.g. ticking an item) is not overwritten.
        current = app.sessions.get_session(session_id)
        if current is not None and current.summary_data is not None:
            current.summary_data.followup = text
            app.sessions.set_summary(session_id, current.summary, current.summary_data)
        return {"followup": text, "style": style, "provider": prov, "model": mdl}

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
        if is_cloud(prov):
            sessions = [s for s in sessions if not s.local_only]
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
    from ..transcription.mentions import MentionSpotter, parse_keywords

    settings = app.settings
    multi = len(recording.processes) > 1

    def prepare():
        """Everything that blocks (pw-dump, importing torch, building models), in a thread:
        on the event loop it held up every request at the start of a recording."""
        try:
            devices = {d.id: d for d in list_devices()}
        except Exception:
            devices = {}
        sources = []
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

        embedder = clusterer = diarizer = stream_model = None
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
            diarizer = app.models.live_rediarizer
            stream_model = app.models.live_stream_model
        return app.models.live_transcriber, sources, embedder, clusterer, diarizer, stream_model

    def match_names(embeddings: dict[str, list[float]]) -> dict[str, str]:
        names = app.speakers.match(embeddings)
        if multi:  # the local user is on the mic channel, never a remote voice
            names = {k: v for k, v in names.items() if v != settings.local_speaker_name}
        return names

    async def run(ctx: JobContext) -> dict:
        ctx.update("Live transcription")
        stream_model = None
        try:
            prepared = await asyncio.to_thread(prepare)
            transcriber, sources, embedder, clusterer, diarizer, stream_model = prepared
            if stream_model is not None:
                await _attach_streams(stream_model, sources)
        except asyncio.CancelledError:  # stopped before it got going
            return {"segments": 0, "speakers": []}
        namer = None
        if any(src.stream is not None for src in sources):
            from ..transcription.live_streaming import VoiceNamer

            namer = VoiceNamer(
                embedder,
                match_names=match_names,
                voices=_session_voices(app, session_id),
                threshold=settings.speaker_match_threshold,
            )
        live = LiveTranscriber(
            transcriber=transcriber,
            sources=sources,
            emit=ctx.emit,
            session_id=session_id,
            interval=settings.live_interval_seconds,
            language=settings.language or None,
            embedder=embedder,
            clusterer=clusterer,
            mentions=MentionSpotter(parse_keywords(settings.mention_keywords)),
            silence_db=settings.live_silence_db,
            adaptive=settings.live_adaptive,
            namer=namer,
        )
        app.live[session_id] = live  # read by the copilot
        rediarize = None
        if diarizer is not None:
            from ..transcription.live_rediarize import LiveRediarizer

            rediarizer = LiveRediarizer(
                diarizer,
                live,
                ctx.emit,
                session_id,
                interval=settings.live_rediarize_seconds,
                match_names=match_names,
            )
            rediarize = asyncio.create_task(rediarizer.run())
        try:
            await live.run()
        except asyncio.CancelledError:
            return _live_result(live)
        finally:
            if rediarize is not None:
                rediarize.cancel()
            app.live.pop(session_id, None)
            if namer is not None:
                _session_voices(app, session_id)  # the next part may start within the hour
            for kind, stats in live.stream_stats().items():
                logger.info("Nemotron streaming (%s): %s", kind, stats)
        return _live_result(live)

    return run


async def _attach_streams(stream_model, sources) -> None:
    """Give every diarized source a Nemotron stream. When the model cannot load, the
    sources keep voice clustering (logged; the live status says which one is in use)."""
    try:
        await stream_model.load()
    except Exception:
        logger.warning("Nemotron streaming unavailable; live speakers by voice", exc_info=True)
        return
    from ..transcription.live_streaming import SpeakerTimeline

    for src in sources:
        if src.diarize:
            src.stream = stream_model.stream()
            src.timeline = SpeakerTimeline()


LIVE_VOICES_TTL = 3600.0  # seconds a finished meeting's live voices are kept for its next part


def _session_voices(app: AppContext, session_id: str) -> dict:
    """This meeting's streamed speakers' voices (label -> embedding), shared by its parts;
    other meetings' are dropped an hour after their last live transcript."""
    now = time.monotonic()
    for sid, (at, _) in list(app.live_voices.items()):
        if sid != session_id and sid not in app.live and now - at > LIVE_VOICES_TTL:
            del app.live_voices[sid]
    _, voices = app.live_voices.get(session_id, (now, {}))
    app.live_voices[session_id] = (now, voices)
    return voices


def _live_result(live) -> dict:
    speakers = [c.label for c in live.clusterer.clusters] if live.clusterer else []
    speakers += [s for s in dict.fromkeys(live.names.labels.values()) if s not in speakers]
    return {"segments": len(live.committed), "speakers": speakers}
