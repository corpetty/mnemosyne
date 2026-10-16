"""Meetings recorded on another computer (docs/plans/2026-10-14-library-sync-rules.md, 3 and 4).

`Session.origin` is "" for a meeting recorded here, else the id of the computer that has the
audio. Work that needs the audio (transcribing, clips, deleting, recovery) belongs to that
computer; these helpers let the others say so.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

if TYPE_CHECKING:
    from ..api.context import AppContext

logger = logging.getLogger(__name__)

FALLBACK_NAME = "another computer"


def is_foreign(session: Any) -> bool:
    """True when the meeting was recorded on another computer."""
    return bool(getattr(session, "origin", ""))


def origin_name(app: AppContext, session: Any) -> str:
    """The name of the computer that recorded the meeting, from sync_peers.json."""
    origin = getattr(session, "origin", "") or ""
    try:
        raw = json.loads((app.settings.data_dir / "sync_peers.json").read_text(encoding="utf-8"))
        for peer in raw.get("peers", []):
            if peer.get("id") == origin and peer.get("name"):
                return str(peer["name"])
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return FALLBACK_NAME


def audio_elsewhere(app: AppContext, session: Any) -> str:
    return f"The audio is on {origin_name(app, session)}"


def require_local_audio(app: AppContext, session: Any) -> None:
    """Raise a 409 naming the computer when the meeting's audio is not on this one."""
    if is_foreign(session):
        raise HTTPException(409, audio_elsewhere(app, session))


def skip_foreign(session: Any, what: str) -> bool:
    """True (with a debug line) when automation `what` should skip this meeting."""
    if is_foreign(session):
        logger.debug("%s skipped for %s: recorded on another computer", what, session.id)
        return True
    return False
