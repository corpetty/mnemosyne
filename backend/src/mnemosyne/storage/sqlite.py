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
from datetime import datetime
from pathlib import Path

from ..models.search import SearchHit, SegmentHit
from ..models.session import Recording, Session, SessionStatus, SessionSummary, SummaryData
from ..models.speaker import SpeakerProfile
from ..models.transcript import TranscriptSegment

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 4

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
    participants TEXT NOT NULL DEFAULT '[]'
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
        SELECT s.id, s.name, s.status, s.created_at, s.updated_at, s.participants,
               length(s.summary) > 0 AS has_summary,
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
                participant_count=len(json.loads(r["participants"])),
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
                                        summary, summary_data, notes, participants)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     name=excluded.name, status=excluded.status, updated_at=excluded.updated_at,
                     audio_file=excluded.audio_file, summary=excluded.summary,
                     summary_data=excluded.summary_data,
                     notes=excluded.notes, participants=excluded.participants""",
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
            if key == "participants":
                value = json.dumps(value)
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
