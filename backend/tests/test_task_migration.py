"""One-time migration of old libraries to linked tasks and decisions (task_migration.py)."""

from datetime import datetime

from mnemosyne.models.session import ActionItem, Session, SummaryData
from mnemosyne.services import task_links, task_migration

TEXT = "Send the budget to finance"


def _old_meeting(ctx, name, day, items, decisions=()):
    """Saved the way old data is: ids on the items but no task_id / decision_links."""
    return ctx.repo.save(
        Session(
            name=name,
            created_at=datetime(2026, 10, day, 10),
            summary="s",
            summary_data=SummaryData(
                action_items=[ActionItem(**i) for i in items],
                decisions=[t for _, t in decisions],
                decision_ids=[i for i, _ in decisions],
            ),
        )
    )


def _items(ctx, sid):
    return {a.id: a for a in ctx.repo.get(sid).summary_data.action_items}


def _library(ctx):
    a = _old_meeting(ctx, "One", 1, [dict(id="a1", text=TEXT)], [("d1", "We ship in October")])
    b = _old_meeting(
        ctx, "Two", 8, [dict(id="b1", text=TEXT.lower() + "!")], [("e1", "we ship in october")]
    )
    c = _old_meeting(ctx, "Three", 15, [dict(id="c1", text=TEXT, done=True)])
    return a, b, c


def test_migrates_exact_duplicates_oldest_first(ctx):
    a, b, c = _library(ctx)
    assert ctx.repo.get_meta(task_migration.FLAG) is None
    result = task_migration.migrate(ctx)
    # Items link only back to OLDER open tasks: Two links to One. Three's item is done, but the
    # rule only excludes done CANDIDATES, and the object One+Two is open, so Three joins it; the
    # head (Three, newest) makes the whole task done.
    assert result == {"tasks_linked": 2, "decisions_linked": 1, "reconciled": result["reconciled"]}
    ids = {_items(ctx, s.id)[i].task_id for s, i in ((a, "a1"), (b, "b1"), (c, "c1"))}
    assert len(ids) == 1 and "" not in ids
    assert all(a_.done for s in (a, b, c) for a_ in _items(ctx, s.id).values())
    d_a = ctx.repo.get(a.id).summary_data.decision_links
    d_b = ctx.repo.get(b.id).summary_data.decision_links
    assert d_a["d1"] == d_b["e1"]
    kinds = [e["kind"] for e in ctx.repo.events(b.id)]
    assert "task_linked" in kinds and "decision_linked" in kinds
    assert "task_linked" in [e["kind"] for e in ctx.repo.events(c.id)]
    assert ctx.repo.get_meta(task_migration.FLAG) == "1"


def test_a_done_older_task_stays_apart_from_newer_open_twins(ctx):
    """An item links only to open tasks of OLDER meetings. One is done: Two (open) finds no open
    older twin and stays a new task; Three joins Two. One is never joined or reopened."""
    a = _old_meeting(ctx, "One", 1, [dict(id="a1", text=TEXT, done=True)])
    b = _old_meeting(ctx, "Two", 8, [dict(id="b1", text=TEXT)])
    c = _old_meeting(ctx, "Three", 15, [dict(id="c1", text=TEXT)])
    result = task_migration.migrate(ctx)
    assert result["tasks_linked"] == 1
    assert _items(ctx, a.id)["a1"].task_id == "" and _items(ctx, a.id)["a1"].done
    t = _items(ctx, b.id)["b1"].task_id
    assert t and t == _items(ctx, c.id)["c1"].task_id
    assert not _items(ctx, b.id)["b1"].done and not _items(ctx, c.id)["c1"].done


def test_done_twins_are_not_linked(ctx):
    """Only open tasks are candidates, so two done mentions stay separate tasks. (A done item
    next to an OPEN one is linked, from whichever meeting is processed: see above.)"""
    a = _old_meeting(ctx, "One", 1, [dict(id="a1", text=TEXT, done=True)])
    b = _old_meeting(ctx, "Two", 8, [dict(id="b1", text=TEXT, done=True)])
    assert task_migration.migrate(ctx)["tasks_linked"] == 0
    assert _items(ctx, a.id)["a1"].task_id == "" and _items(ctx, b.id)["b1"].task_id == ""


def test_second_run_does_nothing(ctx, monkeypatch):
    _library(ctx)
    task_migration.migrate(ctx)

    def boom(*a, **k):
        raise AssertionError("must not run again")

    monkeypatch.setattr(task_links, "link_items", boom)
    monkeypatch.setattr(task_links, "reconcile", boom)
    assert task_migration.migrate(ctx) == {
        "tasks_linked": 0,
        "decisions_linked": 0,
        "reconciled": 0,
        "skipped": True,
    }


def test_failure_leaves_the_flag_unset_and_retries(ctx, monkeypatch):
    a, b, c = _library(ctx)
    real = task_links.reconcile

    def boom(app):
        raise RuntimeError("disk")

    monkeypatch.setattr(task_links, "reconcile", boom)
    result = task_migration.migrate(ctx)
    assert result["failed"] is True
    assert ctx.repo.get_meta(task_migration.FLAG) is None
    monkeypatch.setattr(task_links, "reconcile", real)
    again = task_migration.migrate(ctx)
    assert "failed" not in again and ctx.repo.get_meta(task_migration.FLAG) == "1"
