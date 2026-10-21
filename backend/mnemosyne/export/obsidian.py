"""Obsidian vault exporter."""

import logging
import re
from collections.abc import Callable, Collection, Mapping
from datetime import datetime
from pathlib import Path

from ..models.session import ActionItem, Session
from .templates import render_meeting_note

logger = logging.getLogger(__name__)


def sanitize_filename(name: str) -> str:
    """Remove characters that are problematic in filenames."""
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = name.strip(". ")
    return name or "untitled"


def stem_of(name: str, created_at: datetime) -> str:
    return f"{created_at.strftime('%Y-%m-%d')}-{sanitize_filename(name)}"


def note_stem(session: Session) -> str:
    """The note's file name without .md, which is also its [[wiki link]] target."""
    return stem_of(session.name, session.created_at)


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
        is_mine: Callable[[ActionItem], bool] | None = None,
        known_notes: Callable[[], Collection[str]] | None = None,
        annex: Mapping[str, list[str]] | None = None,
        continued: Mapping[str, str] | None = None,
    ):
        self.vault_path = Path(vault_path)
        self.subfolder = subfolder
        self.tags = tags
        self.link_people = link_people
        self.include_transcript = include_transcript
        # Which action items are checkboxes (None: all of them), see render_meeting_note.
        self.is_mine = is_mine
        # The names of vault notes that words may link to (called when a note is rendered,
        # since scanning the vault is for exports only), None: no links to the user's notes.
        self.known_notes = known_notes
        self.redact = redact  # ID, account and card numbers, see privacy.redact_identifiers
        # Lines the user added under the summary's items in the vault (services/vault_sync.py).
        self.annex = annex
        # Action item id -> the note of the later meeting with the same task: such items are
        # plain "continued in" bullets, only the latest mention is a checkbox (task_links.py).
        self.continued = continued

    def render(self, session: Session, resources: list[str] | None = None) -> str:
        transcript = session.transcript
        if self.redact:
            from ..summarization.privacy import redact_transcript

            # Line by line first: a line may only make sense after the one before it.
            transcript = redact_transcript(transcript)
            session = session.model_copy(update={"transcript": transcript})
        known = None
        if self.known_notes is not None:
            try:
                known = self.known_notes()
            except Exception:
                logger.warning("Could not read the notes of the vault", exc_info=True)
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
            is_mine=self.is_mine,
            known_notes=known,
            annex=self.annex,
            continued=self.continued,
        )
        if self.redact:  # the summary, notes, action items and the rest
            from ..summarization.privacy import redact_identifiers

            note = redact_identifiers(note)
        return note

    def export(
        self,
        session: Session,
        resources: list[str] | None = None,
        write: Callable[[Path, str], None] | None = None,
    ) -> Path:
        """Export a session to the Obsidian vault.

        `write(path, text)` replaces writing the file (it keeps the user's edits, see
        services/vault_sync.py write_note). Returns the path to the created markdown file.
        """
        if not self.vault_path.exists():
            raise FileNotFoundError(f"Vault path does not exist: {self.vault_path}")

        # Build output directory
        output_dir = self.vault_path / self.subfolder
        output_dir.mkdir(parents=True, exist_ok=True)

        output_path = output_dir / f"{note_stem(session)}.md"

        note = self.render(session, resources)
        if write is not None:
            write(output_path, note)
        else:
            output_path.write_text(note, encoding="utf-8")
        logger.info("Exported session %s to %s", session.id, output_path)
        return output_path
