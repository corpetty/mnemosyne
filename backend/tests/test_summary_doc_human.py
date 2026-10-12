"""Hand edits go through the summary doc (services/summary_doc.py `apply_human`): the editor,
a task's text on the Tasks route and restore. A later LLM summary keeps them; without the
sidecar the old replace behaviour stays."""

import pytest

from mnemosyne.models.session import ActionItem, SummaryData
from mnemosyne.services import summary_doc
from tests import fake_link

BUDGET = "Send the budget report to finance"


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    return fake_link.install(tmp_path, monkeypatch)


def _llm_meeting(ctx, sidecar):
    sid = ctx.sessions.create_session("Weekly").id
    data = SummaryData(
        title="Weekly",
        provider="fake",
        model="m",
        decisions=["Ship in October", "Skip the beta"],
        decision_at=[0.0, 30.0],
        open_questions=["Who writes the docs?"],
        question_at=[60.0],
        action_items=[ActionItem(text=BUDGET, owner="Bob")],
    )
    out, body, _ = summary_doc.apply_llm(ctx, sid, data, "We planned.\n")
    ctx.repo.update_fields(sid, summary=body, summary_data=out)
    return sid, out


def _edit_body(d: SummaryData, **over) -> dict:
    body = {
        "summary": "We planned.\n",
        "topics": [],
        "decisions": [
            {"text": t, "at": a, "id": i}
            for t, a, i in zip(d.decisions, d.decision_at, d.decision_ids, strict=True)
        ],
        "open_questions": [
            {"text": t, "at": a, "id": i}
            for t, a, i in zip(d.open_questions, d.question_at, d.question_ids, strict=True)
        ],
        "action_items": [a.model_dump(mode="json") for a in d.action_items],
        "chapters": [],
        "client_facts": [],
    }
    body.update(over)
    return body


def _new_llm(ctx, sid, **kw):
    data = SummaryData(title="Weekly", provider="fake", model="m", **kw)
    out, body, _ = summary_doc.apply_llm(ctx, sid, data, "We planned.\n")
    ctx.repo.update_fields(sid, summary=body, summary_data=out)
    return ctx.repo.get(sid).summary_data, None


def test_editor_edit_is_kept_by_a_later_llm_summary(client, ctx, sidecar):
    sid, d = _llm_meeting(ctx, sidecar)
    ids = list(d.decision_ids)
    body = _edit_body(d)
    body["decisions"][0]["text"] = "Ship in late October"
    r = client.put(f"/api/sessions/{sid}/summary", json=body)
    assert r.status_code == 200, r.text
    assert ctx.repo.get_summary_doc(sid) is not None
    saved = ctx.repo.get(sid).summary_data
    assert saved.decisions[0] == "Ship in late October"
    assert saved.decision_ids == ids
    assert saved.edited_by is not None

    now, _ = _new_llm(
        ctx,
        sid,
        decisions=["Ship in October", "Skip the beta", "Hire a writer"],
        decision_at=[0.0, 30.0, 90.0],
        open_questions=["Who writes the docs?"],
        question_at=[60.0],
        action_items=[ActionItem(text=BUDGET, owner="Bob")],
    )
    assert "Ship in late October" in now.decisions
    assert "Hire a writer" in now.decisions
    assert now.decision_ids[:2] == ids


def test_deleting_a_decision_tombstones_it(client, ctx, sidecar):
    sid, d = _llm_meeting(ctx, sidecar)
    body = _edit_body(d)
    body["decisions"] = body["decisions"][:1]  # "Skip the beta" is deleted
    assert client.put(f"/api/sessions/{sid}/summary", json=body).status_code == 200
    assert ctx.repo.get(sid).summary_data.decisions == ["Ship in October"]

    now, _ = _new_llm(
        ctx,
        sid,
        decisions=["Ship in October", "Skip the betas"],
        decision_at=[0.0, 30.0],
        open_questions=["Who writes the docs?"],
        question_at=[60.0],
        action_items=[ActionItem(text=BUDGET, owner="Bob")],
    )
    assert now.decisions == ["Ship in October"]


def test_task_text_edit_is_kept_by_a_later_llm_summary(client, ctx, sidecar):
    sid, _ = _llm_meeting(ctx, sidecar)
    r = client.patch(
        f"/api/sessions/{sid}/action-items/0",
        json={"text": "Send the budget report to finance today"},
    )
    assert r.status_code == 200, r.text
    assert ctx.repo.get_summary_doc(sid) is not None
    assert ctx.repo.get(sid).summary_data.edited_at is not None

    now, _ = _new_llm(
        ctx,
        sid,
        decisions=["Ship in October", "Skip the beta"],
        decision_at=[0.0, 30.0],
        open_questions=["Who writes the docs?"],
        question_at=[60.0],
        action_items=[ActionItem(text=BUDGET, owner="Bob")],
    )
    assert [a.text for a in now.action_items] == ["Send the budget report to finance today"]


def test_restore_goes_through_the_doc(client, ctx, sidecar):
    sid, d = _llm_meeting(ctx, sidecar)
    body = _edit_body(d)
    body["decisions"][0]["text"] = "Ship in November"
    assert client.put(f"/api/sessions/{sid}/summary", json=body).status_code == 200
    versions = ctx.repo.versions(sid)
    edited = next(v for v in versions if v["reason"] == "edited")

    r = client.post(f"/api/sessions/{sid}/versions/{edited['id']}/restore-summary")
    assert r.status_code == 200, r.text
    assert ctx.repo.get(sid).summary_data.decisions[0] == "Ship in October"
    doc = ctx.repo.get_summary_doc(sid)
    assert doc is not None
    assert d.decision_ids[0] in doc["blocks"]


def test_without_the_sidecar_edits_are_saved_as_before(client, ctx, monkeypatch):
    monkeypatch.setenv("MNEMOSYNE_LINK_BIN", "/nonexistent/mnemosyne-link")
    sid = ctx.sessions.create_session("Weekly").id
    ctx.repo.update_fields(
        sid, summary="Old.", summary_data=SummaryData(decisions=["A"], decision_at=[None])
    )
    body = {"summary": "New.", "decisions": [{"text": "B", "at": None}]}
    r = client.put(f"/api/sessions/{sid}/summary", json=body)
    assert r.status_code == 200, r.text
    got = ctx.repo.get(sid)
    assert got.summary == "New."
    assert got.summary_data.decisions == ["B"]
    assert ctx.repo.get_summary_doc(sid) is None
