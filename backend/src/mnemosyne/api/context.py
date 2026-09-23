"""Application context: every service the routes need, built once per app."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from fastapi import Request, WebSocket

from ..audio.capture import RecordingSession
from ..audio.echo_cancel import EchoCancelManager
from ..config import Settings
from ..events import EventBus
from ..jobs import JobManager
from ..services.model_service import ModelService
from ..services.session_service import SessionService
from ..services.speaker_service import SpeakerService
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
    bus: EventBus
    jobs: JobManager
    active_recordings: dict[str, RecordingSession] = field(default_factory=dict)
    echo: EchoCancelManager = field(default_factory=EchoCancelManager)

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
            bus=bus,
            jobs=JobManager(bus, concurrency={"transcribe": 1, "summarize": 2}),
        )

    async def apply_settings(self, settings: Settings) -> None:
        """Swap in new settings and rebuild anything that depends on them."""
        self.settings = settings
        self.summarizer = SummarizationService(settings)
        self.speakers.threshold = settings.speaker_match_threshold
        await self.models.apply_settings(settings)

    async def startup(self) -> None:
        if self.settings.echo_cancel:
            status = await self.echo.start()
            if not status.active:
                logger.warning("Echo cancellation not started: %s", status.reason)

    async def shutdown(self) -> None:
        await self.echo.stop()
        await self.jobs.shutdown()
        await self.models.unload()
        self.repo.close()


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def get_ws_ctx(ws: WebSocket) -> AppContext:
    return ws.app.state.ctx
