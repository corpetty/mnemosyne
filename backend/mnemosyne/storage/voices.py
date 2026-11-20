"""The narration voice tables and their rows, mixed into SessionRepository (sqlite.py).

Voices are not meetings: nothing here travels with library sync, shares, records or backups'
meeting parts, and a voice is its owner's alone (services/voices.py enforces who may use it).
Paths are relative to <data_dir> (so a moved data folder still works) and may end in `.enc`
when encryption at rest is on.
"""

from __future__ import annotations

import json
from datetime import datetime
from uuid import uuid4

VOICE_SCHEMA = """
-- Narration voices (docs/plans/2026-11-20-narration-voices.md). A voice belongs to its owner
-- (a user id, '' without sign-in); consent_path is the live consent recording that made it.
CREATE TABLE IF NOT EXISTS narration_voices (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'en',
    consent_path TEXT,
    consent_at TEXT,
    consent_text TEXT,
    consent_score REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- Reference clips: mono WAV under voices/<id>/, their transcript and checks. embedding: the
-- speaker embedder's vector (JSON) in `space`, for the same-person and consent checks.
CREATE TABLE IF NOT EXISTS narration_voice_clips (
    voice_id TEXT NOT NULL REFERENCES narration_voices(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    path TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    seconds REAL NOT NULL,
    quality REAL NOT NULL,
    accepted INTEGER NOT NULL DEFAULT 0,
    problem TEXT NOT NULL DEFAULT '',
    space TEXT NOT NULL DEFAULT '',
    embedding TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (voice_id, idx)
);
-- One prepared reference per voice and engine family (synthesis/engine.py VoicePrompt).
CREATE TABLE IF NOT EXISTS narration_prompts (
    voice_id TEXT NOT NULL REFERENCES narration_voices(id) ON DELETE CASCADE,
    space TEXT NOT NULL,
    path TEXT NOT NULL,
    text TEXT NOT NULL,
    clips TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (voice_id, space)
);
-- Every narration made, kept when its voice is deleted (voice_id NULL, voice_name stays).
CREATE TABLE IF NOT EXISTS narrations (
    id TEXT PRIMARY KEY,
    voice_id TEXT REFERENCES narration_voices(id) ON DELETE SET NULL,
    voice_name TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL,
    source_id TEXT,
    title TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    engine TEXT NOT NULL,
    model TEXT NOT NULL,
    path TEXT NOT NULL,
    seconds REAL NOT NULL,
    chunks TEXT NOT NULL DEFAULT '[]',
    watermarked INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_narrations_source ON narrations(source_kind, source_id);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _dt(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


class VoiceRows:
    """Row access for the tables above. Expects the repository's `_conn` and `_lock`."""

    # ---- voices --------------------------------------------------------

    def add_voice(self, name: str, owner_id: str, language: str = "en") -> dict:
        voice_id = uuid4().hex[:12]
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO narration_voices (id, owner_id, name, language, created_at,"
                " updated_at) VALUES (?,?,?,?,?,?)",
                (voice_id, owner_id, name, language, now, now),
            )
        return self.get_voice_row(voice_id)

    def get_voice_row(self, voice_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM narration_voices WHERE id=?", (voice_id,)
            ).fetchone()
        return _voice(row) if row else None

    def list_voice_rows(self, owner_id: str | None = None) -> list[dict]:
        """Every voice, or only `owner_id`'s; oldest first."""
        sql = "SELECT * FROM narration_voices"
        args: tuple = ()
        if owner_id is not None:
            sql += " WHERE owner_id=?"
            args = (owner_id,)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY created_at, id", args).fetchall()
        return [_voice(r) for r in rows]

    def update_voice(self, voice_id: str, **fields) -> dict | None:
        allowed = {"name", "language", "consent_path", "consent_at", "consent_text"}
        allowed |= {"consent_score"}
        changes = {k: v for k, v in fields.items() if k in allowed}
        if changes:
            changes["updated_at"] = _now()
            cols = ", ".join(f"{k}=?" for k in changes)
            with self._lock, self._conn:
                self._conn.execute(
                    f"UPDATE narration_voices SET {cols} WHERE id=?", (*changes.values(), voice_id)
                )
        return self.get_voice_row(voice_id)

    def delete_voice(self, voice_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM narration_voices WHERE id=?", (voice_id,))
        return cur.rowcount > 0

    def voice_paths(self) -> list[tuple[str, str, str]]:
        """(table, key, relative path) of every stored voice file: clips, consent, prompts,
        narrations (encryption on/off converts them, services/voices.py)."""
        with self._lock:
            out = [
                ("clip", f"{r[0]}:{r[1]}", r[2])
                for r in self._conn.execute(
                    "SELECT voice_id, idx, path FROM narration_voice_clips"
                ).fetchall()
            ]
            out += [
                ("consent", r[0], r[1])
                for r in self._conn.execute(
                    "SELECT id, consent_path FROM narration_voices WHERE consent_path IS NOT NULL"
                ).fetchall()
            ]
            out += [
                ("prompt", f"{r[0]}:{r[1]}", r[2])
                for r in self._conn.execute(
                    "SELECT voice_id, space, path FROM narration_prompts"
                ).fetchall()
            ]
            out += [
                ("narration", r[0], r[1])
                for r in self._conn.execute("SELECT id, path FROM narrations").fetchall()
            ]
        return out

    def set_voice_path(self, table: str, key: str, path: str) -> None:
        sql = {
            "clip": "UPDATE narration_voice_clips SET path=? WHERE voice_id=? AND idx=?",
            "consent": "UPDATE narration_voices SET consent_path=? WHERE id=?",
            "prompt": "UPDATE narration_prompts SET path=? WHERE voice_id=? AND space=?",
            "narration": "UPDATE narrations SET path=? WHERE id=?",
        }[table]
        with self._lock, self._conn:
            self._conn.execute(sql, (path, *key.split(":", 1)))

    # ---- clips ---------------------------------------------------------

    def add_voice_clip(
        self,
        voice_id: str,
        path: str,
        text: str,
        seconds: float,
        quality: float,
        accepted: bool,
        problem: str = "",
        space: str = "",
        embedding: list[float] | None = None,
    ) -> int:
        with self._lock, self._conn:
            idx = self._conn.execute(
                "SELECT COALESCE(MAX(idx), -1) + 1 FROM narration_voice_clips WHERE voice_id=?",
                (voice_id,),
            ).fetchone()[0]
            self._conn.execute(
                "INSERT INTO narration_voice_clips (voice_id, idx, path, text, seconds, quality,"
                " accepted, problem, space, embedding, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    voice_id,
                    idx,
                    path,
                    text,
                    seconds,
                    quality,
                    int(accepted),
                    problem,
                    space,
                    json.dumps(embedding) if embedding is not None else None,
                    _now(),
                ),
            )
            self._conn.execute(
                "UPDATE narration_voices SET updated_at=? WHERE id=?", (_now(), voice_id)
            )
            self._conn.execute("DELETE FROM narration_prompts WHERE voice_id=?", (voice_id,))
        return idx

    def voice_clips(self, voice_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM narration_voice_clips WHERE voice_id=? ORDER BY idx", (voice_id,)
            ).fetchall()
        return [_clip(r) for r in rows]

    def update_voice_clip(self, voice_id: str, idx: int, **fields) -> bool:
        allowed = {"text", "accepted", "problem"}
        changes = {k: (int(v) if k == "accepted" else v) for k, v in fields.items() if k in allowed}
        if not changes:
            return False
        cols = ", ".join(f"{k}=?" for k in changes)
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"UPDATE narration_voice_clips SET {cols} WHERE voice_id=? AND idx=?",
                (*changes.values(), voice_id, idx),
            )
            # A changed transcript or clip set means the prepared prompts are stale.
            self._conn.execute("DELETE FROM narration_prompts WHERE voice_id=?", (voice_id,))
        return cur.rowcount > 0

    def delete_voice_clip(self, voice_id: str, idx: int) -> str | None:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT path FROM narration_voice_clips WHERE voice_id=? AND idx=?",
                (voice_id, idx),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "DELETE FROM narration_voice_clips WHERE voice_id=? AND idx=?", (voice_id, idx)
            )
            self._conn.execute("DELETE FROM narration_prompts WHERE voice_id=?", (voice_id,))
        return row[0]

    # ---- prompts -------------------------------------------------------

    def get_prompt_row(self, voice_id: str, space: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM narration_prompts WHERE voice_id=? AND space=?", (voice_id, space)
            ).fetchone()
        if row is None:
            return None
        return {"path": row["path"], "text": row["text"], "clips": json.loads(row["clips"])}

    def set_prompt_row(
        self, voice_id: str, space: str, path: str, text: str, clips: list[int]
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO narration_prompts (voice_id, space, path, text, clips,"
                " updated_at) VALUES (?,?,?,?,?,?)",
                (voice_id, space, path, text, json.dumps(clips), _now()),
            )

    # ---- narrations ----------------------------------------------------

    def add_narration(self, row: dict) -> None:
        cols = (
            "id voice_id voice_name user_id source_kind source_id title text_hash engine model"
            " path seconds chunks watermarked created_at"
        ).split()
        values = dict(row)
        values["chunks"] = json.dumps(values.get("chunks") or [])
        values["watermarked"] = int(bool(values.get("watermarked")))
        values.setdefault("created_at", _now())
        with self._lock, self._conn:
            self._conn.execute(
                f"INSERT INTO narrations ({', '.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                tuple(values.get(c) for c in cols),
            )

    def get_narration_row(self, narration_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM narrations WHERE id=?", (narration_id,)
            ).fetchone()
        return _narration(row) if row else None

    def list_narration_rows(
        self,
        user_id: str | None = None,
        source_kind: str | None = None,
        source_id: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        where, args = [], []
        for col, value in (("user_id", user_id), ("source_kind", source_kind)):
            if value is not None:
                where.append(f"{col}=?")
                args.append(value)
        if source_id is not None:
            where.append("source_id=?")
            args.append(source_id)
        sql = "SELECT * FROM narrations"
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self._lock:
            rows = self._conn.execute(
                sql + " ORDER BY created_at DESC, id LIMIT ?", (*args, limit)
            ).fetchall()
        return [_narration(r) for r in rows]

    def delete_narration(self, narration_id: str) -> str | None:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT path FROM narrations WHERE id=?", (narration_id,)
            ).fetchone()
            if row is None:
                return None
            self._conn.execute("DELETE FROM narrations WHERE id=?", (narration_id,))
        return row[0]


def _voice(row) -> dict:
    return {
        "id": row["id"],
        "owner_id": row["owner_id"],
        "name": row["name"],
        "language": row["language"],
        "consent_path": row["consent_path"],
        "consent_at": _dt(row["consent_at"]),
        "consent_text": row["consent_text"],
        "consent_score": row["consent_score"],
        "created_at": _dt(row["created_at"]),
        "updated_at": _dt(row["updated_at"]),
    }


def _clip(row) -> dict:
    return {
        "idx": row["idx"],
        "path": row["path"],
        "text": row["text"],
        "seconds": row["seconds"],
        "quality": row["quality"],
        "accepted": bool(row["accepted"]),
        "problem": row["problem"],
        "space": row["space"],
        "embedding": json.loads(row["embedding"]) if row["embedding"] else None,
        "created_at": _dt(row["created_at"]),
    }


def _narration(row) -> dict:
    out = {k: row[k] for k in row.keys()}  # noqa: SIM118 - sqlite3.Row
    out["chunks"] = json.loads(out["chunks"] or "[]")
    out["watermarked"] = bool(out["watermarked"])
    out["created_at"] = _dt(out["created_at"])
    return out
