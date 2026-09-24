"""Follow-up drafts from a meeting summary."""

from datetime import datetime

from mnemosyne.models.session import ActionItem, Session, SummaryData
from mnemosyne.services.followup import SYSTEM, clean, prompt_for
from tests.conftest import drain_until_job


def _session():
    return Session(
        name="Release sync",
        created_at=datetime(2026, 9, 24, 10),
        participants=["Alice", "SPEAKER_01"],
        summary="Reviewed the release.",
        summary_data=SummaryData(
            decisions=["Ship in October"],
            action_items=[
                ActionItem(text="Update docs", owner="Alice"),
                ActionItem(text="Tag rc1", done=True),
            ],
            open_questions=["Who owns QA?"],
        ),
    )


def _draft(client, sid, **body):
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        r = client.post(f"/api/sessions/{sid}/followup", json=body)
        assert r.status_code == 200, r.text
        drain_until_job(ws, r.json()["id"])
    return client.get(f"/api/jobs/{r.json()['id']}").json()


def test_prompt_contents():
    p = prompt_for(_session())
    assert p.startswith('Meeting: "Release sync" on Thursday September 24, 2026')
    assert "People: Alice, SPEAKER_01" in p
    assert "- Update docs (owner: Alice)" in p and "- Tag rc1 [already done]" in p
    assert "Open questions:\n- Who owns QA?" in p
    assert "Subject:" in SYSTEM["email"] and "Next steps:" in SYSTEM["chat"]


def test_clean_strips_fences():
    assert clean("```\nSubject: x\n\nHi\n```\n") == "Subject: x\n\nHi"


def test_followup_job_saves_draft(client, ctx, fake_provider):
    s = ctx.repo.save(_session())
    fake_provider.reply = "Recap: shipping in October.\nNext steps:\n- Update docs (Alice)"
    job = _draft(client, s.id, style="chat", provider="fake")
    assert job["status"] == "completed", job["error"]
    assert job["result"]["followup"].startswith("Recap: shipping")
    assert job["result"]["style"] == "chat" and job["result"]["model"] == "fake-model-a"
    assert "team chat" in fake_provider.calls[-1]["system_prompt"]
    saved = ctx.repo.get(s.id).summary_data
    assert saved.followup.startswith("Recap: shipping")
    assert saved.decisions == ["Ship in October"]  # nothing else changed


def test_followup_requires_summary(client, ctx):
    s = ctx.repo.save(Session(name="empty"))
    assert client.post(f"/api/sessions/{s.id}/followup", json={}).status_code == 400
    assert client.post("/api/sessions/nope/followup", json={}).status_code == 404
    r = client.post(f"/api/sessions/{s.id}/followup", json={"style": "fax"})
    assert r.status_code == 422
