"""Weekly digest: date ranges, prompt, deterministic appendix, job, vault file, schedule."""

from datetime import date, datetime

from mnemosyne.models.session import ActionItem, Session, SummaryData
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services.digest_service import (
    due_week,
    file_name,
    range_label,
    week_bounds,
)
from tests.conftest import drain_until_job


def _session(name, when, summary="", decisions=(), items=(), transcript=True):
    return Session(
        name=name,
        created_at=when,
        summary=summary,
        summary_data=SummaryData(
            decisions=list(decisions), action_items=[ActionItem(**i) for i in items]
        )
        if summary
        else None,
        transcript=[TranscriptSegment(text="hi", speaker="S", start=0, end=1)]
        if transcript
        else [],
    )


def _seed(ctx):
    a = _session(
        "Release sync",
        datetime(2026, 9, 21, 10),
        summary="Agreed to ship the Waku migration in October.",
        decisions=["Ship in October"],
        items=[
            {"text": "Update the docs", "owner": "Alice"},
            {"text": "Tag rc1", "issue_url": "https://github.com/o/r/issues/7"},
        ],
    )
    b = _session("Hiring", datetime(2026, 9, 23, 14), summary="Two frontend candidates.")
    c = _session("Standup", datetime(2026, 9, 24, 9))  # transcribed, never summarized
    outside = _session("Old", datetime(2026, 9, 14, 9), summary="Last week.")
    for s in (a, b, c, outside):
        ctx.repo.save(s)
    return a, b, c


def _digest(client, **body):
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        resp = client.post("/api/digests", json=body)
        assert resp.status_code == 200, resp.text
        job = resp.json()
        assert job["kind"] == "digest"
        drain_until_job(ws, job["id"])
    return client.get(f"/api/jobs/{job['id']}").json()


def test_ranges_and_labels():
    assert week_bounds(date(2026, 9, 24)) == (date(2026, 9, 21), date(2026, 9, 27))
    assert range_label(date(2026, 9, 21), date(2026, 9, 27)) == "2026-W39"
    assert range_label(date(2026, 9, 1), date(2026, 9, 10)) == "2026-09-01 to 2026-09-10"
    assert file_name("2026-09-01 to 2026-09-10") == "2026-09-01--2026-09-10.md"
    # Friday 17:00 schedule
    assert due_week(datetime(2026, 9, 24, 23), 4, 17) is None  # Thursday
    assert due_week(datetime(2026, 9, 25, 16), 4, 17) is None  # Friday, too early
    assert due_week(datetime(2026, 9, 25, 17), 4, 17) == (date(2026, 9, 21), date(2026, 9, 27))
    assert due_week(datetime(2026, 9, 27, 8), 4, 17) is not None  # later in the week
    assert due_week(datetime(2026, 9, 25, 17), -1, 17) is None  # schedule off


def test_digest_job_writes_llm_body_and_exact_appendix(client, ctx, fake_provider, tmp_path):
    _seed(ctx)
    vault = tmp_path / "vault"
    vault.mkdir()
    ctx.settings.obsidian_vault_path = str(vault)
    ctx.settings.glossary = "Waku"
    fake_provider.reply = "## Overview\nA busy week.\n\n## Themes\n- Waku migration"

    job = _digest(client, start="2026-09-21", end="2026-09-27", provider="fake")
    assert job["status"] == "completed", job["error"]
    d = job["result"]
    assert d["label"] == "2026-W39"
    assert d["provider"] == "fake" and d["model"] == "fake-model-a"
    md = d["markdown"]
    assert md.startswith("# Digest 2026-W39\n\n## Overview\nA busy week.")
    assert "[[2026-09-21-Release sync|Release sync]]" in md
    assert "- Ship in October ([[2026-09-21-Release sync|Release sync]])" in md
    assert "- [ ] Update the docs (Alice) · [[2026-09-21-Release sync|Release sync]]" in md
    assert "[issue](https://github.com/o/r/issues/7)" in md
    assert "## Not summarized\n- Thu Sep 24 · Standup" in md
    assert "Old" not in md  # outside the range

    sent = fake_provider.calls[-1]
    assert "## Overview" in sent["system_prompt"]
    assert "Waku" in sent["system_prompt"].split("No preamble.")[1]  # glossary appended
    assert sent["transcript"].startswith("Period: 2026-09-21 to 2026-09-27, 2 meetings.")
    assert '"Hiring"' in sent["transcript"] and "Standup" not in sent["transcript"]

    path = vault / "meetings/mnemosyne/digests/2026-W39.md"
    assert d["path"] == str(path)
    text = path.read_text()
    assert text.startswith("---\ntype: digest\nstart: 2026-09-21\nend: 2026-09-27\nmeetings: 2\n")
    assert text.endswith(md)

    listed = client.get("/api/digests").json()
    assert [x["id"] for x in listed] == [d["id"]]
    # Regenerating the same week replaces it.
    again = _digest(client, start="2026-09-21", provider="fake")["result"]
    assert [x["id"] for x in client.get("/api/digests").json()] == [again["id"]]
    d = again
    assert client.get(f"/api/digests/{d['id']}").json()["markdown"] == md
    assert client.delete(f"/api/digests/{d['id']}").status_code == 200
    assert client.get(f"/api/digests/{d['id']}").status_code == 404


def test_digest_without_summaries_fails_clearly(client, ctx, fake_provider):
    ctx.repo.save(_session("Standup", datetime(2026, 9, 24, 9)))
    job = _digest(client, start="2026-09-21", provider="fake")
    assert job["status"] == "failed"
    assert "No summarized meetings between 2026-09-21 and 2026-09-27" in job["error"]
    assert fake_provider.calls == []


def test_digest_rejects_bad_ranges(client):
    r = client.post("/api/digests", json={"start": "2026-09-21", "end": "2026-09-20"})
    assert r.status_code == 400
    r = client.post("/api/digests", json={"start": "2026-01-01", "end": "2026-09-20"})
    assert r.status_code == 400


def test_schedule_submits_once_per_week(ctx, monkeypatch):
    _seed(ctx)
    submitted = []
    monkeypatch.setattr(ctx.jobs, "submit", lambda kind, runner, **_: submitted.append(kind))
    friday = datetime(2026, 9, 25, 18)

    assert ctx.maybe_schedule_digest(friday) is None and submitted == []  # schedule off
    ctx.settings.digest_weekday = 4
    ctx.maybe_schedule_digest(friday)
    assert submitted == ["digest"]

    from mnemosyne.models.digest import Digest

    ctx.repo.save_digest(Digest(label="2026-W39", start=date(2026, 9, 21), end=date(2026, 9, 27)))
    ctx.maybe_schedule_digest(friday)
    assert submitted == ["digest"]  # this week already has one
    # A week with no summarized meetings is skipped.
    ctx.maybe_schedule_digest(datetime(2026, 10, 2, 18))
    assert submitted == ["digest"]
