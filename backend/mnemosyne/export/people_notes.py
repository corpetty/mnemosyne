"""Obsidian person notes: one note per person with their meetings and tasks.

Only notes Mnemosyne created (frontmatter `mnemosyne: person`) are ever rewritten, and no note
is created for someone who already has a note of that name anywhere in the vault, since
`[[Name]]` links already resolve to it."""

from __future__ import annotations

import logging
from pathlib import Path

from ..services.people import PersonDetail, is_person, person_detail
from .obsidian import note_stem, sanitize_filename

logger = logging.getLogger(__name__)

MARKER = "mnemosyne: person"


def _mmss(sec: float) -> str:
    m = int(sec // 60)
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}:{int(sec % 60):02d}"


def render_person(d: PersonDetail, stems: dict[str, str]) -> str:
    def link(sid: str, name: str) -> str:
        return f"[[{stems[sid]}|{name}]]" if sid in stems else name

    last = d.meetings[0].created_at.strftime("%Y-%m-%d") if d.meetings else "never"
    out = [
        "---",
        MARKER,
        "tags: [person, mnemosyne]",
        "---",
        "",
        f"# {d.name}",
        "",
        f"{len(d.meetings)} meetings · last seen {last}"
        + (f" · talked {_mmss(d.total_talk_seconds)} in total" if d.total_talk_seconds else ""),
        "",
    ]
    if d.open_tasks:
        out += ["## Open tasks", ""]
        out += [f"- [ ] {t.text} · {link(t.session_id, t.session_name)}" for t in d.open_tasks]
        out.append("")
    out += ["## Meetings", ""]
    for m in d.meetings:
        role = f"spoke {_mmss(m.talk_seconds)}" if m.talk_seconds else m.role
        out.append(f"- {m.created_at.strftime('%Y-%m-%d')} {link(m.id, m.name)} ({role})")
    out.append("")
    if d.done_tasks:
        out += ["## Done", ""]
        out += [f"- [x] {t.text} · {link(t.session_id, t.session_name)}" for t in d.done_tasks]
        out.append("")
    return "\n".join(out)


def write_person_notes(repo, vault: Path, subfolder: str, names, generic=()) -> list[Path]:
    wanted = {sanitize_filename(n): n for n in names if is_person(n, generic)}
    if not wanted:
        return []
    folder = vault / subfolder / "people"
    theirs = {
        p.stem
        for p in vault.rglob("*.md")
        if p.stem in wanted and p.parent != folder and ".obsidian" not in p.parts
    }
    stems = {s.id: note_stem(s) for s in repo.list_summaries()}
    written = []
    for stem, name in wanted.items():
        if stem in theirs:
            continue  # the user has their own note for this person
        path = folder / f"{stem}.md"
        if path.exists() and MARKER not in path.read_text(encoding="utf-8")[:200]:
            continue
        detail = person_detail(repo, name, generic)
        if detail is None:
            continue
        folder.mkdir(parents=True, exist_ok=True)
        path.write_text(render_person(detail, stems), encoding="utf-8")
        written.append(path)
    return written
