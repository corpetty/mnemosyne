"""Recordings other computers handed to this one (docs/plans/2026-11-09-connections-design.md:
a laptop that uses this computer records while it cannot reach it, then hands the recording
over; api/routes/handoff.py).

A hand-off is idempotent by its id: <data_dir>/handoffs.json maps "<device id>:<hand-off id>" to
the meeting it became, so a laptop that lost the answer and sends again gets the same meeting
and nothing is done twice. The id is scoped by the sending device, so one device can never
learn the meeting another one's hand-off became.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

KEEP = 5000  # the newest hand-offs remembered; a retry comes within days, not thousands later


class HandoffStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._done: dict[str, dict] = {}
        self._receiving: set[str] = set()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            self._done = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            logger.warning("Could not read %s: hand-offs start afresh", path, exc_info=True)

    @staticmethod
    def key(device_id: str | None, handoff_id: str) -> str:
        return f"{device_id or 'local'}:{handoff_id}"

    def session_for(self, key: str) -> str | None:
        with self._lock:
            entry = self._done.get(key)
        return str(entry["session_id"]) if entry and entry.get("session_id") else None

    def claim(self, key: str) -> bool:
        """Start receiving `key`; False when it is being received right now (a retry that
        overtook the first try)."""
        with self._lock:
            if key in self._receiving:
                return False
            self._receiving.add(key)
            return True

    def release(self, key: str) -> None:
        with self._lock:
            self._receiving.discard(key)

    def remember(self, key: str, session_id: str) -> None:
        with self._lock:
            self._done[key] = {
                "session_id": session_id,
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            if len(self._done) > KEEP:
                for old in sorted(self._done, key=lambda k: self._done[k].get("at", ""))[
                    : len(self._done) - KEEP
                ]:
                    del self._done[old]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._done, f, indent=1)
            tmp.replace(self.path)


def store(app) -> HandoffStore:
    if app.handoffs is None:
        app.handoffs = HandoffStore(app.settings.data_dir / "handoffs.json")
    return app.handoffs
