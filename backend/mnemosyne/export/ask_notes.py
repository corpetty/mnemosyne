"""Obsidian notes for Ask answers.

`<subfolder>/asks/<date> <question>.md` (frontmatter `mnemosyne: ask`) holds the question, the
answer as the model wrote it (its [1] markers stay) and the meetings it drew on. Only files
carrying our marker are rewritten."""

from __future__ import annotations

import json
from collections.abc import Callable, Collection
from pathlib import Path

from .obsidian import sanitize_filename
from .topic_notes import _check_writable, _linker

ASK_MARKER = "mnemosyne: ask"
MAX_NAME = 80


def ask_stem(ask) -> str:
    question = " ".join(ask.question.split())
    name = " ".join(sanitize_filename(question).split())[:MAX_NAME].strip(". ") or "question"
    return f"{ask.created_at.strftime('%Y-%m-%d')} {name}"


def _mmss(seconds: float) -> str:
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


def render_ask(
    ask,
    stems: dict[str, str],
    known_notes: Callable[[], Collection[str]] | None = None,
    redact: bool = False,
) -> str:
    """`stems`: session id -> note stem for the meetings that still exist."""
    prep = _linker(known_notes, redact)
    question = " ".join(ask.question.split())
    out = [
        "---",
        ASK_MARKER,
        "tags: [ask, mnemosyne]",
        f"date: {ask.created_at.strftime('%Y-%m-%d')}",
        f"question: {json.dumps(prep(question), ensure_ascii=False)}",
        "---",
        "",
        f"# {prep(question)}",
        "",
        prep(ask.answer.strip()),
        "",
    ]
    sources: dict[str, tuple[object, list[float]]] = {}
    for c in ask.citations:
        _, times = sources.setdefault(c.session_id, (c, []))
        if c.start is not None and c.start not in times:
            times.append(c.start)
    if sources:
        out += ["## Sources", ""]
        for sid, (c, times) in sources.items():
            day = c.created_at.strftime("%Y-%m-%d")
            link = f"[[{stems[sid]}|{c.session_name}]]" if sid in stems else c.session_name
            at = f" ({', '.join(_mmss(t) for t in sorted(times))})" if times else ""
            out.append(f"- {day} {link}{at}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def write_ask_note(vault: Path, subfolder: str, ask, stems: dict[str, str], **kwargs) -> Path:
    folder = vault / subfolder / "asks"
    path = _check_writable(vault, folder, ask_stem(ask), ASK_MARKER)
    folder.mkdir(parents=True, exist_ok=True)
    path.write_text(render_ask(ask, stems, **kwargs), encoding="utf-8")
    return path
