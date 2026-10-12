"""Stable ids for the parts of a summary that are merged one by one (services/summary_doc.py):
action items (`ActionItem.id`), decisions and open questions (`SummaryData.decision_ids` /
`question_ids`, parallel lists like `decision_at`). Rendered as Obsidian block ids `^m-<id>`."""

from __future__ import annotations

from uuid import uuid4

from ..models.session import SummaryData


def new_id() -> str:
    """8 hex characters, never all digits (identifier redaction could take a digit run)."""
    while (item_id := uuid4().hex[:8]).isdigit():
        pass
    return item_id


def _fill(ids: list[str], n: int, taken: set[str]) -> list[str]:
    out = []
    for i in range(n):
        current = ids[i] if i < len(ids) else ""
        if not current or current in taken:
            current = new_id()
            while current in taken:
                current = new_id()
        taken.add(current)
        out.append(current)
    return out


def ensure_ids(data: SummaryData) -> SummaryData:
    """Give every action item, decision and open question an id (in place; also returned):
    missing or duplicate ids are replaced, the parallel id lists are padded or cut to their
    lists' lengths. Ids that are already there stay."""
    taken: set[str] = set()
    ids = _fill([a.id for a in data.action_items], len(data.action_items), taken)
    for item, item_id in zip(data.action_items, ids, strict=True):
        item.id = item_id
    data.decision_ids = _fill(data.decision_ids, len(data.decisions), taken)
    data.question_ids = _fill(data.question_ids, len(data.open_questions), taken)
    return data
