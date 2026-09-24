"""Obsidian markdown export templates."""

from __future__ import annotations

from datetime import datetime

from ..models.session import SummaryData


def _yaml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _is_label(name: str) -> bool:
    return name.startswith("SPEAKER_") or name.upper() == "UNKNOWN"


def render_meeting_note(
    title: str,
    date: datetime,
    participants: list[str],
    transcript_segments: list[dict],
    summary: str,
    notes: str,
    summary_data: SummaryData | None = None,
    tags: list[str] | None = None,
    link_people: bool = True,
    include_transcript: bool = True,
    duration_seconds: float | None = None,
    attendees: list[str] | None = None,
) -> str:
    """Render a complete Obsidian-compatible note with YAML frontmatter.

    Named participants become [[wikilinks]] (never raw SPEAKER_xx labels);
    action items become tasks so Obsidian's Tasks/Dataview plugins pick them up.
    """
    tags = tags if tags is not None else ["meeting", "mnemosyne"]

    def person(name: str) -> str:
        return f"[[{name}]]" if link_people and not _is_label(name) else name

    fm = [
        "---",
        f"title: {_yaml_str(title)}",
        f"date: {date.strftime('%Y-%m-%d')}",
        f"time: {date.strftime('%H:%M')}",
        "type: meeting-note",
        "source: mnemosyne",
    ]
    if duration_seconds:
        fm.append(f"duration_minutes: {round(duration_seconds / 60)}")
    fm.append("participants: [" + ", ".join(_yaml_str(p) for p in participants) + "]")
    if link_people:
        people = [p for p in participants if not _is_label(p)]
        if people:
            fm.append("people: [" + ", ".join(_yaml_str(f"[[{p}]]") for p in people) + "]")
    if attendees:
        fm.append("attendees: [" + ", ".join(_yaml_str(person(a)) for a in attendees) + "]")
    if summary_data and summary_data.topics:
        fm.append("topics: [" + ", ".join(_yaml_str(t) for t in summary_data.topics) + "]")
    fm.append("tags: [" + ", ".join(tags) + "]")
    fm.append("---")

    sections: list[str] = ["\n".join(fm), f"\n# {title}\n"]

    if participants:
        sections.append("**Participants:** " + ", ".join(person(p) for p in participants) + "\n")

    if summary:
        sections.append("## Summary\n")
        sections.append(summary)
        sections.append("")

    if summary_data:
        if summary_data.decisions:
            sections.append("## Decisions\n")
            sections.extend(f"- {d}" for d in summary_data.decisions)
            sections.append("")
        if summary_data.action_items:
            sections.append("## Action Items\n")
            for item in summary_data.action_items:
                owner = f" ({person(item.owner)})" if item.owner else ""
                sections.append(f"- [ ] {item.text}{owner}")
            sections.append("")
        if summary_data.open_questions:
            sections.append("## Open Questions\n")
            sections.extend(f"- {q}" for q in summary_data.open_questions)
            sections.append("")

    if notes:
        sections.append("## Notes\n")
        sections.append(notes)
        sections.append("")

    if include_transcript and transcript_segments:
        sections.append("## Transcript\n")
        for seg in transcript_segments:
            speaker = seg.get("speaker", "UNKNOWN")
            start = seg.get("start", 0)
            text = seg.get("text", "").strip()
            minutes = int(start // 60)
            seconds = int(start % 60)
            sections.append(f"**[{minutes:02d}:{seconds:02d}] {speaker}:** {text}\n")

    return "\n".join(sections)
