"""The summary as a mergeable document (services/summary_doc.py): a new LLM summary keeps the
hand edits made to the last one. The merge engine is the mnemosyne-link sidecar; here the
stand-in of tests/fake_link.py (a difflib three-way merge), plus a few tests with the real one."""

import json
from datetime import date, datetime

import pytest

from mnemosyne.export.note_merge import MergeError, merge_many
from mnemosyne.models.session import ActionItem, Chapter, Session, SummaryData
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services import summary_doc
from mnemosyne.services.link import find_binary
from mnemosyne.services.summary_doc import (
    SummaryDocUnavailable,
    apply_human,
    apply_llm,
    ensure_doc,
    match_ids,
    merge_topics,
    place,
    three_way,
    touched,
    whole_list,
)
from tests import fake_link
from tests.conftest import run_summarize


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    return fake_link.install(tmp_path, monkeypatch)


@pytest.fixture
def real_sidecar(monkeypatch):
    monkeypatch.delenv("MNEMOSYNE_LINK_BIN", raising=False)
    binary = find_binary()
    if binary is None:
        pytest.skip("no mnemosyne-link binary")
    try:
        result = merge_many(binary, [{"app": "a\n"}])
    except MergeError:
        pytest.skip("this mnemosyne-link has no batch `notes merge`")
    if isinstance(result[0], MergeError):
        pytest.skip("this mnemosyne-link has no batch `notes merge`")
    return binary


BUDGET = "Send the budget report to finance"
DECK = "Prepare the slide deck for the board meeting"
VENDOR = "Call the vendor about the delayed shipment"


def _data(items=(), decisions=(), questions=(), topics=(), chapters=(), **kw) -> SummaryData:
    return SummaryData(
        title=kw.pop("title", "Weekly"),
        provider="fake",
        model="m",
        topics=list(topics),
        action_items=[i if isinstance(i, ActionItem) else ActionItem(text=i) for i in items],
        decisions=list(decisions),
        open_questions=list(questions),
        chapters=list(chapters),
        **kw,
    )


def _meeting(ctx) -> str:
    return ctx.sessions.create_session("Weekly").id


def _llm(ctx, sid, data, body="The body.\n", rebase=False):
    out, text, report = apply_llm(ctx, sid, data, body, rebase=rebase)
    ctx.repo.update_fields(sid, summary=text, summary_data=out)
    return out, text, report


def _human(ctx, sid, change, body=None):
    session = ctx.repo.get(sid)
    data = session.summary_data.model_copy(deep=True)
    change(data)
    out, text, report = apply_human(
        ctx, sid, data, session.summary if body is None else body, "Corey"
    )
    ctx.repo.update_fields(sid, summary=text, summary_data=out)
    return out, text, report


def _items(data):
    return [a.text for a in data.action_items]


# ---- pure rules ---------------------------------------------------------------------------


def test_match_ids_exact_first_then_similar_then_later_pools():
    pools = [[("a", "Write the guide"), ("b", "Write the guides")], [("c", "Fix the build")]]
    got = match_ids(["Write the guides", "write the guide!", "Fix the build now", "Other"], pools)
    assert got == [("b", 0), ("a", 0), ("c", 1), (None, None)]


def test_place_after_the_llms_predecessor_in_the_current_order():
    assert place(["b", "a"], ["a", "x", "b"], {"x"}) == ["b", "a", "x"]
    assert place(["a"], ["x", "a"], {"x"}) == ["x", "a"]  # the LLM's first goes first
    assert place(["a"], ["gone", "x"], {"x"}) == ["a", "x"]  # predecessors gone: the end
    assert place([], ["x", "y"], {"x", "y"}) == ["x", "y"]


def test_three_way():
    assert three_way("Ana", "Ana", "Bo") == "Bo"
    assert three_way("Ana", "Me", "Bo") == "Me"


def test_touched():
    base = {"owner": "Ana", "due": None, "at": None, "live": False}
    item = ActionItem(text="T", owner="Ana")
    assert not touched(item, base, "T")
    assert touched(item.model_copy(update={"done": True}), base, "T")
    assert touched(item.model_copy(update={"follow": True}), base, "T")
    assert touched(item.model_copy(update={"issue_url": "u"}), base, "T")
    assert touched(item.model_copy(update={"owner": "Bo"}), base, "T")
    assert touched(item.model_copy(update={"due": date(2026, 1, 1)}), base, "T")
    assert touched(item, base, "Other")
    assert touched(item, None, None)  # added by hand


def test_merge_topics():
    base, current = ["Budget", "Hiring", "Travel"], ["Budget", "travel", "Mine"]
    assert merge_topics(base, current, ["Budget", "Hiring", "Launch"]) == [
        "Budget",
        "Mine",
        "Launch",
    ]


def test_whole_list():
    assert whole_list([{"a": 1}], [{"a": 1}], [{"b": 2}]) == [{"b": 2}]
    assert whole_list([{"a": 1}], [{"a": 3}], [{"b": 2}]) == [{"a": 3}]


# ---- apply_llm ----------------------------------------------------------------------------


def test_a_first_summary_starts_a_doc_in_one_run(ctx, sidecar):
    sid = _meeting(ctx)
    out, body, _ = _llm(ctx, sid, _data([BUDGET, DECK], ["Ship it"], ["When?"]))
    assert all(a.id for a in out.action_items) and out.decision_ids and out.question_ids
    assert len(sidecar.requests) == 1 and isinstance(sidecar.requests[0], list)
    doc = ensure_doc(ctx, sid)
    assert doc.llm.order["action_items"] == [a.id for a in out.action_items]
    assert doc.blocks[out.action_items[0].id].llm == BUDGET


def test_ids_are_inherited_by_similar_items(ctx, sidecar):
    sid = _meeting(ctx)
    first, _, _ = _llm(ctx, sid, _data([BUDGET, DECK], ["We ship on Friday"]))
    again, _, report = _llm(
        ctx, sid, _data([DECK + ".", BUDGET + "!"], ["We ship on Friday."]), body="The body.\n"
    )
    ids = {a.text: a.id for a in first.action_items}
    assert [a.id for a in again.action_items] == [ids[BUDGET], ids[DECK]]  # current order
    assert again.decision_ids == first.decision_ids
    assert report["new"] == 0
    assert len(sidecar.requests) == 2  # one run each


def test_a_block_reworded_on_both_sides_keeps_both(ctx, sidecar):
    sid = _meeting(ctx)
    _llm(ctx, sid, _data([BUDGET]))
    _human(ctx, sid, lambda d: setattr(d.action_items[0], "text", BUDGET + " by Friday"))
    out, _, report = _llm(ctx, sid, _data(["Send the final budget report to finance"]))
    assert _items(out) == ["Send the final budget report to finance by Friday"]
    assert report["both"] == 1
    assert report["both_texts"] == ["Send the final budget report to finance by Friday"]
    assert report["kept"] == 1


def test_human_edits_survive_two_resummaries(ctx, sidecar):
    sid = _meeting(ctx)
    _llm(ctx, sid, _data([BUDGET]), body="Intro.\n\nPlan.\n")
    _human(ctx, sid, lambda d: None, body="Intro.\n\nMy note.\n\nPlan.\n")
    _llm(ctx, sid, _data([BUDGET]), body="Intro.\n\nPlan B.\n")
    _, body, report = _llm(ctx, sid, _data([BUDGET]), body="Intro two.\n\nPlan B.\n")
    assert body == "Intro two.\n\nMy note.\n\nPlan B.\n"
    assert report["kept"] == 1


def test_a_dropped_item_the_human_touched_is_kept(ctx, sidecar):
    sid = _meeting(ctx)
    _llm(ctx, sid, _data([BUDGET, DECK, VENDOR, "Book the room"]))

    def change(d):
        d.action_items[0].text = BUDGET + " today"  # edited
        d.action_items[1].done = True  # only ticked

    _human(ctx, sid, change)
    out, _, report = _llm(ctx, sid, _data([VENDOR]))
    assert _items(out) == [BUDGET + " today", DECK, VENDOR]  # "Book the room" went
    assert out.action_items[1].done
    assert report["dropped_kept"] == 2
    doc = ensure_doc(ctx, sid)
    assert set(doc.blocks) == {a.id for a in out.action_items}


def test_a_deleted_item_is_not_resurrected(ctx, sidecar):
    sid = _meeting(ctx)
    _llm(ctx, sid, _data([BUDGET, DECK]))
    deleted = ctx.repo.get(sid).summary_data.action_items[1].id
    _human(ctx, sid, lambda d: d.action_items.pop(1))
    assert deleted in ensure_doc(ctx, sid).tombstones
    out, _, _ = _llm(ctx, sid, _data([BUDGET, DECK]))
    assert _items(out) == [BUDGET]
    out, _, _ = _llm(ctx, sid, _data([BUDGET, "Prepare the slide deck for the board meetings"]))
    assert _items(out) == [BUDGET]  # reworded: still the deleted one
    out, _, _ = _llm(ctx, sid, _data([BUDGET]))
    out, _, _ = _llm(ctx, sid, _data([BUDGET, DECK]))  # gone for a round, then back
    assert _items(out) == [BUDGET]
    assert deleted in ensure_doc(ctx, sid).tombstones


def test_a_deletion_outside_apply_human_still_counts(ctx, sidecar):
    sid = _meeting(ctx)
    first, _, _ = _llm(ctx, sid, _data([BUDGET, DECK]))
    data = first.model_copy(deep=True)
    data.action_items.pop(0)
    ctx.repo.update_fields(sid, summary_data=data)  # e.g. a path not wired to the doc yet
    out, _, _ = _llm(ctx, sid, _data([BUDGET, DECK]))
    assert _items(out) == [DECK]


def test_a_new_item_goes_after_its_predecessor(ctx, sidecar):
    sid = _meeting(ctx)
    _llm(ctx, sid, _data([BUDGET, DECK]))
    _human(ctx, sid, lambda d: d.action_items.reverse())
    out, _, report = _llm(ctx, sid, _data([BUDGET, VENDOR, DECK]))
    assert _items(out) == [DECK, BUDGET, VENDOR]
    assert report["new"] == 1


def test_a_hand_made_item_the_llm_lists_too_stays_the_humans(ctx, sidecar):
    sid = _meeting(ctx)
    _llm(ctx, sid, _data([BUDGET]))
    mine = ActionItem(text=DECK + " please", owner="Me")
    _human(ctx, sid, lambda d: d.action_items.append(mine))
    out, _, _ = _llm(ctx, sid, _data([BUDGET, ActionItem(text=DECK, owner="Ana", at=12.0)]))
    assert _items(out) == [BUDGET, DECK + " please"]
    assert out.action_items[1].owner == "Me" and out.action_items[1].at == 12.0
    out2, _, _ = _llm(ctx, sid, _data([BUDGET, ActionItem(text=DECK + " now", owner="Ana")]))
    assert len(out2.action_items) == 2 and out2.action_items[1].id == out.action_items[1].id


def test_scalar_rules(ctx, sidecar):
    sid = _meeting(ctx)
    _llm(ctx, sid, _data([ActionItem(text=BUDGET, owner="Ana"), ActionItem(text=DECK)]))

    def change(d):
        d.action_items[0].owner = "Bo"
        d.action_items[1].follow = True
        d.action_items[1].issue_url = "https://example.test/1"
        d.action_items[1].done = True

    _human(ctx, sid, change)
    new = _data(
        [
            ActionItem(text=BUDGET, owner="Cy", due=date(2026, 10, 20)),
            ActionItem(text=DECK, owner="Dee", at=30.0),
        ]
    )
    out, _, _ = _llm(ctx, sid, new)
    budget, deck = out.action_items
    assert budget.owner == "Bo"  # changed by hand: kept
    assert budget.due == date(2026, 10, 20)  # set by the LLM, untouched by hand: taken
    assert deck.owner == "Dee" and deck.at == 30.0
    assert deck.done and deck.follow and deck.issue_url == "https://example.test/1"


def test_decision_times_by_id(ctx, sidecar):
    sid = _meeting(ctx)
    _llm(
        ctx,
        sid,
        _data(decisions=["We ship on Friday", "We hire two people"], decision_at=[1.0, 2.0]),
    )
    _human(ctx, sid, lambda d: d.decision_at.__setitem__(1, 9.0))
    out, _, _ = _llm(
        ctx,
        sid,
        _data(decisions=["We ship on Friday", "We hire two people"], decision_at=[5.0, 6.0]),
    )
    assert out.decision_at == [5.0, 9.0]


def test_topics_rule_with_aliases(ctx, sidecar):
    sid = _meeting(ctx)
    _llm(ctx, sid, _data(topics=["Budget", "Hiring", "Travel"]))
    _human(ctx, sid, lambda d: setattr(d, "topics", ["Budget", "Travel", "Mine"]))
    ctx.settings.topic_aliases = {"launch": "Release"}
    out, _, _ = _llm(ctx, sid, _data(topics=["Budget", "Hiring", "Launch"]))
    assert out.topics == ["Budget", "Mine", "Release"]


def test_chapters_are_taken_whole_unless_edited(ctx, sidecar):
    sid = _meeting(ctx)
    _llm(ctx, sid, _data(chapters=[Chapter(start=0, title="Start")]))
    out, _, _ = _llm(ctx, sid, _data(chapters=[Chapter(start=0, title="Opening")]))
    assert [c.title for c in out.chapters] == ["Opening"]
    _human(ctx, sid, lambda d: setattr(d, "chapters", [Chapter(start=0, title="Mine")]))
    out, _, _ = _llm(ctx, sid, _data(chapters=[Chapter(start=0, title="Other")]))
    assert [c.title for c in out.chapters] == ["Mine"]


def test_metadata_from_the_new_output_followup_and_edit_stamp_kept(ctx, sidecar):
    sid = _meeting(ctx)
    _llm(ctx, sid, _data([BUDGET]))
    out, _, _ = _human(ctx, sid, lambda d: setattr(d.action_items[0], "text", BUDGET + "!"))
    assert out.edited_by == "Corey" and out.edited_at is not None
    data = ctx.repo.get(sid).summary_data.model_copy(update={"followup": "Hi all"})
    ctx.repo.update_fields(sid, summary_data=data)
    out2, _, _ = _llm(ctx, sid, _data([BUDGET], title="New title", source_hash="h2"))
    assert out2.title == "New title" and out2.source_hash == "h2"
    assert out2.followup == "Hi all" and out2.edited_by == "Corey"


def test_revise_rebases_so_echoed_edits_do_not_double(ctx, sidecar):
    sid = _meeting(ctx)
    _llm(ctx, sid, _data([BUDGET], ["We ship on Friday"]), body="Intro.\n")
    _human(
        ctx,
        sid,
        lambda d: setattr(d.action_items[0], "text", BUDGET + " by Friday"),
        body="Intro.\n\nMy note.\n",
    )
    out, body, _ = _llm(
        ctx,
        sid,
        _data([BUDGET + " by Friday"], ["We ship on Friday at noon"]),
        body="Intro, revised.\n\nMy note.\n",
        rebase=True,
    )
    assert _items(out) == [BUDGET + " by Friday"]
    assert out.decisions == ["We ship on Friday at noon"]
    assert body == "Intro, revised.\n\nMy note.\n"
    last = sidecar.requests[-1]
    assert any(r.get("rebase") for r in last)
    doc = ensure_doc(ctx, sid)
    assert doc.body.llm == "Intro, revised.\n\nMy note.\n"


# ---- migration ----------------------------------------------------------------------------


def test_migration_takes_the_baseline_from_versions(ctx, sidecar):
    sid = _meeting(ctx)
    llm_data = _data([BUDGET, DECK, VENDOR])  # an old summary: no ids
    ctx.repo.update_fields(sid, summary="LLM body.\n", summary_data=llm_data)
    before = ctx.repo.get(sid)
    edited = before.summary_data.model_copy(deep=True)
    edited.action_items[0].text = BUDGET + " by Friday"
    edited.action_items.pop(1)  # DECK deleted by hand
    ctx.repo.keep_version(before, "edited")
    ctx.repo.update_fields(sid, summary="LLM body.\n\nMine.\n", summary_data=edited)
    assert not ctx.repo.get(sid).summary_data.action_items[0].id

    doc = ensure_doc(ctx, sid)
    data = ctx.repo.get(sid).summary_data
    budget, vendor = (a.id for a in data.action_items)
    assert budget and vendor  # ids written back
    assert doc.blocks[budget].llm == BUDGET and doc.body.llm == "LLM body.\n"
    assert [t["text"] for t in doc.tombstones.values()] == [DECK]
    assert len(doc.llm.order["action_items"]) == 3

    out, body, report = _llm(ctx, sid, _data([BUDGET, DECK, VENDOR]), body="LLM body.\n")
    assert _items(out) == [BUDGET + " by Friday", VENDOR]
    assert body == "LLM body.\n\nMine.\n"
    assert report["kept"] == 2


def test_migration_without_an_edit_version_uses_the_current_state(ctx, sidecar):
    sid = _meeting(ctx)
    ctx.repo.update_fields(sid, summary="Body.\n", summary_data=_data([BUDGET]))
    doc = ensure_doc(ctx, sid)
    (item_id,) = doc.llm.order["action_items"]
    assert doc.blocks[item_id].llm == BUDGET and not doc.tombstones
    assert len(sidecar.requests) == 1


def test_no_summary_no_doc(ctx, sidecar):
    sid = _meeting(ctx)
    assert ensure_doc(ctx, sid) is None
    assert sidecar.requests == []


# ---- apply_human --------------------------------------------------------------------------


def test_apply_human_ids_tombstones_and_stamp(ctx, sidecar):
    sid = _meeting(ctx)
    first, _, _ = _llm(ctx, sid, _data([BUDGET, DECK]))

    def change(d):
        d.action_items.pop(0)
        d.action_items.append(ActionItem(text=VENDOR))

    out, _, report = _human(ctx, sid, change)
    assert report == {"changed": 0, "added": 1, "deleted": 1}
    assert out.action_items[1].id and out.action_items[1].id not in {
        a.id for a in first.action_items
    }
    doc = ensure_doc(ctx, sid)
    assert first.action_items[0].id in doc.tombstones
    assert doc.blocks[out.action_items[1].id].llm is None
    assert out.edited_by == "Corey"


def test_apply_human_without_changes_does_not_stamp(ctx, sidecar):
    sid = _meeting(ctx)
    _llm(ctx, sid, _data([BUDGET]))
    out, _, report = _human(ctx, sid, lambda d: None)
    assert out.edited_at is None and report == {"changed": 0, "added": 0, "deleted": 0}


# ---- fallback -----------------------------------------------------------------------------


def test_no_sidecar_raises_and_forgets_the_doc(ctx, sidecar, monkeypatch, tmp_path):
    sid = _meeting(ctx)
    _llm(ctx, sid, _data([BUDGET]))
    assert ctx.repo.get_summary_doc(sid) is not None
    monkeypatch.setenv("MNEMOSYNE_LINK_BIN", str(tmp_path / "missing"))
    summary_doc._warned.clear()
    with pytest.raises(SummaryDocUnavailable):
        apply_llm(ctx, sid, _data([DECK]), "x")
    assert ctx.repo.get_summary_doc(sid) is None
    with pytest.raises(SummaryDocUnavailable):
        apply_human(ctx, sid, _data([DECK]), "x", "Corey")


def test_a_failing_sidecar_raises_and_forgets_the_doc(ctx, sidecar, monkeypatch):
    sid = _meeting(ctx)
    _llm(ctx, sid, _data([BUDGET]))
    monkeypatch.setenv("FAKE_MERGE_ITEM_FAIL", "1")
    with pytest.raises(SummaryDocUnavailable, match="item boom"):
        apply_llm(ctx, sid, _data([BUDGET + " now"]), "y")
    assert ctx.repo.get_summary_doc(sid) is None
    monkeypatch.delenv("FAKE_MERGE_ITEM_FAIL")
    monkeypatch.setenv("FAKE_MERGE_FAIL", "1")
    with pytest.raises(SummaryDocUnavailable, match="boom"):
        apply_llm(ctx, sid, _data([BUDGET + " now"]), "y")
    assert ctx.repo.get_summary_doc(sid) is None


def test_merge_many_speaks_the_batch_protocol(sidecar):
    binary = find_binary()
    (history, text), (_, merged) = merge_many(
        binary, [{"app": "a b"}, {"base": "a b c", "app": "a B c", "vault": "a b c d"}]
    )
    assert text == "a b" and merged == "a B c d"
    (_, again), failed = merge_many(
        binary,
        [
            {"history": history, "app": "a b c", "vault": "z a b"},
            {"base": "x", "history": history, "app": "y"},
        ],
    )
    assert again == "z a b c" and isinstance(failed, MergeError)
    assert merge_many(binary, []) == []


# ---- the real sidecar ---------------------------------------------------------------------


def test_real_sidecar_keeps_a_human_edit_over_two_resummaries(ctx, real_sidecar):
    sid = _meeting(ctx)
    _llm(ctx, sid, _data([BUDGET, DECK]), body="Intro.\n\nPlan.\n")
    _human(
        ctx,
        sid,
        lambda d: setattr(d.action_items[0], "text", BUDGET + " by Friday"),
        body="Intro.\n\nMy note.\n\nPlan.\n",
    )
    _llm(ctx, sid, _data([BUDGET, DECK]), body="Intro.\n\nPlan B.\n")
    out, body, _ = _llm(ctx, sid, _data([BUDGET, DECK, VENDOR]), body="Intro two.\n\nPlan B.\n")
    assert _items(out) == [BUDGET + " by Friday", DECK, VENDOR]
    assert body == "Intro two.\n\nMy note.\n\nPlan B.\n"


def test_real_sidecar_both_sides_reworded(ctx, real_sidecar):
    sid = _meeting(ctx)
    _llm(ctx, sid, _data([BUDGET]))
    _human(ctx, sid, lambda d: setattr(d.action_items[0], "text", BUDGET + " by Friday"))
    out, _, report = _llm(ctx, sid, _data(["Send the final budget report to finance"]))
    assert _items(out) == ["Send the final budget report to finance by Friday"]
    assert report["both"] == 1


def test_real_sidecar_revise_does_not_double_echoed_edits(ctx, real_sidecar):
    sid = _meeting(ctx)
    _llm(ctx, sid, _data([BUDGET]), body="Intro.\n")
    _human(
        ctx,
        sid,
        lambda d: setattr(d.action_items[0], "text", BUDGET + " by Friday"),
        body="Intro.\n\nMy note.\n",
    )
    # Merged as a plain re-summarize, both would come out twice ("... by Friday by Friday",
    # "My note." twice): hashseq ops of the echo are not the human's.
    final = "Send the final budget report to finance by Friday"
    out, body, _ = _llm(ctx, sid, _data([final]), body="Intro, revised.\n\nMy note.\n", rebase=True)
    assert _items(out) == [final]
    assert body == "Intro, revised.\n\nMy note.\n"
    # A plain re-summarize afterwards still keeps them once.
    out, body, _ = _llm(ctx, sid, _data([final]), body="Intro, revised.\n\nMy note.\n")
    assert _items(out) == [final]
    assert body == "Intro, revised.\n\nMy note.\n"


# ---- the summarize job --------------------------------------------------------------------


def _reply(items=(), body="Intro.", decisions=()) -> str:
    return json.dumps(
        {
            "summary": body,
            "decisions": list(decisions),
            "action_items": [{"text": t} for t in items],
        }
    )


def _talked(ctx) -> str:
    return ctx.repo.save(
        Session(
            name="Weekly",
            transcript=[TranscriptSegment(text="send the budget", speaker="Ann", start=0, end=2)],
        )
    ).id


def _summarize(client, sid, **body):
    job = run_summarize(client, sid, {"provider": "fake", **body})
    assert job["status"] == "completed", job
    return job["result"]


def _kinds(ctx, sid):
    return [e["kind"] for e in ctx.repo.events(sid)]


def test_summarizing_again_keeps_a_hand_edit(client, ctx, fake_provider, sidecar):
    sid = _talked(ctx)
    fake_provider.summary = _reply([BUDGET, DECK])
    first = _summarize(client, sid)
    assert "merged" not in first and _kinds(ctx, sid).count("summarized") == 1
    _human(ctx, sid, lambda d: setattr(d.action_items[0], "text", BUDGET + " by Friday"))
    assert ctx.repo.get(sid).summary_data.edited_at is not None
    fake_provider.summary = _reply([BUDGET, DECK, VENDOR], body="Intro two.")
    result = _summarize(client, sid)
    assert result["merged"] == {"kept": 1, "both": 0, "dropped_kept": 0}
    data = ctx.repo.get(sid).summary_data
    assert _items(data) == [BUDGET + " by Friday", DECK, VENDOR]
    assert data.edited_at is not None  # the edit is still there
    events = ctx.repo.events(sid)
    merged = [e for e in events if e["kind"] == "summary_merged"]
    assert len(merged) == 1 and [e["kind"] for e in events].count("summarized") == 1
    assert merged[0]["detail"]["kept"] == 1 and merged[0]["detail"]["provider"] == "fake"
    assert merged[0]["detail"]["both"] == 0 and merged[0]["detail"]["dropped_kept"] == 0


def test_summarizing_again_without_edits_replaces_and_logs_summarized(
    client, ctx, fake_provider, sidecar
):
    sid = _talked(ctx)
    fake_provider.summary = _reply([BUDGET])
    _summarize(client, sid)
    fake_provider.summary = _reply([DECK], body="Other.")
    result = _summarize(client, sid)
    assert "merged" not in result
    session = ctx.repo.get(sid)
    assert _items(session.summary_data) == [DECK] and session.summary == "Other."
    assert session.summary_data.edited_at is None
    assert _kinds(ctx, sid).count("summarized") == 2
    assert "summary_merged" not in _kinds(ctx, sid)


def test_edited_at_is_cleared_when_no_edit_remains(client, ctx, fake_provider, sidecar):
    sid = _talked(ctx)
    fake_provider.summary = _reply([BUDGET, DECK])
    _summarize(client, sid)
    # An edit that changes nothing the doc can see (stamp only) is not a remaining edit.
    session = ctx.repo.get(sid)
    data = session.summary_data.model_copy(deep=True)
    data.edited_at, data.edited_by = datetime.now(), "Corey"
    ctx.repo.update_fields(sid, summary_data=data)
    fake_provider.summary = _reply([BUDGET, DECK], body="Again.")
    result = _summarize(client, sid)
    assert "merged" not in result
    after = ctx.repo.get(sid).summary_data
    assert after.edited_at is None and after.edited_by == ""


def test_both_sides_reworded_is_logged_in_the_merge_event(client, ctx, fake_provider, sidecar):
    sid = _talked(ctx)
    fake_provider.summary = _reply([BUDGET])
    _summarize(client, sid)
    _human(ctx, sid, lambda d: setattr(d.action_items[0], "text", BUDGET + " by Friday"))
    fake_provider.summary = _reply(["Send the final budget report to finance"])
    result = _summarize(client, sid)
    assert result["merged"]["both"] == 1 and result["merged"]["kept"] == 1
    event = [e for e in ctx.repo.events(sid) if e["kind"] == "summary_merged"][0]
    assert event["detail"]["both"] == 1
    assert event["detail"]["both_texts"] == [
        "Send the final budget report to finance by Friday",
    ]


def test_revise_rebases_in_the_job(client, ctx, fake_provider, sidecar):
    sid = _talked(ctx)
    fake_provider.summary = _reply([BUDGET], body="Intro.")
    _summarize(client, sid)
    _human(ctx, sid, lambda d: setattr(d.action_items[0], "text", BUDGET + " by Friday"))
    # The LLM saw the edited summary and echoes it.
    fake_provider.summary = _reply([BUDGET + " by Friday"], body="Intro, revised.")
    result = _summarize(client, sid, revise="make it shorter")
    assert result["merged"]["kept"] == 1
    assert _items(ctx.repo.get(sid).summary_data) == [BUDGET + " by Friday"]
    assert any(r.get("rebase") for r in sidecar.requests[-1])
    event = [e for e in ctx.repo.events(sid) if e["kind"] == "summary_merged"][0]
    assert event["detail"]["revised"] == "make it shorter"


def test_real_sidecar_revise_in_the_job_does_not_double(client, ctx, fake_provider, real_sidecar):
    sid = _talked(ctx)
    fake_provider.summary = _reply([BUDGET], body="Intro.")
    _summarize(client, sid)
    _human(ctx, sid, lambda d: setattr(d.action_items[0], "text", BUDGET + " by Friday"))
    fake_provider.summary = _reply([BUDGET + " by Friday"], body="Intro, revised.")
    _summarize(client, sid, revise="tighten")
    assert _items(ctx.repo.get(sid).summary_data) == [BUDGET + " by Friday"]


def test_without_a_sidecar_the_summary_is_replaced_and_ticks_carried(
    client, ctx, fake_provider, tmp_path, monkeypatch
):
    sid = _talked(ctx)
    monkeypatch.setenv("MNEMOSYNE_LINK_BIN", str(tmp_path / "missing"))
    summary_doc._warned.clear()
    fake_provider.summary = _reply([BUDGET, DECK])
    _summarize(client, sid)
    data = ctx.repo.get(sid).summary_data.model_copy(deep=True)
    first_ids = [a.id for a in data.action_items]
    assert all(len(i) == 8 for i in first_ids) and len(set(first_ids)) == 2  # ids without a doc
    data.action_items[0].done = True
    data.action_items[1].text = DECK + " tomorrow"  # a hand edit the old path throws away
    data.edited_at = datetime.now()
    ctx.repo.update_fields(sid, summary_data=data)
    fake_provider.summary = _reply([BUDGET, DECK], body="New body.")
    result = _summarize(client, sid)
    assert "merged" not in result
    session = ctx.repo.get(sid)
    assert _items(session.summary_data) == [BUDGET, DECK] and session.summary == "New body."
    assert session.summary_data.action_items[0].done is True
    assert [a.id for a in session.summary_data.action_items] == first_ids  # carried over
    assert session.summary_data.edited_at is None
    assert "summary_merged" not in _kinds(ctx, sid)
    assert ctx.repo.get_summary_doc(sid) is None
