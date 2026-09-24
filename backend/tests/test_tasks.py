"""Action items across meetings: listing, filtering, done state, carry-over."""

import json
from datetime import datetime

from mnemosyne.models.session import ActionItem, Session, SummaryData
from mnemosyne.services.tasks import carry_over, same_item
from tests.conftest import run_summarize


def _seed(ctx):
    a = Session(
        name="Release sync",
        created_at=datetime(2026, 9, 21, 10),
        summary="s",
        summary_data=SummaryData(
            action_items=[
                ActionItem(text="Update the docs", owner="Alice"),
                ActionItem(text="Tag rc1", owner="Bob", done=True),
            ]
        ),
    )
    b = Session(
        name="Hiring",
        created_at=datetime(2026, 9, 23, 14),
        summary="s",
        summary_data=SummaryData(action_items=[ActionItem(text="Schedule final round")]),
    )
    c = Session(name="Unsummarized", created_at=datetime(2026, 9, 24))
    for s in (a, b, c):
        ctx.repo.save(s)
    return a, b


def test_list_and_filter(client, ctx):
    a, b = _seed(ctx)
    open_items = client.get("/api/action-items").json()
    assert [(t["session_name"], t["text"]) for t in open_items] == [
        ("Hiring", "Schedule final round"),
        ("Release sync", "Update the docs"),
    ]
    assert open_items[1]["idx"] == 0 and open_items[1]["owner"] == "Alice"
    done = client.get("/api/action-items?status=done").json()
    assert [t["text"] for t in done] == ["Tag rc1"]
    assert len(client.get("/api/action-items?status=all").json()) == 3
    assert [t["text"] for t in client.get("/api/action-items?status=all&owner=bob").json()] == [
        "Tag rc1"
    ]
    assert client.get("/api/action-items?status=bogus").status_code == 422


def test_toggle_done(client, ctx):
    a, _ = _seed(ctx)
    r = client.patch(f"/api/sessions/{a.id}/action-items/0", json={"done": True})
    assert r.status_code == 200 and r.json()["done"] is True
    assert ctx.repo.get(a.id).summary_data.action_items[0].done is True
    assert [t["text"] for t in client.get("/api/action-items").json()] == ["Schedule final round"]
    assert (
        client.patch(f"/api/sessions/{a.id}/action-items/9", json={"done": True}).status_code == 404
    )
    assert client.patch("/api/sessions/nope/action-items/0", json={"done": True}).status_code == 404


def test_same_item_and_carry_over():
    assert same_item("Update the docs.", "update the docs")
    assert same_item("Update the release docs", "Update the release doc")
    assert not same_item("Update the docs", "Tag rc1")
    old = SummaryData(
        action_items=[
            ActionItem(text="Update the docs", done=True, issue_url="https://x/1"),
            ActionItem(text="Tag rc1"),
        ]
    )
    new = SummaryData(
        action_items=[ActionItem(text="Update the docs."), ActionItem(text="Book a room")]
    )
    carry_over(old, new)
    assert new.action_items[0].done and new.action_items[0].issue_url == "https://x/1"
    assert not new.action_items[1].done and new.action_items[1].issue_url is None
    assert carry_over(None, new) is new


def test_resummarize_keeps_done(client, ctx, transcribed_session, fake_provider):
    sid = transcribed_session["id"]
    fake_provider.summary = json.dumps(
        {"summary": "s", "action_items": [{"text": "Write the plan", "owner": "SPEAKER_00"}]}
    )
    assert run_summarize(client, sid, {"provider": "fake"})["status"] == "completed"
    client.patch(f"/api/sessions/{sid}/action-items/0", json={"done": True})
    fake_provider.summary = json.dumps(
        {"summary": "s2", "action_items": [{"text": "write the plan"}, {"text": "New thing"}]}
    )
    assert run_summarize(client, sid, {"provider": "fake"})["status"] == "completed"
    items = ctx.repo.get(sid).summary_data.action_items
    assert [(i.text, i.done) for i in items] == [("write the plan", True), ("New thing", False)]
