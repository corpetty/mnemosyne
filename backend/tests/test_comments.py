"""Comments on the desktop (services/comments.py, routes/comments.py): anchors re-found after
transcript edits, a new transcription and new summaries; replies, resolve/reopen, history."""

import pytest

from mnemosyne.models.session import ActionItem, Recording, SummaryData
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services import comments
from mnemosyne.services.summary_doc import apply_llm
from tests import fake_link

SEGS = [
    TranscriptSegment(text="We will ship the release on Friday", speaker="A", start=0.0, end=4.0),
    TranscriptSegment(text="Sounds good to me", speaker="B", start=4.5, end=6.0),
    TranscriptSegment(text="Let's also update the docs", speaker="A", start=6.5, end=8.0),
]
BODY = (
    "The team agreed to ship the release on Friday. Docs need an update before then. "
    "Budget talks move to next week."
)


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    return fake_link.install(tmp_path, monkeypatch)


def _meeting(ctx, segments=SEGS) -> str:
    sid = ctx.sessions.create_session("Weekly").id
    ctx.sessions.set_transcript(sid, list(segments))
    return sid


def _url(sid, rest=""):
    return f"/api/sessions/{sid}/comments{rest}"


def _post(client, sid, **body):
    r = client.post(_url(sid), json={"text": "A comment", **body})
    assert r.status_code == 200, r.text
    return r.json()


def _root(client, sid, cid):
    for t in client.get(_url(sid)).json()["threads"]:
        if t["root"]["id"] == cid:
            return t["root"]
    raise AssertionError(f"no thread {cid}")


def test_a_line_comment_is_a_time_span_with_the_lines_text(client, ctx):
    ctx.settings.local_speaker_name = "Corey"
    sid = _meeting(ctx)
    c = _post(client, sid, target="segment", idx=1, text="Really?")
    assert c["anchor"] == {"part": 0, "start": 4.5, "end": 6.0}
    assert c["quote"] == "Sounds good to me"
    assert (c["author"], c["author_id"]) == ("Corey", "")
    assert c["segment_idx"] == 1 and not c["changed"] and not c["detached"]
    assert c["can_edit"] and c["can_delete"] and c["can_resolve"]
    span = _post(client, sid, target="segment", idx=0, idx_to=1)
    assert span["anchor"]["end"] == 6.0 and span["quote"] == f"{SEGS[0].text} {SEGS[1].text}"
    assert span["segment_idx"] == 0 and not span["changed"] and not span["detached"]
    got = client.get(_url(sid)).json()
    assert got["can_comment"] and [t["root"]["id"] for t in got["threads"]] == [c["id"], span["id"]]
    # Places that are not there.
    for bad in ({"idx": 3}, {"idx": 1, "idx_to": 0}, {}, {"seconds": -1}):
        assert (
            client.post(_url(sid), json={"text": "x", "target": "segment", **bad}).status_code
            == 400
        )
    assert (
        client.post(_url(sid), json={"text": " ", "target": "segment", "idx": 0}).status_code == 400
    )
    assert client.post(_url(sid), json={"text": "x", "target": "nope"}).status_code == 400
    assert client.get(_url("nope")).status_code == 404
    assert client.post(_url("nope"), json={"text": "x"}).status_code == 404


def test_the_desktop_name_falls_back_to_you(client, ctx):
    ctx.settings.local_speaker_name = ""
    sid = _meeting(ctx)
    assert _post(client, sid)["author"] == "You"


def test_line_comments_follow_edits_splits_merges_and_deletes(client, ctx):
    sid = _meeting(ctx)
    cid = _post(client, sid, target="segment", idx=1)["id"]
    seg = f"/api/sessions/{sid}/segments"

    # Text edited: still there, flagged.
    client.patch(f"{seg}/1", json={"text": "Sounds great to me"})
    root = _root(client, sid, cid)
    assert root["segment_idx"] == 1 and root["changed"] and not root["detached"]
    client.patch(f"{seg}/1", json={"text": "Sounds good to me"})
    assert not _root(client, sid, cid)["changed"]

    # Split (times by text length: "Sounds good" | "to me"): the bigger half; the words are
    # all still there.
    client.post(f"{seg}/1/split", json={"offset": 11})
    root = _root(client, sid, cid)
    assert root["segment_idx"] == 1 and not root["changed"]
    # A line written about the second half lands there.
    late = _post(client, sid, target="segment", idx=2)["id"]
    assert _root(client, sid, late)["segment_idx"] == 2

    # Merged back, then merged into the line before: it follows.
    client.post(f"{seg}/2/merge")
    assert _root(client, sid, cid)["segment_idx"] == 1
    client.post(f"{seg}/1/merge")
    root = _root(client, sid, cid)
    assert root["segment_idx"] == 0 and not root["changed"] and not root["detached"]

    # Deleted: detached (kept, with its quote); the undo puts it back.
    session = client.get(f"/api/sessions/{sid}").json()
    line = session["transcript"][0]
    client.delete(f"{seg}/0")
    root = _root(client, sid, cid)
    assert root["detached"] and root["segment_idx"] is None
    assert root["quote"] == "Sounds good to me"
    client.post(seg, json={"index": 0, "segment": line})
    root = _root(client, sid, cid)
    assert root["segment_idx"] == 0 and not root["detached"]


def test_a_new_transcription_reanchors_by_time(client, ctx):
    sid = _meeting(ctx)
    cid = _post(client, sid, target="segment", idx=1)["id"]
    shifted = [
        TranscriptSegment(text="We will ship it on Friday", speaker="A", start=0.0, end=3.0),
        TranscriptSegment(text="Okay, and", speaker="B", start=3.2, end=4.7),
        TranscriptSegment(text="sounds good to me", speaker="B", start=4.7, end=6.2),
        TranscriptSegment(text="Let's also update the docs", speaker="A", start=6.4, end=8.0),
    ]
    ctx.sessions.set_transcript(sid, shifted)
    root = _root(client, sid, cid)
    assert root["segment_idx"] == 2 and not root["changed"] and not root["detached"]


def test_a_later_part_is_anchored_into_that_part(client, ctx):
    sid = ctx.sessions.create_session("Two parts").id
    rec = {"source": "import", "device_id": 0, "device_name": "x", "path": "/f"}
    ctx.sessions.set_audio(
        sid, "/fake/joined.ogg", [Recording(**rec), Recording(**rec, part=1, offset=100.0)]
    )
    ctx.sessions.set_transcript(
        sid,
        [
            TranscriptSegment(text="First part", speaker="A", start=1.0, end=3.0),
            TranscriptSegment(text="Second part", speaker="A", start=101.0, end=104.0),
        ],
    )
    c = _post(client, sid, target="segment", idx=1)
    assert c["anchor"] == {"part": 1, "start": 1.0, "end": 4.0} and c["segment_idx"] == 1
    moment = _post(client, sid, target="segment", seconds=102.5)
    assert moment["anchor"] == {"part": 1, "start": 2.5, "end": 2.5}
    assert moment["quote"] == "Second part" and moment["segment_idx"] == 1


def test_a_moment_lands_on_the_line_playing_then(client, ctx):
    sid = _meeting(ctx)
    c = _post(client, sid, target="segment", seconds=5.0, text="Listen here")
    assert c["anchor"] == {"part": 0, "start": 5.0, "end": 5.0}
    assert c["quote"] == "Sounds good to me" and c["segment_idx"] == 1
    # Split at about 5.5 s (by text length): the moment is in the first half.
    client.post(f"/api/sessions/{sid}/segments/1/split", json={"offset": 11})
    root = _root(client, sid, c["id"])
    assert root["segment_idx"] == 1 and not root["detached"]
    assert root["at_seconds"] == 5.0
    # Between lines: nothing playing, so the nearest line (0.2 s to line 0's end).
    gap = _post(client, sid, target="segment", seconds=4.2)
    assert gap["quote"] == "" and gap["segment_idx"] == 0 and not gap["detached"]


def test_a_moment_in_silence_is_kept_with_its_time(client, ctx):
    sid = _meeting(
        ctx,
        [
            TranscriptSegment(text="Before the pause", speaker="A", start=0.0, end=2.0),
            TranscriptSegment(text="After the pause", speaker="A", start=22.0, end=24.0),
        ],
    )
    quiet = _post(client, sid, target="segment", seconds=12.0, text="Long silence here")
    assert quiet["segment_idx"] is None and not quiet["detached"]
    assert quiet["at_seconds"] == 12.0 and quiet["quote"] == ""
    near = _post(client, sid, target="segment", seconds=18.5)  # 3.5 s before the next line
    assert near["segment_idx"] == 1 and not near["detached"] and near["at_seconds"] == 18.5
    # The lines around it deleted: still a moment of the audio.
    ctx.sessions.set_transcript(sid, [])
    root = _root(client, sid, quiet["id"])
    assert root["segment_idx"] is None and not root["detached"] and root["at_seconds"] == 12.0


def test_block_comments_survive_rewording_and_detach_when_gone(client, ctx, sidecar):
    sid = ctx.sessions.create_session("Weekly").id
    first = SummaryData(
        title="Weekly",
        action_items=[ActionItem(text="Send the budget report to finance")],
        decisions=["We ship on Friday"],
        open_questions=["Who writes the docs?"],
    )
    data, body, _ = apply_llm(ctx, sid, first, BODY)
    ctx.repo.update_fields(sid, summary=body, summary_data=data)
    item_id, decision_id = data.action_items[0].id, data.decision_ids[0]
    item = _post(client, sid, target="item", target_id=item_id, text="Which report?")
    assert item["quote"] == "Send the budget report to finance" and not item["changed"]
    decision = _post(client, sid, target="decision", target_id=decision_id)
    question = _post(client, sid, target="question", target_id=data.question_ids[0])
    assert question["quote"] == "Who writes the docs?"
    bad = {"text": "x", "target": "item", "target_id": "nope"}
    assert client.post(_url(sid), json=bad).status_code == 400
    bad = {"text": "x", "target": "decision", "target_id": item_id}  # an item's id
    assert client.post(_url(sid), json=bad).status_code == 400

    reworded = SummaryData(
        title="Weekly",
        action_items=[ActionItem(text="Send the budget report to finance today")],
        decisions=["We ship on Friday"],
        open_questions=[],
    )
    data, body, _ = apply_llm(ctx, sid, reworded, BODY)
    ctx.repo.update_fields(sid, summary=body, summary_data=data)
    assert data.action_items[0].id == item_id
    root = _root(client, sid, item["id"])
    assert root["changed"] and not root["detached"]
    root = _root(client, sid, decision["id"])
    assert not root["changed"] and not root["detached"]
    assert _root(client, sid, question["id"])["detached"]  # the question is gone

    data = data.model_copy(update={"action_items": []})
    ctx.repo.update_fields(sid, summary_data=data)
    root = _root(client, sid, item["id"])
    assert root["detached"] and root["quote"] == "Send the budget report to finance"


def test_summary_text_comments_by_quote(client, ctx):
    sid = ctx.sessions.create_session("Weekly").id
    ctx.repo.update_fields(sid, summary=BODY)
    whole = _post(client, sid, target="body", text="Good summary")
    exact = "Docs need an update before then."
    c = _post(client, sid, target="body", exact=exact, prefix="Friday. ", suffix=" Budget")
    assert c["quote"] == exact and not c["changed"] and not c["detached"]
    bad = {"text": "x", "target": "body", "exact": "Not in it"}
    assert client.post(_url(sid), json=bad).status_code == 400

    # Reworded a little: found, changed.
    ctx.repo.update_fields(sid, summary=BODY.replace("need an update", "needs an update"))
    root = _root(client, sid, c["id"])
    assert root["changed"] and not root["detached"]
    # Rewritten away: detached, quote kept.
    ctx.repo.update_fields(sid, summary="We only talked about lunch.")
    root = _root(client, sid, c["id"])
    assert root["detached"] and root["quote"] == exact
    assert not _root(client, sid, whole["id"])["detached"]  # the summary as a whole


RICH = (
    "## Decisions\n\n"
    "- The team agreed to **ship the release** on Friday, see [the plan](https://x.test/plan).\n"
    "- Docs need an `update` before _then_.\n"
)


def test_plain_markdown_is_the_rendered_text():
    assert comments.plain_markdown(RICH) == (
        "Decisions The team agreed to ship the release on Friday, see the plan. "
        "Docs need an update before then."
    )
    assert comments.plain_markdown("1. [ ] snake_case *stays*\n\n---\n> quoted") == (
        "snake_case stays quoted"
    )


def test_a_selection_of_rendered_text_is_found_in_the_markdown(client, ctx):
    sid = ctx.sessions.create_session("Weekly").id
    ctx.repo.update_fields(sid, summary=RICH)
    # As selected in the rendered summary: across the bold words and the link, over two items.
    exact = "ship the release on Friday, see the plan.\nDocs need an update"
    c = _post(client, sid, target="body", exact=exact, prefix="agreed to ", suffix=" before")
    assert c["quote"] == exact and c["anchor"]["exact"] == exact  # stored as sent
    assert not c["changed"] and not c["detached"]
    # Rewritten a little (still Markdown): found by the fuzzy match.
    ctx.repo.update_fields(
        sid,
        summary=RICH.replace("**ship the release**", "*ship this release*").replace(
            "need an", "needs an"
        ),
    )
    root = _root(client, sid, c["id"])
    assert root["changed"] and not root["detached"]
    ctx.repo.update_fields(sid, summary="## Decisions\n\n- Lunch at noon.\n")
    assert _root(client, sid, c["id"])["detached"]


def test_locate_body_picks_the_repeat_its_context_names():
    text = "Ship it. Then test. Ship it. Then release."
    first = comments.locate_body(text, {"exact": "Ship it.", "prefix": "", "suffix": " Then test"})
    second = comments.locate_body(text, {"exact": "Ship it.", "prefix": "test. ", "suffix": ""})
    assert first == (0, 8, False) and second == (20, 28, False)
    # A fragment of a reworded sentence.
    found = comments.locate_body(
        "We agreed that the new release ships on Friday morning.",
        {"exact": "the release ships on Friday"},
    )
    assert found is not None and found[2]
    assert comments.locate_body("Nothing alike here.", {"exact": "the release ships"}) is None


def test_replies_resolve_reopen_delete_and_history(client, ctx):
    ctx.settings.local_speaker_name = "Corey"
    sid = _meeting(ctx)
    root = _post(client, sid, target="segment", idx=0, text="Is Friday firm? " + "x" * 100)
    reply = _post(client, sid, parent_id=root["id"], text="Yes", target="segment", idx=2)
    assert reply["parent_id"] == root["id"] and reply["target"] == "" and reply["anchor"] == {}
    assert not reply["can_resolve"]
    # Replies only to a thread's first comment, in the same meeting.
    assert client.post(_url(sid), json={"text": "x", "parent_id": reply["id"]}).status_code == 400
    other = _meeting(ctx)
    assert client.post(_url(other), json={"text": "x", "parent_id": root["id"]}).status_code == 400
    thread = client.get(_url(sid)).json()["threads"][0]
    assert [r["text"] for r in thread["replies"]] == ["Yes"]

    r = client.patch(_url(sid, f"/{reply['id']}"), json={"text": "Yes, firm"})
    assert r.status_code == 200 and r.json()["text"] == "Yes, firm"
    assert client.patch(_url(sid, f"/{reply['id']}"), json={"text": ""}).status_code == 400
    assert client.patch(_url(other, f"/{reply['id']}"), json={"text": "x"}).status_code == 404

    resolved = client.post(_url(sid, f"/{root['id']}/resolve")).json()
    assert resolved["resolved_at"] and resolved["resolved_by"] == "Corey"
    assert client.post(_url(sid, f"/{reply['id']}/resolve")).status_code == 400
    assert client.post(_url(sid, f"/{root['id']}/resolve")).status_code == 200  # no-op
    reopened = client.post(_url(sid, f"/{root['id']}/reopen")).json()
    assert reopened["resolved_at"] is None and reopened["resolved_by"] == ""

    events = [e for e in ctx.repo.events(sid) if e["kind"].startswith("comment")]
    assert [e["kind"] for e in events] == [
        "commented",
        "commented",
        "comment_resolved",
        "comment_reopened",
    ]
    first = events[0]["detail"]
    assert first["by"] == "Corey" and first["target"] == "segment" and not first["reply"]
    assert len(first["excerpt"]) <= 80 and first["excerpt"].startswith("Is Friday firm?")
    assert events[1]["detail"]["reply"] and events[1]["detail"]["target"] == "segment"

    # Deleting the first comment keeps the thread for its reply; the last reply takes it along.
    assert client.delete(_url(sid, f"/{root['id']}")).status_code == 200
    thread = client.get(_url(sid)).json()["threads"][0]
    assert thread["root"]["deleted"] and thread["root"]["text"] == ""
    assert not thread["root"]["can_edit"] and len(thread["replies"]) == 1
    assert client.delete(_url(sid, f"/{root['id']}")).status_code == 404
    assert client.delete(_url(sid, f"/{reply['id']}")).status_code == 200
    assert client.get(_url(sid)).json()["threads"] == []
    assert ctx.repo.list_comments(sid) == []


def test_every_change_is_announced(client, ctx):
    sid = _meeting(ctx)
    queue = ctx.bus.subscribe()
    c = _post(client, sid, target="segment", idx=0)
    client.patch(_url(sid, f"/{c['id']}"), json={"text": "Changed"})
    client.post(_url(sid, f"/{c['id']}/resolve"))
    client.post(_url(sid, f"/{c['id']}/reopen"))
    client.delete(_url(sid, f"/{c['id']}"))
    got = []
    while not queue.empty():
        event = queue.get_nowait()
        if event["type"] == "comments":
            got.append(event)
    assert got == [{"type": "comments", "session_id": sid, "mentions": []}] * 5
    ctx.bus.unsubscribe(queue)
