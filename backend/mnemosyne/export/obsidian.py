"""Obsidian vault exporter."""

import logging
import re
from pathlib import Path

from ..models.session import Session
from .templates import render_meeting_note

logger = logging.getLogger(__name__)


class ObsidianExporter:
    """Exports sessions as markdown files to an Obsidian vault."""

    def __init__(
        self,
        vault_path: str,
        subfolder: str = "meetings/mnemosyne",
        tags: list[str] | None = None,
        link_people: bool = True,
        include_transcript: bool = True,
    ):
        self.vault_path = Path(vault_path)
        self.subfolder = subfolder
        self.tags = tags
        self.link_people = link_people
        self.include_transcript = include_transcript

    def render(self, session: Session) -> str:
        transcript = session.transcript
        duration = max((s.end for s in transcript), default=None)
        return render_meeting_note(
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
        )

    def _sanitize_filename(self, name: str) -> str:
        """Remove characters that are problematic in filenames."""
        name = re.sub(r'[<>:"/\\|?*]', "", name)
        name = name.strip(". ")
        return name or "untitled"

    def export(self, session: Session) -> Path:
        """Export a session to the Obsidian vault.

        Returns the path to the created markdown file.
        """
        if not self.vault_path.exists():
            raise FileNotFoundError(f"Vault path does not exist: {self.vault_path}")

        # Build output directory
        output_dir = self.vault_path / self.subfolder
        output_dir.mkdir(parents=True, exist_ok=True)

        # Build filename: YYYY-MM-DD-session-name.md
        date_str = session.created_at.strftime("%Y-%m-%d")
        safe_name = self._sanitize_filename(session.name)
        filename = f"{date_str}-{safe_name}.md"
        output_path = output_dir / filename

        output_path.write_text(self.render(session), encoding="utf-8")
        logger.info("Exported session %s to %s", session.id, output_path)
        return output_path
