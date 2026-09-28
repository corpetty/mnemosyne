"""Meetings in Obsidian's daily note: a line per meeting (time, link, what it was about) and
your open tasks from it, in a section of that day's note.

The daily note is found the way Obsidian's Daily Notes plugin finds it (.obsidian/daily-notes.json:
folder, date format, template), unless `obsidian_daily_folder` says where. The section sits
between two HTML comments; everything else in the note is left as it is. Exporting a meeting
again replaces its entry.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from ..models.session import Session

logger = logging.getLogger(__name__)

BEGIN = "<!-- mnemosyne:meetings -->"
END = "<!-- /mnemosyne:meetings -->"
HEADING = "## Meetings"


def moment_format(fmt: str, when: datetime) -> str:
    """The date in a Moment.js format (Obsidian's), for the common tokens; text in [brackets]
    is kept as it is."""
    tokens = {
        "YYYY": "%Y", "YY": "%y", "MMMM": "%B", "MMM": "%b", "MM": "%m", "M": "{m}",
        "DD": "%d", "D": "{d}", "dddd": "%A", "ddd": "%a",
    }  # fmt: skip
    out = []
    for literal, token in re.findall(r"\[([^\]]*)\]|(YYYY|YY|MMMM|MMM|MM|M|DD|D|dddd|ddd|.)", fmt):
        if literal:
            out.append(literal)
        elif token in tokens:
            out.append(
                when.strftime(tokens[token])
                .replace("{m}", str(when.month))
                .replace("{d}", str(when.day))
            )
        else:
            out.append(token)
    return "".join(out)


def daily_note_path(vault: Path, when: datetime, folder: str = "") -> tuple[Path, str | None]:
    """The daily note for a day, and the template to start it from (if Obsidian has one)."""
    config: dict = {}
    try:
        config = json.loads((vault / ".obsidian" / "daily-notes.json").read_text())
    except (OSError, ValueError):
        pass
    folder = folder or config.get("folder", "")
    name = moment_format(config.get("format") or "YYYY-MM-DD", when)
    template = config.get("template") or None
    return vault / folder / f"{name}.md", template


def _one_line(session: Session, limit: int = 160) -> str:
    text = re.sub(r"[*_`#>]", "", session.summary or "").strip()
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0] if text else ""
    return first if len(first) <= limit else first[: limit - 1].rstrip() + "…"


def entry(session: Session, link: str, me: str) -> list[str]:
    """The lines for one meeting: `- 14:00 [[note|Title]]: one line`, then your open tasks."""
    line = f"- {session.created_at:%H:%M} [[{link}|{session.name}]]"
    if about := _one_line(session):
        line += f": {about}"
    lines = [line]
    for item in session.summary_data.action_items if session.summary_data else []:
        if item.done or (item.owner or "") != me:
            continue
        due = f" 📅 {item.due.isoformat()}" if item.due else ""
        lines.append(f"    - [ ] {item.text}{due}")
    return lines


def _entries(block: list[str]) -> list[list[str]]:
    out: list[list[str]] = []
    for line in block:
        if line.startswith("- ") or not out:
            out.append([line])
        else:
            out[-1].append(line)
    return [e for e in out if any(x.strip() for x in e)]


def upsert_entry(text: str, link: str, lines: list[str]) -> str:
    """The note with this meeting's entry added or replaced in the Meetings section."""
    target = f"[[{link}|"
    if BEGIN in text and END in text:
        before, rest = text.split(BEGIN, 1)
        block, after = rest.split(END, 1)
        entries = [e for e in _entries(block.strip("\n").splitlines()) if target not in e[0]]
    else:  # a new section at the end of the note
        base = text.rstrip("\n")
        before, after, entries = (f"{base}\n\n" if base else "") + f"{HEADING}\n", "\n", []
    entries.append(lines)
    entries.sort(key=lambda e: e[0][2:7])  # by the HH:MM that starts each entry
    body = "\n".join(line for e in entries for line in e)
    return f"{before}{BEGIN}\n{body}\n{END}{after}"


def write_daily_entry(vault: Path, session: Session, link: str, me: str, folder: str = "") -> Path:
    path, template = daily_note_path(vault, session.created_at, folder)
    if path.exists():
        text = path.read_text(encoding="utf-8")
    else:
        text = ""
        if template:
            tpl = vault / (template if template.endswith(".md") else f"{template}.md")
            try:
                day = path.stem
                text = (
                    tpl.read_text(encoding="utf-8")
                    .replace("{{date}}", day)
                    .replace("{{title}}", day)
                )
            except OSError:
                logger.warning("Daily note template %s not found", tpl)
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(upsert_entry(text, link, entry(session, link, me)), encoding="utf-8")
    logger.info("Daily note %s: %s", path, session.name)
    return path
