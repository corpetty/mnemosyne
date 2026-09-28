"""Resources for meetings: links and files, in a library shared by all meetings.

A meeting lists the resources attached to it (a deck, the doc being discussed, the ticket);
one resource can be attached to several meetings, so what was shared last week is at hand this
week. Files are kept under <data_dir>/assets/<id>/ (encrypted when encryption at rest is on);
the text of text-like files (Markdown, plain text, HTML, Word, PDF with pdftotext) is kept too
and given to the summary. Links in the calendar event are attached when recording starts,
except the call's own join link.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from ..models.session import Asset

if TYPE_CHECKING:
    from ..api.context import AppContext

logger = logging.getLogger(__name__)

MAX_TEXT = 200_000  # characters kept of a file's text
TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".csv", ".json", ".log", ".yaml", ".yml", ".rst"}
# Where video calls are joined, not resources about the meeting.
JOIN_LINKS = re.compile(
    r"^https?://([^/]*\.)?(meet\.google\.com|zoom\.us/j/|[^/]*\.zoom\.us/j/|teams\.microsoft\.com"
    r"/l/meetup-join|teams\.live\.com/meet|whereby\.com|meet\.jit\.si|webex\.com|gotomeet\.me)",
    re.IGNORECASE,
)
URL = re.compile(r"https?://[^\s<>\"')\]]+")


def link_title(url: str) -> str:
    """A readable default title: the site and the last part of the path."""
    parsed = urlparse(url)
    host = parsed.netloc.removeprefix("www.")
    tail = [p for p in parsed.path.split("/") if p][-1:] if parsed.path else []
    return f"{host}/{tail[0]}"[:120] if tail else host or url[:120]


def links_in(text: str) -> list[str]:
    """Resource links in a text (an event description): not the call's join link."""
    out: list[str] = []
    for url in URL.findall(text or ""):
        url = url.rstrip(".,;:")
        if not JOIN_LINKS.match(url) and url not in out:
            out.append(url)
    return out


def extract_text(path: Path, filename: str) -> str | None:
    """The text of a text-like file (None for others, like images and audio)."""
    suffix = Path(filename).suffix.lower()
    try:
        if suffix in TEXT_SUFFIXES:
            text = path.read_text(encoding="utf-8", errors="replace")
        elif suffix in (".html", ".htm"):
            from .calendar_service import plain_text

            text = plain_text(path.read_text(encoding="utf-8", errors="replace"))
        elif suffix == ".docx":
            with zipfile.ZipFile(path) as z:
                xml = z.read("word/document.xml").decode("utf-8", errors="replace")
            xml = re.sub(r"</w:p>", "\n", xml)
            text = re.sub(r"<[^>]+>", "", xml)
        elif suffix == ".pdf" and shutil.which("pdftotext"):
            out = subprocess.run(
                ["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, timeout=60
            )
            text = out.stdout if out.returncode == 0 else ""
        else:
            return None
    except (OSError, zipfile.BadZipFile, KeyError, subprocess.SubprocessError, UnicodeError):
        logger.warning("Could not read text from %s", filename, exc_info=True)
        return None
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:MAX_TEXT] or None


def store_file(app: AppContext, asset: Asset, source: Path) -> tuple[str, str | None]:
    """Keep an uploaded file as `asset`'s: moved under the assets folder, its text read, then
    encrypted when encryption is on. Returns (stored path relative to the folder, text)."""
    from ..storage.crypto import encrypt_file

    folder = app.settings.assets_dir / asset.id
    folder.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.\- ]", "_", asset.filename or "file").strip() or "file"
    dest = folder / safe
    shutil.move(str(source), dest)
    text = extract_text(dest, safe)
    if app.settings.encrypt_at_rest and app.file_key is not None:
        dest = encrypt_file(dest, app.file_key)
    return str(dest.relative_to(app.settings.assets_dir)), text


def file_path(app: AppContext, relative: str) -> Path:
    path = (app.settings.assets_dir / relative).resolve()
    if not path.is_relative_to(app.settings.assets_dir.resolve()):
        raise ValueError("Not an asset path")
    return path


def convert_files(app: AppContext, key: bytes | None) -> int:
    """Encryption turned on (key) or off (None): encrypt or decrypt every stored file."""
    from ..storage.crypto import decrypt_file, encrypt_file, is_encrypted

    done = 0
    for asset_id, relative in app.repo.asset_paths():
        path = file_path(app, relative)
        if not path.exists():
            continue
        if key is not None and not is_encrypted(path):
            new = encrypt_file(path, key)
        elif key is None and is_encrypted(path):
            new = decrypt_file(path, app.file_key)
        else:
            continue
        app.repo.update_asset(asset_id, path=str(new.relative_to(app.settings.assets_dir)))
        done += 1
    return done


def attach_calendar_links(app: AppContext, session_id: str, description: str) -> int:
    """At recording start: the resource links in the meeting's calendar event."""
    n = 0
    for url in links_in(description)[:10]:
        asset = app.repo.find_link(url) or app.repo.add_asset(
            Asset(kind="link", title=link_title(url), url=url, source="calendar")
        )
        app.repo.attach_asset(session_id, asset.id)
        n += 1
    return n


def resources_hint(app: AppContext, session_id: str, budget: int = 8000) -> str:
    """The meeting's resources for the summary: titles and links, then excerpts of their
    text (within `budget` characters)."""
    session = app.sessions.get_session(session_id)
    if session is None or not session.assets:
        return ""
    lines = [f"- {a.title}" + (f" ({a.url})" if a.url else "") for a in session.assets]
    out = "Resources shared for this meeting:\n" + "\n".join(lines)
    left = budget
    for asset, text in app.repo.asset_texts(session_id):
        if left <= 200:
            break
        excerpt = text[: min(left, 3000)]
        out += f"\n\nExcerpt of {asset.title}:\n{excerpt}"
        left -= len(excerpt)
    return (
        out + "\n(Use the resources for context and correct names; the meeting is what is "
        "summarized.)"
    )
