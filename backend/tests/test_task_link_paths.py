"""New meetings and hand edits get their action items linked (services/task_links.py run from
the summarize job and services/summary_edit.py)."""

import json
from datetime import datetime

from mnemosyne.models.session import ActionItem, Session, SummaryData
from mnemosyne.models.transcript import TranscriptSegment
from tests.conftest import run_summarize

DOCS = "Update the docs for the new release"


def _earlier(ctx, name="Infra weekly") -> Session:
    return ctx.repo.save(
        Session(
            name=name,
            created_at=datetime(2026, 10, 1, 10),
            summary="s",
            summary_data=SummaryData(action_items=[ActionItem(id="a1", text=DOCS, owner="Ann")]),
        )
    )


def _new(ctx, name="Infra weekly 2") -> Session:
    return ctx.repo.save(
        Session(
            name=name,
            created_at=datetime(2026, 10, 8, 10),
            transcript=[TranscriptSegment(text="update the docs", speaker="Ann", start=0, end=2)],
        )
    )


def _reply(*texts) -> str:
    return json.dumps(
        {
            "summary": ["Notes."],
            "decisions": [],
            "open_questions": [],
            "action_items": [{"text": t, "owner": "Ann"} for t in texts],
        }
    )


def _items(ctx, sid):
    return ctx.repo.get(sid).summary_data.action_items


def _linked(ctx, sid):
    return [e["detail"] for e in ctx.repo.events(sid) if e["kind"] == "task_linked"]


def test_a_summarize_job_links_an_item_to_an_earlier_open_task(client, ctx, fake_provider):
    a, b = _earlier(ctx), _new(ctx)
    fake_provider.summary = _reply(DOCS)
    assert run_summarize(client, b.id, {"provider": "fake"})["status"] == "completed"
    (item,) = _items(ctx, b.id)
    (earlier,) = _items(ctx, a.id)
    assert len(item.task_id) == 12 and item.task_id == earlier.task_id
    assert _linked(ctx, b.id) == [{"task": DOCS, "earlier": "Infra weekly", "session": a.id}]
    # Summarizing again keeps the one link (no second history line, same id).
    assert run_summarize(client, b.id, {"provider": "fake"})["status"] == "completed"
    assert _items(ctx, b.id)[0].task_id == item.task_id
    assert len(_linked(ctx, b.id)) == 1


def test_task_linking_off_links_nothing(client, ctx, fake_provider):
    ctx.settings.task_linking = "off"
    a, b = _earlier(ctx), _new(ctx)
    fake_provider.summary = _reply(DOCS)
    assert run_summarize(client, b.id, {"provider": "fake"})["status"] == "completed"
    assert _items(ctx, b.id)[0].task_id == "" and _items(ctx, a.id)[0].task_id == ""
    assert _linked(ctx, b.id) == []


def test_a_linking_failure_does_not_fail_the_job(client, ctx, fake_provider, monkeypatch):
    from mnemosyne.services import task_links

    def boom(*a, **k):
        raise RuntimeError("db is sad")

    monkeypatch.setattr(task_links, "link_items", boom)
    _earlier(ctx)
    b = _new(ctx)
    fake_provider.summary = _reply(DOCS)
    job = run_summarize(client, b.id, {"provider": "fake"})
    assert job["status"] == "completed"
    assert [a.text for a in _items(ctx, b.id)] == [DOCS]


def test_related_only_links_with_the_related_setting(client, ctx, fake_provider):
    close = "Update the docs for the new releases"
    a = ctx.repo.save(
        Session(
            name="Infra weekly 2026-10-01",
            created_at=datetime(2026, 10, 1, 10),
            summary="s",
            summary_data=SummaryData(action_items=[ActionItem(id="a1", text=DOCS)]),
        )
    )
    b = _new(ctx, "Infra weekly 2026-10-08")
    ctx.settings.task_linking = "exact"
    fake_provider.summary = _reply(close)
    run_summarize(client, b.id, {"provider": "fake"})
    assert _items(ctx, b.id)[0].task_id == ""
    ctx.settings.task_linking = "related"
    run_summarize(client, b.id, {"provider": "fake"})
    assert _items(ctx, b.id)[0].task_id == _items(ctx, a.id)[0].task_id != ""


def _edit(texts, items_extra=None):
    return {
        "summary": "Notes.",
        "topics": [],
        "decisions": [],
        "open_questions": [],
        "action_items": [{"text": t, "owner": "Ann"} for t in texts],
        "chapters": [],
        "client_facts": [],
    }


def test_an_edit_that_adds_an_exact_twin_links_it(client, ctx):
    a = _earlier(ctx)
    b = ctx.repo.save(
        Session(
            name="Hiring",
            created_at=datetime(2026, 10, 8, 10),
            summary="s",
            summary_data=SummaryData(action_items=[ActionItem(id="b1", text="Something else")]),
        )
    )
    r = client.put(f"/api/sessions/{b.id}/summary", json=_edit(["Something else", DOCS]))
    assert r.status_code == 200, r.text
    items = r.json()["summary_data"]["action_items"]
    twin = next(i for i in items if i["text"] == DOCS)
    assert len(twin["task_id"]) == 12 and twin["task_id"] == _items(ctx, a.id)[0].task_id
    assert next(i for i in items if i["text"] == "Something else")["task_id"] == ""
    assert len(_linked(ctx, b.id)) == 1
    # Editing again without the field (an older client) keeps the link.
    r = client.put(f"/api/sessions/{b.id}/summary", json=_edit(["Something else", DOCS, "More"]))
    got = {i["text"]: i["task_id"] for i in r.json()["summary_data"]["action_items"]}
    assert got[DOCS] == twin["task_id"] and got["More"] == ""


def test_an_edit_does_not_link_when_linking_is_off(client, ctx):
    ctx.settings.task_linking = "off"
    _earlier(ctx)
    b = ctx.repo.save(
        Session(name="Hiring", created_at=datetime(2026, 10, 8, 10), summary="s",
                summary_data=SummaryData(action_items=[ActionItem(id="b1", text="x y z")]))
    )  # fmt: skip
    r = client.put(f"/api/sessions/{b.id}/summary", json=_edit([DOCS]))
    assert r.json()["summary_data"]["action_items"][0]["task_id"] == ""


def test_restoring_a_summary_links_exact_twins(client, ctx):
    a = _earlier(ctx)
    b = ctx.repo.save(
        Session(
            name="Hiring",
            created_at=datetime(2026, 10, 8, 10),
            summary="old",
            summary_data=SummaryData(action_items=[ActionItem(id="b1", text=DOCS + "!")]),
        )
    )
    # An older version of the meeting that has the twin's exact wording.
    old = b.model_copy(deep=True)
    old.summary_data.action_items[0].text = DOCS.upper()
    ctx.repo.keep_version(old, "edited")
    version = next(v for v in ctx.repo.versions(b.id) if v["has_summary"])
    r = client.post(f"/api/sessions/{b.id}/versions/{version['id']}/restore-summary")
    assert r.status_code == 200, r.text
    (item,) = _items(ctx, b.id)
    assert item.task_id == _items(ctx, a.id)[0].task_id != ""
