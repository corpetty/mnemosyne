"""Application context: every service the routes need, built once per app."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Request, WebSocket

from ..audio.capture import RecordingSession
from ..audio.echo_cancel import EchoCancelManager
from ..config import Settings
from ..events import EventBus
from ..jobs import JobManager
from ..search.index import VectorIndex
from ..services.calendar_service import CalendarService, calendar_source
from ..services.link import LinkService
from ..services.model_service import ModelService
from ..services.pairing import PairingService
from ..services.session_service import SessionService
from ..services.speaker_service import SpeakerService
from ..services.storage_service import StorageService
from ..services.summarization_service import SummarizationService
from ..storage.sqlite import SessionRepository, import_json_sessions

logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    settings: Settings
    repo: SessionRepository
    sessions: SessionService
    models: ModelService
    summarizer: SummarizationService
    speakers: SpeakerService
    storage: StorageService
    calendar: CalendarService
    bus: EventBus
    jobs: JobManager
    index: VectorIndex
    pairing: PairingService
    link: LinkService
    active_recordings: dict[str, RecordingSession] = field(default_factory=dict)
    starting: set[str] = field(default_factory=set)  # sessions whose recording is starting
    echo: EchoCancelManager = field(default_factory=EchoCancelManager)
    _retention_task: asyncio.Task | None = None
    _digest_task: asyncio.Task | None = None
    _apps_task: asyncio.Task | None = None
    _backup_task: asyncio.Task | None = None
    _idle_task: asyncio.Task | None = None
    _intervals: tuple[float, float] = (6 * 3600, 900)
    capture_apps_now: list = field(default_factory=list)  # last poll, for /api/audio/apps
    live: dict = field(default_factory=dict)  # session id -> running LiveTranscriber
    copilot_notes: dict = field(default_factory=dict)  # session id -> CopilotNotes
    recovered: list = field(default_factory=list)  # RecoveredRecording, since this start
    # Encryption at rest: where the master key lives, the key once known, and whether the
    # meetings are encrypted but the key is missing (then only the recovery code gets in).
    keystore: Any = None
    master_key: bytes | None = None
    locked: bool = False
    level_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    http_transport: object | None = None  # tests inject an httpx transport for integrations
    app_watch: Any = None  # api/app_watch.py: the desktop app this backend belongs to

    def __post_init__(self) -> None:
        self.summarizer.name_source = self.known_names
        self.sessions.type_source = lambda: self.settings.meeting_types
        self.sessions.is_recording = lambda sid: sid in self.active_recordings

    def known_names(self) -> list[str]:
        """People's names, for redaction before cloud calls."""
        from ..services.people import list_people

        st = self.settings
        return [
            p.name for p in list_people(self.repo, (st.local_speaker_name, st.remote_speaker_name))
        ]

    @property
    def file_key(self) -> bytes | None:
        """Key for encrypted audio files (None when encryption is off or locked)."""
        from ..storage.crypto import derive

        return derive(self.master_key, "files") if self.master_key else None

    @classmethod
    def build(cls, settings: Settings, keystore: Any = None) -> AppContext:
        from ..storage.crypto import SystemKeyStore, derive, key_check

        settings.data_dir.mkdir(parents=True, exist_ok=True)
        from ..services.backup import apply_path_rebase, apply_pending_restore

        apply_pending_restore(settings)  # before the database (and its key check) is opened
        bus = EventBus()
        if keystore is None:
            from ..demo import enabled as demo_mode
            from ..storage.crypto import FileKeyStore

            keystore = (
                FileKeyStore(settings.data_dir / "demo-key")
                if demo_mode()
                else SystemKeyStore(settings.data_dir)
            )
        master, locked = None, False
        if settings.encrypt_at_rest:
            master = keystore.get()
            if master is None or key_check(master) != settings.encryption_check:
                logger.warning("Meetings are encrypted and the key is not available: locked")
                master, locked = None, True
        if locked:  # an empty stand-in until the recovery code arrives (see unlock)
            repo = SessionRepository(Path(":memory:"))
        else:
            repo = SessionRepository(settings.db_path, derive(master, "db") if master else None)
            apply_path_rebase(repo, settings)
            import_json_sessions(repo, settings.sessions_dir)
        return cls(
            keystore=keystore,
            master_key=master,
            locked=locked,
            settings=settings,
            repo=repo,
            sessions=SessionService(repo, settings.recordings_dir, bus),
            models=ModelService(settings),
            summarizer=SummarizationService(settings),
            speakers=SpeakerService(repo, settings.speaker_match_threshold),
            storage=StorageService(repo, settings.data_dir, settings.recordings_dir),
            calendar=CalendarService(calendar_source(settings)),
            bus=bus,
            index=VectorIndex(repo, settings),
            pairing=PairingService(settings.data_dir / "paired_devices.json"),
            link=LinkService(settings.data_dir, int(os.environ.get("MNEMOSYNE_BIND_PORT", "8008"))),
            jobs=JobManager(
                bus,
                concurrency={
                    "transcribe": 1,
                    "summarize": 2,
                    "ask": 2,
                    "digest": 1,
                    "followup": 2,
                    "thread": 2,
                },
            ),
        )

    async def apply_settings(self, settings: Settings) -> None:
        """Swap in new settings and rebuild anything that depends on them."""
        self.settings = settings
        self.summarizer = SummarizationService(settings)
        self.summarizer.name_source = self.known_names
        self.speakers.threshold = settings.speaker_match_threshold
        if (settings.semantic_search, settings.embedding_model) != (
            self.index.enabled,
            self.index.model,
        ):
            await self.index.stop()
            self.index = VectorIndex(self.repo, settings)
            self.index.start(self.bus)
        if calendar_source(settings) != self.calendar.source:
            self.calendar = CalendarService(calendar_source(settings))
        await self.models.apply_settings(settings)
        await self.apply_remote_access()

    async def apply_remote_access(self) -> None:
        relays = [u.strip() for u in self.settings.remote_relays.split(",") if u.strip()]
        if self.link.active and (not self.settings.remote_access or relays != self.link.relays):
            await self.link.stop()
        self.link.relays = relays
        if self.settings.remote_access and not self.link.active:
            self.link.start()

    def busy_sessions(self) -> set[str]:
        """Sessions whose audio must not be touched right now."""
        busy = set(self.active_recordings)
        busy |= {j.session_id for j in self.jobs.list(active_only=True) if j.session_id}
        return busy

    def run_retention(self):
        days = self.settings.audio_retention_days
        if days <= 0:
            return None
        result = self.storage.cleanup(days, busy=self.busy_sessions())
        if result.sessions:
            logger.info(
                "Retention: removed audio from %d session(s), %d bytes",
                len(result.sessions),
                result.freed_bytes,
            )
            for u in result.sessions:
                self.bus.publish(
                    {"type": "session", "session_id": u.session_id, "status": "audio_deleted"}
                )
        return result

    async def _retention_loop(self, interval_seconds: float) -> None:
        while True:
            try:
                self.run_retention()
            except Exception:
                logger.exception("Audio retention pass failed")
            await asyncio.sleep(interval_seconds)

    def maybe_schedule_digest(self, now: datetime | None = None):
        """Queue this week's digest if the schedule says it is due and none exists yet."""
        from ..services.digest_service import due_week, range_label
        from ..services.pipeline import make_digest

        st = self.settings
        week = due_week(now or datetime.now(), st.digest_weekday, st.digest_hour)
        if week is None or self.repo.has_digest(range_label(*week)):
            return None
        if any(j.kind == "digest" for j in self.jobs.list(active_only=True)):
            return None
        if not any(s.summary.strip() for s in self.repo.sessions_between(*week)):
            return None
        logger.info("Scheduled digest for %s", range_label(*week))
        return self.jobs.submit("digest", make_digest(self, *week))

    def poll_capture_apps(self, dump: list | None = None) -> None:
        """Compare the apps capturing audio with the last poll and publish changes."""
        from ..audio.streams import capture_apps

        ignore = {x.strip() for x in self.settings.auto_record_ignore_apps.split(",") if x.strip()}
        now = capture_apps(dump, ignore)
        before = {a.app for a in self.capture_apps_now}
        after = {a.app for a in now}
        self.capture_apps_now = now
        for app in sorted(after - before):
            self.bus.publish({"type": "meeting_app", "status": "started", "app": app})
        for app in sorted(before - after):
            self.bus.publish({"type": "meeting_app", "status": "stopped", "app": app})

    async def _apps_loop(self, interval_seconds: float) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            if self.settings.auto_record == "off":
                self.capture_apps_now = []
                continue
            try:
                dump = await asyncio.to_thread(_pw_dump)
                self.poll_capture_apps(dump)
            except Exception as e:
                logger.debug("Capture app poll failed: %s", e)

    async def _digest_loop(self, interval_seconds: float) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                self.maybe_schedule_digest()
            except Exception:
                logger.exception("Digest schedule check failed")

    async def _idle_loop(self, interval_seconds: float) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await self.models.unload_if_idle(busy=self.busy())
            except Exception:
                logger.exception("Unloading idle models failed")

    def busy(self) -> bool:
        """Recording, or any job running: models may be in use."""
        return bool(self.active_recordings) or bool(self.jobs.list(active_only=True))

    async def _backup_loop(self, interval_seconds: float) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                self.maybe_back_up()
            except Exception:
                logger.exception("Backup schedule check failed")

    def maybe_back_up(self):
        """Start an automatic backup when one is due, not while recording or backing up."""
        from ..services.backup import due

        if self.active_recordings or any(
            j.kind == "backup" for j in self.jobs.list(active_only=True)
        ):
            return None
        if not due(self.settings):
            return None
        return self.jobs.submit("backup", backup_runner(self, prune_after=True))

    async def startup(
        self, retention_interval: float = 6 * 3600, digest_interval: float = 900
    ) -> None:
        from ..storage.crypto import clean_scratch

        self._intervals = (retention_interval, digest_interval)
        clean_scratch()
        await self.apply_remote_access()  # also while locked: a paired computer can unlock
        if self.locked:
            logger.warning("Waiting for the recovery code before starting")
            return
        await self._start_services()

    async def unlock(self, master: bytes) -> None:
        """The recovery code arrived: keep the key, open the real database, start up."""
        from ..services.backup import apply_path_rebase
        from ..storage.crypto import derive

        self.repo.reopen(self.settings.db_path, derive(master, "db"))
        apply_path_rebase(self.repo, self.settings)  # a restore from another machine
        self.keystore.set(master)
        self.master_key, self.locked = master, False
        await self._start_services()

    async def _start_services(self) -> None:
        from ..services.recovery import recover_interrupted

        retention_interval, digest_interval = self._intervals
        # A transcription the last backend never finished (killed, crashed): no job runs it
        # now, so the meeting goes back to what it was, transcript or not.
        from ..models.session import SessionStatus

        for s in self.sessions.list_sessions():
            if s.status == SessionStatus.TRANSCRIBING:
                done = SessionStatus.COMPLETED if s.has_transcript else SessionStatus.CREATED
                self.sessions.set_status(s.id, done)
        # First, so retention and the index never see a half-finished recording.
        recover_interrupted(self)
        if self.settings.echo_cancel:
            try:
                status = await self.echo.start(self.settings.echo_cancel_mic or None)
            except ValueError as e:  # a bad saved mic: fall back to the default source
                logger.warning("%s", e)
                status = await self.echo.start()
            if not status.active:
                logger.warning("Echo cancellation not started: %s", status.reason)
        self._retention_task = asyncio.create_task(self._retention_loop(retention_interval))
        self._digest_task = asyncio.create_task(self._digest_loop(digest_interval))
        self.index.start(self.bus)
        self._apps_task = asyncio.create_task(self._apps_loop(5.0))
        self._backup_task = asyncio.create_task(self._backup_loop(3600.0))
        self._idle_task = asyncio.create_task(self._idle_loop(60.0))

    async def shutdown(self) -> None:
        # Recordings still running end here, their files closed properly; the next start
        # recovers them into their meetings (services/recovery.py). Left running, pw-record
        # would outlive the backend and record on with nobody to save it.
        from ..audio.capture import stop_capture

        for recording in list(self.active_recordings.values()):
            try:
                await stop_capture(recording)
            except Exception:
                logger.exception("Could not stop a recording at shutdown")
        await self.link.stop()
        await self.index.stop()
        for task in (
            self._retention_task,
            self._digest_task,
            self._apps_task,
            self._backup_task,
            self._idle_task,
        ):
            if task is not None:
                task.cancel()
        await self.echo.stop()
        await self.jobs.shutdown()
        await self.models.unload()
        self.repo.close()


def backup_runner(app: AppContext, prune_after: bool = False):
    """Job runner: write a backup (services/backup.py), then drop the oldest beyond the limit."""
    from ..services.backup import create_backup, prune

    async def run(ctx) -> dict:
        ctx.update("Backing up", progress=0.0)
        loop = asyncio.get_running_loop()
        last = [-1.0]

        def progress(frac: float) -> None:  # from the backup thread; whole percents only
            if frac - last[0] >= 0.01:
                last[0] = frac
                loop.call_soon_threadsafe(ctx.update, None, round(min(frac, 0.99), 2))

        info = await asyncio.to_thread(create_backup, app, progress)
        removed = prune(app.settings) if prune_after else []
        return {**info.model_dump(mode="json"), "removed": removed}

    return run


def _pw_dump() -> list:
    import json
    import subprocess

    out = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5, check=True)
    return json.loads(out.stdout)


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def get_ws_ctx(ws: WebSocket) -> AppContext:
    return ws.app.state.ctx
