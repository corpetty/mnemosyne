"""Obsidian vault exporter."""

import logging
import re
from pathlib import Path

from ..models.session import Session
from .templates import render_meeting_note

logger = logging.getLogger(__name__)


def sanitize_filename(name: str) -> str:
    """Remove characters that are problematic in filenames."""
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = name.strip(". ")
    return name or "untitled"


def note_stem(session: Session) -> str:
    """The note's file name without .md, which is also its [[wiki link]] target."""
    return f"{session.created_at.strftime('%Y-%m-%d')}-{sanitize_filename(session.name)}"


def _bookmark(session: Session, bookmark) -> tuple[float, str, str]:
    from ..models.transcript import line_at

    line = line_at(session.transcript, bookmark.at)
    said = f"{line.speaker}: {line.text}" if line is not None else ""
    return bookmark.at, said, bookmark.note


class ObsidianExporter:
    """Exports sessions as markdown files to an Obsidian vault."""

    def __init__(
        self,
        vault_path: str,
        subfolder: str = "meetings/mnemosyne",
        tags: list[str] | None = None,
        link_people: bool = True,
        include_transcript: bool = True,
        redact: bool = False,
    ):
        self.vault_path = Path(vault_path)
        self.subfolder = subfolder
        self.tags = tags
        self.link_people = link_people
        self.include_transcript = include_transcript
        self.redact = redact  # ID, account and card numbers, see privacy.redact_identifiers

    def render(self, session: Session, resources: list[str] | None = None) -> str:
        transcript = session.transcript
        if self.redact:
            from ..summarization.privacy import redact_transcript

            # Line by line first: a line may only make sense after the one before it.
            transcript = redact_transcript(transcript)
            session = session.model_copy(update={"transcript": transcript})
        duration = max((s.end for s in transcript), default=None)
        note = render_meeting_note(
            title=session.name,
            date=session.created_at,
            participants=session.participants,
            transcript_segments=[seg.model_dump() for seg in transcript],
            summary=session.summary,
            notes=session.notes,
            summary_data=session.summary_data,
            tags=self.tags,
            link_people=self.link_people,
            include_transcript=self.include_transcript,
            duration_seconds=duration,
            attendees=session.attendees,
            bookmarks=[_bookmark(session, b) for b in session.bookmarks],
            agenda=[(a.text, a.covered) for a in session.agenda],
            resources=resources,
            external_notes=[(n.source, n.text) for n in session.external_notes],
        )
        if self.redact:  # the summary, notes, action items and the rest
            from ..summarization.privacy import redact_identifiers

            note = redact_identifiers(note)
        return note

    def export(self, session: Session, resources: list[str] | None = None) -> Path:
        """Export a session to the Obsidian vault.

        Returns the path to the created markdown file.
        """
        if not self.vault_path.exists():
            raise FileNotFoundError(f"Vault path does not exist: {self.vault_path}")

        # Build output directory
        output_dir = self.vault_path / self.subfolder
        output_dir.mkdir(parents=True, exist_ok=True)

        output_path = output_dir / f"{note_stem(session)}.md"

        output_path.write_text(self.render(session, resources), encoding="utf-8")
        logger.info("Exported session %s to %s", session.id, output_path)
        return output_path
