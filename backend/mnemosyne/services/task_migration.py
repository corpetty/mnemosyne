"""One-time migration to tasks and decisions as linked objects (phase 4).

Meetings summarized before links existed carry items without `task_id` / `decision_links`.
Once per data dir (repo meta `task_links_migrated` = "1", set only after success) every
summarized meeting is visited OLDEST first and gets the same exact-tier linking a fresh summary
gets (`task_links.link_items(..., "exact")`, `decision_links.link_items`), then `reconcile`
settles every linked task on its head. Close matches are never linked here: they stay
suggestions. The rule is the live one: only OPEN tasks are candidates, so a done task that comes
up again in a later meeting is a new task. Runs without a principal (all meetings). Never
raises: on a failure the flag stays unset and the next start retries (links already made stay
and are skipped, since linked items are not linked again).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..api.context import AppContext

logger = logging.getLogger(__name__)

FLAG = "task_links_migrated"


def migrate(app: AppContext) -> dict:
    """Returns {tasks_linked, decisions_linked, reconciled}; `skipped: True` when it already ran,
    `failed: True` when it raised (flag left unset)."""
    counts = {"tasks_linked": 0, "decisions_linked": 0, "reconciled": 0}
    try:
        if app.repo.get_meta(FLAG) == "1":
            return {**counts, "skipped": True}
        from . import decision_links, task_links

        # action_item_sources is newest first; the principal-free read sees every meeting.
        for src in reversed(app.repo.action_item_sources()):
            sid = src["id"]
            counts["tasks_linked"] += len(task_links.link_items(app, sid, "exact"))
            counts["decisions_linked"] += len(decision_links.link_items(app, sid))
        counts["reconciled"] = task_links.reconcile(app)
        app.repo.set_meta(FLAG, "1")
    except Exception:
        logger.warning("Task links: migration failed, will retry at the next start", exc_info=True)
        return {**counts, "failed": True}
    logger.info(
        "Task links: migrated, %d task(s) and %d decision(s) linked, %d meeting(s) reconciled",
        counts["tasks_linked"],
        counts["decisions_linked"],
        counts["reconciled"],
    )
    return counts
