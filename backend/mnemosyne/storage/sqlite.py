"""SQLite-backed session repository.

One file, WAL mode, stdlib sqlite3. Transcript segments live in their own
table so listing sessions never deserializes transcripts.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path

from ..models.ask import Ask, Citation, Passage, PassageLine
from ..models.digest import Digest
from ..models.search import SearchHit, SegmentHit
from ..models.session import Recording, Session, SessionStatus, SessionSummary, SummaryData
from ..models.speaker import SpeakerProfile
from ..models.transcript import TranscriptSegment

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    audio_file TEXT,
    summary TEXT NOT NULL DEFAULT '',
    summary_data TEXT,
    notes TEXT NOT NULL DEFAULT '',
    participants TEXT NOT NULL DEFAULT '[]',
    attendees TEXT NOT NULL DEFAULT '[]',
    local_only INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created_at DESC);
CREATE TABLE IF NOT EXISTS segments (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    text TEXT NOT NULL,
    speaker TEXT NOT NULL,
    start REAL NOT NULL,
    "end" REAL NOT NULL,
    words TEXT,
    PRIMARY KEY (session_id, idx)
);
CREATE TABLE IF NOT EXISTS recordings (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    device_id INTEGER NOT NULL,
    device_name TEXT NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recordings_session ON recordings(session_id);
CREATE TABLE IF NOT EXISTS speakers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    embedding TEXT NOT NULL,
    sample_count INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
    text, session_id UNINDEXED, idx UNINDEXED,
    tokenize='unicode61 remove_diacritics 2'
);
CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
    name, summary, notes, session_id UNINDEXED,
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS segments_ai AFTER INSERT ON segments BEGIN
    INSERT INTO segments_fts(text, session_id, idx) VALUES (new.text, new.session_id, new.idx);
END;
CREATE TRIGGER IF NOT EXISTS segments_ad AFTER DELETE ON segments BEGIN
    DELETE FROM segments_fts WHERE session_id = old.session_id AND idx = old.idx;
END;
CREATE TRIGGER IF NOT EXISTS sessions_ai AFTER INSERT ON sessions BEGIN
    INSERT INTO sessions_fts(name, summary, notes, session_id)
    VALUES (new.name, new.summary, new.notes, new.id);
END;
CREATE TRIGGER IF NOT EXISTS sessions_au AFTER UPDATE OF name, summary, notes ON sessions BEGIN
    DELETE FROM sessions_fts WHERE session_id = old.id;
    INSERT INTO sessions_fts(name, summary, notes, session_id)
    VALUES (new.name, new.summary, new.notes, new.id);
END;
CREATE TRIGGER IF NOT EXISTS sessions_ad AFTER DELETE ON sessions BEGIN
    DELETE FROM sessions_fts WHERE session_id = old.id;
END;
CREATE TABLE IF NOT EXISTS asks (
    id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    citations TEXT NOT NULL DEFAULT '[]',
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS digests (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    markdown TEXT NOT NULL,
    session_ids TEXT NOT NULL DEFAULT '[]',
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    path TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunk_vectors (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,           -- lines | summary
    first_idx INTEGER NOT NULL,   -- -1 for a summary
    last_idx INTEGER NOT NULL,
    text_hash TEXT NOT NULL,
    vec BLOB NOT NULL,            -- float16, unit length
    PRIMARY KEY (session_id, kind, first_idx)
);
CREATE TABLE IF NOT EXISTS session_speakers (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    embedding TEXT NOT NULL,
    PRIMARY KEY (session_id, label)
);
"""


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


_TERM = re.compile(r"[\w']+", re.UNICODE)


# Words that carry no retrieval signal in questions about meetings.
STOPWORDS = frozenset(
    """a about above after again against all am an and any are as at be because been before
    being below between both but by can could did do does doing down during each few for from
    further had has have having he her here hers him his how i if in into is it its itself
    just me more most my no nor not now of off on once only or other our ours out over own
    same she should so some such than that the their theirs them then there these they this
    those through to too under until up very was we were what when where which while who whom
    why will with would you your yours yourself anything something everything tell said say
    says talk talked talking discuss discussed meeting meetings call calls did we us last
    week weeks month months day days ago recently""".split()
)


def fts_any_query(question: str) -> str:
    """OR-query of the question's meaningful words (prefix-matched when long enough),
    for ranking passages by relevance rather than requiring every word."""
    terms = []
    for t in _TERM.findall(question.lower()):
        t = t.strip("'")
        if len(t) < 3 or t in STOPWORDS or t in terms:
            continue
        terms.append(t)
    return " OR ".join(f'"{t}"*' if len(t) >= 4 else f'"{t}"' for t in terms[:24])


def fts_query(user_query: str) -> str:
    """Turn free text into a safe FTS5 query: every term quoted and required,
    the last one as a prefix so results appear while typing."""
    terms = _TERM.findall(user_query)
    if not terms:
        return ""
    quoted = [f'"{t}"' for t in terms]
    quoted[-1] = quoted[-1] + "*"
    return " ".join(quoted)


class SessionRepository:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._migrate_columns()
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()
        self._backfill_fts()

    def _migrate_columns(self) -> None:
        """Additive column migrations for databases created by older schemas."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(sessions)")}
        if "summary_data" not in cols:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN summary_data TEXT")
        if "attendees" not in cols:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN attendees TEXT NOT NULL DEFAULT '[]'"
            )
        if "local_only" not in cols:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN local_only INTEGER NOT NULL DEFAULT 0"
            )
        self._conn.commit()

    def _backfill_fts(self) -> None:
        """Index rows that predate the FTS tables (databases from schema < 3)."""
        with self._lock, self._conn:
            if self._conn.execute("SELECT count(*) FROM segments_fts").fetchone()[0] == 0:
                self._conn.execute(
                    "INSERT INTO segments_fts(text, session_id, idx)"
                    " SELECT text, session_id, idx FROM segments"
                )
            if self._conn.execute("SELECT count(*) FROM sessions_fts").fetchone()[0] == 0:
                self._conn.execute(
                    "INSERT INTO sessions_fts(name, summary, notes, session_id)"
                    " SELECT name, summary, notes, id FROM sessions"
                )
            self._conn.execute(
                "UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),)
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- reads ---------------------------------------------------------

    def exists(self, session_id: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone()
        return row is not None

    def list_summaries(self) -> list[SessionSummary]:
        sql = """
        SELECT s.id, s.name, s.status, s.created_at, s.updated_at, s.participants, s.local_only,
               length(s.summary) > 0 AS has_summary,
               s.audio_file IS NOT NULL AS has_audio,
               EXISTS(SELECT 1 FROM segments g WHERE g.session_id = s.id) AS has_transcript
        FROM sessions s ORDER BY s.created_at DESC
        """
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [
            SessionSummary(
                id=r["id"],
                name=r["name"],
                status=SessionStatus(r["status"]),
                created_at=_dt(r["created_at"]),
                updated_at=_dt(r["updated_at"]),
                has_transcript=bool(r["has_transcript"]),
                has_summary=bool(r["has_summary"]),
                has_audio=bool(r["has_audio"]),
                participant_count=len(json.loads(r["participants"])),
                local_only=bool(r["local_only"]),
            )
            for r in rows
        ]

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                return None
            seg_rows = self._conn.execute(
                "SELECT * FROM segments WHERE session_id=? ORDER BY idx", (session_id,)
            ).fetchall()
            rec_rows = self._conn.execute(
                "SELECT * FROM recordings WHERE session_id=? ORDER BY created_at", (session_id,)
            ).fetchall()
        return Session(
            id=row["id"],
            name=row["name"],
            status=SessionStatus(row["status"]),
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
            audio_file=row["audio_file"],
            summary=row["summary"],
            summary_data=(
                SummaryData.model_validate_json(row["summary_data"])
                if row["summary_data"]
                else None
            ),
            notes=row["notes"],
            participants=json.loads(row["participants"]),
            attendees=json.loads(row["attendees"]),
            local_only=bool(row["local_only"]),
            transcript=[
                TranscriptSegment(
                    text=r["text"],
                    speaker=r["speaker"],
                    start=r["start"],
                    end=r["end"],
                    words=json.loads(r["words"]) if r["words"] else None,
                )
                for r in seg_rows
            ],
            recordings=[
                Recording(
                    id=r["id"],
                    source=r["source"],
                    device_id=r["device_id"],
                    device_name=r["device_name"],
                    path=r["path"],
                    created_at=_dt(r["created_at"]),
                )
                for r in rec_rows
            ],
        )

    # ---- writes --------------------------------------------------------

    def save(self, session: Session) -> Session:
        """Insert or fully replace a session including segments and recordings."""
        session.updated_at = datetime.now()
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO sessions(id, name, status, created_at, updated_at, audio_file,
                                        summary, summary_data, notes, participants,
                                        attendees, local_only)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     name=excluded.name, status=excluded.status, updated_at=excluded.updated_at,
                     audio_file=excluded.audio_file, summary=excluded.summary,
                     summary_data=excluded.summary_data,
                     notes=excluded.notes, participants=excluded.participants,
                     attendees=excluded.attendees, local_only=excluded.local_only""",
                (
                    session.id,
                    session.name,
                    session.status.value,
                    session.created_at.isoformat(),
                    session.updated_at.isoformat(),
                    session.audio_file,
                    session.summary,
                    session.summary_data.model_dump_json() if session.summary_data else None,
                    session.notes,
                    json.dumps(session.participants),
                    json.dumps(session.attendees),
                    int(session.local_only),
                ),
            )
            self._write_segments(session.id, session.transcript)
            self._write_recordings(session.id, session.recordings)
        return session

    def update_fields(self, session_id: str, **fields) -> Session | None:
        """Cheap metadata update. Valid keys: name, status, audio_file, summary, notes,
        participants."""
        allowed = {
            "name",
            "status",
            "audio_file",
            "summary",
            "summary_data",
            "notes",
            "participants",
            "attendees",
            "local_only",
        }
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"Cannot update fields: {sorted(bad)}")
        if not fields:
            return self.get(session_id)
        values = []
        sets = []
        for key, value in fields.items():
            if key == "status" and isinstance(value, SessionStatus):
                value = value.value
            if key in ("participants", "attendees"):
                value = json.dumps(value)
            if key == "local_only":
                value = int(bool(value))
            if key == "summary_data":
                value = value.model_dump_json() if isinstance(value, SummaryData) else value
            sets.append(f"{key}=?")
            values.append(value)
        sets.append("updated_at=?")
        values.append(datetime.now().isoformat())
        values.append(session_id)
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"UPDATE sessions SET {', '.join(sets)} WHERE id=?", tuple(values)
            )
            if cur.rowcount == 0:
                return None
        return self.get(session_id)

    def replace_segments(self, session_id: str, segments: list[TranscriptSegment]) -> None:
        with self._lock, self._conn:
            self._write_segments(session_id, segments)
            self._conn.execute(
                "UPDATE sessions SET updated_at=? WHERE id=?",
                (datetime.now().isoformat(), session_id),
            )

    def add_recordings(self, session_id: str, recordings: list[Recording]) -> None:
        with self._lock, self._conn:
            self._conn.executemany(
                """INSERT OR REPLACE INTO recordings
                   (id, session_id, source, device_id, device_name, path, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                [
                    (
                        r.id,
                        session_id,
                        r.source,
                        r.device_id,
                        r.device_name,
                        r.path,
                        r.created_at.isoformat(),
                    )
                    for r in recordings
                ],
            )

    def clear_audio(self, session_id: str) -> bool:
        """Forget a session's audio (recordings rows + mixed file path). Files are the
        caller's job. Returns False for an unknown session."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE sessions SET audio_file=NULL, updated_at=? WHERE id=?",
                (datetime.now().isoformat(), session_id),
            )
            if cur.rowcount == 0:
                return False
            self._conn.execute("DELETE FROM recordings WHERE session_id=?", (session_id,))
        return True

    def delete(self, session_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        return cur.rowcount > 0

    # ---- editing -------------------------------------------------------

    def edit_segments(
        self, session_id: str, edit: Callable[[list[TranscriptSegment]], list[TranscriptSegment]]
    ) -> Session | None:
        """Apply `edit` to the transcript, then recompute participants."""
        session = self.get(session_id)
        if session is None:
            return None
        segments = edit(list(session.transcript))
        speakers = list(dict.fromkeys(s.speaker for s in segments if s.speaker != "UNKNOWN"))
        # Keep participants that still appear, in their existing order, then new ones.
        participants = [p for p in session.participants if p in speakers]
        participants += [s for s in speakers if s not in participants]
        with self._lock, self._conn:
            self._write_segments(session_id, segments)
            self._conn.execute(
                "UPDATE sessions SET participants=?, updated_at=? WHERE id=?",
                (json.dumps(participants), datetime.now().isoformat(), session_id),
            )
        return self.get(session_id)

    # ---- search --------------------------------------------------------

    def search(self, query: str, limit: int = 50, per_session: int = 5) -> list[SearchHit]:
        """Full-text search over transcript segments and session name/summary/notes."""
        fts = fts_query(query)
        if not fts:
            return []
        with self._lock:
            seg_rows = self._conn.execute(
                """SELECT session_id, idx, snippet(segments_fts, 0, '[[', ']]', '…', 12) AS snip,
                          bm25(segments_fts) AS score
                   FROM segments_fts WHERE segments_fts MATCH ?
                   ORDER BY score LIMIT ?""",
                (fts, limit * 4),
            ).fetchall()
            sess_rows = self._conn.execute(
                """SELECT session_id, snippet(sessions_fts, -1, '[[', ']]', '…', 12) AS snip,
                          bm25(sessions_fts) AS score
                   FROM sessions_fts WHERE sessions_fts MATCH ?
                   ORDER BY score LIMIT ?""",
                (fts, limit),
            ).fetchall()
            # speaker/start for segment hits
            details = {}
            for r in seg_rows:
                d = self._conn.execute(
                    "SELECT speaker, start FROM segments WHERE session_id=? AND idx=?",
                    (r["session_id"], r["idx"]),
                ).fetchone()
                if d:
                    details[(r["session_id"], r["idx"])] = d

        summaries = {x.id: x for x in self.list_summaries()}
        hits: dict[str, SearchHit] = {}
        order: list[str] = []

        def hit(session_id: str) -> SearchHit | None:
            if session_id not in summaries:
                return None
            if session_id not in hits:
                sm = summaries[session_id]
                hits[session_id] = SearchHit(
                    session_id=session_id,
                    session_name=sm.name,
                    created_at=sm.created_at,
                    score=0.0,
                )
                order.append(session_id)
            return hits[session_id]

        for r in sess_rows:
            h = hit(r["session_id"])
            if h is not None:
                h.session_snippet = r["snip"]
                h.score += -float(r["score"])
        for r in seg_rows:
            h = hit(r["session_id"])
            if h is None or len(h.segments) >= per_session:
                continue
            d = details.get((r["session_id"], r["idx"]))
            h.segments.append(
                SegmentHit(
                    idx=r["idx"],
                    speaker=d["speaker"] if d else "",
                    start=float(d["start"]) if d else 0.0,
                    snippet=r["snip"],
                )
            )
            h.score += -float(r["score"])
        results = [hits[i] for i in order]
        results.sort(key=lambda h: h.score, reverse=True)
        return results[:limit]

    # ---- retrieval for ask ---------------------------------------------

    def retrieve(
        self, question: str, limit: int = 20, context: int = 2, per_session: int = 6
    ) -> list[Passage]:
        """Best-matching transcript windows (each hit plus `context` lines either side,
        overlapping windows merged) and session summaries, most relevant first."""
        fts = fts_any_query(question)
        if not fts:
            return []
        with self._lock:
            seg_hits = self._conn.execute(
                """SELECT session_id, idx, bm25(segments_fts) AS score FROM segments_fts
                   WHERE segments_fts MATCH ? ORDER BY score LIMIT ?""",
                (fts, limit * 4),
            ).fetchall()
            sess_hits = self._conn.execute(
                """SELECT session_id, bm25(sessions_fts) AS score FROM sessions_fts
                   WHERE sessions_fts MATCH ? ORDER BY score LIMIT ?""",
                (fts, max(3, limit // 4)),
            ).fetchall()
            meta = {
                r["id"]: r
                for r in self._conn.execute(
                    "SELECT id, name, created_at, summary FROM sessions"
                ).fetchall()
            }

            # Merge hit windows per session, keeping each window's best score.
            windows: dict[str, list[list]] = {}
            counts: dict[str, int] = {}
            for h in seg_hits:
                sid, idx, score = h["session_id"], h["idx"], -float(h["score"])
                if sid not in meta:
                    continue
                lo, hi = idx - context, idx + context
                spans = windows.setdefault(sid, [])
                for span in spans:
                    if lo <= span[1] + 1 and hi >= span[0] - 1:
                        span[0], span[1] = min(span[0], lo), max(span[1], hi)
                        if score > span[2]:
                            span[2], span[3] = score, idx
                        break
                else:
                    if counts.get(sid, 0) >= per_session:
                        continue
                    counts[sid] = counts.get(sid, 0) + 1
                    spans.append([lo, hi, score, idx])

            passages: list[Passage] = []
            for sid, spans in windows.items():
                m = meta[sid]
                for lo, hi, score, focus in spans:
                    rows = self._conn.execute(
                        """SELECT idx, speaker, start, text FROM segments
                           WHERE session_id=? AND idx BETWEEN ? AND ? ORDER BY idx""",
                        (sid, max(lo, 0), hi),
                    ).fetchall()
                    passages.append(
                        Passage(
                            session_id=sid,
                            session_name=m["name"],
                            created_at=_dt(m["created_at"]),
                            lines=[
                                PassageLine(
                                    idx=r["idx"],
                                    speaker=r["speaker"],
                                    start=r["start"],
                                    text=r["text"],
                                )
                                for r in rows
                            ],
                            score=score,
                            focus_idx=focus,
                        )
                    )
            for h in sess_hits:
                m = meta.get(h["session_id"])
                if m is not None and m["summary"].strip():
                    passages.append(
                        Passage(
                            session_id=m["id"],
                            session_name=m["name"],
                            created_at=_dt(m["created_at"]),
                            kind="summary",
                            text=m["summary"].strip(),
                            score=-float(h["score"]),
                        )
                    )
        passages.sort(key=lambda p: p.score, reverse=True)
        return passages[:limit]

    # ---- asks ------------------------------------------------------------

    def save_ask(self, ask: Ask) -> Ask:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO asks(id, question, answer, citations, provider, model,
                                               created_at) VALUES (?,?,?,?,?,?,?)""",
                (
                    ask.id,
                    ask.question,
                    ask.answer,
                    json.dumps([c.model_dump(mode="json") for c in ask.citations]),
                    ask.provider,
                    ask.model,
                    ask.created_at.isoformat(),
                ),
            )
        return ask

    def list_asks(self, limit: int = 50) -> list[Ask]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM asks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            Ask(
                id=r["id"],
                question=r["question"],
                answer=r["answer"],
                citations=[Citation.model_validate(c) for c in json.loads(r["citations"])],
                provider=r["provider"],
                model=r["model"],
                created_at=_dt(r["created_at"]),
            )
            for r in rows
        ]

    def delete_ask(self, ask_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM asks WHERE id=?", (ask_id,))
        return cur.rowcount > 0

    # ---- action items ----------------------------------------------------

    def list_action_items(self):
        """Every action item of every summarized session, newest meeting first."""
        from ..services.tasks import TaskItem

        with self._lock:
            rows = self._conn.execute(
                "SELECT id, name, created_at, summary_data FROM sessions"
                " WHERE summary_data IS NOT NULL ORDER BY created_at DESC"
            ).fetchall()
        out = []
        for r in rows:
            data = SummaryData.model_validate_json(r["summary_data"])
            for i, a in enumerate(data.action_items):
                out.append(
                    TaskItem(
                        session_id=r["id"],
                        session_name=r["name"],
                        created_at=_dt(r["created_at"]),
                        idx=i,
                        text=a.text,
                        owner=a.owner,
                        done=a.done,
                        issue_url=a.issue_url,
                    )
                )
        return out

    def meeting_meta(self):
        """Name, date, attendees and summary of every session, without transcripts."""
        from ..services.brief import MeetingMeta

        with self._lock:
            rows = self._conn.execute(
                "SELECT id, name, created_at, attendees, summary, summary_data FROM sessions"
            ).fetchall()
        return [
            MeetingMeta(
                id=r["id"],
                name=r["name"],
                created_at=_dt(r["created_at"]),
                attendees=json.loads(r["attendees"] or "[]"),
                summary=r["summary"] or "",
                summary_data=(
                    SummaryData.model_validate_json(r["summary_data"])
                    if r["summary_data"]
                    else None
                ),
            )
            for r in rows
        ]

    # ---- semantic index ------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            r = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def vector_hashes(self, session_id: str) -> dict[tuple[str, int], str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, first_idx, text_hash FROM chunk_vectors WHERE session_id=?",
                (session_id,),
            ).fetchall()
        return {(r["kind"], r["first_idx"]): r["text_hash"] for r in rows}

    def replace_vectors(self, session_id: str, rows: list[tuple]) -> None:
        """rows: (kind, first_idx, last_idx, text_hash, vec_bytes)."""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM chunk_vectors WHERE session_id=?", (session_id,))
            self._conn.executemany(
                "INSERT INTO chunk_vectors(session_id, kind, first_idx, last_idx, text_hash, vec)"
                " VALUES (?,?,?,?,?,?)",
                [(session_id, *r) for r in rows],
            )

    def clear_vectors(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM chunk_vectors")

    def all_vectors(self) -> list:
        with self._lock:
            return self._conn.execute(
                "SELECT session_id, kind, first_idx, last_idx, vec FROM chunk_vectors"
            ).fetchall()

    def indexed_session_count(self) -> int:
        with self._lock:
            r = self._conn.execute(
                "SELECT COUNT(DISTINCT session_id) AS n FROM chunk_vectors"
            ).fetchone()
        return int(r["n"])

    def session_ids(self) -> list[str]:
        with self._lock:
            return [r["id"] for r in self._conn.execute("SELECT id FROM sessions").fetchall()]

    def passage_window(self, session_id: str, lo: int, hi: int, focus: int, score: float):
        """A transcript Passage for lines lo..hi of a session, or None."""
        with self._lock:
            m = self._conn.execute(
                "SELECT id, name, created_at FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            if m is None:
                return None
            rows = self._conn.execute(
                """SELECT idx, speaker, start, text FROM segments
                   WHERE session_id=? AND idx BETWEEN ? AND ? ORDER BY idx""",
                (session_id, max(lo, 0), hi),
            ).fetchall()
        if not rows:
            return None
        return Passage(
            session_id=session_id,
            session_name=m["name"],
            created_at=_dt(m["created_at"]),
            lines=[
                PassageLine(idx=r["idx"], speaker=r["speaker"], start=r["start"], text=r["text"])
                for r in rows
            ],
            score=score,
            focus_idx=focus,
        )

    def summary_passage(self, session_id: str, score: float):
        with self._lock:
            m = self._conn.execute(
                "SELECT id, name, created_at, summary FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
        if m is None or not (m["summary"] or "").strip():
            return None
        return Passage(
            session_id=session_id,
            session_name=m["name"],
            created_at=_dt(m["created_at"]),
            kind="summary",
            text=m["summary"].strip(),
            score=score,
        )

    def local_only_ids(self) -> set[str]:
        with self._lock:
            rows = self._conn.execute("SELECT id FROM sessions WHERE local_only=1").fetchall()
        return {r["id"] for r in rows}

    def people_rows(self) -> list:
        """id, name, created_at, participants, attendees, summary_data of every session."""
        with self._lock:
            return self._conn.execute(
                "SELECT id, name, created_at, participants, attendees, summary_data FROM sessions"
                " ORDER BY created_at DESC"
            ).fetchall()

    # ---- digests ---------------------------------------------------------

    def sessions_between(self, start: date, end: date) -> list[Session]:
        """Full sessions created on start..end (inclusive), oldest first."""
        lo = datetime.combine(start, time.min).isoformat()
        hi = datetime.combine(end + timedelta(days=1), time.min).isoformat()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM sessions WHERE created_at >= ? AND created_at < ?"
                " ORDER BY created_at",
                (lo, hi),
            ).fetchall()
        return [s for s in (self.get(r["id"]) for r in rows) if s is not None]

    def save_digest(self, digest: Digest) -> Digest:
        """Save, replacing any earlier digest of the same range (label)."""
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM digests WHERE label=? AND id<>?", (digest.label, digest.id)
            )
            self._conn.execute(
                """INSERT OR REPLACE INTO digests(id, label, start, end, markdown, session_ids,
                                                  provider, model, path, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    digest.id,
                    digest.label,
                    digest.start.isoformat(),
                    digest.end.isoformat(),
                    digest.markdown,
                    json.dumps(digest.session_ids),
                    digest.provider,
                    digest.model,
                    digest.path,
                    digest.created_at.isoformat(),
                ),
            )
        return digest

    @staticmethod
    def _digest(r) -> Digest:
        return Digest(
            id=r["id"],
            label=r["label"],
            start=date.fromisoformat(r["start"]),
            end=date.fromisoformat(r["end"]),
            markdown=r["markdown"],
            session_ids=json.loads(r["session_ids"]),
            provider=r["provider"],
            model=r["model"],
            path=r["path"],
            created_at=_dt(r["created_at"]),
        )

    def list_digests(self, limit: int = 50) -> list[Digest]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM digests ORDER BY start DESC, created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._digest(r) for r in rows]

    def get_digest(self, digest_id: str) -> Digest | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM digests WHERE id=?", (digest_id,)).fetchone()
        return self._digest(r) if r else None

    def has_digest(self, label: str) -> bool:
        with self._lock:
            r = self._conn.execute("SELECT 1 FROM digests WHERE label=?", (label,)).fetchone()
        return r is not None

    def delete_digest(self, digest_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM digests WHERE id=?", (digest_id,))
        return cur.rowcount > 0

    # ---- speakers ------------------------------------------------------

    def set_session_embeddings(self, session_id: str, embeddings: dict[str, list[float]]) -> None:
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO session_speakers(session_id, label, embedding)"
                " VALUES (?,?,?)",
                [(session_id, label, json.dumps(vec)) for label, vec in embeddings.items()],
            )

    def get_session_embeddings(self, session_id: str) -> dict[str, list[float]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT label, embedding FROM session_speakers WHERE session_id=?", (session_id,)
            ).fetchall()
        return {r["label"]: json.loads(r["embedding"]) for r in rows}

    def relabel_session_speaker(self, session_id: str, old: str, new: str) -> Session | None:
        """Rename a speaker label everywhere in one session (segments, words,
        participants, stored embedding)."""
        session = self.get(session_id)
        if session is None:
            return None
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE segments SET speaker=? WHERE session_id=? AND speaker=?",
                (new, session_id, old),
            )
            self._conn.execute(
                "UPDATE OR REPLACE session_speakers SET label=? WHERE session_id=? AND label=?",
                (new, session_id, old),
            )
            participants = [new if p == old else p for p in session.participants]
            participants = list(dict.fromkeys(participants))
            self._conn.execute(
                "UPDATE sessions SET participants=?, updated_at=? WHERE id=?",
                (json.dumps(participants), datetime.now().isoformat(), session_id),
            )
        return self.get(session_id)

    def list_speakers(self) -> list[SpeakerProfile]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM speakers ORDER BY name").fetchall()
        return [self._speaker(r) for r in rows]

    def get_speaker(self, speaker_id: str) -> SpeakerProfile | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM speakers WHERE id=?", (speaker_id,)).fetchone()
        return self._speaker(row) if row else None

    def get_speaker_by_name(self, name: str) -> SpeakerProfile | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM speakers WHERE name=?", (name,)).fetchone()
        return self._speaker(row) if row else None

    def upsert_speaker_sample(self, name: str, embedding: list[float]) -> SpeakerProfile:
        """Add one voice sample to `name`, keeping a running mean embedding."""
        existing = self.get_speaker_by_name(name)
        now = datetime.now().isoformat()
        with self._lock, self._conn:
            if existing is None:
                profile = SpeakerProfile(name=name, embedding=embedding, sample_count=1)
                self._conn.execute(
                    "INSERT INTO speakers"
                    "(id, name, embedding, sample_count, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (profile.id, name, json.dumps(embedding), 1, now, now),
                )
                return profile
            n = existing.sample_count
            if len(existing.embedding) == len(embedding):
                mean = [
                    (o * n + v) / (n + 1)
                    for o, v in zip(existing.embedding, embedding, strict=True)
                ]
            else:
                mean = embedding  # embedding model changed; start over
                n = 0
            self._conn.execute(
                "UPDATE speakers SET embedding=?, sample_count=?, updated_at=? WHERE id=?",
                (json.dumps(mean), n + 1, now, existing.id),
            )
        return self.get_speaker(existing.id)

    def rename_speaker(self, speaker_id: str, name: str) -> SpeakerProfile | None:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE speakers SET name=?, updated_at=? WHERE id=?",
                (name, datetime.now().isoformat(), speaker_id),
            )
        return self.get_speaker(speaker_id) if cur.rowcount else None

    def delete_speaker(self, speaker_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM speakers WHERE id=?", (speaker_id,))
        return cur.rowcount > 0

    @staticmethod
    def _speaker(row) -> SpeakerProfile:
        return SpeakerProfile(
            id=row["id"],
            name=row["name"],
            embedding=json.loads(row["embedding"]),
            sample_count=row["sample_count"],
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    # ---- internals -----------------------------------------------------

    def _write_segments(self, session_id: str, segments: list[TranscriptSegment]) -> None:
        self._conn.execute("DELETE FROM segments WHERE session_id=?", (session_id,))
        self._conn.executemany(
            """INSERT INTO segments(session_id, idx, text, speaker, start, "end", words)
               VALUES (?,?,?,?,?,?,?)""",
            [
                (
                    session_id,
                    i,
                    s.text,
                    s.speaker,
                    s.start,
                    s.end,
                    json.dumps([w.model_dump() for w in s.words]) if s.words else None,
                )
                for i, s in enumerate(segments)
            ],
        )

    def _write_recordings(self, session_id: str, recordings: list[Recording]) -> None:
        self._conn.execute("DELETE FROM recordings WHERE session_id=?", (session_id,))
        self.add_recordings(session_id, recordings)


# ---- migration from the v2 JSON-per-session layout ------------------------


def import_json_sessions(repo: SessionRepository, sessions_dir: Path) -> int:
    """Import data/sessions/*.json files that are not yet in the database.

    Files are left in place. Returns the number imported.
    """
    if not sessions_dir.is_dir():
        return 0
    imported = 0
    for path in sorted(sessions_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
            if repo.exists(data.get("id", "")):
                continue
            status = data.get("status", "created")
            if status == "processing":
                status = "completed" if data.get("transcript") else "created"
            data["status"] = status
            session = Session.model_validate(data)
            repo.save(session)
            imported += 1
        except Exception:
            logger.warning("Skipping unreadable legacy session file: %s", path, exc_info=True)
    if imported:
        logger.info("Imported %d legacy JSON session(s) from %s", imported, sessions_dir)
    return imported
