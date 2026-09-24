"""Application context: every service the routes need, built once per app."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from fastapi import Request, WebSocket

from ..audio.capture import RecordingSession
from ..audio.echo_cancel import EchoCancelManager
from ..config import Settings
from ..events import EventBus
from ..jobs import JobManager
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
    active_recordings: dict[str, RecordingSession] = field(default_factory=dict)
    echo: EchoCancelManager = field(default_factory=EchoCancelManager)
    _retention_task: asyncio.Task | None = None

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
            jobs=JobManager(bus, concurrency={"transcribe": 1, "summarize": 2, "ask": 2}),
        )

    async def apply_settings(self, settings: Settings) -> None:
        """Swap in new settings and rebuild anything that depends on them."""
        self.settings = settings
        self.summarizer = SummarizationService(settings)
        self.speakers.threshold = settings.speaker_match_threshold
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

    async def startup(self, retention_interval: float = 6 * 3600) -> None:
        if self.settings.echo_cancel:
            status = await self.echo.start()
            if not status.active:
                logger.warning("Echo cancellation not started: %s", status.reason)
        self._retention_task = asyncio.create_task(self._retention_loop(retention_interval))

    async def shutdown(self) -> None:
        if self._retention_task is not None:
            self._retention_task.cancel()
        await self.echo.stop()
        await self.jobs.shutdown()
        await self.models.unload()
        self.repo.close()


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def get_ws_ctx(ws: WebSocket) -> AppContext:
    return ws.app.state.ctx
