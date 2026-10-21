"""Obsidian topic notes and the decisions log.

A topic note (`<subfolder>/topics/<Topic>.md`, frontmatter `mnemosyne: topic`) holds one topic
thread: where it stands, then the meetings that discussed it, oldest first. The decisions log
(`<subfolder>/Decisions.md`, `mnemosyne: decisions`) lists every meeting's decisions, newest
first. Only files carrying our marker are ever rewritten, and nothing is created when the user
has a note of that name anywhere else in the vault."""

from __future__ import annotations

import re
from collections.abc import Callable, Collection
from datetime import date
from pathlib import Path

from .links import link_known_notes
from .obsidian import note_stem, sanitize_filename, stem_of
from .vault_index import _scans, vault_stems

TOPIC_MARKER = "mnemosyne: topic"
DECISIONS_MARKER = "mnemosyne: decisions"
DECISIONS_STEM = "Decisions"


class NoteConflict(Exception):
    """The vault already has a note of the user's with this name."""


def _ours(path: Path, marker: str) -> bool:
    try:
        return marker in path.read_text(encoding="utf-8")[:200]
    except OSError:
        return False


def _check_writable(vault: Path, folder: Path, stem: str, marker: str) -> Path:
    path = folder / f"{stem}.md"
    if path.exists():
        if not _ours(path, marker):
            raise NoteConflict(f'"{path.relative_to(vault)}" is your own note; not overwritten')
    else:
        _scans.pop(vault, None)  # a fresh look: a note made minutes ago counts
        if stem in vault_stems(vault, [folder]):
            raise NoteConflict(f'The vault already has a note called "{stem}"; not overwritten')
    return path


def _linker(text_fn: Callable[[], Collection[str]] | None, redact: bool):
    from ..summarization.privacy import redact_identifiers

    names = set(text_fn()) if text_fn is not None else set()

    def prep(text: str, seen: set[str] | None = None) -> str:
        if redact:
            text = redact_identifiers(text)
        return link_known_notes(text, names, seen) if names else text

    return prep


def render_topic(
    thread,
    stems: dict[str, str],
    explanation: str = "",
    known_notes: Callable[[], Collection[str]] | None = None,
    is_mine: Callable[[str, int], bool] | None = None,
    redact: bool = False,
    today: date | None = None,
) -> str:
    """`is_mine(session_id, item index)`: is that action item a checkbox (None: plain bullets
    for all, which keeps vault-wide task queries to the user's own work)."""
    prep = _linker(known_notes, redact)
    out = [
        "---",
        TOPIC_MARKER,
        "tags: [topic, mnemosyne]",
        f"updated: {(today or date.today()).isoformat()}",
        "---",
        "",
        f"# {thread.query}",
        "",
    ]
    if explanation.strip():
        out += ["## Where it stands", "", prep(explanation.strip()), ""]
    out += ["## Meetings", ""]
    for m in thread.meetings:
        day = m.created_at.strftime("%Y-%m-%d")
        title = f"[[{stems[m.id]}|{m.name}]]" if m.id in stems else m.name
        out += [f"### {day} {title}", ""]
        if m.summary:
            out += [prep(m.summary), ""]
        lines = []
        if m.decisions:
            seen: set[str] = set()
            lines += ["**Decisions**", ""] + [f"- {prep(d, seen)}" for d in m.decisions] + [""]
        if m.action_items:
            seen = set()
            lines += ["**Action items**", ""]
            for a in m.action_items:
                box = "[x] " if a.done else "[ ] "
                checkbox = is_mine is not None and is_mine(m.id, a.idx)
                owner = f" ({a.owner})" if a.owner else ""
                lines.append(f"- {box if checkbox else ''}{prep(a.text, seen)}{owner}")
            lines.append("")
        if m.open_questions:
            seen = set()
            lines += ["**Open questions**", ""]
            lines += [f"- {prep(q, seen)}" for q in m.open_questions] + [""]
        out += lines
    return "\n".join(out).rstrip() + "\n"


def write_topic_note(
    vault: Path,
    subfolder: str,
    thread,
    stems: dict[str, str],
    explanation: str = "",
    **kwargs,
) -> Path:
    folder = vault / subfolder / "topics"
    path = _check_writable(vault, folder, sanitize_filename(thread.query), TOPIC_MARKER)
    folder.mkdir(parents=True, exist_ok=True)
    path.write_text(render_topic(thread, stems, explanation, **kwargs), encoding="utf-8")
    return path


def render_decisions(
    meetings,
    known_notes: Callable[[], Collection[str]] | None = None,
    redact: bool = False,
    index=None,
) -> str:
    """`meetings`: (summary-like with id/name/created_at, decisions) newest first. `index`:
    the decision objects the caller can read (services/decision_links.py `index`, by key).
    A decision with a newer mention in another meeting gets " → superseded <date> in
    [[note]]" (the next newer mention's note); the newest mention of a linked object gets a
    sub-bullet per earlier mention. Without links the lines are plain bullets."""
    prep = _linker(known_notes, redact)
    out = ["---", DECISIONS_MARKER, "tags: [decisions, mnemosyne]", "---", "", "# Decisions", ""]
    for s, decisions in meetings:
        if not decisions:
            continue
        day = s.created_at.strftime("%Y-%m-%d")
        out += [f"## {day} [[{note_stem(s)}|{s.name}]]", ""]
        seen: set[str] = set()
        data = getattr(s, "summary_data", None)
        ids = list(data.decision_ids) if data is not None else []
        links = dict(data.decision_links) if data is not None else {}
        for i, d in enumerate(decisions):
            line = f"- {prep(re.sub(chr(10) + '+', ' ', d), seen)}"
            extra: list[str] = []
            decision_id = ids[i] if i < len(ids) and len(ids) == len(decisions) else ""
            obj = index.get(links.get(decision_id, "")) if index and decision_id else None
            mentions = obj.mentions if obj is not None else []
            at = next(
                (
                    j
                    for j, m in enumerate(mentions)
                    if (m.session_id, m.decision_id) == (s.id, decision_id)
                ),
                None,
            )
            if at is not None and len(mentions) > 1:
                if at > 0:  # a newer mention elsewhere: this wording was superseded
                    later = mentions[at - 1]
                    line += (
                        f" → superseded {later.created_at.strftime('%Y-%m-%d')}"
                        f" in [[{stem_of(later.session_name, later.created_at)}]]"
                    )
                else:  # the current wording: what it repeats or replaced
                    for m in mentions[1:]:
                        text = prep(re.sub(chr(10) + "+", " ", m.text), set())
                        extra.append(
                            f"  - earlier: {text} ([[{stem_of(m.session_name, m.created_at)}]])"
                        )
            out += [line] + extra
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def write_decisions_log(vault: Path, subfolder: str, meetings, **kwargs) -> Path:
    folder = vault / subfolder
    path = _check_writable(vault, folder, DECISIONS_STEM, DECISIONS_MARKER)
    folder.mkdir(parents=True, exist_ok=True)
    path.write_text(render_decisions(meetings, **kwargs), encoding="utf-8")
    return path
