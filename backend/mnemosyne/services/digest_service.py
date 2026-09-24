"""Digest of the meetings in a date range (usually one ISO week).

The LLM writes the overview and themes from each meeting's summary. The meeting list,
decisions and action items are appended verbatim from the stored summaries, so the
digest never invents or drops a task.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from pathlib import Path

from ..export.obsidian import note_stem
from ..models.digest import Digest
from ..models.session import Session

Complete = Callable[[str, str], Awaitable[str]]

SYSTEM_PROMPT = """\
You write a short digest of someone's meetings over a period, from the summaries provided.
Write markdown with exactly these sections:
## Overview
Three to six sentences: what the period was about and what moved forward.
## Themes
Three to eight bullets. Each names a thread that ran through the meetings, says where it
stands now, and names the meetings involved by their title.
## Watch
Up to five bullets: risks, blockers, unanswered questions or things that keep coming back.
Omit the section if there are none.
Rules: use only what the summaries say; do not invent names, dates or numbers. Do not list
action items or decisions one by one; those are appended separately. No preamble.
"""

PER_MEETING_BUDGET = 3500
TOTAL_BUDGET = 28000


def week_bounds(day: date) -> tuple[date, date]:
    """Monday..Sunday of the ISO week containing `day`."""
    start = day - timedelta(days=day.weekday())
    return start, start + timedelta(days=6)


def range_label(start: date, end: date) -> str:
    if start.weekday() == 0 and end == start + timedelta(days=6):
        year, week, _ = start.isocalendar()
        return f"{year}-W{week:02d}"
    return f"{start.isoformat()} to {end.isoformat()}"


def file_name(label: str) -> str:
    return label.replace(" to ", "--") + ".md"


def _meeting_block(s: Session) -> str:
    d = s.summary_data
    parts = [f'### "{s.name}" ({s.created_at.strftime("%a %Y-%m-%d")})', s.summary.strip()]
    if d is not None:
        if d.decisions:
            parts.append("Decisions:\n" + "\n".join(f"- {x}" for x in d.decisions))
        if d.open_questions:
            parts.append("Open questions:\n" + "\n".join(f"- {x}" for x in d.open_questions))
    block = "\n\n".join(p for p in parts if p)
    if len(block) > PER_MEETING_BUDGET:
        block = block[: PER_MEETING_BUDGET - 1].rstrip() + "…"
    return block


def prompt_for(sessions: list[Session], start: date, end: date) -> str:
    blocks: list[str] = []
    total = 0
    for s in sessions:
        b = _meeting_block(s)
        if blocks and total + len(b) > TOTAL_BUDGET:
            blocks.append(f"(… {len(sessions) - len(blocks)} more meetings not shown)")
            break
        blocks.append(b)
        total += len(b)
    return (
        f"Period: {start.isoformat()} to {end.isoformat()}, {len(sessions)} meetings.\n\n"
        + "\n\n".join(blocks)
    )


def appendix(summarized: list[Session], unsummarized: list[Session]) -> str:
    """The parts of the digest copied straight from the stored summaries."""

    def link(s: Session) -> str:
        alias = s.name.replace("|", "/").replace("[", "(").replace("]", ")")
        return f"[[{note_stem(s)}|{alias}]]"

    out = ["## Meetings"]
    for s in summarized:
        out.append(f"- {s.created_at.strftime('%a %b %d')} · {link(s)}")
    decisions = [(s, x) for s in summarized if s.summary_data for x in s.summary_data.decisions]
    if decisions:
        out += ["", "## Decisions"]
        out += [f"- {x} ({link(s)})" for s, x in decisions]
    items = [(s, a) for s in summarized if s.summary_data for a in s.summary_data.action_items]
    if items:
        out += ["", "## Action items"]
        for s, a in items:
            owner = f" ({a.owner})" if a.owner else ""
            issue = f" [issue]({a.issue_url})" if a.issue_url else ""
            box = "x" if a.done else " "
            out.append(f"- [{box}] {a.text}{owner} · {link(s)}{issue}")
    if unsummarized:
        out += ["", "## Not summarized"]
        out += [f"- {s.created_at.strftime('%a %b %d')} · {s.name}" for s in unsummarized]
    return "\n".join(out)


async def build_digest(
    sessions: list[Session],
    start: date,
    end: date,
    complete: Complete,
    provider: str = "",
    model: str = "",
) -> Digest:
    """`complete(system, user)` runs one LLM turn with the chosen provider and model."""
    summarized = [s for s in sessions if s.summary.strip()]
    unsummarized = [s for s in sessions if not s.summary.strip() and s.transcript]
    if not summarized:
        raise ValueError(
            f"No summarized meetings between {start.isoformat()} and {end.isoformat()}"
        )
    label = range_label(start, end)
    body = (await complete(SYSTEM_PROMPT, prompt_for(summarized, start, end))).strip()
    title = f"# Digest {label}"
    markdown = f"{title}\n\n{body}\n\n{appendix(summarized, unsummarized)}\n"
    return Digest(
        label=label,
        start=start,
        end=end,
        markdown=markdown,
        session_ids=[s.id for s in summarized],
        provider=provider,
        model=model,
    )


def write_to_vault(digest: Digest, vault_path: str, subfolder: str) -> Path:
    vault = Path(vault_path).expanduser()
    if not vault.exists():
        raise FileNotFoundError(f"Vault path does not exist: {vault}")
    out_dir = vault / subfolder / "digests"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / file_name(digest.label)
    front = (
        "---\n"
        f"type: digest\nstart: {digest.start.isoformat()}\nend: {digest.end.isoformat()}\n"
        f"meetings: {len(digest.session_ids)}\ntags: [digest, mnemosyne]\n"
        f"generated: {digest.created_at.isoformat(timespec='minutes')}\n"
        "---\n\n"
    )
    path.write_text(front + digest.markdown, encoding="utf-8")
    return path


def due_week(now: datetime, weekday: int, hour: int) -> tuple[date, date] | None:
    """The week a scheduled digest should cover at `now`, or None if not due yet.

    `weekday` is 0 (Monday) to 6 (Sunday); negative disables the schedule.
    """
    if weekday < 0 or weekday > 6:
        return None
    start, end = week_bounds(now.date())
    due_at = datetime.combine(start + timedelta(days=weekday), datetime.min.time()).replace(
        hour=max(0, min(hour, 23))
    )
    return (start, end) if now >= due_at else None
