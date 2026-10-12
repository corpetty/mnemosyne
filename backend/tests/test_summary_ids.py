"""Stable ids for action items, decisions and open questions (services/summary_ids.py)."""

from mnemosyne.models.session import ActionItem, SummaryData
from mnemosyne.services.summary_ids import ensure_ids


def test_ids_filled_for_all_three_kinds():
    data = SummaryData(
        action_items=[ActionItem(text="Send the plan"), ActionItem(text="Book room")],
        decisions=["Ship in October"],
        open_questions=["Who writes the docs?", "Is legal needed?"],
    )
    out = ensure_ids(data)
    assert out is data
    assert all(item.id for item in data.action_items)
    assert len(data.decision_ids) == 1 and data.decision_ids[0]
    assert len(data.question_ids) == 2 and all(data.question_ids)


def test_existing_ids_are_kept():
    data = SummaryData(
        action_items=[ActionItem(text="Send the plan", id="a1b2c3d4")],
        decisions=["Ship in October"],
        decision_ids=["deadbeef"],
        open_questions=["Who writes the docs?"],
        question_ids=["cafef00d"],
    )
    ensure_ids(data)
    assert data.action_items[0].id == "a1b2c3d4"
    assert data.decision_ids == ["deadbeef"]
    assert data.question_ids == ["cafef00d"]


def test_duplicate_ids_are_replaced():
    data = SummaryData(
        action_items=[
            ActionItem(text="One", id="same0000"),
            ActionItem(text="Two", id="same0000"),
        ],
        decisions=["A", "B"],
        decision_ids=["dup11111", "dup11111"],
    )
    ensure_ids(data)
    first, second = data.action_items
    assert first.id == "same0000"
    assert second.id and second.id != "same0000"
    assert data.decision_ids[0] == "dup11111"
    assert data.decision_ids[1] not in {"dup11111", "same0000", ""}


def test_parallel_lists_are_padded_and_cut():
    padded = SummaryData(decisions=["A", "B", "C"], decision_ids=["keep0001"])
    ensure_ids(padded)
    assert len(padded.decision_ids) == 3
    assert padded.decision_ids[0] == "keep0001"
    assert all(padded.decision_ids)

    cut = SummaryData(open_questions=["Q"], question_ids=["x1", "x2", "x3"])
    ensure_ids(cut)
    assert cut.question_ids == ["x1"]


def test_ids_unique_across_kinds():
    data = SummaryData(
        action_items=[ActionItem(text=f"item {i}", id="shared00") for i in range(3)],
        decisions=["d0", "d1", "d2"],
        decision_ids=["shared00"] * 3,
        open_questions=["q0", "q1", "q2"],
        question_ids=["shared00"] * 3,
    )
    ensure_ids(data)
    ids = [a.id for a in data.action_items] + data.decision_ids + data.question_ids
    assert len(ids) == 9
    assert len(set(ids)) == 9
    assert all(ids)
