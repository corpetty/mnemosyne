"""Decisions across meetings: the same decision, as it came up in several of them.

The link lives on the meeting (`SummaryData.decision_links`, decision id -> object id, 12 hex;
a decision without an entry is not linked); objects are derived, as for tasks
(services/task_links.py). A decision object is every decision, in any meeting, with the same
object id; an unlinked decision is its own object, keyed `<session_id>:<decision_id>`. Links
are pruned with their decisions (services/summary_doc.py) and travel inside summary_data.

The head is the mention in the newest meeting (created_at) among the meetings the caller can
see: its text is the object's current wording. Earlier mentions are what it repeats or
supersedes.

Linking: `link_items` (run after a summary is written, with the task linking) links a
meeting's unlinked decisions whose normalized text (services/tasks.py `_norm`) is exactly the
head's of an object seen in another meeting the caller can see, each object used once, and logs
a `decision_linked` history line. Only objects whose head is in an OLDER meeting (created_at,
then session id) qualify: a decision never links forward.
Different wording is never linked automatically: "supersedes"
is a manual link (`link_pair`), after which the newer wording is the head. `unlink` takes one
mention out.

Access: `index(repo, visible)` filters with the set the caller passes (None = all meetings);
the writers filter with `repo.visible_ids()`. Writing a link needs read access to the meeting
(`SessionRepository.set_decision_links`); the decision texts are never written here."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from .. import access
from ..models.base import ApiModel
from . import history
from .task_links import _notify
from .tasks import _norm

if TYPE_CHECKING:
    from ..api.context import AppContext
    from ..storage.sqlite import SessionRepository

logger = logging.getLogger(__name__)

Ref = tuple[str, str]  # (session_id, decision_id)


class DecisionMention(ApiModel):
    """One decision of one meeting, as part of a decision object."""

    session_id: str
    session_name: str
    created_at: datetime
    decision_id: str
    text: str
    at: float | None  # seconds into the meeting, when known


class DecisionObject(ApiModel):
    """A decision across meetings: the head mention's text is the current wording."""

    key: str  # the object id, or "<session_id>:<decision_id>" for an unlinked decision
    text: str
    mentions: list[DecisionMention]  # visible ones, newest first: mentions[0] is the head
    linked: bool  # more than one visible mention
    # Open (not resolved, not deleted) comment threads on the visible mentions.
    open_comments: int = 0


@dataclass
class _Meeting:
    id: str
    name: str
    created_at: datetime
    decisions: list[tuple[str, str, float | None]]  # (decision id, text, at)
    links: dict[str, str]


def _meetings(repo: SessionRepository) -> list[_Meeting]:
    out = []
    for r in repo.action_item_sources():
        data = r["summary_data"]
        ids = data.decision_ids
        decisions = [
            (ids[i], text, data.decision_at[i] if i < len(data.decision_at) else None)
            for i, text in enumerate(data.decisions)
            if i < len(ids) and ids[i]
        ]
        if decisions:
            out.append(
                _Meeting(r["id"], r["name"], r["created_at"], decisions, dict(data.decision_links))
            )
    return out


def _key(session_id: str, decision_id: str, links: dict[str, str]) -> str:
    return links.get(decision_id) or f"{session_id}:{decision_id}"


def _newest_first(mentions: list[DecisionMention]) -> list[DecisionMention]:
    return sorted(mentions, key=lambda x: (x.created_at, x.session_id, x.decision_id), reverse=True)


def _grouped(
    meetings: list[_Meeting], visible: set[str] | None
) -> dict[str, list[DecisionMention]]:
    groups: dict[str, list[DecisionMention]] = {}
    for m in meetings:
        if visible is not None and m.id not in visible:
            continue
        for decision_id, text, at in m.decisions:
            groups.setdefault(_key(m.id, decision_id, m.links), []).append(
                DecisionMention(
                    session_id=m.id,
                    session_name=m.name,
                    created_at=m.created_at,
                    decision_id=decision_id,
                    text=text,
                    at=at,
                )
            )
    return groups


def _objects(meetings: list[_Meeting], visible: set[str] | None) -> dict[str, DecisionObject]:
    out = {}
    for key, mentions in _grouped(meetings, visible).items():
        mentions = _newest_first(mentions)
        out[key] = DecisionObject(
            key=key, text=mentions[0].text, mentions=mentions, linked=len(mentions) > 1
        )
    return out


def index(repo: SessionRepository, visible: set[str] | None = None) -> dict[str, DecisionObject]:
    """Every decision object of the summarized meetings in `visible` (None = all meetings; pass
    `repo.visible_ids()` for the caller's view), by key. Mentions in other meetings are left
    out, and the head is the newest of those left."""
    objs = _objects(_meetings(repo), visible)
    _count_comments(repo, objs, visible)
    return objs


def _count_comments(repo: SessionRepository, objs: dict[str, DecisionObject], visible) -> None:
    """Fill `open_comments`: one query of the comment roots for all the objects."""

    counts = repo.open_comment_counts(visible)
    for o in objs.values():
        o.open_comments = sum(
            counts.get((m.session_id, "decision", m.decision_id), 0) for m in o.mentions
        )


def object_of(
    repo: SessionRepository, session_id: str, decision_id: str, visible: set[str] | None = None
) -> DecisionObject | None:
    """The decision object a decision (by id) belongs to, or None for no such visible one."""
    meetings = _meetings(repo)
    for m in meetings:
        if m.id == session_id and any(d[0] == decision_id for d in m.decisions):
            if visible is not None and session_id not in visible:
                return None
            objs = _objects(meetings, visible)
            _count_comments(repo, objs, visible)
            return objs.get(_key(m.id, decision_id, m.links))
    return None


def mint() -> str:
    """A new object id: 12 hex characters, never all digits (identifier redaction)."""
    while (object_id := uuid4().hex[:12]).isdigit():
        pass
    return object_id


def exact_match(a: str, b: str) -> bool:
    na = _norm(a)
    return bool(na) and na == _norm(b)


def _all_mentions(meetings: list[_Meeting], key: str) -> list[DecisionMention]:
    """Every mention of a key in every meeting (no access filter)."""
    return _grouped(meetings, None).get(key, [])


def _set(app: AppContext, session_id: str, decision_id: str, object_id: str) -> bool:
    return app.repo.set_decision_links(session_id, {decision_id: object_id})


# ---- linking ----------------------------------------------------------------------------------


def link_items(app: AppContext, session_id: str) -> list[tuple[str, str, str]]:
    """Link the meeting's unlinked decisions to decisions of other meetings the caller can
    see, when the text is exactly (normalized) the head's of an object, each object once.
    Returns (decision id, object id, other session id) per link made."""
    repo = app.repo
    meetings = _meetings(repo)
    this = next((m for m in meetings if m.id == session_id), None)
    if this is None:
        return []
    all_groups = _grouped(meetings, None)
    visible = repo.visible_ids()
    candidates = [
        o
        for o in _objects(meetings, visible).values()
        if all(x.session_id != session_id for x in o.mentions)
        and (o.mentions[0].created_at, o.mentions[0].session_id) < (this.created_at, this.id)
    ]
    candidates.sort(
        key=lambda o: (o.mentions[0].created_at, o.mentions[0].session_id), reverse=True
    )
    used: set[str] = set()
    made: list[tuple[str, str, str]] = []
    touched: set[str] = set()
    for decision_id, text, _ in this.decisions:
        key = this.links.get(decision_id)
        if key and len(all_groups.get(key, [])) > 1:
            continue  # already linked to something
        for o in candidates:
            if o.key in used or not exact_match(text, o.text):
                continue
            head = o.mentions[0]
            object_id = o.key if ":" not in o.key else mint()
            if ":" in o.key and not _set(app, head.session_id, head.decision_id, object_id):
                continue
            if not _set(app, session_id, decision_id, object_id):
                continue
            used.add(o.key)
            made.append((decision_id, object_id, head.session_id))
            touched.add(head.session_id)
            history.log(app, session_id, "decision_linked", decision=text,
                        earlier=head.session_name, session=head.session_id)  # fmt: skip
            break
    _notify(app, touched, session_id)
    return made


def link_pair(app: AppContext, a: Ref, b: Ref, acting: str = "") -> str:
    """Link two decisions (session_id, decision_id) by hand, e.g. "ship in November"
    superseding "ship in October": neither linked → a new object id; one linked → the other
    joins it; two different objects → merged (the smaller one's id is rewritten on every
    mention; it must have none in a meeting the caller cannot see, else the other is
    rewritten, and when both do: access.Forbidden). Returns the object id. LookupError: no
    such decision, or one the caller cannot see; ValueError: the same decision twice, or two
    of one meeting. Other meetings that changed are announced on the bus (not `acting`)."""
    if a == b or a[0] == b[0]:
        raise ValueError("A decision can only be linked to a decision of another meeting")
    repo = app.repo
    meetings = _meetings(repo)
    visible = repo.visible_ids()
    by_id = {m.id: m for m in meetings}

    def find(ref: Ref) -> tuple[_Meeting, str, str]:
        m = by_id.get(ref[0])
        found = next((d for d in m.decisions if d[0] == ref[1]), None) if m else None
        if m is None or found is None or (visible is not None and m.id not in visible):
            raise LookupError("No such decision")
        return m, found[1], m.links.get(ref[1], "")

    ma, text_a, ta = find(a)
    mb, text_b, tb = find(b)
    groups = _grouped(meetings, None)
    # A lone leftover id (its other mentions are gone) counts as no link.
    ta = ta if len(groups.get(ta, [])) > 1 else ""
    tb = tb if len(groups.get(tb, [])) > 1 else ""
    touched: set[str] = set()
    linked = True
    if ta and ta == tb:
        object_id, linked = ta, False
    elif not ta and not tb:
        object_id = mint()
        _set(app, ma.id, a[1], object_id)
        _set(app, mb.id, b[1], object_id)
        touched |= {ma.id, mb.id}
    elif not tb:
        object_id = ta
        _set(app, mb.id, b[1], object_id)
        touched.add(mb.id)
    elif not ta:
        object_id = tb
        _set(app, ma.id, a[1], object_id)
        touched.add(ma.id)
    else:
        mentions = {t: groups[t] for t in (ta, tb)}

        def hidden(t: str) -> bool:
            return visible is not None and any(x.session_id not in visible for x in mentions[t])

        loser, object_id = (tb, ta) if len(mentions[tb]) <= len(mentions[ta]) else (ta, tb)
        if hidden(loser):
            loser, object_id = object_id, loser
            if hidden(loser):
                raise access.Forbidden("Both decisions are also in meetings you cannot see")
        for x in mentions[loser]:
            _set(app, x.session_id, x.decision_id, object_id)
            touched.add(x.session_id)
    if linked:
        newer, older = (ma, mb) if (ma.created_at, ma.id) >= (mb.created_at, mb.id) else (mb, ma)
        text = text_a if newer is ma else text_b
        history.log(app, newer.id, "decision_linked", decision=text, earlier=older.name,
                    session=older.id)  # fmt: skip
    _notify(app, touched, acting)
    return object_id


def unlink(app: AppContext, session_id: str, decision_id: str, acting: str = "") -> bool:
    """Take a decision out of its object. When only one mention is left, that one is unlinked
    too (no object of one). False: not linked, or no such visible decision."""
    repo = app.repo
    meetings = _meetings(repo)
    visible = repo.visible_ids()
    m = next((x for x in meetings if x.id == session_id), None)
    if m is None or (visible is not None and m.id not in visible):
        return False
    object_id = m.links.get(decision_id, "")
    if not object_id or not any(d[0] == decision_id for d in m.decisions):
        return False
    if not _set(app, session_id, decision_id, ""):
        return False
    rest = [
        x
        for x in _all_mentions(meetings, object_id)
        if (x.session_id, x.decision_id) != (session_id, decision_id)
    ]
    touched = {session_id}
    if len(rest) == 1:
        _set(app, rest[0].session_id, rest[0].decision_id, "")
        touched.add(rest[0].session_id)
    _notify(app, touched, acting)
    return True
