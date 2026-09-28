"""The search index follows transcript rows by rowid (storage/sqlite.py FTS_TRIGGERS)."""

import time

from mnemosyne.models.session import Session
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.storage.sqlite import SessionRepository


def _lines(n, word="plain"):
    return [
        TranscriptSegment(text=f"line {i} {word}", speaker="A", start=i, end=i + 1)
        for i in range(n)
    ]


def _hits(repo, q):
    return {h.session_id for h in repo.search(q)}


def test_an_old_index_is_rebuilt_and_edits_stay_findable(tmp_path):
    db = tmp_path / "m.db"
    repo = SessionRepository(db)
    s = repo.save(Session(name="Budget", transcript=_lines(5, "apples")))
    # Turn it into a database from before: old triggers, index rows with other rowids.
    c = repo._conn
    with c:
        c.execute("DELETE FROM meta WHERE key='fts_rowids'")
        for t in ("segments_ai", "segments_ad", "segments_au"):
            c.execute(f"DROP TRIGGER IF EXISTS {t}")
        c.execute("DELETE FROM segments_fts")
        c.execute(
            "INSERT INTO segments_fts(rowid, text, session_id, idx)"
            " SELECT rowid + 1000, text, session_id, idx FROM segments"
        )
    repo.close()

    repo = SessionRepository(db)  # rebuilt here
    assert _hits(repo, "apples") == {s.id}

    def edit(segments):
        segments[2] = segments[2].model_copy(update={"text": "line 2 pears"})
        return segments

    repo.edit_segments(s.id, edit)
    assert _hits(repo, "pears") == {s.id}
    rows = repo._conn.execute("SELECT count(*) FROM segments_fts").fetchone()[0]
    assert rows == 5  # one index row per line: the old one went

    repo.edit_segments(s.id, lambda segs: segs[:3])  # fewer lines: a rewrite
    assert repo._conn.execute("SELECT count(*) FROM segments_fts").fetchone()[0] == 3
    assert _hits(repo, "pears") == {s.id}


def test_rewriting_a_long_transcript_stays_fast(tmp_path):
    repo = SessionRepository(tmp_path / "big.db")
    for i in range(100):  # a library of 40,000 lines
        repo.save(Session(name=f"m{i}", transcript=_lines(400)))
    long = repo.save(Session(name="long", transcript=_lines(2000)))
    t = time.monotonic()
    repo.edit_segments(long.id, lambda segs: segs[1:])  # every row rewritten
    assert time.monotonic() - t < 2.0  # was about a minute with the old triggers
    assert repo.get(long.id).transcript[0].text == "line 1 plain"
