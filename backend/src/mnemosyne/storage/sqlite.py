"""SQLite-backed session repository.

One file, WAL mode, stdlib sqlite3. Transcript segments live in their own
table so listing sessions never deserializes transcripts.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from ..models.session import Recording, Session, SessionStatus, SessionSummary
from ..models.transcript import TranscriptSegment

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

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
"""


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


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
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

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
                                        summary, notes, participants)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     name=excluded.name, status=excluded.status, updated_at=excluded.updated_at,
                     audio_file=excluded.audio_file, summary=excluded.summary,
                     notes=excluded.notes, participants=excluded.participants""",
                (
                    session.id,
                    session.name,
                    session.status.value,
                    session.created_at.isoformat(),
                    session.updated_at.isoformat(),
                    session.audio_file,
                    session.summary,
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
        allowed = {"name", "status", "audio_file", "summary", "notes", "participants"}
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
