"""Application context: every service the routes need, built once per app."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

from fastapi import Request, WebSocket

from ..audio.capture import RecordingSession
from ..audio.echo_cancel import EchoCancelManager
from ..config import Settings
from ..events import EventBus
from ..jobs import JobManager
from ..search.index import VectorIndex
from ..services.calendar_service import CalendarService
from ..services.model_service import ModelService
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
    active_recordings: dict[str, RecordingSession] = field(default_factory=dict)
    echo: EchoCancelManager = field(default_factory=EchoCancelManager)
    _retention_task: asyncio.Task | None = None
    _digest_task: asyncio.Task | None = None
    _apps_task: asyncio.Task | None = None
    capture_apps_now: list = field(default_factory=list)  # last poll, for /api/audio/apps
    level_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    http_transport: object | None = None  # tests inject an httpx transport for integrations

    def __post_init__(self) -> None:
        self.summarizer.name_source = self.known_names

    def known_names(self) -> list[str]:
        """People's names, for redaction before cloud calls."""
        from ..services.people import list_people

        st = self.settings
        return [
            p.name for p in list_people(self.repo, (st.local_speaker_name, st.remote_speaker_name))
        ]

    @classmethod
    def build(cls, settings: Settings) -> AppContext:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        bus = EventBus()
        repo = SessionRepository(settings.db_path)
        import_json_sessions(repo, settings.sessions_dir)
        return cls(
            settings=settings,
            repo=repo,
            sessions=SessionService(repo, settings.recordings_dir, bus),
            models=ModelService(settings),
            summarizer=SummarizationService(settings),
            speakers=SpeakerService(repo, settings.speaker_match_threshold),
            storage=StorageService(repo, settings.data_dir, settings.recordings_dir),
            calendar=CalendarService(settings.calendar_ics_url),
            bus=bus,
            index=VectorIndex(repo, settings),
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
        if settings.calendar_ics_url != self.calendar.source:
            self.calendar = CalendarService(settings.calendar_ics_url)
        await self.models.apply_settings(settings)

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

    async def startup(
        self, retention_interval: float = 6 * 3600, digest_interval: float = 900
    ) -> None:
        if self.settings.echo_cancel:
            status = await self.echo.start()
            if not status.active:
                logger.warning("Echo cancellation not started: %s", status.reason)
        self._retention_task = asyncio.create_task(self._retention_loop(retention_interval))
        self._digest_task = asyncio.create_task(self._digest_loop(digest_interval))
        self.index.start(self.bus)
        self._apps_task = asyncio.create_task(self._apps_loop(5.0))

    async def shutdown(self) -> None:
        await self.index.stop()
        for task in (self._retention_task, self._digest_task, self._apps_task):
            if task is not None:
                task.cancel()
        await self.echo.stop()
        await self.jobs.shutdown()
        await self.models.unload()
        self.repo.close()


def _pw_dump() -> list:
    import json
    import subprocess

    out = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5, check=True)
    return json.loads(out.stdout)


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def get_ws_ctx(ws: WebSocket) -> AppContext:
    return ws.app.state.ctx
