"""Draft a follow-up message (email or chat) from a meeting's summary."""

from __future__ import annotations

from typing import Literal

from ..models.session import Session

FollowupStyle = Literal["email", "chat"]

SYSTEM = {
    "email": """\
You write the follow-up email someone sends after a meeting, from its summary.
Format: first line "Subject: <short subject>", a blank line, then the body: one or two
sentences of thanks and recap, "Decisions" and "Next steps" as short bulleted lists (next
steps with owners in parentheses when known), open questions if any, and a one-line
sign-off without a name. Plain text, no markdown headings, under 200 words.
Use only facts from the summary. Speaker labels like SPEAKER_01 are not names; leave owners
out rather than print a label.""",
    "chat": """\
You write the short follow-up message someone posts in a team chat after a meeting, from
its summary. Two to four lines of recap, then "Next steps:" with short bullets (owners in
parentheses when known). No greeting, no sign-off, under 100 words. Use only facts from the
summary. Speaker labels like SPEAKER_01 are not names; leave owners out rather than print a
label.""",
}


def prompt_for(session: Session) -> str:
    d = session.summary_data
    parts = [
        f'Meeting: "{session.name}" on {session.created_at.strftime("%A %B %d, %Y")}',
    ]
    people = session.participants or session.attendees
    if people:
        parts.append("People: " + ", ".join(people))
    parts.append("Summary:\n" + session.summary.strip())
    if d is not None:
        if d.decisions:
            parts.append("Decisions:\n" + "\n".join(f"- {x}" for x in d.decisions))
        if d.action_items:
            parts.append(
                "Action items:\n"
                + "\n".join(
                    f"- {a.text}"
                    + (f" (owner: {a.owner})" if a.owner else "")
                    + (" [already done]" if a.done else "")
                    for a in d.action_items
                )
            )
        if d.open_questions:
            parts.append("Open questions:\n" + "\n".join(f"- {x}" for x in d.open_questions))
    return "\n\n".join(parts)


def clean(text: str) -> str:
    """Drop code fences and surrounding blank lines some models add."""
    lines = [ln for ln in text.strip().splitlines() if not ln.strip().startswith("```")]
    return "\n".join(lines).strip()
