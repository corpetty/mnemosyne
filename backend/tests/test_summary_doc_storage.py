"""Summary documents are stored per meeting in summary_docs (storage/sqlite.py), next to the
vault note bookkeeping. The table is internal: no access checks, and it goes with the meeting."""

from datetime import datetime

from mnemosyne.models.session import Session, SummaryData


def _session(ctx) -> Session:
    session = Session(
        name="Summary doc",
        created_at=datetime(2026, 10, 12, 10),
        summary="A summary.",
        summary_data=SummaryData(),
    )
    ctx.repo.save(session)
    return ctx.sessions.get_session(session.id)


def test_summary_doc_round_trip(ctx):
    session = _session(ctx)
    assert ctx.repo.get_summary_doc(session.id) is None

    doc = {"blocks": {"s1": {"text": "Decided to ship.", "history": ["a", "b"]}}, "deleted": ["s0"]}
    ctx.repo.put_summary_doc(session.id, doc)

    assert ctx.repo.get_summary_doc(session.id) == doc


def test_summary_doc_overwrite_replaces_the_document(ctx):
    session = _session(ctx)
    ctx.repo.put_summary_doc(session.id, {"blocks": {"s1": {"text": "first"}}})
    ctx.repo.put_summary_doc(session.id, {"blocks": {"s1": {"text": "second"}}, "deleted": []})

    assert ctx.repo.get_summary_doc(session.id) == {
        "blocks": {"s1": {"text": "second"}},
        "deleted": [],
    }


def test_summary_doc_delete(ctx):
    session = _session(ctx)
    ctx.repo.put_summary_doc(session.id, {"blocks": {}})

    ctx.repo.delete_summary_doc(session.id)

    assert ctx.repo.get_summary_doc(session.id) is None
    # Deleting again is a no-op.
    ctx.repo.delete_summary_doc(session.id)


def test_deleting_the_meeting_removes_the_summary_doc(ctx):
    session = _session(ctx)
    ctx.repo.put_summary_doc(session.id, {"blocks": {"s1": {"text": "gone soon"}}})

    assert ctx.repo.delete(session.id) is True

    assert ctx.repo.get_summary_doc(session.id) is None
    row = ctx.repo._conn.execute(
        "SELECT COUNT(*) AS n FROM summary_docs WHERE session_id=?", (session.id,)
    ).fetchone()
    assert row["n"] == 0
