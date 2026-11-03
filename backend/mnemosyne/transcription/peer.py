"""A TranscriptionEngine that runs on another of this person's computers (a library-sync peer).

The sources are uploaded through the link tunnel to that computer's backend
(api/routes/compute.py), which transcribes and diarizes them with its own engine (its GPU, its
models) in a job of its own, keeps no copy of the audio once done, and hands the segments back.
This one asks how it goes every second, so progress shows here as usual. Live transcription
while recording stays on this computer.

When the other computer cannot be reached, or fails, and `fallback` is given, this computer's
own engine does the work instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import httpx

from ..models.transcript import TranscriptSegment
from ..summarization.peer import Connect, PeerComputeError, PeerUnavailable, detail_of
from .engine import AudioSource, StageProgressFn, TranscriptionEngine

logger = logging.getLogger(__name__)

POLL_INTERVAL = 1.0
UPLOAD_TIMEOUT = 1800.0  # hours of audio over a relayed connection
# Polls in a row that may fail (a dropped tunnel that comes back) before giving up.
POLL_FAILURES = 30


class PeerEngine:
    def __init__(
        self,
        peer_name: str,
        connect: Connect,
        *,
        options: dict | None = None,
        fallback: Callable[[], Awaitable[TranscriptionEngine]] | None = None,
        poll_interval: float = POLL_INTERVAL,
    ):
        self.peer_name = peer_name
        self._connect = connect
        # language, min/max speakers, echo_dedup, echo_similarity, mic_speech_gate: this
        # computer's settings, applied there to this job only.
        self.options = dict(options or {})
        self.fallback = fallback
        self.poll_interval = poll_interval
        self.remote_name = ""  # the other computer's engine, once it said
        self.used_fallback = False
        self.last_speaker_embeddings: dict[str, list[float]] = {}
        self.last_dropped_echo = 0
        self.last_dropped_noise = 0
        self.last_segment_sources: list[tuple[TranscriptSegment, str]] = []

    @property
    def name(self) -> str:
        if self.used_fallback:
            return f"here (instead of {self.peer_name})"
        return f"{self.remote_name or 'engine'} on {self.peer_name}"

    def is_loaded(self) -> bool:
        return True  # the other computer loads its models

    async def load(self) -> None:
        pass

    async def unload(self) -> None:
        pass

    async def transcribe_sources(
        self, sources: list[AudioSource], on_progress: StageProgressFn | None = None
    ) -> AsyncIterator[TranscriptSegment]:
        self.used_fallback = False
        try:
            segments = await self._remote(sources, on_progress)
        except (PeerUnavailable, PeerComputeError) as e:
            if self.fallback is None:
                raise RuntimeError(str(e)) from e
            logger.warning("Transcribing here instead: %s", e)
            if on_progress:
                on_progress(f"{e}. Transcribing on this computer", 0.0)
            self.used_fallback = True
            engine = await self.fallback()
            async for seg in engine.transcribe_sources(sources, on_progress=on_progress):
                yield seg
            for attr in (
                "last_speaker_embeddings",
                "last_dropped_echo",
                "last_dropped_noise",
                "last_segment_sources",
            ):
                setattr(self, attr, getattr(engine, attr, getattr(self, attr)))
            return
        for seg in segments:
            yield seg

    async def _remote(
        self, sources: list[AudioSource], on_progress: StageProgressFn | None
    ) -> list[TranscriptSegment]:
        def report(stage: str, frac: float) -> None:
            if on_progress:
                on_progress(stage, frac)

        report(f"Sending the audio to {self.peer_name}", 0.0)
        spec = {
            "sources": [{"kind": s.kind, "speaker_label": s.speaker_label} for s in sources],
            **self.options,
        }
        try:
            async with await self._connect() as client:
                task_id = await self._upload(client, sources, spec)
                try:
                    body = await self._wait(client, task_id, report)
                except asyncio.CancelledError:
                    with contextlib.suppress(Exception):
                        await asyncio.shield(client.delete(f"/api/compute/transcribe/{task_id}"))
                    raise
                with contextlib.suppress(httpx.HTTPError):  # taken: it may forget the result
                    await client.delete(f"/api/compute/transcribe/{task_id}")
        except httpx.HTTPStatusError as e:
            raise PeerComputeError(f"{self.peer_name} could not transcribe: {detail_of(e)}") from e
        except httpx.HTTPError as e:
            raise PeerUnavailable(
                f"{self.peer_name} did not answer ({e or type(e).__name__})"
            ) from e
        return self._take(body, sources)

    async def _upload(
        self, client: httpx.AsyncClient, sources: list[AudioSource], spec: dict
    ) -> str:
        with contextlib.ExitStack() as stack:
            files = [
                (
                    "files",
                    (
                        f"source-{n}{Path(s.path).suffix}",
                        stack.enter_context(open(s.path, "rb")),
                        "application/octet-stream",
                    ),
                )
                for n, s in enumerate(sources)
            ]
            r = await client.post(
                "/api/compute/transcribe",
                data={"spec": json.dumps(spec)},
                files=files,
                timeout=UPLOAD_TIMEOUT,
            )
        r.raise_for_status()
        return str(r.json()["id"])

    async def _wait(self, client: httpx.AsyncClient, task_id: str, report) -> dict:
        failures = 0
        while True:
            try:
                r = await client.get(f"/api/compute/transcribe/{task_id}")
                r.raise_for_status()
            except httpx.TransportError:
                failures += 1
                if failures > POLL_FAILURES:
                    raise
                await asyncio.sleep(self.poll_interval)
                continue
            failures = 0
            body = r.json()
            self.remote_name = str(body.get("engine") or self.remote_name)
            state = body.get("state")
            if state == "done":
                return body
            if state == "failed":
                raise PeerComputeError(
                    f"{self.peer_name} could not transcribe: {body.get('error') or 'it failed'}"
                )
            if state == "cancelled":
                raise PeerComputeError(f"The transcription was stopped on {self.peer_name}")
            if state == "queued":
                report(f"Waiting for {self.peer_name} to finish other work", 0.0)
            else:
                message = str(body.get("message") or "Transcribing").rstrip(".")
                report(f"{message} on {self.peer_name}", float(body.get("progress") or 0.0))
            await asyncio.sleep(self.poll_interval)

    def _take(self, body: dict, sources: list[AudioSource]) -> list[TranscriptSegment]:
        """The result's segments, and the details the pipeline reads off an engine."""
        result = body.get("result") or {}
        segments: list[TranscriptSegment] = []
        self.last_segment_sources = []
        kinds = [s.kind for s in sources]
        for raw in result.get("segments", []):
            item = dict(raw)
            kind = str(item.pop("source", "") or (kinds[0] if len(kinds) == 1 else "mixed"))
            seg = TranscriptSegment.model_validate(item)
            segments.append(seg)
            self.last_segment_sources.append((seg, kind))
        self.last_speaker_embeddings = {
            str(k): [float(x) for x in v] for k, v in (result.get("embeddings") or {}).items()
        }
        self.last_dropped_echo = int(result.get("dropped_echo") or 0)
        self.last_dropped_noise = int(result.get("dropped_noise") or 0)
        return segments
