"""A publication as one self-contained HTML file (phase 7, "a file"): the viewer's single.html
with the sealed manifest and clips inlined. Not a publication row: nothing to update or
unpublish, and it never expires. A disclosure all the same, so it is logged in the meeting's
history and sealed into its records.

The file's key is random (and then it travels in the file: whoever has the file can read it) or
derived from a password (PBKDF2, random salt; then the file alone is not enough).
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .. import access
from ..models.publication import PublicationSelection
from . import history, publishing
from . import publication_format as pf
from .origin import audio_elsewhere, is_foreign

if TYPE_CHECKING:
    from ..api.context import AppContext
    from ..jobs import JobContext

TEMPLATE = Path(__file__).resolve().parent.parent / "api" / "static" / "viewer" / "single.html"
PLACEHOLDER = "__PAYLOAD__"
MIN_PASSWORD = 8
KEEP_SECONDS = 24 * 3600  # a file waits a day for its download


class NoTemplate(RuntimeError):
    """The viewer is not built into this Mnemosyne (503)."""


def files_dir(app: AppContext) -> Path:
    return app.settings.data_dir / "exports" / "publication-files"


def template() -> str:
    try:
        text = TEMPLATE.read_text(encoding="utf-8")
    except OSError as e:
        raise NoTemplate(
            "The viewer for published meetings is not built into this Mnemosyne"
        ) from e
    if text.count(PLACEHOLDER) != 1:
        raise NoTemplate("The viewer's single-file template is not usable (no payload slot)")
    return text


def check_password(password: str | None) -> str | None:
    if password is None or password == "":
        return None
    if len(password) < MIN_PASSWORD:
        raise publishing.PublishError(f"A password needs at least {MIN_PASSWORD} characters")
    return password


def check_request(app: AppContext, session, sel: PublicationSelection, password: str | None):
    """What can be refused before the job starts."""
    from .origin import require_local_audio

    publishing.check_may_publish(app, session)
    check_password(password)
    template()
    if any(q.with_clip for q in sel.quotes):
        require_local_audio(app, session)


def safe_filename(title: str) -> str:
    name = re.sub(r"[^\w\- ]+", "", title, flags=re.ASCII).strip().replace(" ", "-")
    name = re.sub(r"-{2,}", "-", name).strip("-")[:60]
    return f"{name or 'meeting'}.html"


def payload_json(payload: dict) -> str:
    """JSON safe inside a <script> element: `<` is escaped (so no `</script>` or `<!--`)."""
    return json.dumps(payload, separators=(",", ":")).replace("<", "\\u003c")


def render(html: str, payload: dict) -> str:
    # str.replace with the slot only: the payload is never interpreted as a pattern.
    return html.replace(PLACEHOLDER, payload_json(payload), 1)


async def build(
    app: AppContext,
    session_id: str,
    sel: PublicationSelection,
    password: str | None,
    job: JobContext,
) -> dict:
    """The file job: result {token, filename, size}."""
    session = app.repo.get(session_id)
    if session is None:
        raise publishing.PublishError("The meeting is gone")
    publishing.check_may_publish(app, session)
    pw = check_password(password)
    html = template()
    pub_id, version = pf.new_id(), 1
    if pw is None:
        key = pf.new_key()
        protect: dict = {"key": pf.encode_key(key)}
    else:
        salt = pf.new_salt()
        key = pf.derive_key(pw, salt)
        protect = {"password": {"salt": pf.b64url_encode(salt), "iterations": pf.PBKDF2_ITERATIONS}}

    _, specs = publishing.build_content(app, session, sel)
    wanted = [s for s in specs if s["with_clip"]]
    refs: dict[str, pf.ClipRef] = {}
    blobs: dict[str, bytes] = {}
    padded: list[int] = []
    folder = files_dir(app)
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f"tmp-{pub_id}"
    try:
        for n, spec in enumerate(wanted):
            job.update(f"Clip {n + 1} of {len(wanted)}", 0.1 + 0.6 * n / max(1, len(wanted)))
            if is_foreign(session):
                raise publishing.PublishError(audio_elsewhere(app, session))
            if not session.audio_file:
                raise publishing.PublishError("This meeting has no audio to cut clips from")
            try:
                ogg = await publishing._cut(app, session, spec, tmp)
            except ValueError as e:
                raise publishing.PublishError(str(e)) from e
            blob, sha = pf.seal_clip(pub_id, version, ogg, key)
            blobs[sha] = blob
            padded.append(len(blob) - pf.NONCE_SIZE - pf.TAG_SIZE)
            refs[spec["id"]] = pf.ClipRef(
                sha256=sha,
                size=len(ogg),
                seconds=round(spec["end"] - spec["start"], 2),
                mime=publishing.CLIP_MIME,
                v=version,
            )
    except pf.PublicationFormatError as e:
        raise publishing.PublishError(str(e)) from e
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    try:
        pf.check_clips_total(padded)
        manifest, _ = publishing.build_manifest(app, session, sel, pub_id, version, clip_refs=refs)
        job.update("Sealing", 0.8)
        manifest_blob, _ = pf.seal_manifest(manifest, key)
    except pf.PublicationFormatError as e:
        raise publishing.PublishError(str(e)) from e

    payload = {
        "format": pf.FORMAT,
        "id": pub_id,
        "version": version,
        "manifest": pf.b64url_encode(manifest_blob),
        "clips": {sha: pf.b64url_encode(b) for sha, b in blobs.items()},
        **protect,
    }
    out = render(html, payload).encode("utf-8")
    token = _token()
    prune(folder)
    path = folder / f"{token}.html"
    path.write_bytes(out)
    filename = safe_filename(manifest.title)
    who = access.principal()
    (folder / f"{token}.json").write_text(
        json.dumps(
            {
                "owner_id": access.user_id(),
                "session_id": session_id,
                "filename": filename,
                "created": time.time(),
            }
        )
    )
    parts = ["title", *[p for p in publishing.PARTS if getattr(sel, p)]]
    if sel.attendees:
        parts.append("attendees")
    history.log(
        app,
        session_id,
        "exported_publication",
        parts=parts,
        quotes=len(sel.quotes),
        clips=len(wanted),
        names=sel.names,
        hide_identifiers=sel.hide_identifiers,
        password=pw is not None,
        by=who.name if who else "",
    )
    from .records import seal_later

    seal_later(app, session_id, "publication exported")
    job.update("Ready", 1.0)
    return {"token": token, "filename": filename, "size": len(out)}


def _token() -> str:
    import secrets

    return secrets.token_hex(16)


def prune(folder: Path) -> None:
    """Files are for downloading, not keeping."""
    if not folder.is_dir():
        return
    cutoff = time.time() - KEEP_SECONDS
    for f in folder.iterdir():
        if f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)


def find(app: AppContext, session_id: str, token: str) -> tuple[Path, Path, dict] | None:
    """(html, meta, meta contents) of a file this caller made for this meeting, else None."""
    folder = files_dir(app)
    html, meta = folder / f"{token}.html", folder / f"{token}.json"
    if not token.isalnum() or not html.is_file() or not meta.is_file():
        return None
    info = json.loads(meta.read_text())
    if info.get("session_id") != session_id:
        return None
    if info.get("owner_id") != access.user_id() and not access.is_admin():
        return None
    return html, meta, info
