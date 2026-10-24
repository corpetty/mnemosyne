"""Ticking (done) and starring (follow) an action item: the one path for the action-item route
(routes/tasks.py) and a contact's ticks taken by the hub (services/shares.py, phase 8).

Read access suffices, as for the repository's setters. done and follow belong to the task: a
linked item's other mentions follow (services/task_links.py, which logs a line in each meeting
it changes). An unlinked tick by someone other than the meeting's owner is logged in its
history. A tick is written to the meeting's Obsidian note too, or the next pass over the vault
would take the old state back. On a meeting someone shared with me, only when the share includes
its action items (phase 8).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import access
from ..models.session import ActionItem, Session
from . import history, share_push, shared_with_me, task_links

if TYPE_CHECKING:
    from ..api.context import AppContext


def set_item_flags(
    app: AppContext,
    session: Session,
    idx: int,
    *,
    done: bool | None = None,
    follow: bool | None = None,
    by: str = "",
    item: ActionItem | None = None,
) -> ActionItem:
    """Set `done` and/or `follow` (None: left as it is) on the meeting's action item at `idx`,
    then announce the meeting on the bus (always, once). `item`: the item as the caller has it
    (default: `session.summary_data.action_items[idx]`); its flags are updated in place and it
    is returned. `by`: who did it, for history lines. Blocking (the vault note)."""
    from .vault_sync import write_tick  # vault_sync imports services.tasks: not at the top

    if session.shared_from and not access.is_internal():
        # Someone else's meeting shared with me: only when its action items were shared, as
        # the owner's hub will check (services/shared_with_me.py).
        parts = shared_with_me.parts_of(app, session.id, session.shared_from)
        shared_with_me.require_part(parts, "action_items")
        if not by:
            me = shared_with_me.acting(app, session)
            by = me.name if me else ""
    if item is None:
        item = session.summary_data.action_items[idx]
    if session.shared_from:  # my tick on their meeting travels back (services/share_push.py)
        if done is not None and done != item.done:
            share_push.enqueue_item_flag(app, session.id, item.id, "done", done)
        if follow is not None and follow != item.follow:
            share_push.enqueue_item_flag(app, session.id, item.id, "follow", follow)
    obj = task_links.object_of(app.repo, session.id, item.id, app.repo.visible_ids())
    if done is not None and done != item.done:
        item.done = done
        if obj is not None:  # logs a line in each meeting it changes, this one included
            task_links.set_done(app, obj.key, done, by=by, acting=session.id)
        else:
            app.repo.set_action_item_done(session.id, idx, done)  # readers may tick too
            _log_tick(app, session, item.text, done, by)
        write_tick(app, session, idx, done)  # never raises
    if follow is not None and follow != item.follow:
        item.follow = follow
        if obj is not None:
            task_links.set_follow(app, obj.key, follow, acting=session.id)
        else:
            app.repo.set_action_item_follow(session.id, idx, follow)  # readers may too
    app.bus.publish({"type": "session", "session_id": session.id, "status": session.status.value})
    return item


def _log_tick(app: AppContext, session: Session, task: str, done: bool, by: str) -> None:
    """Someone other than the owner ticking an item off shows in the meeting's history."""
    if session.owner_id != access.user_id():
        history.log(app, session.id, "task_done" if done else "task_reopened", task=task, by=by)
