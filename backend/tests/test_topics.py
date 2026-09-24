"""Topic threads across meetings."""

from datetime import datetime

from mnemosyne.models.session import ActionItem, Chapter, Session, SummaryData
from mnemosyne.models.transcript import TranscriptSegment
from tests.conftest import drain_until_job


def _s(name, day, topics, decisions=(), items=(), questions=(), chapters=(), text="talk"):
    return Session(
        name=name,
        created_at=datetime(2026, 9, day, 10),
        transcript=[TranscriptSegment(text=text, speaker="A", start=0, end=5)],
        summary=f"{name} summary. More detail here. Even more. And more.",
        summary_data=SummaryData(
            topics=list(topics),
            decisions=list(decisions),
            action_items=[ActionItem(text=t) for t in items],
            open_questions=list(questions),
            chapters=[Chapter(start=0, title=c) for c in chapters],
        ),
    )


def _seed(ctx):
    a = _s(
        "Release sync",
        10,
        ["Waku migration", "release"],
        decisions=["Ship the Waku migration in October", "Hire a designer"],
        items=["Update the migration docs", "Book the offsite"],
        questions=["Is a second rc needed for the migration?"],
        chapters=["Migration status", "Offsite"],
    )
    b = _s(
        "Infra weekly",
        17,
        ["costs", "Waku migration"],
        decisions=["Move store nodes after the migration"],
        text="store nodes and costs",
    )
    c = _s("Hiring", 20, ["hiring"], decisions=["Final round Thursday"])
    for s in (a, b, c):
        ctx.repo.save(s)
        ctx.index.index_session(s.id)
    return a, b, c


def test_frequent_topics(client, ctx):
    _seed(ctx)
    topics = client.get("/api/topics").json()
    assert topics[0]["topic"] == "Waku migration" and topics[0]["meetings"] == 2
    assert topics[0]["last_seen"].startswith("2026-09-17")


def test_thread_picks_meetings_and_matching_pieces(client, ctx):
    a, b, c = _seed(ctx)
    t = client.get("/api/topics/thread", params={"q": "migration"}).json()
    names = [m["name"] for m in t["meetings"]]
    assert names[:2] == ["Release sync", "Infra weekly"]  # oldest first
    assert "Hiring" not in names
    first = t["meetings"][0]
    assert first["decisions"] == ["Ship the Waku migration in October"]
    assert [x["text"] for x in first["action_items"]] == ["Update the migration docs"]
    assert first["open_questions"] == ["Is a second rc needed for the migration?"]
    assert [c["title"] for c in first["chapters"]] == ["Migration status"]
    assert first["summary"] == "Release sync summary. More detail here. Even more."


def test_thread_summary_job(client, ctx, fake_provider):
    _seed(ctx)
    ctx.settings.default_provider = "fake"
    fake_provider.reply = "## Where it stands\nShipping in October."
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        job = client.post("/api/topics/thread/summary", json={"q": "migration"}).json()
        drain_until_job(ws, job["id"])
    job = client.get(f"/api/jobs/{job['id']}").json()
    assert job["status"] == "completed", job["error"]
    assert job["result"]["text"].startswith("## Where it stands")
    sent = fake_provider.calls[-1]["transcript"]
    assert sent.startswith("Topic: migration")
    assert "Decision: Ship the Waku migration in October" in sent
    assert client.post("/api/topics/thread/summary", json={"q": "x"}).status_code == 400
