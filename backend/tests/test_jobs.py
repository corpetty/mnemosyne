"""JobManager behaviour in isolation."""

import asyncio

import pytest

from mnemosyne.events import EventBus
from mnemosyne.jobs import JobManager, JobStatus


@pytest.mark.anyio
async def test_job_lifecycle_and_events():
    bus = EventBus()
    q = bus.subscribe()
    manager = JobManager(bus)

    async def runner(ctx):
        ctx.update("working", progress=0.5)
        return {"ok": True}

    job = manager.submit("demo", runner, session_id="s1")
    while not job.is_terminal:
        await asyncio.sleep(0.01)

    assert job.status == JobStatus.COMPLETED
    assert job.result == {"ok": True}
    assert job.progress == 1.0
    assert job.started_at is not None and job.finished_at is not None

    statuses = []
    while not q.empty():
        ev = q.get_nowait()
        assert ev["type"] == "job"
        statuses.append((ev["job"]["status"], ev["job"]["message"]))
    assert statuses == [
        ("queued", ""),
        ("running", ""),
        ("running", "working"),
        ("completed", "working"),
    ]


@pytest.mark.anyio
async def test_failed_job_records_error():
    manager = JobManager(EventBus())

    async def runner(ctx):
        raise RuntimeError("boom")

    job = manager.submit("demo", runner)
    while not job.is_terminal:
        await asyncio.sleep(0.01)
    assert job.status == JobStatus.FAILED
    assert job.error == "boom"


@pytest.mark.anyio
async def test_concurrency_limit_serializes_kind():
    manager = JobManager(EventBus(), concurrency={"gpu": 1})
    order = []

    def make(name):
        async def runner(ctx):
            order.append(f"{name}-start")
            await asyncio.sleep(0.02)
            order.append(f"{name}-end")

        return runner

    a = manager.submit("gpu", make("a"))
    b = manager.submit("gpu", make("b"))
    while not (a.is_terminal and b.is_terminal):
        await asyncio.sleep(0.01)
    assert order == ["a-start", "a-end", "b-start", "b-end"]


@pytest.mark.anyio
async def test_cancel_running_job():
    manager = JobManager(EventBus())

    async def runner(ctx):
        await asyncio.sleep(10)

    job = manager.submit("slow", runner)
    await asyncio.sleep(0.01)
    assert await manager.cancel(job.id) is True
    assert job.status == JobStatus.CANCELLED
    assert await manager.cancel(job.id) is False
    assert manager.list(active_only=True) == []


def test_jobs_api(client):
    assert client.get("/api/jobs").json() == []
    assert client.get("/api/jobs/nope").status_code == 404
    assert client.post("/api/jobs/nope/cancel").status_code == 409


@pytest.mark.anyio
async def test_late_progress_does_not_reopen_a_finished_job():
    """Progress is scheduled onto the loop, so a report can run after the job ended (the demo
    engine finishes in one loop turn). It used to publish the job as 'completed' again with a
    stale message, and the UI showed 'Transcription complete' three times."""
    bus = EventBus()
    manager = JobManager(bus)
    kept = {}

    async def runner(ctx):
        kept["ctx"] = ctx
        return None

    job = manager.submit("demo", runner)
    while not job.is_terminal:
        await asyncio.sleep(0.01)
    q = bus.subscribe()
    kept["ctx"].update("Transcribing audio...", progress=0.7)
    assert q.empty()
    assert job.progress == 1.0
    assert job.message == ""
