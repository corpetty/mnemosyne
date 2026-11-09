"""This computer records, its main computer does the work (docs/plans/2026-11-09-connections-
design.md, "Uses my main computer", decision D1; api/routes/satellite.py).

A laptop that uses a main computer (`AppContext.main_computer`, set by the desktop shell through
PUT /api/system/main) records with its own microphone and call audio, but never into its own
library: no meeting is made here, ever. Each recording is an outbox item, a folder
<data_dir>/outbox/<local id>/ holding the captured WAVs and a `manifest.json`.

Connected (the main computer answered POST /api/audio/start-browser): the main computer records
it as a browser recording, so live transcription, live speakers, the copilot and the final
transcription run there. Capture here is a normal local capture (audio/capture.py pw-record per
source, echo canceller and noise-suppression targets as a local recording takes them), and one
`SourceStream` per source tails the growing WAV and sends its new 16-bit mono PCM over the
main computer's /api/record/{recording id}/{source} WebSocket as it is written (the format the
browser recorder sends: pw-record already writes s16 mono at SAMPLE_RATE, so nothing is
converted; any other header stops that source's stream and counts as a gap). A dropped
connection reconnects with backoff and carries on from the first byte not yet sent; it counts
as a gap, because the bytes in flight when it dropped may never have been written there.

At stop: capture ends, each stream sends what is left (FLUSH_SECONDS at most) and closes. With
no gap the main computer's recording is stopped with `transcribe` left to its own
`auto_transcribe` (exactly as if it was recorded there) and the local copy is deleted. With a
gap it is stopped with `transcribe: false` and the item waits in the outbox as a REPLACE
hand-off (`session_id` = the main computer's meeting): POST /api/connections/handoff sends the
complete local files, the main computer swaps the streamed audio for them and its finish job
transcribes per its `auto_transcribe`. Either way the meeting is transcribed once, by the main
computer, after its audio is complete.

Not connected at start: capture only; at stop the item waits for the main computer and is
handed over as a new meeting there. The outbox worker retries with BACKOFF (1, 2, 5, 10, 30 min)
and at once when the shell says the main computer is back (PUT /api/system/main: `kick`).
`handoff_id` is the local id, so a retry after a lost answer is the same meeting. The local copy
is deleted only once the main computer answered with its meeting id. A 4xx other than 409/429
(and a 409 that says the streamed meeting can no longer be replaced) stops the retries: the item
stays, `failed`, with `last_error`. Items survive restarts (scanned at startup; one cut off by a
crash is repaired and queued). Audio waiting in the outbox is sealed with encryption at rest.

Tests and demo mode never touch a device: `FakeCapture` writes synthetic PCM.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import secrets
import shutil
import struct
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)

SAMPLE_RATE = 48000  # what audio/capture.py start_recording records: 16-bit mono
BACKOFF = (60.0, 120.0, 300.0, 600.0, 1800.0)  # outbox retries: 1, 2, 5, 10, 30 minutes
RECONNECT = (0.5, 1.0, 2.0, 5.0)  # a stream's reconnects
POLL = 0.1  # how often a stream looks for new audio in its file
CHUNK = 64 * 1024  # bytes per WebSocket message at most
FLUSH_SECONDS = 15.0  # at stop, how long the streams may take to send what is left
DRAIN_SECONDS = 0.5  # after the sockets closed, before the main computer's recording stops
PROBE_SECONDS = 5.0  # how long a reachability answer is reused
IDLE_WAKE = 60.0  # the outbox worker looks again at least this often
SOURCES = ("mic", "system")
# A replace hand-off refused for good (api/routes/handoff.py `_check_replace`): the streamed
# meeting was transcribed, or is not this computer's stream any more. Other 409s are "busy".
FINAL_409 = (
    "Transcription has started",
    "not recorded from this computer's stream",
    "has audio from elsewhere",
    "Only the computer that streamed it",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ---- capture ---------------------------------------------------------------------------------


@dataclass
class CapturedSource:
    source: str  # "mic" | "system"
    label: str  # what this computer calls the device
    path: Path  # the growing WAV


@dataclass
class Captured:
    sources: list[CapturedSource]
    sample_rate: int
    handle: Any = None  # the capture's own state (a RecordingSession, a fake's task)


class Capture(Protocol):
    async def start(self, app, wanted: dict[str, int], out_dir: Path) -> Captured: ...

    async def stop(self, captured: Captured) -> None: ...


class PipeWireCapture:
    """A local recording's capture (audio/capture.py), into the outbox folder."""

    async def start(self, app, wanted: dict[str, int], out_dir: Path) -> Captured:
        from ..api.routes.audio import _apply_echo_config
        from ..audio.capture import list_devices, start_recording

        sources = list(wanted)
        ids = await _apply_echo_config(app, [wanted[s] for s in sources])  # keeps positions
        by_id = dict(zip(ids, sources, strict=True))
        rec = await start_recording(
            ids, out_dir, sample_rate=SAMPLE_RATE, targets=app.recording_targets()
        )
        try:
            devices = {d.id: d for d in await asyncio.to_thread(list_devices)}
        except Exception:
            devices = {}
        out = []
        for proc in rec.processes:
            device = devices.get(proc.device_id)
            source = by_id.get(proc.device_id, "mic")
            default = "Microphone" if source == "mic" else "Call audio"
            label = device.description if device else default
            out.append(CapturedSource(source=source, label=label[:80], path=proc.output_path))
        return Captured(sources=out, sample_rate=SAMPLE_RATE, handle=rec)

    async def stop(self, captured: Captured) -> None:
        from ..audio.capture import stop_capture
        from .recovery import repair_wav

        await stop_capture(captured.handle)
        for s in captured.sources:
            if s.path.exists():
                await asyncio.to_thread(repair_wav, s.path)


class FakeCapture:
    """Synthetic PCM, never a device (demo mode and tests): one WAV per source, a quiet tone
    appended every `interval` seconds when `auto`, else only what `write` adds."""

    def __init__(self, auto: bool = True, interval: float = 0.1, sample_rate: int = SAMPLE_RATE):
        self.auto = auto
        self.interval = interval
        self.sample_rate = sample_rate
        self.paths: dict[str, Path] = {}
        self.stopped = False

    async def start(self, app, wanted: dict[str, int], out_dir: Path) -> Captured:
        from ..audio.capture import wav_header

        out_dir.mkdir(parents=True, exist_ok=True)
        self.stopped = False
        sources = []
        for source in wanted:
            path = out_dir / f"fake_{source}.wav"
            path.write_bytes(wav_header(self.sample_rate))
            self.paths[source] = path
            label = "Demo microphone" if source == "mic" else "Demo call audio"
            sources.append(CapturedSource(source=source, label=label, path=path))
        task = asyncio.create_task(self._run()) if self.auto else None
        return Captured(sources=sources, sample_rate=self.sample_rate, handle=task)

    def write(self, source: str, pcm: bytes) -> None:
        if self.stopped:
            return
        with self.paths[source].open("ab") as f:
            f.write(pcm)

    def tone(self, source: str, seconds: float) -> bytes:
        hz = 220.0 if source == "mic" else 330.0
        n = int(self.sample_rate * seconds)
        return b"".join(
            struct.pack("<h", int(800 * math.sin(2 * math.pi * hz * i / self.sample_rate)))
            for i in range(n)
        )

    async def _run(self) -> None:
        chunks = {s: self.tone(s, self.interval) for s in self.paths}
        while not self.stopped:
            await asyncio.sleep(self.interval)
            for source, pcm in chunks.items():
                self.write(source, pcm)

    async def stop(self, captured: Captured) -> None:
        from .recovery import repair_wav

        self.stopped = True
        if captured.handle is not None:
            captured.handle.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await captured.handle
        for s in captured.sources:
            await asyncio.to_thread(repair_wav, s.path)


def default_capture() -> Capture:
    from .. import demo

    return FakeCapture() if demo.enabled() else PipeWireCapture()


# ---- the outbox ------------------------------------------------------------------------------


@dataclass
class OutboxSource:
    source: str
    part: int
    file: str  # file name in the item's folder (".enc" when sealed)
    sample_rate: int
    label: str = ""


@dataclass
class OutboxItem:
    id: str
    title: str = ""
    started_at: str = ""
    ended_at: str = ""
    notes: str = ""
    # recording: capture runs; waiting: for the main computer; sending: being handed over;
    # failed: refused for good (last_error), kept here.
    state: str = "recording"
    session_id: str | None = None  # the main computer's meeting this streamed into
    recording_id: str | None = None
    stop_pending: bool = False  # that meeting's recording still needs stopping there
    sources: list[OutboxSource] = field(default_factory=list)
    attempts: int = 0
    next_at: float = 0.0
    last_error: str = ""

    @classmethod
    def from_json(cls, raw: dict) -> OutboxItem:
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        known["sources"] = [OutboxSource(**s) for s in raw.get("sources") or []]
        return cls(**known)


class Outbox:
    def __init__(self, root: Path):
        self.root = root
        self.items: dict[str, OutboxItem] = {}

    def folder(self, item_id: str) -> Path:
        return self.root / item_id

    def load(self) -> None:
        self.items = {}
        if not self.root.is_dir():
            return
        for folder in sorted(self.root.iterdir()):
            manifest = folder / "manifest.json"
            if not manifest.is_file():
                continue
            try:
                item = OutboxItem.from_json(json.loads(manifest.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError):
                logger.warning("Outbox: could not read %s; left as it is", manifest, exc_info=True)
                continue
            if item.id == folder.name:
                self.items[item.id] = item

    def save(self, item: OutboxItem) -> None:
        folder = self.folder(item.id)
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / "manifest.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(asdict(item), f, indent=1)
        tmp.replace(folder / "manifest.json")
        self.items[item.id] = item

    def delete(self, item: OutboxItem) -> None:
        self.items.pop(item.id, None)
        shutil.rmtree(self.folder(item.id), ignore_errors=True)


# ---- talking to the main computer ------------------------------------------------------------


class MainError(Exception):
    def __init__(self, status: int | None, detail: str):
        super().__init__(detail)
        self.status = status  # None: not reached
        self.detail = detail

    def final(self, replacing: bool) -> bool:
        """Retrying cannot help: a 4xx other than 409/429, or a replace refused for good."""
        if self.status is None or self.status >= 500 or self.status in (408, 429):
            return False
        if self.status == 409:
            return replacing and any(p in self.detail for p in FINAL_409)
        return 400 <= self.status < 500


def _client(main, timeout: httpx.Timeout | float = 15.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=main.url,
        headers={"Authorization": f"Bearer {main.token}"},
        timeout=timeout,
        trust_env=False,  # always the shell's loopback tunnel: never a proxy
    )


async def _call(main, method: str, path: str, timeout=15.0, **kwargs) -> dict:
    try:
        async with _client(main, timeout) as c:
            r = await c.request(method, path, **kwargs)
    except httpx.HTTPError as e:
        raise MainError(None, f"The main computer is not reachable ({type(e).__name__})") from e
    if r.status_code >= 400:
        try:
            detail = str(r.json().get("detail") or r.text)
        except ValueError:
            detail = r.text[:300]
        raise MainError(r.status_code, detail or f"HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError:
        return {}


# ---- one source's stream ---------------------------------------------------------------------


class SourceStream:
    """Tails one source's growing WAV and sends its PCM to the main computer."""

    def __init__(self, sat: Satellite, source: CapturedSource, sample_rate: int, rid: str):
        self.sat = sat
        self.source = source.source
        self.path = source.path
        self.sample_rate = sample_rate
        self.recording_id = rid
        self.sent = 0  # PCM bytes handed to the socket
        self.drops = 0  # connections lost while open: their bytes in flight may be lost
        self.connected = False
        self.problem = ""  # a reason this source is not streamed any more
        self.closed_by_main: int | None = None  # the main computer's close code, if it ended it
        self.ws = None
        self.final: int | None = None  # PCM bytes in the file once capture stopped
        self._data_offset: int | None = None
        self.task: asyncio.Task | None = None

    # -- the file

    def _header(self) -> bool:
        if self._data_offset is not None:
            return True
        try:
            with self.path.open("rb") as f:
                head = f.read(4096)
        except OSError:
            return False
        if len(head) < 44 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            return False
        fmt, data = head.find(b"fmt "), head.find(b"data")
        if fmt < 0 or data < 0 or len(head) < fmt + 24:
            return False
        _tag, channels, rate, _br, _align, bits = struct.unpack("<HHIIHH", head[fmt + 8 : fmt + 24])
        if (channels, rate, bits) != (1, self.sample_rate, 16):
            # Never the case with our capture; converting would be guesswork, the hand-off
            # sends the file as it is instead.
            self.problem = f"{channels} channel(s) at {rate} Hz, {bits} bit: not streamed"
        self._data_offset = data + 8
        return True

    def captured(self) -> int:
        if self.final is not None:
            return self.final
        if not self._header():
            return 0
        try:
            size = self.path.stat().st_size - (self._data_offset or 0)
        except OSError:
            return 0
        return max(0, size - size % 2)

    def _read(self) -> bytes:
        end = self.captured()
        if end <= self.sent:
            return b""
        with self.path.open("rb") as f:
            f.seek((self._data_offset or 0) + self.sent)
            data = f.read(min(CHUNK, end - self.sent))
        return data[: len(data) - len(data) % 2]

    @property
    def gap(self) -> bool:
        """The main computer may lack some of this source's audio."""
        if self.closed_by_main == 1000:  # stopped there: what came after is not the meeting's
            return self.drops > 0
        return bool(self.drops or self.problem or self.sent < self.captured())

    def done(self) -> bool:
        return self.problem != "" or (self.final is not None and self.sent >= self.final)

    # -- the socket

    def start(self) -> None:
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI

        failures = 0
        while not self.done():
            main = self.sat.app.main_computer
            if main is None:  # forgotten mid-meeting: the hand-off has everything
                self.problem = "No main computer any more"
                return
            if not self._header():
                await asyncio.sleep(POLL)
                continue
            if self.problem:
                return
            url = f"ws{main.url[4:]}/api/record/{self.recording_id}/{self.source}"
            opened = False
            try:
                async with connect(
                    url,
                    additional_headers={"Authorization": f"Bearer {main.token}"},
                    open_timeout=10,
                    ping_interval=20,
                    ping_timeout=30,
                    close_timeout=5,
                    max_size=2**20,
                    proxy=None,
                ) as ws:
                    opened = True
                    self.ws, self.connected, failures = ws, True, 0
                    await self._pump(ws)
                    self.connected = False
                return  # everything sent and the socket closed cleanly
            except ConnectionClosed as e:
                code = e.rcvd.code if e.rcvd is not None else None
                if code in (1000, 4401, 4403, 4404):
                    # 1000: the main computer stopped this recording; the others: it is not
                    # there for us any more. Reconnecting cannot help.
                    self.closed_by_main = code
                    self.problem = f"The main computer closed the stream ({code})"
                    self.sat._stream_ended(self, code)
                    return
                self.drops += 1
                logger.info("Stream %s: connection lost (%s); reconnecting", self.source, code)
            except (OSError, TimeoutError, InvalidHandshake, InvalidURI) as e:
                if opened:
                    self.drops += 1
                logger.debug("Stream %s: could not connect: %s", self.source, e)
            finally:
                self.connected = False
                self.ws = None
            failures += 1
            await asyncio.sleep(RECONNECT[min(failures, len(RECONNECT)) - 1])

    async def _pump(self, ws) -> None:
        # Closed by the other side while there was nothing to send: noticed at once, not at the
        # next send (the main computer closes with 1000 when its recording was stopped there).
        closed = asyncio.ensure_future(ws.wait_closed())
        try:
            while True:
                data = self._read()
                if data:
                    await ws.send(data)
                    self.sent += len(data)
                    continue
                if self.final is not None and self.sent >= self.final:
                    return
                done, _ = await asyncio.wait({closed}, timeout=POLL)
                if done:
                    raise ws.protocol.close_exc
        finally:
            closed.cancel()

    async def cancel(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task


# ---- the service -----------------------------------------------------------------------------


@dataclass
class Active:
    item: OutboxItem
    captured: Captured
    capture: Capture
    streams: dict[str, SourceStream]
    started: float = field(default_factory=time.time)
    stopping: bool = False


class Satellite:
    def __init__(self, app, capture: Capture | None = None):
        self.app = app
        self.capture = capture
        self.outbox = Outbox(app.settings.data_dir / "outbox")
        self.outbox.load()
        self.active: Active | None = None
        self._ready = False
        self._worker: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None
        self._lock = asyncio.Lock()  # start and stop, one at a time
        self._probe: tuple[float, bool] | None = None
        self._auto_stop: asyncio.Task | None = None

    # -- life

    async def ready(self) -> None:
        """Once per process: items a crash cut off are repaired and queued; the worker runs."""
        if not self._ready:
            self._ready = True
            for item in list(self.outbox.items.values()):
                if item.state in ("recording", "sending"):
                    await asyncio.to_thread(self._recover, item)
                    self.outbox.save(item)
                elif item.state == "waiting":
                    item.next_at = 0.0  # a new start is a good moment to try again
        self._ensure_worker()

    def _recover(self, item: OutboxItem) -> None:
        from .recovery import recorder_owner, repair_wav, stop_orphan_recorders

        folder = self.outbox.folder(item.id)
        if item.state == "recording":
            wavs = [folder / s.file for s in item.sources if not s.file.endswith(".enc")]
            if recorder_owner(wavs) is not None:
                return  # another backend still records it
            stop_orphan_recorders(wavs)
            for wav in wavs:
                if wav.exists():
                    repair_wav(wav)
            item.ended_at = item.ended_at or _now_iso()
            if item.session_id:
                item.stop_pending = True
            logger.info("Outbox %s: a recording cut off by a restart waits for hand-off", item.id)
        item.state, item.next_at = "waiting", 0.0
        self._seal(item)

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._wake = asyncio.Event()
            self._worker = asyncio.create_task(self._run_outbox())

    async def shutdown(self) -> None:
        active = self.active
        if active is not None:
            self.active = None
            try:
                await active.capture.stop(active.captured)
            except Exception:
                logger.exception("Could not stop the capture at shutdown")
            for s in active.streams.values():
                await s.cancel()
            item = active.item
            self._fill_sources(item, active.captured)
            item.ended_at = _now_iso()
            if item.session_id:
                item.stop_pending = True
                main = self.app.main_computer
                if main is not None:
                    try:
                        await self._stop_main(main, item.session_id, False, timeout=3.0)
                        item.stop_pending = False
                    except MainError:
                        pass
            item.state, item.next_at = "waiting", 0.0
            await asyncio.to_thread(self._seal, item)
            self.outbox.save(item)
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._worker
            self._worker = None

    def kick(self) -> None:
        """The main computer is (back) there: every waiting item is tried now."""
        for item in self.outbox.items.values():
            if item.state == "waiting":
                item.next_at = 0.0
        self._probe = None
        self._ensure_worker()
        if self._wake is not None:
            self._wake.set()

    # -- reachability

    async def reachable(self) -> bool:
        main = self.app.main_computer
        if main is None:
            return False
        if self._probe is not None and time.monotonic() - self._probe[0] < PROBE_SECONDS:
            return self._probe[1]
        try:
            async with _client(main, timeout=3.0) as c:
                ok = (await c.get("/health")).status_code == 200
        except httpx.HTTPError:
            ok = False
        self._probe = (time.monotonic(), ok)
        return ok

    def _seen(self, ok: bool) -> None:
        self._probe = (time.monotonic(), ok)

    # -- recording

    async def start(
        self,
        mic_device: int | None,
        system_device: int | None,
        title: str = "",
        consent: str | None = None,
    ) -> Active:
        from fastapi import HTTPException

        wanted = {
            s: d for s, d in (("mic", mic_device), ("system", system_device)) if d is not None
        }
        if not wanted:
            raise HTTPException(400, "Nothing to record: no microphone or call audio")
        async with self._lock:
            if self.active is not None:
                raise HTTPException(409, "This computer is already recording")
            await self.ready()
            item = OutboxItem(id=secrets.token_hex(6), title=title.strip()[:200])
            item.started_at = _now_iso()
            folder = self.outbox.folder(item.id)
            folder.mkdir(parents=True, exist_ok=True)
            capture = self.capture or default_capture()
            try:
                captured = await capture.start(self.app, wanted, folder)
            except (RuntimeError, FileNotFoundError, OSError) as e:
                shutil.rmtree(folder, ignore_errors=True)
                raise HTTPException(503, f"Could not start recording: {e}") from e
            if not captured.sources:
                shutil.rmtree(folder, ignore_errors=True)
                raise HTTPException(400, "None of the selected devices could be opened")
            self._fill_sources(item, captured)

            main = self.app.main_computer
            started = None
            if main is not None:
                try:
                    started = await self._start_main(main, item, captured, consent)
                except MainError as e:
                    if e.status in (400, 422):  # the request itself: say so, record nothing
                        await capture.stop(captured)
                        shutil.rmtree(folder, ignore_errors=True)
                        raise HTTPException(400, e.detail) from e
                    logger.info("Outbox %s: main computer not used (%s)", item.id, e.detail)
            streams: dict[str, SourceStream] = {}
            if started is not None:
                item.session_id, item.recording_id = started
                for src in captured.sources:
                    streams[src.source] = SourceStream(
                        self, src, captured.sample_rate, item.recording_id
                    )
            self.outbox.save(item)
            self.active = Active(item=item, captured=captured, capture=capture, streams=streams)
            for s in streams.values():
                s.start()
            logger.info(
                "Outbox %s: recording %s",
                item.id,
                f"streamed into {main.name or 'the main computer'}'s meeting {item.session_id}"
                if started
                else "here; it waits for the main computer",
            )
            return self.active

    async def _start_main(self, main, item: OutboxItem, captured: Captured, consent):
        body: dict = {
            "sources": [s.source for s in captured.sources],
            "sample_rate": captured.sample_rate,
            "labels": {s.source: s.label for s in captured.sources},
        }
        if consent is not None:
            body["consent"] = consent
        try:
            answer = await _call(main, "POST", "/api/audio/start-browser", timeout=8.0, json=body)
        except MainError as e:
            self._seen(e.status is not None)
            raise
        self._seen(True)
        sid, rid = str(answer["session_id"]), str(answer["recording_id"])
        if item.title:
            with contextlib.suppress(MainError):
                await _call(main, "PATCH", f"/api/sessions/{sid}", json={"name": item.title})
        return sid, rid

    def _fill_sources(self, item: OutboxItem, captured: Captured) -> None:
        item.sources = [
            OutboxSource(
                source=s.source,
                part=0,
                file=s.path.name,
                sample_rate=captured.sample_rate,
                label=s.label,
            )
            for s in captured.sources
        ]

    def _stream_ended(self, stream: SourceStream, code: int) -> None:
        """The main computer closed a stream for good. 1000: it stopped the recording (Stop
        there): this computer stops too, nothing more would reach the meeting."""
        active = self.active
        if code != 1000 or active is None or active.stopping:
            return
        if self._auto_stop is None or self._auto_stop.done():
            self._auto_stop = asyncio.create_task(self._stop_quietly())

    async def _stop_quietly(self) -> None:
        from fastapi import HTTPException

        try:
            await self.stop(main_stopped=True)
        except HTTPException:
            pass  # stopped here meanwhile
        except Exception:
            logger.exception("Could not stop after the main computer stopped the recording")

    async def stop(self, notes: str | None = None, main_stopped: bool = False) -> dict:
        from fastapi import HTTPException

        async with self._lock:
            active = self.active
            if active is None:
                raise HTTPException(404, "This computer is not recording")
            active.stopping = True
            try:
                return await self._stop(active, notes, main_stopped)
            finally:
                self.active = None

    async def _stop(self, active: Active, notes: str | None, main_stopped: bool) -> dict:
        item = active.item
        await active.capture.stop(active.captured)
        item.ended_at = _now_iso()
        if notes is not None:
            item.notes = notes
        if not active.streams:
            return await self._queue(item, "waiting")

        for s in active.streams.values():
            s.final = s.captured()
        # Only connected streams are waited for: the others' audio goes in the hand-off.
        pending = [
            s.task for s in active.streams.values() if s.task and not s.task.done() and s.connected
        ]
        if pending:
            await asyncio.wait(pending, timeout=FLUSH_SECONDS)
        for s in active.streams.values():
            await s.cancel()  # those still trying: the hand-off sends their audio
        gap = any(s.gap for s in active.streams.values())
        main_stopped = main_stopped or any(
            s.closed_by_main == 1000 for s in active.streams.values()
        )
        sid = item.session_id
        for s in active.streams.values():
            logger.info(
                "Outbox %s: %s sent %d of %d bytes (%d dropped connection(s))%s",
                item.id, s.source, s.sent, s.captured(), s.drops,
                f"; {s.problem}" if s.problem else "",
            )  # fmt: skip
        if not main_stopped:
            await asyncio.sleep(DRAIN_SECONDS)  # its sockets write what they received
            main = self.app.main_computer
            try:
                if main is None:
                    raise MainError(None, "No main computer")
                # No gap: the main computer goes on as with any recording (its auto_transcribe).
                # A gap: nothing transcribes until the complete audio has replaced the stream's.
                await self._stop_main(main, sid, None if not gap else False)
            except MainError as e:
                if e.status != 404:  # 404: it had stopped already (no audio for a while)
                    logger.info("Outbox %s: could not stop it there: %s", item.id, e.detail)
                    item.stop_pending, gap = True, True
        if not gap:
            logger.info(
                "Outbox %s: streamed whole into meeting %s; local copy deleted", item.id, sid
            )
            self.outbox.delete(item)
            return {"local_id": item.id, "session_id": sid, "outcome": "streamed"}
        logger.info("Outbox %s: the stream had gaps; the complete audio replaces it", item.id)
        return await self._queue(item, "handing_over")

    async def _queue(self, item: OutboxItem, outcome: str) -> dict:
        item.state, item.next_at, item.attempts = "waiting", 0.0, 0
        await asyncio.to_thread(self._seal, item)
        self.outbox.save(item)
        self.kick()
        return {"local_id": item.id, "session_id": item.session_id, "outcome": outcome}

    async def _stop_main(self, main, sid: str, transcribe: bool | None, timeout=30.0) -> None:
        await _call(
            main, "POST", f"/api/audio/stop/{sid}", json={"transcribe": transcribe}, timeout=timeout
        )

    # -- encryption at rest

    def _seal(self, item: OutboxItem) -> None:
        from ..storage.crypto import encrypt_file, is_encrypted

        key = self.app.file_key
        if not (self.app.settings.encrypt_at_rest and key is not None):
            return
        folder = self.outbox.folder(item.id)
        for s in item.sources:
            path = folder / s.file
            if is_encrypted(path) or not path.exists():
                continue
            try:
                s.file = encrypt_file(path, key).name
            except Exception:
                logger.exception("Outbox %s: %s left unencrypted", item.id, s.file)

    # -- hand-off

    async def _run_outbox(self) -> None:
        if not self._ready:  # kicked before startup looked at the outbox
            await self.ready()
        while True:
            try:
                now = time.time()
                waiting = [i for i in self.outbox.items.values() if i.state == "waiting"]
                due = sorted((i for i in waiting if i.next_at <= now), key=lambda i: i.started_at)
                if due and self.app.main_computer is not None:
                    for item in due:
                        await self._handoff(item)
                    continue
                later = [i.next_at - now for i in waiting if i.next_at > now]
                wait = min([IDLE_WAKE, *later])
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=max(wait, 0.01))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Outbox: the hand-off worker failed; trying again")
                await asyncio.sleep(5)

    async def _handoff(self, item: OutboxItem) -> None:
        main = self.app.main_computer
        if main is None or item.state != "waiting":
            return
        item.state = "sending"
        self.outbox.save(item)
        replacing = item.session_id is not None
        try:
            if item.stop_pending and item.session_id:
                try:
                    await self._stop_main(main, item.session_id, False)
                except MainError as e:
                    if e.status != 404:
                        raise
                item.stop_pending = False
            answer = await self._upload(main, item)
        except MainError as e:
            self._seen(e.status is not None)
            item.last_error = e.detail
            if e.final(replacing):
                item.state = "failed"
                logger.warning("Outbox %s: refused by the main computer: %s", item.id, e.detail)
            else:
                item.attempts += 1
                delay = BACKOFF[min(item.attempts, len(BACKOFF)) - 1]
                item.state, item.next_at = "waiting", time.time() + delay
                logger.info(
                    "Outbox %s: not handed over (%s); next try in %d min",
                    item.id, e.detail, round(delay / 60),
                )  # fmt: skip
            self.outbox.save(item)
            return
        except Exception as e:  # unreadable audio, a locked key: keep it, say why
            item.last_error = str(e) or type(e).__name__
            item.state = "failed"
            self.outbox.save(item)
            logger.exception("Outbox %s: could not hand it over", item.id)
            return
        self._seen(True)
        logger.info(
            "Outbox %s: handed over to %s as meeting %s%s; local copy deleted",
            item.id, main.name or "the main computer", answer.get("session_id"),
            " (replacing the streamed audio)" if answer.get("replaced") else "",
        )  # fmt: skip
        self.outbox.delete(item)

    async def _upload(self, main, item: OutboxItem) -> dict:
        from ..storage.crypto import plaintext_async

        folder = self.outbox.folder(item.id)
        meta = {
            "handoff_id": item.id,
            "title": item.title,
            "started_at": item.started_at or None,
            "ended_at": item.ended_at or None,
            "notes": item.notes,
            "sources": [
                {
                    "source": s.source,
                    "part": s.part,
                    "offset": 0.0,
                    "sample_rate": s.sample_rate,
                    "file": f"{s.source}{s.part}",
                    "label": s.label,
                }
                for s in item.sources
            ],
        }
        if item.session_id:
            meta["session_id"] = item.session_id
        timeout = httpx.Timeout(30.0, read=600.0, write=600.0)
        async with contextlib.AsyncExitStack() as stack:
            files = {}
            for s in item.sources:
                path = await stack.enter_async_context(
                    plaintext_async(folder / s.file, self.app.file_key)
                )
                handle = stack.enter_context(open(path, "rb"))  # noqa: SIM115
                files[f"{s.source}{s.part}"] = (f"{s.source}{s.part}.wav", handle, "audio/wav")
            return await _call(
                main,
                "POST",
                "/api/connections/handoff",
                timeout=timeout,
                data={"meta": json.dumps(meta)},
                files=files,
            )

    # -- what the UI shows

    def recording(self) -> dict | None:
        active = self.active
        if active is None:
            return None
        sources = []
        for src in active.captured.sources:
            stream = active.streams.get(src.source)
            if stream is not None:
                captured, sent = stream.captured(), stream.sent
                connected, gap = stream.connected, bool(stream.drops or stream.problem)
            else:
                try:
                    captured = max(0, src.path.stat().st_size - 44)
                except OSError:
                    captured = 0
                sent, connected, gap = 0, False, False
            sources.append(
                {
                    "source": src.source,
                    "label": src.label,
                    "captured": captured,
                    "sent": sent,
                    "connected": connected,
                    "gap": gap,
                }
            )
        return {
            "local_id": active.item.id,
            "session_id": active.item.session_id,
            "streaming": bool(active.streams),
            "title": active.item.title,
            "started_at": active.item.started_at,
            "sources": sources,
        }

    def items(self) -> list[dict]:
        out = []
        for item in sorted(self.outbox.items.values(), key=lambda i: i.started_at):
            out.append(
                {
                    "id": item.id,
                    "title": item.title,
                    "started_at": item.started_at,
                    "parts": len({s.part for s in item.sources}),
                    "state": item.state,
                    "last_error": item.last_error,
                    "replaces": item.session_id,
                }
            )
        return out


def service(app) -> Satellite:
    if app.satellite is None:
        app.satellite = Satellite(app)  # tests put their own, with a FakeCapture
    return app.satellite
