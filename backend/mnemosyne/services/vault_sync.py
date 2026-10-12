"""Ticks come back from Obsidian: the checkboxes in the Action Items of the meeting notes we
wrote (frontmatter `source: mnemosyne`) win over our copy, and ticking an item in the app
rewrites that one line of its note, so the two never disagree on our side.

- `read_ticks` parses a note's Action Items (not "### Others": plain bullets, no boxes).
  `[x]`/`[X]` is done, `[ ]` open, any other status (Tasks' `[/]` In Progress, `[-]`
  Cancelled) is left alone.
- `apply_vault_ticks` runs before every export (api/routes/export.py `before_export`), so
  re-exporting never undoes a tick made in Obsidian, and from `sync_vault`, every few minutes
  while a vault is set, for notes whose mtime changed since the last pass.
- `write_tick` runs when an item is ticked in the app (PATCH action item).

A note that says an item is open while ours is done is only believed when the note was written
after the meeting last changed: notes exported before this existed, then ticked in the app, are
stale, and must not reopen what was finished.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from . import history
from .tasks import same_item

if TYPE_CHECKING:
    from ..api.context import AppContext
    from ..models.session import ActionItem, Session

logger = logging.getLogger(__name__)

_BOX = re.compile(r"^(\s*[-*+] \[)(.)(\])(.*?)(\r?\n)?\Z")
_SECTION_END = re.compile(r"^#{1,2}\s")  # "# " or "## ": the next section
_SUBSECTION = re.compile(r"^###\s+(.*?)\s*$")
_SOURCE = re.compile(r"""^source:\s*["']?mnemosyne["']?\s*$""")
# Obsidian Tasks' emoji fields (priority, recurrence, dates, ids): our text ends before them.
_TASKS_FIELDS = re.compile("[\u23eb\U0001f53c\U0001f53d\u23ec\U0001f53a\U0001f501\U0001f4c5"
                           "\U0001f4c6\U0001f5d3\u23f3\u231b\U0001f6eb\u2795\u2705\u274c"
                           "\U0001f194\u26d4\U0001f3c1]")  # fmt: skip
_ISSUE = re.compile(r"\s*\[issue\]\([^)]*\)\s*$")
_LINKED_OWNER = re.compile(r"\s*\(\[\[[^\]]+\]\]\)\s*$")
_DONE_DATE = re.compile("\\s*\u2705\ufe0f?\\s*\\d{4}-\\d{2}-\\d{2}")
_BLOCK_ID = re.compile(r"\s+\^[\w-]+\s*$")
_WIKILINK = re.compile(r"\[\[([^\]|]*)(?:\|([^\]]*))?\]\]")

_lock = threading.Lock()  # an export and the periodic pass must not tick the same item twice


@dataclass
class Tick:
    """One checkbox line of a note's Action Items."""

    text: str  # the item's text, without the owner, issue link and Tasks fields we add
    done: bool | None  # None: another status ([/], [-]): left as it is
    line: int  # index in the note's lines (keepends)


def is_ours(text: str) -> bool:
    """A note we exported: its frontmatter says `source: mnemosyne`."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    for line in lines[1:]:
        if line.strip() == "---":
            return False
        if _SOURCE.match(line.strip()):
            return True
    return False


def strip_links(text: str) -> str:
    """[[Note]] as Note and [[Note|Shown]] as Shown: links to the user's notes (export/links.py)
    are not part of what an item says."""
    return _WIKILINK.sub(lambda m: m.group(2) if m.group(2) is not None else m.group(1), text)


def item_text(rest: str) -> str:
    """What follows a checkbox, without the decorations: block id, Tasks fields (📅 due,
    ✅ done, ...), the issue link and a linked owner ` ([[Owner]])`; other [[links]] in the
    text are replaced by their words."""
    rest = _BLOCK_ID.sub("", rest)
    m = _TASKS_FIELDS.search(rest)
    if m:
        rest = rest[: m.start()]
    rest = _ISSUE.sub("", rest)
    rest = _LINKED_OWNER.sub("", rest)
    return strip_links(rest).strip()


def parse_ticks(text: str) -> list[Tick]:
    """The checkboxes of the "## Action Items" section (up to the next "## " heading), not
    those under "### Others"."""
    ticks: list[Tick] = []
    inside = others = False
    for n, line in enumerate(text.splitlines(keepends=True)):
        bare = line.rstrip("\r\n")
        if not inside:
            inside = bare.strip() == "## Action Items"
            continue
        if _SECTION_END.match(bare):
            break
        sub = _SUBSECTION.match(bare)
        if sub:
            others = sub.group(1) == "Others"
            continue
        if others:
            continue
        m = _BOX.match(line)
        if m is None:
            continue
        box = m.group(2)
        done = True if box in "xX" else False if box == " " else None
        ticks.append(Tick(item_text(m.group(4)), done, n))
    return ticks


def read_ticks(note_path: Path) -> list[Tick]:
    """The Action Items checkboxes of a note we exported; [] when it is missing, unreadable or
    not ours."""
    try:
        text = Path(note_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return parse_ticks(text) if is_ours(text) else []


def note_path(ctx: AppContext, session: Session, vault_path: str) -> Path:
    """Where export_session writes the meeting's note."""
    from ..export.obsidian import note_stem
    from .meeting_types import obsidian_folder

    folder = obsidian_folder(ctx.settings, session)
    return Path(vault_path).expanduser() / folder / f"{note_stem(session)}.md"


def _forms(text: str, item: ActionItem):
    yield text
    if item.owner:  # an owner not linked (a speaker label, or link_people off): "text (Ana)"
        suffix = f"({item.owner})"
        if text.endswith(suffix):
            yield text[: -len(suffix)].rstrip()


def _match(text: str, items: list[ActionItem], used: set[int]) -> int | None:
    """The first unused item with this text; failing that the first similar one (same_item)."""
    similar = None
    for i, item in enumerate(items):
        if i in used:
            continue
        wanted = strip_links(item.text)  # the note's text has none either
        for form in _forms(text, item):
            if form == wanted.strip():
                return i
            if similar is None and same_item(form, wanted):
                similar = i
    return similar


def pairs(ticks: list[Tick], items: list[ActionItem]) -> list[tuple[int, Tick]]:
    """(item index, tick) for the lines that are action items, each item at most once."""
    used: set[int] = set()
    out = []
    for tick in ticks:
        idx = _match(tick.text, items, used)
        if idx is not None:
            used.add(idx)
            out.append((idx, tick))
    return out


def apply_vault_ticks(ctx: AppContext, session: Session, vault_path: str) -> bool:
    """Take the done state of the meeting's action items from its note in the vault. True when
    anything changed (the caller reads the session again). Never raises."""
    try:
        with _lock:
            return _apply(ctx, session, note_path(ctx, session, vault_path))
    except Exception:
        logger.warning("Could not read the ticks of session %s", session.id, exc_info=True)
        return False


def _apply(ctx: AppContext, session: Session, path: Path) -> bool:
    try:
        written = datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return False
    ticks = read_ticks(path)
    if not ticks:
        return False
    session = ctx.sessions.get_session(session.id) or session  # as it is now
    items = session.summary_data.action_items if session.summary_data else []
    changed = False
    for idx, tick in pairs(ticks, items):
        item = items[idx]
        if tick.done is None or tick.done == item.done:
            continue
        if not tick.done and written <= session.updated_at:
            continue  # a note older than our tick: stale, not a reopen
        if not ctx.repo.set_action_item_done(session.id, idx, tick.done):
            continue
        item.done = tick.done
        history.log(
            ctx, session.id, "task_done" if tick.done else "task_reopened",
            task=item.text, by="Obsidian",
        )  # fmt: skip
        changed = True
    if changed:
        logger.info("Ticks from Obsidian applied to session %s", session.id)
        ctx.bus.publish(
            {"type": "session", "session_id": session.id, "status": session.status.value}
        )
    return changed


def _set_box(line: str, done: bool, today: date) -> str:
    m = _BOX.match(line)
    if m is None:
        return line
    head, box, close, rest, eol = m.groups()
    if done:
        if box in "xX":
            return line
        box = "x"
        if "\u2705" not in rest:
            rest = rest.rstrip() + f" \u2705 {today.isoformat()}"
    else:
        if box not in "xX":
            return line  # open already, or another status: left alone
        box = " "
        rest = _DONE_DATE.sub("", rest)
    return f"{head}{box}{close}{rest}{eol or ''}"


def write_tick(
    ctx: AppContext, session: Session, idx: int, done: bool, vault_path: str | None = None
) -> bool:
    """An item was ticked (or reopened) in the app: rewrite its line in the meeting's note,
    when the note exists and is ours. Everything else in the note stays as it is. True when
    the note was written. Never raises."""
    try:
        vault = vault_path or ctx.settings.obsidian_vault_path
        items = session.summary_data.action_items if session.summary_data else []
        if not vault or not 0 <= idx < len(items):
            return False
        path = note_path(ctx, session, vault)
        with _lock:
            try:
                text = path.read_bytes().decode("utf-8")
            except (OSError, UnicodeDecodeError):
                return False
            if not is_ours(text):
                return False
            tick = next((t for i, t in pairs(parse_ticks(text), items) if i == idx), None)
            if tick is None:
                return False
            lines = text.splitlines(keepends=True)
            line = _set_box(lines[tick.line], done, date.today())
            if line == lines[tick.line]:
                return False
            lines[tick.line] = line
            # The user's note: written whole or not at all, like the daily note.
            tmp = path.with_name(f".{path.name}.mnemosyne")
            tmp.write_bytes("".join(lines).encode("utf-8"))
            tmp.replace(path)
            ctx.vault_seen[str(path)] = path.stat().st_mtime_ns  # our own write: nothing new
        return True
    except Exception:
        logger.warning("Could not write a tick to the note of %s", session.id, exc_info=True)
        return False


def sync_vault(ctx: AppContext) -> int:
    """One pass: apply the ticks of every summarized meeting's note that changed since the
    last pass (by mtime, remembered in ctx.vault_seen). Returns the meetings changed."""
    from ..export.obsidian import note_stem
    from .meeting_types import available_types

    st = ctx.settings
    vault = st.obsidian_vault_path
    if not vault or ctx.locked:
        return 0
    root = Path(vault).expanduser()
    if not root.is_dir():
        return 0
    folders = {st.obsidian_subfolder} | {
        t.obsidian_folder for t in available_types(st) if t.obsidian_folder
    }
    changed = 0
    for summary in ctx.repo.list_summaries():
        if not summary.has_summary:
            continue
        stem = note_stem(summary)  # name and date are all it needs
        for folder in folders:
            path = root / folder / f"{stem}.md"
            try:
                mtime = path.stat().st_mtime_ns
            except OSError:
                continue
            key = str(path)
            if ctx.vault_seen.get(key) == mtime:
                continue
            ctx.vault_seen[key] = mtime
            session = ctx.sessions.get_session(summary.id)
            if session is None or note_path(ctx, session, vault) != path:
                continue  # another meeting's folder, or no longer there
            if apply_vault_ticks(ctx, session, vault):
                changed += 1
    return changed


async def sync_loop(ctx: AppContext, interval: float) -> None:
    """sync_vault every `interval` seconds, started with the app (AppContext)."""
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(sync_vault, ctx)
        except Exception:
            logger.exception("Reading ticks from the Obsidian vault failed")
