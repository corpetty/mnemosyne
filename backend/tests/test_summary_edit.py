"""Editing a summary by hand (services/summary_edit.py, PUT /api/sessions/{id}/summary)."""

from mnemosyne.models.session import ActionItem, Chapter, Session, SummaryData
from mnemosyne.models.transcript import TranscriptSegment
from tests.conftest import run_summarize
from tests.test_sharing import _meeting, team  # noqa: F401  (fixture)


def _session(ctx) -> Session:
    return ctx.repo.save(
        Session(
            name="Planning",
            transcript=[
                TranscriptSegment(text="we ship in October", speaker="Ann", start=0, end=2),
                TranscriptSegment(text="who writes the docs?", speaker="Bob", start=60, end=62),
            ],
            summary="We planned the launch.",
            summary_data=SummaryData(
                source_hash="abc",
                provider="vllm",
                topics=["Launch"],
                decisions=["Ship in October", "Skip the beta"],
                decision_at=[0.0, 30.0],
                open_questions=["Who writes the docs?"],
                question_at=[60.0],
                action_items=[
                    ActionItem(text="Send the plan", owner="Bob", issue_url="https://x/1", at=5.0)
                ],
                chapters=[Chapter(start=0, title="Intro")],
                followup="Hi all,",
            ),
        )
    )


def _edit(**over) -> dict:
    body = {
        "summary": "We planned the launch and the docs.",
        "topics": ["Launch", " #Docs ", "launch", ""],
        # Reordered and one removed: each keeps its own transcript time.
        "decisions": [{"text": "Skip the beta", "at": 30.0}, {"text": "  ", "at": None}],
        "open_questions": [{"text": "Who writes the docs?", "at": 60.0}],
        "action_items": [
            {"text": "Send the plan", "owner": "Bob", "issue_url": "https://x/1", "at": 5.0},
            {"text": "Write the docs", "owner": " Ann ", "due": "2026-10-20"},
            {"text": "", "owner": "nobody"},
        ],
        "chapters": [{"start": 60, "title": "Docs"}, {"start": 0, "title": "Intro"}],
        "client_facts": [],
    }
    body.update(over)
    return body


def test_everything_in_a_summary_can_be_edited(client, ctx):
    s = _session(ctx)
    r = client.put(f"/api/sessions/{s.id}/summary", json=_edit())
    assert r.status_code == 200, r.text
    got = r.json()
    d = got["summary_data"]
    assert got["summary"] == "We planned the launch and the docs."
    assert d["topics"] == ["Launch", "Docs"]
    assert d["decisions"] == ["Skip the beta"] and d["decision_at"] == [30.0]
    assert d["open_questions"] == ["Who writes the docs?"] and d["question_at"] == [60.0]
    items = d["action_items"]
    assert [(a["text"], a["owner"], a["due"]) for a in items] == [
        ("Send the plan", "Bob", None),
        ("Write the docs", "Ann", "2026-10-20"),
    ]
    assert items[0]["issue_url"] == "https://x/1" and items[0]["at"] == 5.0
    assert [c["title"] for c in d["chapters"]] == ["Intro", "Docs"]  # in time order
    # What the edit does not cover stays; the edit is marked.
    assert d["source_hash"] == "abc" and d["provider"] == "vllm" and d["followup"] == "Hi all,"
    assert d["edited_at"] is not None
    assert got["summary_stale"] == s.summary_stale  # same transcript fingerprint as before
    # Kept: the summary as it was, in the history, findable by the new text.
    assert [v["reason"] for v in ctx.repo.versions(s.id)][-1] == "edited"
    assert "summary_edited" in [e["kind"] for e in ctx.repo.events(s.id)]
    hits = client.get("/api/search", params={"q": "docs"}).json()
    assert s.id in [h["session_id"] for h in hits]


def test_a_summary_can_be_written_where_there_was_none(client, ctx):
    s = ctx.repo.save(Session(name="Hallway chat"))
    r = client.put(f"/api/sessions/{s.id}/summary", json=_edit(summary="Agreed on lunch."))
    assert r.status_code == 200
    assert r.json()["summary"] == "Agreed on lunch."
    assert r.json()["summary_data"]["style"] == "manual"
    assert client.put("/api/sessions/nope/summary", json=_edit()).status_code == 404


def test_summarizing_again_starts_unedited_and_keeps_ticks(client, ctx, fake_provider):
    s = _session(ctx)
    body = _edit()
    body["action_items"][0]["done"] = True
    client.put(f"/api/sessions/{s.id}/summary", json=body)
    run_summarize(client, s.id, {"provider": "fake"})
    d = client.get(f"/api/sessions/{s.id}").json()["summary_data"]
    assert d["edited_at"] is None and d["edited_by"] == ""


def test_someone_elses_summary_stays_as_it_is(team):  # noqa: F811
    client, ctx, people = team
    s = _meeting(ctx, people, "Ann", "Planning")
    ann, bob = people["Ann"][1], people["Bob"][1]
    client.put(
        f"/api/sessions/{s.id}/shares", json={"user_ids": [people["Bob"][0].id]}, headers=ann
    )
    r = client.put(f"/api/sessions/{s.id}/summary", json=_edit(), headers=bob)
    assert r.status_code == 403
    assert ctx.repo.get(s.id).summary == "We planned."
    assert [v["reason"] for v in ctx.repo.versions(s.id)] == []  # no version for a refused edit
    r = client.put(f"/api/sessions/{s.id}/summary", json=_edit(), headers=ann)
    assert r.status_code == 200
    assert r.json()["summary_data"]["edited_by"] == "Ann"


def test_an_edited_follow_up_is_kept_without_marking_the_summary(client, ctx):
    s = _session(ctx)
    r = client.put(f"/api/sessions/{s.id}/followup", json={"text": " Hi all, see you Monday. "})
    assert r.status_code == 200
    d = r.json()["summary_data"]
    assert d["followup"] == "Hi all, see you Monday." and d["edited_at"] is None
    bare = ctx.repo.save(Session(name="No summary"))
    assert client.put(f"/api/sessions/{bare.id}/followup", json={"text": "x"}).status_code == 404
