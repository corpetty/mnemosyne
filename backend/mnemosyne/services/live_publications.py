"""Live publications (docs/plans/2026-11-05-live-publications.md): keeping a published link up
to date by itself (`follow`) and taking readers' comments (`comments`).

The watcher (task 5) and the bridge to the host's room (task 6) come later; for now this module
only answers what `publishing.to_model` shows of a publication's live channel.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..api.context import AppContext


def status(app: AppContext, row: dict) -> tuple[str, str, int]:
    """(state, reason, readers) of a live publication's channel, for `Publication.live`,
    `live_reason` and `readers`: state is "live", "offline" or "unsupported". Only called for a
    row with `follow` or `comments` on. Until the bridge exists nothing is connected."""
    return ("offline", "", 0)
