"""Live meeting copilot: running notes while recording, and questions about the meeting so far.

The copilot reads the live transcript (the running LiveTranscriber's committed lines) and asks
the default LLM to *update* its previous notes with the lines added since, so each call stays
small however long the meeting runs. Notes are pushed as `copilot_notes` events and saved with
the session; the final summary gets them as a hint, and to-dos it missed become tasks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING

from ..jobs import JobContext
from ..models.session import CopilotItem, CopilotNotes
from ..models.transcript import TranscriptSegment
from ..summarization.privacy import is_cloud
from ..summarization.prompts import extract_json, transcript_lines

if TYPE_CHECKING:
    from ..api.context import AppContext

logger = logging.getLogger(__name__)

Complete = Callable[[str, str], Awaitable[str]]

NOTES_PROMPT = """\
You keep running notes for a meeting that is still in progress. You get your previous notes
(JSON) and the transcript lines spoken since. Update the notes: keep what is still true, add
what is new, drop open questions that were answered, and keep it short.
Respond with ONLY a JSON object:
{"summary": ["<3 to 6 bullets: what has been discussed so far>"],
 "decisions": ["<decisions made so far>"],
 "action_items": [{"text": "<task>", "owner": "<speaker label or name, or null>"}],
 "open_questions": ["<questions raised and not answered yet>"]}
Use speaker labels as they appear; do not invent names."""

ASK_PROMPT = """\
You answer a question about a meeting that is still in progress, from its transcript so far and
the running notes. Be brief (one to three sentences). Cite the time of the relevant line like
[12:34]. If the transcript does not cover it, say so."""

MIN_NEW_CHARS = 400  # don't call the LLM for a sentence or two
ASK_CONTEXT_CHARS = 14000  # most recent transcript sent with a question


def copilot_hint(notes: CopilotNotes | None) -> str:
    """The live notes as extra instructions for the final summary (empty when there are none)."""
    if notes is None:
        return ""
    parts = [
        ("Decided", notes.decisions),
        ("To do", [f"{a.text} ({a.owner})" if a.owner else a.text for a in notes.action_items]),
        ("Open questions", notes.open_questions),
    ]
    body = "\n".join(f"{title}: " + "; ".join(items) for title, items in parts if items)
    if not body:
        return ""
    return (
        "Notes an assistant took live during the meeting (may be incomplete or wrong; the "
        "transcript wins, use them only to avoid missing something):\n" + body
    )


def _strings(v) -> list[str]:
    return [x.strip() for x in v if isinstance(x, str) and x.strip()] if isinstance(v, list) else []


def parse_notes(raw: str, session_id: str, lines: int) -> CopilotNotes | None:
    obj = extract_json(raw) or _loose_json(raw)
    if obj is None:
        return None
    items = []
    for a in obj.get("action_items") or []:
        if isinstance(a, dict) and isinstance(a.get("text"), str) and a["text"].strip():
            owner = a.get("owner")
            owner = owner.strip() if isinstance(owner, str) and owner.strip() else None
            if owner and owner.lower() in ("null", "none", "unknown"):
                owner = None
            items.append(CopilotItem(text=a["text"].strip(), owner=owner))
        elif isinstance(a, str) and a.strip():
            items.append(CopilotItem(text=a.strip()))
    return CopilotNotes(
        session_id=session_id,
        summary=_strings(obj.get("summary")),
        decisions=_strings(obj.get("decisions")),
        action_items=items,
        open_questions=_strings(obj.get("open_questions")),
        lines=lines,
    )


def _loose_json(raw: str) -> dict | None:
    """extract_json wants a "summary" key with a string; the notes use a list."""
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _segment_dicts(segments: list[TranscriptSegment]) -> list[dict]:
    return [s.model_dump() for s in segments]


async def update_notes(
    complete: Complete,
    previous: CopilotNotes | None,
    session_id: str,
    segments: list[TranscriptSegment],
) -> CopilotNotes | None:
    """Fold the lines after `previous.lines` into the notes. None if the model's reply
    could not be read (the previous notes stay)."""
    start = previous.lines if previous else 0
    new = segments[start:]
    if not new:
        return previous
    prev_json = (
        previous.model_dump_json(include={"summary", "decisions", "action_items", "open_questions"})
        if previous
        else "{}"
    )
    user = f"Previous notes:\n{prev_json}\n\nTranscript lines since then:\n" + "\n".join(
        transcript_lines(_segment_dicts(new))
    )
    raw = await complete(NOTES_PROMPT, user)
    return parse_notes(raw, session_id, len(segments))


async def answer_live(
    complete: Complete,
    notes: CopilotNotes | None,
    segments: list[TranscriptSegment],
    question: str,
) -> str:
    lines = transcript_lines(_segment_dicts(segments))
    kept: list[str] = []
    size = 0
    for line in reversed(lines):  # most recent first, within the budget
        if size + len(line) > ASK_CONTEXT_CHARS and kept:
            break
        kept.append(line)
        size += len(line) + 1
    notes_json = (
        notes.model_dump_json(include={"summary", "decisions", "action_items", "open_questions"})
        if notes
        else "{}"
    )
    user = (
        f"Running notes:\n{notes_json}\n\n"
        f"Transcript so far{' (most recent part)' if len(kept) < len(lines) else ''}:\n"
        + "\n".join(reversed(kept))
        + f"\n\nQuestion: {question}"
    )
    return (await complete(ASK_PROMPT, user)).strip()


# ---- jobs -------------------------------------------------------------------------------


def _completer(app: AppContext) -> tuple[str, Complete]:
    st = app.settings
    provider = st.default_provider

    async def complete(system: str, user: str) -> str:
        return await app.summarizer.complete(system, user, provider, st.default_model)

    return provider, complete


def copilot_runner(app: AppContext, session_id: str, tick: float = 5.0):
    """Keeps the running notes up to date until the recording stops (the job is cancelled)."""

    async def run(ctx: JobContext) -> dict:
        provider, complete = _completer(app)
        session = app.sessions.get_session(session_id)
        if session is not None and session.local_only and is_cloud(provider):
            ctx.update("Copilot off: this meeting is local-only and the model is a cloud one")
            return {"updates": 0, "skipped": "local-only"}
        interval = max(30, app.settings.copilot_interval_seconds)
        ctx.update("Copilot listening")
        updates, last = 0, time.monotonic()
        try:
            while True:
                await asyncio.sleep(tick)
                live = app.live.get(session_id)
                if live is None:
                    continue
                segments = list(live.committed)
                notes = app.copilot_notes.get(session_id)
                new = segments[notes.lines if notes else 0 :]
                if not new:
                    continue
                due = time.monotonic() - last >= interval
                # First notes as soon as there is something to say; then at most once per
                # interval.
                if notes is None:
                    ready = due or sum(len(s.text) for s in new) >= MIN_NEW_CHARS
                else:
                    ready = due
                if not ready:
                    continue
                try:
                    fresh = await update_notes(complete, notes, session_id, segments)
                except Exception as e:  # provider down: try again next round
                    logger.warning("Copilot update failed: %s", e)
                    ctx.update(f"Copilot: {e}")
                    last = time.monotonic()
                    continue
                last = time.monotonic()
                if fresh is None:
                    continue
                app.copilot_notes[session_id] = fresh
                app.repo.update_fields(session_id, copilot_notes=fresh)  # outlives the meeting
                updates += 1
                ctx.update(f"Notes updated ({fresh.lines} lines)")
                ctx.emit(
                    {
                        "type": "copilot_notes",
                        "session_id": session_id,
                        "notes": fresh.model_dump(mode="json"),
                    }
                )
        except asyncio.CancelledError:
            return {"updates": updates}

    return run


def copilot_ask_runner(app: AppContext, session_id: str, question: str):
    async def run(ctx: JobContext) -> dict:
        provider, complete = _completer(app)
        session = app.sessions.get_session(session_id)
        if session is not None and session.local_only and is_cloud(provider):
            raise ValueError("This meeting is local-only and the default model is a cloud one")
        live = app.live.get(session_id)
        segments = list(live.committed) if live else (session.transcript if session else [])
        if not segments:
            raise ValueError("Nothing has been transcribed yet")
        ctx.update("Reading the meeting so far")
        answer = await answer_live(complete, app.copilot_notes.get(session_id), segments, question)
        return {"question": question, "answer": answer, "asked_at": datetime.now().isoformat()}

    return run
