"""Background job runner.

Long-running work (transcription, later summarization and live capture) runs
as a Job so HTTP handlers return immediately and progress streams over the
event bus. Jobs of the same kind can be serialized with a per-kind limit,
which is how we keep a single GPU pipeline from being invoked concurrently.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from .events import EventBus

logger = logging.getLogger(__name__)


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Job(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4())[:8])
    kind: str
    session_id: str | None = None
    status: JobStatus = JobStatus.QUEUED
    message: str = ""
    progress: float | None = None  # 0..1 when known
    error: str | None = None
    result: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=datetime.now)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)


class JobContext:
    """Handed to a job runner so it can report progress and emit events."""

    def __init__(self, job: Job, bus: EventBus, manager: JobManager):
        self.job = job
        self.bus = bus
        self._manager = manager

    def update(self, message: str | None = None, progress: float | None = None) -> None:
        if message is not None:
            self.job.message = message
        if progress is not None:
            self.job.progress = progress
        self._manager._publish(self.job)

    def emit(self, event: dict[str, Any]) -> None:
        self.bus.publish(event)


JobRunner = Callable[[JobContext], Awaitable[dict[str, Any] | None]]


class JobManager:
    def __init__(self, bus: EventBus, concurrency: dict[str, int] | None = None):
        self.bus = bus
        self.jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._limits: dict[str, asyncio.Semaphore] = {
            kind: asyncio.Semaphore(n) for kind, n in (concurrency or {}).items()
        }

    # ---- public API ----------------------------------------------------

    def submit(self, kind: str, runner: JobRunner, session_id: str | None = None) -> Job:
        job = Job(kind=kind, session_id=session_id)
        self.jobs[job.id] = job
        self._publish(job)
        task = asyncio.create_task(self._run(job, runner), name=f"job-{kind}-{job.id}")
        self._tasks[job.id] = task
        return job

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def list(self, session_id: str | None = None, active_only: bool = False) -> list[Job]:
        jobs = list(self.jobs.values())
        if session_id is not None:
            jobs = [j for j in jobs if j.session_id == session_id]
        if active_only:
            jobs = [j for j in jobs if not j.is_terminal]
        return sorted(jobs, key=lambda j: j.created_at)

    async def cancel(self, job_id: str) -> bool:
        task = self._tasks.get(job_id)
        job = self.jobs.get(job_id)
        if task is None or job is None or job.is_terminal:
            return False
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        return True

    async def shutdown(self) -> None:
        for job_id in list(self._tasks):
            await self.cancel(job_id)

    # ---- internals -----------------------------------------------------

    def _publish(self, job: Job) -> None:
        self.bus.publish({"type": "job", "job": job.model_dump(mode="json")})

    async def _run(self, job: Job, runner: JobRunner) -> None:
        sem = self._limits.get(job.kind)
        try:
            if sem is not None:
                await sem.acquire()
            job.status = JobStatus.RUNNING
            job.started_at = datetime.now()
            self._publish(job)
            ctx = JobContext(job, self.bus, self)
            job.result = await runner(ctx)
            job.status = JobStatus.COMPLETED
            job.progress = 1.0
        except asyncio.CancelledError:
            job.status = JobStatus.CANCELLED
            job.message = "Cancelled"
        except Exception as e:
            logger.exception("Job %s (%s) failed", job.id, job.kind)
            job.status = JobStatus.FAILED
            job.error = str(e)
        finally:
            if sem is not None and job.status != JobStatus.QUEUED:
                sem.release()
            job.finished_at = datetime.now()
            self._publish(job)
            self._tasks.pop(job.id, None)
