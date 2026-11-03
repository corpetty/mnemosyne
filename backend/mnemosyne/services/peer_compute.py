"""Using the models of another of this person's computers, and lending ours.

A person's computers paired for library sync (services/sync_peers.py) already reach each
other's whole API through the link tunnel. This puts that to work for the heavy jobs: a laptop
can summarize with the LLMs the desktop uses (summarization/peer.py: provider `peer:<id>`) and
have its meetings transcribed by the desktop's engine (transcription/peer.py: setting
`transcribe_on`, with `transcribe_on_fallback` to do it here when the desktop cannot).

Lending (api/routes/compute.py) runs on the other computer: its own providers, never the cloud
ones (LENT), and its own engine in a `peer_transcribe` job that waits for its transcriptions
(the same limit of one), on audio kept only in its scratch folder until the job ends.
Never on a team server, and never passed on: a computer does not lend what it borrows.
"""

from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ..summarization.peer import Connect, PeerProvider, PeerUnavailable
from .link import LinkError
from .sync_peers import ensure_dialed

if TYPE_CHECKING:
    from ..api.context import AppContext
    from ..transcription.engine import TranscriptionEngine
    from ..transcription.peer import PeerEngine
    from .sync_peers import SyncPeer

logger = logging.getLogger(__name__)

# Providers another computer may use: they keep meetings on the person's machines.
LENT = ("vllm", "ollama", "local", "demo")
KEEP_FINISHED = 600.0  # seconds a finished transcription's result waits to be fetched
MAX_SOURCES = 8
SOURCE_KINDS = ("mic", "system", "mixed")
OPTION_KEYS = (
    "language",
    "min_speakers",
    "max_speakers",
    "echo_dedup",
    "echo_similarity",
    "mic_speech_gate",
)


# ---- using another computer -----------------------------------------------------------------


def connector(ctx: AppContext, peer_id: str) -> Connect:
    """An async factory for an httpx client to that computer's backend, dialing it first when
    the sidecar has no listener for it. Raises PeerUnavailable with a reason for the person."""

    async def connect():
        peer = ctx.sync_peers.get(peer_id)
        if peer is None:
            raise PeerUnavailable("That computer is no longer paired with this one")
        if not peer.listen and ctx.link.status.running:
            try:
                await ensure_dialed(ctx.link, ctx.sync_peers, peer)
            except LinkError as e:
                raise PeerUnavailable(f"{peer.name} could not be reached ({e})") from e
            peer = ctx.sync_peers.get(peer_id) or peer
        if not peer.listen:
            raise PeerUnavailable(
                f"{peer.name} could not be reached: remote access must be on on both computers"
            )
        return ctx.sync.client_for(peer)

    return connect


class PeerProviders:
    """The `peer:<id>` providers, one per paired computer, kept so their model lists stay
    cached (SummarizationService.peer_source)."""

    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self._providers: dict[str, PeerProvider] = {}

    def __call__(self) -> dict[str, PeerProvider]:
        if self.ctx.settings.team_mode:
            return {}
        out: dict[str, PeerProvider] = {}
        for peer in self.ctx.sync_peers.peers():
            provider = self._providers.get(peer.id)
            if provider is None:
                provider = PeerProvider(peer.id, peer.name, connector(self.ctx, peer.id))
                self._providers[peer.id] = provider
            provider.peer_name = peer.name
            out[provider.name] = provider
        return out


def remote_engine(
    ctx: AppContext, fallback: Callable[[], Awaitable[TranscriptionEngine]]
) -> PeerEngine | None:
    """The engine for final transcriptions when `transcribe_on` names a paired computer (None:
    this computer's own). A computer no longer paired is ignored: transcribed here."""
    st = ctx.settings
    if not st.transcribe_on or st.team_mode:
        return None
    peer = ctx.sync_peers.get(st.transcribe_on)
    if peer is None:
        logger.warning("transcribe_on names a computer no longer paired: transcribing here")
        return None
    from ..transcription.peer import PeerEngine

    return PeerEngine(
        peer.name,
        connector(ctx, peer.id),
        options={k: getattr(st, k) for k in OPTION_KEYS},
        fallback=fallback if st.transcribe_on_fallback else None,
    )


# ---- lending to another computer ------------------------------------------------------------


def lent_providers(ctx: AppContext) -> list[str]:
    """This computer's providers another may use, its default first."""
    names = [n for n in LENT if n in ctx.summarizer.local_providers]
    default = ctx.settings.default_provider
    return sorted(names, key=lambda n: n != default)


async def catalog(ctx: AppContext) -> list[dict]:
    """[{provider, models, max_chars}] of the lent providers that have models; the default
    model first."""
    out = []
    st = ctx.settings
    for name in lent_providers(ctx):
        provider = ctx.summarizer.local_providers[name]
        models = await provider.list_models()
        if not models:
            continue
        if name == st.default_provider and st.default_model in models:
            models = [st.default_model] + [m for m in models if m != st.default_model]
        out.append(
            {
                "provider": name,
                "models": models,
                "max_chars": int(getattr(provider, "max_chars", 0) or 0),
            }
        )
    return out


def lend_provider(ctx: AppContext, provider: str) -> str:
    """The provider a request may use here: the one asked for when lent, or (none asked) the
    default when lent, else the first lent. ValueError when there is none."""
    lent = lent_providers(ctx)
    if provider:
        if provider not in lent:
            raise ValueError(f"This computer does not lend '{provider}'")
        return provider
    if not lent:
        raise ValueError("This computer has no models to lend")
    return lent[0]


@dataclass
class HostTask:
    id: str
    peer_id: str
    peer_name: str
    folder: Path
    job_id: str = ""
    engine: str = ""
    result: dict[str, Any] | None = None
    finished_at: float = 0.0
    files: list[Path] = field(default_factory=list)


class ComputeHost:
    """Transcriptions this computer runs for its paired computers (in memory, per start)."""

    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self.tasks: dict[str, HostTask] = {}

    def new_task(self, peer: SyncPeer) -> HostTask:
        from ..storage.crypto import scratch_dir

        self.prune()
        task_id = uuid4().hex[:12]
        folder = scratch_dir() / f"compute-{task_id}"
        folder.mkdir(mode=0o700, parents=True)
        task = HostTask(id=task_id, peer_id=peer.id, peer_name=peer.name, folder=folder)
        self.tasks[task_id] = task
        return task

    def get(self, task_id: str, peer: SyncPeer) -> HostTask | None:
        task = self.tasks.get(task_id)
        return task if task is not None and task.peer_id == peer.id else None

    def start(self, task: HostTask, sources: list[dict], options: dict) -> None:
        """Queue the job; `task.files` hold the uploaded audio, one per source."""
        from ..jobs import JobContext
        from ..transcription.engine import AudioSource

        ctx = self.ctx
        audio = [
            AudioSource(path=str(path), kind=s["kind"], speaker_label=s.get("speaker_label"))
            for path, s in zip(task.files, sources, strict=True)
        ]

        async def run(jctx: JobContext) -> dict:
            try:
                jctx.update(f"Loading models for {task.peer_name}...")
                engine = await ctx.models.ensure_loaded()
                view = with_options(engine, options)
                task.engine = str(getattr(view, "name", "") or "")

                def on_progress(stage: str, frac: float) -> None:
                    jctx.update(f"{stage}...", progress=round(min(max(frac, 0.0), 0.99), 3))

                jctx.update("Transcribing...")
                segments = [s async for s in view.transcribe_sources(audio, on_progress)]
                ctx.models.touch()
                kinds = {id(seg): kind for seg, kind in getattr(view, "last_segment_sources", [])}
                task.result = {
                    "segments": [
                        {**seg.model_dump(mode="json"), "source": kinds.get(id(seg), "")}
                        for seg in segments
                    ],
                    "embeddings": dict(getattr(view, "last_speaker_embeddings", {}) or {}),
                    "dropped_echo": int(getattr(view, "last_dropped_echo", 0) or 0),
                    "dropped_noise": int(getattr(view, "last_dropped_noise", 0) or 0),
                }
                logger.info(
                    "Transcribed %d source(s) for %s: %d segments",
                    len(audio),
                    task.peer_name,
                    len(segments),
                )
                return {"segments": len(segments), "for": task.peer_name}
            finally:
                shutil.rmtree(task.folder, ignore_errors=True)
                task.finished_at = time.monotonic()

        task.job_id = ctx.jobs.submit("peer_transcribe", run).id

    def status(self, task: HostTask) -> dict:
        job = self.ctx.jobs.get(task.job_id)
        state = {
            "queued": "queued",
            "running": "running",
            "completed": "done",
            "failed": "failed",
            "cancelled": "cancelled",
        }.get(str(job.status) if job else "", "failed")
        if state == "done" and task.result is None:
            state = "failed"
        out: dict[str, Any] = {
            "id": task.id,
            "state": state,
            "message": job.message if job else "",
            "progress": job.progress if job else None,
            "engine": task.engine,
            "error": (job.error if job else "The job was lost") if state == "failed" else None,
        }
        if state == "done":
            out["result"] = task.result
        return out

    async def cancel(self, task: HostTask) -> None:
        """Stop the job (when it still runs) and forget the task."""
        job = self.ctx.jobs.get(task.job_id)
        if job is not None and not job.is_terminal:
            await self.ctx.jobs.cancel(task.job_id)
        shutil.rmtree(task.folder, ignore_errors=True)
        self.tasks.pop(task.id, None)

    def prune(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        for task in list(self.tasks.values()):
            if task.finished_at and now - task.finished_at > KEEP_FINISHED:
                self.tasks.pop(task.id, None)


def with_options(engine: Any, options: dict) -> Any:
    """The engine with the asking computer's options for this job only: a ComposedEngine
    sharing the loaded models. Any other engine (tests' fakes) as it is."""
    from ..transcription.composed import ComposedEngine

    if not isinstance(engine, ComposedEngine):
        return engine
    pick = {k: options[k] for k in OPTION_KEYS if k in options}
    return ComposedEngine(
        transcriber=engine.transcriber,
        diarizer=engine.diarizer,
        language=pick.get("language", engine.language) or None,
        min_speakers=pick.get("min_speakers", engine.min_speakers),
        max_speakers=pick.get("max_speakers", engine.max_speakers),
        echo_dedup=bool(pick.get("echo_dedup", engine.echo_dedup)),
        echo_similarity=float(pick.get("echo_similarity", engine.echo_similarity)),
        mic_speech_gate=bool(pick.get("mic_speech_gate", engine.mic_speech_gate)),
    )
