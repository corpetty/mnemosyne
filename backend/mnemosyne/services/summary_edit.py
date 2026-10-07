"""Editing a meeting's summary by hand: the text, topics (the tags), decisions, open questions,
action items, chapters and client facts, saved together.

Each decision and open question travels with its transcript time (`SummaryData.decision_at` /
`question_at` are parallel lists), so links to the transcript survive reordering and deletions.
The previous summary is kept as a version (storage `keep_version`), the edit goes into the
meeting's history and is sealed (records), and a `session` event lets the search index and other
windows follow. A later re-summarize replaces edits; the UI asks first (`edited_at`).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import Field

from .. import access
from ..models.base import ApiModel
from ..models.session import ActionItem, Chapter, ClientFact, Session, SummaryData
from . import history

if TYPE_CHECKING:
    from ..api.context import AppContext

logger = logging.getLogger(__name__)


class TimedText(ApiModel):
    text: str
    at: float | None = None  # seconds: where it came up in the transcript


class SummaryEdit(ApiModel):
    summary: str  # the markdown body
    topics: list[str] = Field(default_factory=list)
    decisions: list[TimedText] = Field(default_factory=list)
    open_questions: list[TimedText] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    chapters: list[Chapter] = Field(default_factory=list)
    client_facts: list[ClientFact] = Field(default_factory=list)


def _texts(items: list[TimedText]) -> tuple[list[str], list[float | None]]:
    kept = [(i.text.strip(), i.at) for i in items if i.text.strip()]
    return [t for t, _ in kept], [a for _, a in kept]


def _topics(topics: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for t in topics:
        t = " ".join(t.split()).strip().lstrip("#").strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def apply_edit(data: SummaryData | None, edit: SummaryEdit, by: str) -> SummaryData:
    """The summary data after `edit`; what the edit does not cover (style, model, source
    fingerprint, follow-up draft) stays."""
    data = data.model_copy(deep=True) if data else SummaryData(style="manual")
    data.topics = _topics(edit.topics)
    data.decisions, data.decision_at = _texts(edit.decisions)
    data.open_questions, data.question_at = _texts(edit.open_questions)
    data.action_items = [
        a.model_copy(update={"text": a.text.strip(), "owner": (a.owner or "").strip() or None})
        for a in edit.action_items
        if a.text.strip()
    ]
    data.chapters = sorted(
        (
            c.model_copy(update={"title": c.title.strip(), "start": max(0.0, c.start)})
            for c in edit.chapters
            if c.title.strip()
        ),
        key=lambda c: c.start,
    )
    data.client_facts = [
        f.model_copy(update={"text": f.text.strip()}) for f in edit.client_facts if f.text.strip()
    ]
    data.edited_at = datetime.now()
    data.edited_by = by
    return data


def edit_summary(app: AppContext, session_id: str, edit: SummaryEdit) -> Session | None:
    """Save an edited summary. Raises access.Forbidden / Hidden for someone else's meeting."""
    before = app.sessions.get_session(session_id)
    if before is None:
        return None
    who = access.principal()
    by = who.name if who else ""
    data = apply_edit(before.summary_data, edit, by)
    summary = edit.summary.strip()
    app.repo.keep_version(before, "edited")  # before update_fields, which would say "summarized"
    session = app.repo.update_fields(session_id, summary=summary, summary_data=data)
    if session is None:
        return None
    history.log(app, session_id, "summary_edited", by=by)
    from . import records

    records.seal_later(app, session_id, "summary_edited")
    app.bus.publish({"type": "session", "session_id": session_id, "status": session.status.value})
    st = app.settings
    if st.obsidian_auto_export and st.obsidian_vault_path:
        from .pipeline import _auto_export

        try:
            asyncio.get_running_loop().run_in_executor(None, _auto_export, app, session_id)
        except RuntimeError:  # no event loop (a script): export now
            _auto_export(app, session_id)
    return session


def save_followup(app: AppContext, session_id: str, text: str) -> Session | None:
    """Keep an edited follow-up draft (the summary itself does not count as edited)."""
    session = app.sessions.get_session(session_id)
    if session is None or session.summary_data is None:
        return None
    data = session.summary_data.model_copy(update={"followup": text.strip()})
    session = app.repo.update_fields(session_id, summary_data=data)
    if session is not None:
        app.bus.publish(
            {"type": "session", "session_id": session_id, "status": session.status.value}
        )
    return session
