"""Obsidian markdown export templates."""

from datetime import datetime


def render_meeting_note(
    title: str,
    date: datetime,
    participants: list[str],
    transcript_segments: list[dict],
    summary: str,
    notes: str,
) -> str:
    """Render a complete Obsidian-compatible meeting note.

    Returns markdown with YAML frontmatter.
    """
    # YAML frontmatter
    participant_list = ", ".join(f'"{p}"' for p in participants)
    frontmatter = f"""---
title: "{title}"
date: {date.strftime('%Y-%m-%d')}
type: meeting-note
source: mnemosyne
participants: [{participant_list}]
tags: [meeting, mnemosyne]
---"""

    sections = [frontmatter, f"\n# {title}\n"]

    # Summary
    if summary:
        sections.append("## Summary\n")
        sections.append(summary)
        sections.append("")

    # Action items (extracted from summary if present)
    # The summary already contains action items, so we don't duplicate

    # Participants
    if participants:
        sections.append("## Participants\n")
        for p in participants:
            sections.append(f"- {p}")
        sections.append("")

    # Notes
    if notes:
        sections.append("## Notes\n")
        sections.append(notes)
        sections.append("")

    # Transcript
    if transcript_segments:
        sections.append("## Transcript\n")
        for seg in transcript_segments:
            speaker = seg.get("speaker", "UNKNOWN")
            start = seg.get("start", 0)
            text = seg.get("text", "").strip()
            minutes = int(start // 60)
            seconds = int(start % 60)
            sections.append(f"**[{minutes:02d}:{seconds:02d}] {speaker}:** {text}\n")

    return "\n".join(sections)
