"""Publishing a meeting (phase 7 of docs/plans/2026-10-12-hashseq-collaboration.md).

A publication is a curated projection of a meeting, never the record: the parts its owner picks
(`PublicationSelection`), names as they are, first names or "Person N", identifiers redacted,
quotes (a few transcript lines, maybe with their audio) but never the transcript. It is sealed
with services/publication_format.py under a random key that only the link's fragment carries
(`https://<host>/p/<id>#<key>`), so whoever serves it sees ciphertext.

Every version is stored on this computer, `<data_dir>/publications/<id>/` (meta.json, manifest,
blobs/<sha256>), which the backend serves read-only at /pub/<id> (api/routes/publishing.py) and a
file export can reuse; with `where = "host"` it is also uploaded to the publish host
(`publish_host`, publish-host/). Updates keep the id and the key, so the link stays; clips whose
span did not change are reused. Unpublishing deletes the hosted copy and the local files (copies
already opened cannot be taken back).

meta.json never holds the key. It is what /pub serves from, so serving needs no database (it
works while the meetings are locked). After an unpublish, an expiry or the meeting's deletion it
stays as a tombstone (`gone`), so readers get 410 instead of 404, for TOMBSTONE_DAYS.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from .. import access
from ..models.publication import (
    NotesImage,
    Publication,
    PublicationDefaults,
    PublicationSelection,
    QuoteSpan,
)
from ..models.session import Session
from . import clips, history, live_publications, notes_media, ours
from . import pub_live_format as plf
from . import publication_format as pf
from .notes_chips import image_lines, plain_notes
from .origin import audio_elsewhere, device_name, is_foreign

if TYPE_CHECKING:
    from ..api.context import AppContext
    from ..jobs import JobContext

logger = logging.getLogger(__name__)

MAX_HOST_DAYS = 365
TOMBSTONE_DAYS = 30
CLIP_MIME = "audio/ogg"
# A publication's images are kept in the row's `clip_blobs` next to the clips', under this prefix
# and the image's plain hash (a clip's key is a quote span, never this).
IMAGE_PREFIX = "image:"
PARTS = ("summary", "decisions", "questions", "action_items", "chapters", "topics", "notes")
_GENERIC_SPEAKER = re.compile(r"^(SPEAKER_\d+|UNKNOWN|Speaker \d+)$", re.IGNORECASE)
# Why a record version is sealed (services/records.py EVENT_SEALS), as the records list shows it.
SEAL_PUBLISHED = "published"
SEAL_UPDATED = "publication updated"
SEAL_UNPUBLISHED = "unpublished"
# The history line of a version made by the watcher (follow): no seal, see `publish(auto=)`.
EVENT_AUTO_UPDATED = "publication_auto_updated"
LIVE_NEEDS_HOST = "Live updates and comments need a publish host"
_PREVIEW_ID = "0" * 16
_ID = re.compile(r"^[0-9a-f]{16}$")


class PublishError(RuntimeError):
    """Publishing failed in a way the person can act on; the message says how."""


class Gone(LookupError):
    """The publication was unpublished, expired, or its meeting deleted (410)."""


# ---- who may ------------------------------------------------------------------------


def allowed(app: AppContext) -> bool:
    """Publishing is on: always on the desktop, on a team server once an admin allows it."""
    return not app.settings.team_mode or app.settings.allow_publishing


def check_may_publish(app: AppContext, session: Session) -> None:
    if not allowed(app):
        raise access.Forbidden("Publishing is turned off on this server; an admin can allow it")
    if not access.can_write(session.owner_id):
        raise access.Forbidden("Only the meeting's owner or an admin can publish it")


def served_by(app: AppContext, row: dict) -> str | None:
    """The name of the other computer that serves this "this computer" publication (it was
    published there and reached this one by library sync), else None."""
    if row["place"] == "local" and row.get("origin"):
        return device_name(app, row["origin"])
    return None


def served_elsewhere_message(app: AppContext, row: dict) -> str | None:
    """Why this computer can neither update nor unpublish the publication: its files are on
    another computer. A hosted publication can be changed from any computer."""
    name = served_by(app, row)
    return None if name is None else f"Published from {name}"


# ---- names ----------------------------------------------------------------------------


def _clean(name: str) -> str:
    return " ".join((name or "").split())


def meeting_people(session: Session) -> list[str]:
    """The meeting's people, for the attendee list: calendar attendees, then speakers who
    have a name (not SPEAKER_00 or UNKNOWN). In order, each once."""
    out: list[str] = []
    seen: set[str] = set()
    for name in [*session.attendees, *session.participants]:
        n = _clean(name)
        if n and not _GENERIC_SPEAKER.match(n) and n.casefold() not in seen:
            seen.add(n.casefold())
            out.append(n)
    return out


class Names:
    """Names in a publication: as they are, first names, or "Person N" (in the meeting's
    order). People left out of the attendee list are always "Person N". Full names match in
    any case; single words of a name (capitalized, 2+ letters: "Bo" too) match as written, so
    "will" stays when Will is hidden."""

    def __init__(self, people: list[str], mode: str, hidden: set[str]):
        self._full: dict[str, str] = {}  # casefolded full name -> replacement
        single: dict[str, str] = {}  # a word of a name, as written -> replacement
        n = 0
        for person in people:
            hide = mode == "hidden" or person.casefold() in hidden
            if hide:
                n += 1
                repl = f"Person {n}"
            elif mode == "first":
                repl = person.split(" ")[0]
            else:
                continue
            self._full.setdefault(person.casefold(), repl)
            words = person.split(" ")
            for w in words if len(words) > 1 else [person]:
                if len(w) >= 2 and w[0].isupper():
                    # A word two people share goes to the first who hides it (privacy first).
                    if w not in single or (hide and not single[w].startswith("Person ")):
                        single[w] = repl
        self._single = single

        def pattern(words, flags=0):
            if not words:
                return None
            alts = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
            return re.compile(r"(?<!\w)(" + alts + r")(?!\w)", flags)

        full_names = {p for p in people if " " in p and p.casefold() in self._full}
        self._full_re = pattern(full_names, re.IGNORECASE)
        self._single_re = pattern(set(single))

    def text(self, text: str) -> str:
        if not text:
            return text
        if self._full_re is not None:
            text = self._full_re.sub(lambda m: self._full[m.group(0).casefold()], text)
        if self._single_re is not None:
            text = self._single_re.sub(lambda m: self._single[m.group(0)], text)
        return text

    def name(self, name: str | None) -> str | None:
        """A speaker, an owner or an attendee: the whole name when it is one we know, else
        the names inside it ("Ana and Bo")."""
        if not name:
            return name
        n = _clean(name)
        return self._full.get(n.casefold()) or self.text(n)


# ---- the selection --------------------------------------------------------------------


def defaults(app: AppContext, session: Session) -> PublicationDefaults:
    """What the Publish dialog starts from: the live publication's selection, else the
    defaults; with who can reach a link to this computer."""
    row = app.repo.live_publication(session.id)
    if row is not None:
        sel = PublicationSelection.model_validate(row["selection"])
    else:
        sel = default_selection(app, session)
    reach, url = local_reach(app)
    host = host_url(app)
    return PublicationDefaults(
        **sel.model_dump(),
        local_reach=reach,
        local_url=url,
        host_url=host,
        host_is_ours=ours.is_our_host(host),
        notes_images=notes_images(app, session),
    )


def _own_notes(app: AppContext, session: Session) -> str:
    """The notes without the backend's sections (summary, copilot): those are curated by the
    selection, never published as the person's notes."""
    from .notes_sections import own_notes

    return own_notes(app.repo, session.id, session.notes or "")


def notes_images(app: AppContext, session: Session) -> list[NotesImage]:
    """The images of the meeting's notes, in order, each once, for the Publish dialog."""
    sizes = {r["sha256"]: r["size"] for r in app.repo.media_rows(session.id)}
    found: dict[str, NotesImage] = {}
    for line in image_lines(_own_notes(app, session)):
        if line.sha not in found:
            found[line.sha] = NotesImage(
                sha256=line.sha,
                caption=" ".join(line.caption.split()),
                width=line.width,
                height=line.height,
                size=sizes.get(line.sha, 0),
            )
    return list(found.values())


def default_selection(app: AppContext, session: Session) -> PublicationSelection:
    st = app.settings
    return PublicationSelection(
        title=session.name,
        attendees=meeting_people(session),
        where="host" if ours.publish_host(st) else "local",
        expires_days=st.publish_default_expiry_days,
    )


def _offsets(session: Session) -> dict[int, float]:
    from .parts import offsets

    return offsets(session)


def _joined_span(session: Session, q: QuoteSpan) -> tuple[float, float]:
    off = 0.0
    if q.part is not None:
        off = _offsets(session).get(q.part, 0.0)
    start, end = q.start + off, q.end + off
    return (start, end) if end >= start else (end, start)


def _lines(session: Session, start: float, end: float) -> list[int]:
    """Indexes of the transcript lines whose middle is inside [start, end]."""
    return [
        i
        for i, seg in enumerate(session.transcript)
        if start - 0.01 <= (seg.start + seg.end) / 2 <= end + 0.01
    ]


def _quote_key(part: int | None, start: float, end: float) -> str:
    return f"{'' if part is None else part}|{start:.3f}|{end:.3f}"


def _quote_id(key: str) -> str:
    return "q" + hashlib.sha256(key.encode()).hexdigest()[:8]


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> datetime:
    return datetime.now(UTC)


class Redaction:
    """The names and text rules a publication applies under a selection: `names` (as they
    are, first names, "Person N"; people not listed are always "Person N") and `text()`
    (identifiers and contacts redacted when `hide_identifiers`, then names). One
    implementation for the manifest (`build_content`) and for what readers of a live
    publication see of the comments (live_publications.threads_for_readers)."""

    def __init__(self, session: Session, sel: PublicationSelection) -> None:
        self.sel = sel
        self.people = meeting_people(session)
        self.wanted = {_clean(a).casefold() for a in sel.attendees}
        lower = {p.casefold() for p in self.people}
        hidden = {p for p in lower if p not in self.wanted}
        data = session.summary_data
        owners = [_real_name(a.owner) or "" for a in (data.action_items if data else [])]
        known = self.people + [o for o in owners if o and o.casefold() not in lower]
        self.names = Names(known, sel.names, hidden)

    def text(self, value: str) -> str:
        if self.sel.hide_identifiers:
            from ..summarization.privacy import redact_contacts, redact_identifiers

            value = redact_contacts(redact_identifiers(value))
        return self.names.text(value)


def build_content(
    app: AppContext, session: Session, sel: PublicationSelection
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """What the selection shows of the meeting, redacted: the manifest's fields except id,
    version, times and clips; and one spec per quote ({id, key, first, last, start, end,
    with_clip}, joined-timeline seconds of the clip to cut). Never the transcript."""
    redaction = Redaction(session, sel)
    people, wanted, names, text = (
        redaction.people,
        redaction.wanted,
        redaction.names,
        redaction.text,
    )
    data = session.summary_data

    content: dict[str, Any] = {
        "title": text(_clean(sel.title) or session.name),
        "date": session.created_at.date().isoformat(),
        "duration_seconds": (
            round(max(s.end for s in session.transcript), 1) if session.transcript else None
        ),
        "summary": text(session.summary) if sel.summary else "",
        "decisions": [],
        "questions": [],
        "action_items": [],
        "chapters": [],
        "topics": [],
        "notes": None,
        "attendees": [names.name(p) for p in people if p.casefold() in wanted],
        "quotes": [],
    }
    notes = _own_notes(app, session)
    if sel.notes and notes.strip():
        content["notes"], shown = _notes_text(notes, sel, text)
        if shown:  # only when there are some: a publication without images digests as it did
            content["images"] = shown
    if data is not None:
        if sel.decisions:
            content["decisions"] = [
                {
                    "id": _at(data.decision_ids, i) or f"d{i}",
                    "text": text(d),
                    "at": _at(data.decision_at, i),
                }
                for i, d in enumerate(data.decisions)
            ]
        if sel.questions:
            content["questions"] = [
                {
                    "id": _at(data.question_ids, i) or f"q{i}",
                    "text": text(q),
                    "at": _at(data.question_at, i),
                }
                for i, q in enumerate(data.open_questions)
            ]
        if sel.action_items:
            content["action_items"] = [
                {
                    "id": a.id or f"a{i}",
                    "text": text(a.text),
                    "owner": names.name(_real_name(a.owner)),
                    "done": a.done,
                    "due": a.due.isoformat() if a.due else None,
                }
                for i, a in enumerate(data.action_items)
            ]
        if sel.chapters:
            content["chapters"] = [
                {"title": text(c.title), "start": c.start} for c in data.chapters
            ]
        if sel.topics:
            content["topics"] = [text(t) for t in data.topics]

    specs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for q in sel.quotes:
        start, end = _joined_span(session, q)
        idx = _lines(session, start, end)
        if not idx:
            continue
        first, last = idx[0], idx[-1]
        lines = [session.transcript[i] for i in idx]
        cut_start = max(0.0, lines[0].start - clips.PAD)
        cut_end = max(s.end for s in lines) + clips.PAD
        key = _quote_key(q.part, cut_start, cut_end) + f"|{first}-{last}"
        if key in seen:
            continue
        seen.add(key)
        speaker = _dominant(lines)
        if speaker is not None:
            body = " ".join(s.text.strip() for s in lines)
        else:
            # One line per turn; an unnamed speaker's turn has no label.
            body = "\n".join(
                f"{_real_name(s.speaker)}: {s.text.strip()}"
                if _real_name(s.speaker)
                else s.text.strip()
                for s in lines
            )
        quote_id = _quote_id(key)
        content["quotes"].append(
            {
                "id": quote_id,
                "text": text(body),
                "speaker": names.name(_real_name(speaker)),
                "start": round(lines[0].start, 2),
                "end": round(max(s.end for s in lines), 2),
            }
        )
        specs.append(
            {
                "id": quote_id,
                "key": key,
                "first": first,
                "last": last,
                "start": cut_start,
                "end": cut_end,
                "with_clip": q.with_clip,
            }
        )
    return content, specs


def _escape_caption(caption: str) -> str:
    return re.sub(r"([\\\[\]])", r"\\\1", " ".join(caption.split()))


def _notes_text(md: str, sel: PublicationSelection, text) -> tuple[str, list[dict[str, Any]]]:
    """The notes as readers get them (`text`: names and identifiers redacted) and the images
    they show: chips become their text; an image line the selection keeps stays
    `![caption](mnemosyne:image/<value>)` (the caption redacted too), one it leaves out is
    `[image: caption]`. The shown images: [{id: plain hash, ext, width, height}], each once."""
    lines = image_lines(md)
    if not lines:
        return text(plain_notes(md)), []
    chosen = None if sel.images is None else set(sel.images)
    out: list[str] = []
    shown: dict[str, dict[str, Any]] = {}
    last = 0
    for line in lines:
        out.append(text(plain_notes(md[last : line.start])))
        if chosen is None or line.sha in chosen:
            value = f"{line.sha}.{line.ext}@{line.width}x{line.height}"
            out.append(f"![{_escape_caption(text(line.caption))}](mnemosyne:image/{value})")
            shown.setdefault(
                line.sha,
                {"id": line.sha, "ext": line.ext, "width": line.width, "height": line.height},
            )
        else:
            out.append(text(plain_notes(md[line.start : line.end])))
        last = line.end
    out.append(text(plain_notes(md[last:])))
    return "".join(out), list(shown.values())


def _real_name(name: str | None) -> str | None:
    """A person's name, or None when it is empty or still a diarizer label (SPEAKER_01,
    UNKNOWN): those are never published. Renaming a speaker rewrites the transcript and the
    summary's owners in place (repo.relabel_session_speaker), so there is no other name to
    look up for a label that is left."""
    n = _clean(name or "")
    return None if not n or _GENERIC_SPEAKER.match(n) else n


def _at(values: list, i: int):
    return values[i] if i < len(values) else None


def _dominant(lines) -> str | None:
    """The speaker of at least two thirds of the quote's words, else None."""
    counts: dict[str, int] = {}
    for s in lines:
        counts[s.speaker] = counts.get(s.speaker, 0) + max(1, len(s.text.split()))
    speaker, n = max(counts.items(), key=lambda kv: kv[1])
    if n * 3 >= sum(counts.values()) * 2 and not _GENERIC_SPEAKER.match(speaker or ""):
        return speaker
    return None


def digest(content: dict[str, Any], specs: list[dict[str, Any]]) -> str:
    """What a version shows, as a hash: a different one means the published copy is behind."""
    clipped = sorted(s["key"] for s in specs if s["with_clip"])
    return hashlib.sha256(pf.canonical_json({"content": content, "clips": clipped})).hexdigest()


def build_manifest(
    app: AppContext,
    session: Session,
    selection: PublicationSelection,
    pub_id: str,
    version: int,
    *,
    published_at: datetime | None = None,
    expires_at: datetime | None = None,
    clip_refs: dict[str, pf.ClipRef] | None = None,
    image_refs: dict[str, pf.ImageRef] | None = None,
    live: pf.LiveKeys | None = None,
) -> tuple[pf.Manifest, list[dict[str, Any]]]:
    """The manifest for this selection (clips from `clip_refs`, by quote id; images from
    `image_refs`, by plain hash; an image without one is not listed) and the quotes' clip
    specs. `live`: the owner's public keys, given while `follow` or `comments` is on. The
    Manifest model has no field a transcript could ride in."""
    content, specs = build_content(app, session, selection)
    refs = clip_refs or {}
    images = image_refs or {}
    shown = content.pop("images", [])
    for q in content["quotes"]:
        q["clip"] = refs.get(q["id"])
    manifest = pf.Manifest(
        id=pub_id,
        version=version,
        published_at=_iso(published_at or _now()),
        expires_at=_iso(expires_at) if expires_at else None,
        images=[images[i["id"]] for i in shown if i["id"] in images],
        live=live,
        **content,
    )
    return manifest, specs


def preview(app: AppContext, session: Session, selection: PublicationSelection) -> dict:
    """The manifest as readers would get it, unsealed, for the Publish dialog. A quote with a
    clip carries `clip.preview_url` (this backend's audio, with a media fragment for the span)
    instead of a sealed blob; an image carries `preview_url` (the notes' media route) instead of
    its sealed blob's fields. Both need the API token like any /api URL."""
    live = app.repo.live_publication(session.id)  # the version a publish would make
    version = (live["version"] if live else 0) + 1
    manifest, specs = build_manifest(app, session, selection, _PREVIEW_ID, version)
    out = manifest.model_dump(mode="json")
    by_id = {s["id"]: s for s in specs}
    has_audio = bool(session.audio_file) or is_foreign(session)
    for q in out["quotes"]:
        spec = by_id[q["id"]]
        if spec["with_clip"] and has_audio:
            q["clip"] = {
                "preview_url": (
                    f"/api/audio/file/{session.id}#t={spec['start']:.2f},{spec['end']:.2f}"
                ),
                "seconds": round(spec["end"] - spec["start"], 1),
                "mime": CLIP_MIME,
            }
    content, _ = build_content(app, session, selection)
    out["images"] = [
        {
            "id": i["id"],
            "width": i["width"],
            "height": i["height"],
            "mime": notes_media.MEDIA_TYPES[i["ext"]],
            "preview_url": f"/api/sessions/{session.id}/notes/media/{i['id']}",
        }
        for i in content.get("images", [])
    ]
    return out


# ---- the local store ------------------------------------------------------------------


def store_root(app: AppContext) -> Path:
    return app.settings.data_dir / "publications"


def pub_dir(app: AppContext, pub_id: str) -> Path:
    return store_root(app) / pf.check_id(pub_id)


def _write_json(path: Path, value: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def read_meta(app: AppContext, pub_id: str) -> dict | None:
    try:
        return json.loads((pub_dir(app, pub_id) / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, pf.PublicationFormatError):
        return None


def bury(app: AppContext, pub_id: str, why: str, session_id: str = "") -> None:
    """Remove a publication's files, leaving meta.json as a tombstone (410 for readers)."""
    folder = pub_dir(app, pub_id)
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    _write_json(
        folder / "meta.json",
        {"id": pub_id, "session_id": session_id, "gone": why, "at": int(time.time())},
    )


def _live_meta(app: AppContext, pub_id: str) -> dict:
    """meta.json of a publication readers may load; LookupError (404) or Gone (410)."""
    try:
        pf.check_id(pub_id)
    except pf.PublicationFormatError as e:
        raise LookupError(pub_id) from e
    meta = read_meta(app, pub_id)
    if meta is None:
        raise LookupError(pub_id)
    if meta.get("gone"):
        raise Gone(pub_id)
    expires = meta.get("expires")
    if expires is not None and time.time() >= expires:
        raise Gone(pub_id)
    return meta


def local_manifest(app: AppContext, pub_id: str) -> tuple[dict, bytes]:
    meta = _live_meta(app, pub_id)
    try:
        return meta, (pub_dir(app, pub_id) / "manifest").read_bytes()
    except OSError as e:
        raise LookupError(pub_id) from e


def local_blob_path(app: AppContext, pub_id: str, sha: str) -> Path:
    meta = _live_meta(app, pub_id)
    if not re.fullmatch(r"[0-9a-f]{64}", sha) or sha not in meta.get("blobs", []):
        raise LookupError(sha)
    path = pub_dir(app, pub_id) / "blobs" / sha
    if not path.is_file():
        raise LookupError(sha)
    return path


# ---- the publish host -----------------------------------------------------------------


def _host_message(status: int, body: dict, what: str) -> str:
    code = str(body.get("error") or body.get("detail") or "")
    if status == 401:
        return "The publish host wants this publication's token and none was sent"
    if status == 403 and code == "upload_token_required":
        return (
            "The publish host only takes uploads with its upload token: "
            "set it in Settings (publish host token)"
        )
    if status == 403:
        return (
            "The publish host refused this publication's token "
            f"(was it published from another app?){': ' + code if code else ''}"
        )
    if status == 409 and body.get("missing"):
        return f"The publish host is missing {len(body['missing'])} file(s) of this publication"
    if status == 409:
        return f"The publish host has a newer version (version {body.get('current')})"
    if status == 413:
        return "This publication is too large for the publish host"
    if status == 507:
        return "The publish host is full"
    if status == 429:
        return "The publish host is busy; try again in a minute"
    return f"The publish host refused the {what}: {status} {code}".rstrip()


async def _host(
    app: AppContext,
    method: str,
    url: str,
    token: str,
    *,
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
):
    """One request to the publish host, retried once on a network error, a 5xx (not 507) or a
    short 429."""
    import httpx

    hdrs = {**({"Authorization": f"Bearer {token}"} if token else {}), **(headers or {})}
    configured = app.settings.publish_host.strip().rstrip("/")
    # the upload token belongs to the publish host the person set, never to Mnemosyne's
    if app.settings.publish_host_token and configured and url.startswith(configured):
        hdrs["X-Upload-Token"] = app.settings.publish_host_token
    if content is not None:
        hdrs["Content-Type"] = "application/octet-stream"
    for attempt in (0, 1):
        try:
            async with httpx.AsyncClient(timeout=120, transport=app.http_transport) as client:
                resp = await client.request(method, url, content=content, headers=hdrs)
        except httpx.HTTPError as e:
            if attempt:
                raise PublishError(f"Could not reach the publish host: {e}") from e
            await asyncio.sleep(1)
            continue
        if attempt == 0 and (resp.status_code >= 500 and resp.status_code != 507):
            await asyncio.sleep(1)
            continue
        if attempt == 0 and resp.status_code == 429:
            try:
                wait = float(resp.headers.get("retry-after", "60"))
            except ValueError:
                wait = 60.0
            if wait <= 10:
                await asyncio.sleep(wait)
                continue
        return resp
    return resp  # pragma: no cover


def _body(resp) -> dict:
    try:
        body = resp.json()
        return body if isinstance(body, dict) else {}
    except ValueError:
        return {}


async def _host_upload(
    app: AppContext,
    host_url: str,
    pub_id: str,
    token: str,
    version: int,
    expires: int,
    manifest: bytes,
    blobs: dict[str, bytes],
    referenced: list[str],
    progress=None,
) -> None:
    """Blobs first (only `blobs`, the ones the host does not hold yet), then the manifest
    naming every blob it references (`referenced`)."""
    base = f"{host_url}/pub/{pub_id}"
    for n, (sha, blob) in enumerate(blobs.items()):
        resp = await _host(app, "PUT", f"{base}/{sha}", token, content=blob)
        if resp.status_code not in (200, 201, 204):
            raise PublishError(_host_message(resp.status_code, _body(resp), "upload"))
        if progress is not None:
            progress(n + 1, len(blobs))
    resp = await _host(
        app,
        "PUT",
        base,
        token,
        content=manifest,
        headers={
            "X-Pub-Version": str(version),
            "X-Pub-Expires": str(expires),
            "X-Pub-Blobs": ",".join(referenced),
        },
    )
    if resp.status_code == 409 and _body(resp).get("current") == version:
        # A retry of a PUT that was applied, or the other computer publishing the same version
        # at the same time: only ours if the host holds exactly this manifest.
        held = await _host(app, "GET", base, "")
        if held.headers.get("etag", "").strip('"') == hashlib.sha256(manifest).hexdigest():
            return
        raise PublishError(
            f"Another computer published version {version} at the same time; publish again"
        )
    if resp.status_code not in (200, 201, 204):
        raise PublishError(_host_message(resp.status_code, _body(resp), "upload"))


async def _host_delete(app: AppContext, host_url: str, pub_id: str, token: str) -> None:
    resp = await _host(app, "DELETE", f"{host_url}/pub/{pub_id}", token)
    if resp.status_code not in (200, 204, 404, 410):
        raise PublishError(_host_message(resp.status_code, _body(resp), "deletion"))


async def _fetch_blob(
    app: AppContext, host_url: str, pub_id: str, sha: str, blobs_dir: Path
) -> str:
    """Fetch a sealed clip or image from the publish host into `blobs_dir`, checking its
    SHA-256 (the name it is stored under). "" when it is there, else what went wrong."""
    try:
        resp = await _host(app, "GET", f"{host_url}/pub/{pub_id}/{sha}", "")
    except PublishError as e:
        return str(e)
    if resp.status_code != 200:
        return _host_message(resp.status_code, _body(resp), "download of a file")
    if hashlib.sha256(resp.content).hexdigest() != sha:
        return "The publish host sent a file that does not match; try again"
    blobs_dir.mkdir(parents=True, exist_ok=True)
    tmp = blobs_dir / f"{sha}.tmp"
    tmp.write_bytes(resp.content)
    tmp.replace(blobs_dir / sha)
    return ""


# ---- publications as the API shows them ------------------------------------------------


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def link(app: AppContext, row: dict, local_base: str = "") -> str:
    """The reader's link, key in the fragment (never sent to a server)."""
    if row["place"] == "host":
        base = row["host_url"]
    else:
        base = local_address(app) or local_base
    return f"{base.rstrip('/')}/p/{row['id']}#{pf.encode_key(row['key'])}"


def local_reach(app: AppContext) -> tuple[str, str]:
    """Who can open this computer's /p/ pages, and at what address: ("team", the team
    listener's URL, its Tailscale name first) while it runs; ("phone_url", that base) when one
    is set; else ("this_device", ""): only 127.0.0.1, nobody else can open the link."""
    st = app.settings
    if app.team_host.running:
        from .team_host import lan_addresses

        host = app.team_host.tailscale or next(iter(lan_addresses()), "")
        if host:
            return "team", f"https://{host}:{st.team_port}"
    if st.phone_url.strip():
        return "phone_url", st.phone_url.strip().rstrip("/")
    return "this_device", ""


def local_address(app: AppContext) -> str:
    """Where others reach this computer's /p/ pages; "" when nobody can (local_reach)."""
    return local_reach(app)[1]


def is_behind(app: AppContext, row: dict, session: Session) -> bool:
    if row.get("unpublished_at"):
        return False
    try:
        sel = PublicationSelection.model_validate(row["selection"])
        content, specs = build_content(app, session, sel)
    except Exception:
        logger.warning("Could not compare publication %s", row["id"], exc_info=True)
        return False
    return digest(content, specs) != row.get("published_digest", "")


def to_model(app: AppContext, row: dict, session: Session, local_base: str = "") -> Publication:
    may_see_link = access.can_write(session.owner_id)
    elsewhere = served_by(app, row)
    return Publication(
        id=row["id"],
        session_id=row["session_id"],
        where=row["place"],
        host_url=row["host_url"],
        url=(
            link(app, row, local_base)
            if may_see_link and not row["unpublished_at"] and elsewhere is None
            else None
        ),
        version=row["version"],
        selection=PublicationSelection.model_validate(row["selection"]),
        created_at=_dt(row["created_at"]),
        updated_at=_dt(row["updated_at"]),
        expires_at=_dt(row["expires_at"]),
        unpublished_at=_dt(row["unpublished_at"]),
        behind=is_behind(app, row, session),
        published_by=row.get("published_by", ""),
        local_reach=local_reach(app)[0],
        served_by=elsewhere,
        **_live_fields(app, row),
    )


def _live_fields(app: AppContext, row: dict) -> dict[str, Any]:
    """`Publication.live`, `live_reason` and `readers`: "off" unless `follow` or `comments` is
    on, else what the bridge says (services/live_publications.py). Never the owner keys."""
    sel = row["selection"]
    if row.get("unpublished_at") or not (sel.get("follow") or sel.get("comments")):
        return {"live": "off", "live_reason": "", "readers": 0}
    state, reason, readers = live_publications.status(app, row)
    return {"live": state, "live_reason": reason, "readers": readers}


# ---- publishing -----------------------------------------------------------------------


def _expiry(app: AppContext, sel: PublicationSelection, now: datetime) -> datetime | None:
    days = sel.expires_days
    if sel.where == "host":
        if days is None:
            raise PublishError(
                f"A link on the publish host must expire (at most {MAX_HOST_DAYS} days)"
            )
        days = min(days, MAX_HOST_DAYS)
    if days is None:
        return None
    return now + timedelta(days=max(1, days))


def host_url(app: AppContext) -> str:
    """The publish host: `publish_host`, else Mnemosyne's while `use_our_servers` is on
    (services/ours.py); "" when there is none."""
    return ours.publish_host(app.settings)


def stamp_moderation(
    sel: PublicationSelection, old: dict | None, now: datetime
) -> PublicationSelection:
    """`moderated_since` is the server's, never the caller's: None while `moderate` is off; the
    moment `moderate` went on (kept while it stays on across versions); so the comments made
    before it, which were shown to every reader, count as approved
    (live_publications.threads_for_readers)."""
    since = None
    if sel.moderate:
        before = (old or {}).get("selection") or {}
        kept = before.get("moderated_since") if before.get("moderate") else None
        since = kept or _iso(now)
    if sel.moderated_since == since:
        return sel
    return sel.model_copy(update={"moderated_since": since})


def check_live(session: Session, sel: PublicationSelection) -> None:
    """PublishError when `follow` or `comments` is asked for where it cannot work: a "this
    computer" publication (no room there in v1) or a meeting someone shared with us (a reader's
    comment would reach the sharer as ours)."""
    if not (sel.follow or sel.comments):
        return
    if sel.where != "host":
        raise PublishError(LIVE_NEEDS_HOST)
    if session.shared_from:
        raise PublishError(
            "Someone shared this meeting with you: its link cannot be kept up to date "
            "or take comments"
        )


def check_request(app: AppContext, session: Session, sel: PublicationSelection) -> None:
    """What can be refused before the job starts: Forbidden, PublishError, or a 409 (raised as
    HTTPException): the publication's files are on another computer ("Published from <name>"),
    or new clips of a meeting whose audio is elsewhere."""
    from .origin import require_local_audio

    check_may_publish(app, session)
    check_live(session, sel)
    live = app.repo.live_publication(session.id)
    if live is not None and (why := served_elsewhere_message(app, live)):
        raise HTTPException(409, why)
    if sel.where == "host" and not host_url(app):
        raise PublishError(
            "No publish host is set (Settings → publish host), and Mnemosyne's servers are off"
        )
    _expiry(app, sel, _now())
    if any(q.with_clip for q in sel.quotes) and is_foreign(session):
        row = app.repo.live_publication(session.id)
        _, specs = build_content(app, session, sel)
        reusable = (row or {}).get("clip_blobs", {})
        if any(s["with_clip"] and s["key"] not in reusable for s in specs):
            require_local_audio(app, session)
    if sel.notes:
        content, _ = build_content(app, session, sel)
        reusable_images = {
            k[len(IMAGE_PREFIX) :]
            for k in ((app.repo.live_publication(session.id) or {}).get("clip_blobs") or {})
            if k.startswith(IMAGE_PREFIX)
        }
        require_images(app, session, content, reusable_images)


def require_images(
    app: AppContext, session: Session, content: dict[str, Any], reusable: set[str] = frozenset()
) -> None:
    """PublishError when an image the notes show here (`content["images"]`) is neither among
    the `reusable` hashes (already in the publication) nor stored on this computer."""
    for item in content.get("images", []):
        if item["id"] not in reusable and notes_media.path(app, session.id, item["id"]) is None:
            raise PublishError(
                "An image of the notes is not on this computer yet; "
                "leave it out of the publication, or try again once it has arrived"
            )


async def read_image(app: AppContext, session: Session, sha: str) -> bytes:
    """The stored bytes of one of the notes' images (decrypted), checked against its hash."""
    try:
        data = await asyncio.to_thread(notes_media.read, app, session.id, sha)
    except FileNotFoundError as e:
        raise PublishError(
            "An image of the notes is not on this computer yet; "
            "leave it out of the publication, or try again once it has arrived"
        ) from e
    except PermissionError as e:
        raise PublishError("The notes' images are locked; unlock Mnemosyne first") from e
    if hashlib.sha256(data).hexdigest() != sha:
        raise PublishError("An image of the notes is damaged on this computer")
    return data


async def seal_image(
    app: AppContext, session: Session, item: dict[str, Any], pub_id: str, version: int, key: bytes
) -> tuple[bytes, str, dict[str, Any]]:
    """Seal one of `content["images"]` for the publication: (blob, its address, the record kept
    in the row's clip_blobs)."""
    data = await read_image(app, session, item["id"])
    try:
        blob, sha = pf.seal_image(pub_id, version, data, key)
    except pf.PublicationFormatError as e:
        raise PublishError(str(e)) from e
    return (
        blob,
        sha,
        {
            "sha256": sha,
            "size": len(data),
            "mime": notes_media.MEDIA_TYPES[item["ext"]],
            "v": version,
            "padded": len(blob) - pf.NONCE_SIZE - pf.TAG_SIZE,
        },
    )


def image_ref(item: dict[str, Any], rec: dict[str, Any]) -> pf.ImageRef:
    return pf.ImageRef(
        id=item["id"],
        sha256=rec["sha256"],
        size=rec["size"],
        mime=rec["mime"],
        v=rec["v"],
        width=item["width"],
        height=item["height"],
    )


async def _cut(app: AppContext, session: Session, spec: dict, tmp: Path) -> bytes:
    path = await clips.cut_clip(session, tmp, spec["first"], spec["last"], app.file_key)
    if path.name.endswith(".enc"):
        from ..storage.crypto import EncryptedFile

        return await asyncio.to_thread(EncryptedFile(path, app.file_key).read)
    return await asyncio.to_thread(path.read_bytes)


async def publish(
    app: AppContext,
    session_id: str,
    selection: PublicationSelection,
    job: JobContext,
    local_base: str = "",
    *,
    auto: bool = False,
) -> dict:
    """The publish job: a new publication, or a new version of the meeting's live one.
    `local_base`: this backend's address as the caller reached it, for links to this computer
    when it has no other (local_address). `auto`: the watcher's version of a publication kept
    up to date (services/live_publications.py): the same work, but it is logged as
    `publication_auto_updated`, adds no seal (the seal is on a person's acts: publish, update,
    unpublish) and keeps `published_by`."""
    session = app.repo.get(session_id)
    if session is None:
        raise PublishError("The meeting is gone")
    check_may_publish(app, session)
    check_live(session, selection)
    sel = selection
    now = _now()
    old = app.repo.live_publication(session_id)
    if old is not None and old["expires_at"] and _dt(old["expires_at"]) <= now:
        # Expired (the sweep has not run yet): its link stays dead, this is a new publication.
        app.repo.mark_unpublished(old["id"], old["expires_at"], touch=False)  # put() below touches
        bury(app, old["id"], "expired", session_id)
        old = None
    if old is not None and (why := served_elsewhere_message(app, old)):
        raise PublishError(why)
    if auto and old is None:
        raise PublishError("There is no publication to update")
    if old is None:
        row: dict[str, Any] = {
            "id": pf.new_id(),
            "session_id": session_id,
            "key": pf.new_key(),
            "token": pf.new_token(),
            "place": sel.where,
            "host_url": "",
            "version": 0,
            "created_at": now.isoformat(),
            "clip_blobs": {},
            "origin": "",
            "sign_key": None,
            "box_key": None,
        }
    else:
        row = dict(old)
    pub_id, key = row["id"], row["key"]
    sel = stamp_moderation(sel, old, now)
    # The owner keys: made once, when `follow` or `comments` is first on, then kept across
    # versions and while the switches are off; the manifest names the public halves only while
    # one is on.
    live: pf.LiveKeys | None = None
    if sel.follow or sel.comments:
        if not (row.get("sign_key") and row.get("box_key")):
            made = plf.OwnerKeys.new()
            row["sign_key"], row["box_key"] = made.sign, made.box
        live = plf.OwnerKeys(sign=row["sign_key"], box=row["box_key"]).live()
    version = row["version"] + 1
    expires_at = _expiry(app, sel, now)
    target_host = host_url(app) if sel.where == "host" else ""
    if sel.where == "host" and not target_host:
        raise PublishError(
            "No publish host is set (Settings → publish host), and Mnemosyne's servers are off"
        )
    old_host = row["host_url"] if row["place"] == "host" and row["version"] else ""
    folder = pub_dir(app, pub_id)
    blobs_dir = folder / "blobs"
    blobs_dir.mkdir(parents=True, exist_ok=True)

    # The hosted copy goes first when it moves (to this computer, or another host).
    if old_host and old_host != target_host:
        job.update("Removing the copy on the old host", 0.05)
        await _host_delete(app, old_host, pub_id, row["token"])

    # Clips: reused when their span is unchanged and their blob is here, else cut (origin only).
    content, specs = build_content(app, session, sel)
    if sel.follow and sel.images is None:
        # Kept up to date: "all of the notes' images" becomes the list published now, so a
        # picture pasted in later never goes out unseen (the dialog lists it as unticked).
        sel = sel.model_copy(update={"images": [i["id"] for i in content.get("images", [])]})
    stored: dict[str, dict] = row.get("clip_blobs") or {}
    old_clips = {k: v for k, v in stored.items() if not k.startswith(IMAGE_PREFIX)}
    old_images = {
        k[len(IMAGE_PREFIX) :]: v for k, v in stored.items() if k.startswith(IMAGE_PREFIX)
    }
    new_clips: dict[str, dict] = {}
    new_images: dict[str, dict] = {}
    image_refs: dict[str, pf.ImageRef] = {}
    refs: dict[str, pf.ClipRef] = {}
    fresh: dict[str, bytes] = {}  # sha -> blob, sealed in this version
    wanted = [s for s in specs if s["with_clip"]]
    tmp = folder / "tmp"
    try:
        for n, spec in enumerate(wanted):
            job.update(f"Clip {n + 1} of {len(wanted)}", 0.1 + 0.5 * n / max(1, len(wanted)))
            prev = old_clips.get(spec["key"])
            have = prev is not None and (blobs_dir / prev["sha256"]).is_file()
            fetch_error = ""
            if prev is not None and not have and row["place"] == "host" and row["host_url"]:
                # Published from another computer: its clip is on the host, which serves blobs
                # to anyone (they are sealed), so this one can keep it without the audio.
                fetch_error = await _fetch_blob(
                    app, row["host_url"], pub_id, prev["sha256"], blobs_dir
                )
                have = not fetch_error
            if prev is not None and have:
                new_clips[spec["key"]] = prev
            else:
                if fetch_error and is_foreign(session):
                    raise PublishError(fetch_error)
                if is_foreign(session):
                    raise PublishError(audio_elsewhere(app, session))
                if not session.audio_file:
                    raise PublishError("This meeting has no audio to cut clips from")
                try:
                    ogg = await _cut(app, session, spec, tmp)
                except ValueError as e:
                    raise PublishError(str(e)) from e
                blob, sha = pf.seal_clip(pub_id, version, ogg, key)
                fresh[sha] = blob
                new_clips[spec["key"]] = {
                    "sha256": sha,
                    "size": len(ogg),
                    "seconds": round(spec["end"] - spec["start"], 2),
                    "v": version,
                    "padded": len(blob) - pf.NONCE_SIZE - pf.TAG_SIZE,
                }
            c = new_clips[spec["key"]]
            refs[spec["id"]] = pf.ClipRef(
                sha256=c["sha256"], size=c["size"], seconds=c["seconds"], mime=CLIP_MIME, v=c["v"]
            )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # Images of the notes: reused when sealed already (here, or on the host), else sealed now.
    shown = content.get("images", [])
    for n, item in enumerate(shown):
        job.update(f"Image {n + 1} of {len(shown)}", 0.6 + 0.05 * n / max(1, len(shown)))
        prev = old_images.get(item["id"])
        have = prev is not None and (blobs_dir / prev["sha256"]).is_file()
        if prev is not None and not have and row["place"] == "host" and row["host_url"]:
            have = not await _fetch_blob(app, row["host_url"], pub_id, prev["sha256"], blobs_dir)
        if prev is not None and have:
            new_images[item["id"]] = prev
        else:
            blob, sha, rec = await seal_image(app, session, item, pub_id, version, key)
            fresh[sha] = blob
            new_images[item["id"]] = rec
        image_refs[item["id"]] = image_ref(item, new_images[item["id"]])
    try:
        pf.check_clips_total(c["padded"] for c in [*new_clips.values(), *new_images.values()])
        manifest, _ = build_manifest(
            app,
            session,
            sel,
            pub_id,
            version,
            published_at=now,
            expires_at=expires_at,
            clip_refs=refs,
            image_refs=image_refs,
            live=live,
        )
        job.update("Sealing", 0.65)
        manifest_blob, manifest_sha = pf.seal_manifest(manifest, key)
    except pf.PublicationFormatError as e:
        raise PublishError(str(e)) from e
    referenced = list(
        dict.fromkeys(c["sha256"] for c in [*new_clips.values(), *new_images.values()])
    )

    # Blobs are content-addressed: writing new ones first is harmless if the rest fails.
    for sha, blob in fresh.items():
        (blobs_dir / sha).write_bytes(blob)

    if target_host:
        job.update("Uploading", 0.7)
        same_host = old_host == target_host
        upload = {
            sha: (fresh[sha] if sha in fresh else (blobs_dir / sha).read_bytes())
            for sha in referenced
            if sha in fresh or not same_host
        }
        expires_unix = int(expires_at.timestamp()) if expires_at else 0
        await _host_upload(
            app,
            target_host,
            pub_id,
            row["token"],
            version,
            expires_unix,
            manifest_blob,
            upload,
            referenced,
            progress=lambda i, n: job.update(f"Uploading file {i} of {n}", 0.7 + 0.2 * i / n),
        )

    # The new version, here: manifest and meta last, then blobs no version references.
    (folder / "manifest.tmp").write_bytes(manifest_blob)
    (folder / "manifest.tmp").replace(folder / "manifest")
    _write_json(
        folder / "meta.json",
        {
            "id": pub_id,
            "session_id": session_id,
            "version": version,
            "expires": int(expires_at.timestamp()) if expires_at else None,
            "manifest_sha": manifest_sha,
            "blobs": referenced,
            "published_at": int(now.timestamp()),
        },
    )
    for f in blobs_dir.iterdir():
        if f.name not in referenced:
            f.unlink(missing_ok=True)

    who = access.principal()
    row.update(
        place=sel.where,
        host_url=target_host,
        version=version,
        selection=sel.model_dump(mode="json"),
        updated_at=now.isoformat(),
        expires_at=expires_at.isoformat() if expires_at else None,
        unpublished_at=None,
        clip_blobs={**new_clips, **{IMAGE_PREFIX + sha: rec for sha, rec in new_images.items()}},
        published_digest=digest(content, specs),
        published_by=row.get("published_by", "") if auto else (who.name if who else ""),
    )
    if sel.where == "local":
        row["origin"] = ""  # the files that serve it are here
    app.repo.put_publication(row)
    _log(
        app,
        session_id,
        EVENT_AUTO_UPDATED if auto else ("published" if version == 1 else "publication_updated"),
        row,
        sel,
        len(new_images),
    )
    if not auto:
        from .records import seal_later

        seal_later(app, session_id, SEAL_PUBLISHED if version == 1 else SEAL_UPDATED)
    # Open readers hear of it: VERSION through the room (the origin's bridge, or a one-shot from
    # another computer, C12); a publish that turned live off ends the room.
    live_publications.version_published(
        app,
        row,
        manifest_sha,
        published_at=_iso(now),
        expires_at=_iso(expires_at) if expires_at else "",
    )
    job.update("Published", 1.0)
    return {"publication": to_model(app, row, session, local_base).model_dump(mode="json")}


def _log(
    app: AppContext,
    session_id: str,
    kind: str,
    row: dict,
    sel: PublicationSelection,
    images: int = 0,
):
    who = access.principal()
    parts = ["title", *[p for p in PARTS if getattr(sel, p)]]
    if sel.attendees:
        parts.append("attendees")
    history.log(
        app,
        session_id,
        kind,
        publication=row["id"],
        version=row["version"],
        parts=parts,
        quotes=len(sel.quotes),
        clips=sum(1 for q in sel.quotes if q.with_clip),
        **({"images": images} if images else {}),
        names=sel.names,
        hide_identifiers=sel.hide_identifiers,
        where="publish host" if row["place"] == "host" else "this computer",
        host=row["host_url"],
        expires_at=row["expires_at"],
        by=who.name if who else "",
    )


async def unpublish(app: AppContext, session_id: str) -> dict | None:
    """Delete the hosted copy and the local files; the row stays, marked unpublished."""
    session = app.repo.get(session_id)
    if session is None:
        return None
    if not access.can_write(session.owner_id):
        raise access.Forbidden("Only the meeting's owner or an admin can unpublish it")
    row = app.repo.live_publication(session_id)
    if row is None:
        return None
    if why := served_elsewhere_message(app, row):
        raise HTTPException(409, why)
    # The publication first, then its room: a reader that hears the room go (4410) re-reads the
    # publication and must find it gone (410) to show "No longer published" at once (C13).
    if row["place"] == "host" and row["host_url"]:
        await _host_delete(app, row["host_url"], row["id"], row["token"])
    await live_publications.retire(app, row)
    bury(app, row["id"], "unpublished", session_id)
    at = _now().isoformat()
    app.repo.mark_unpublished(row["id"], at)
    row.update(unpublished_at=at, updated_at=at)
    who = access.principal()
    history.log(
        app,
        session_id,
        "unpublished",
        publication=row["id"],
        where="publish host" if row["place"] == "host" else "this computer",
        by=who.name if who else "",
    )
    from .records import seal_later

    seal_later(app, session_id, SEAL_UNPUBLISHED)
    return row


def sweep_expired(app: AppContext) -> int:
    """Remove the local files of expired publications (and tombstones older than
    TOMBSTONE_DAYS); mark their rows unpublished. Returns how many expired."""
    root = store_root(app)
    if not root.is_dir():
        return 0
    now = time.time()
    expired = 0
    for folder in root.iterdir():
        meta = read_meta(app, folder.name) if _ID.match(folder.name) else None
        if meta is None:
            continue
        if meta.get("gone"):
            if now - meta.get("at", now) > TOMBSTONE_DAYS * 86400:
                shutil.rmtree(folder, ignore_errors=True)
            continue
        if meta.get("expires") is not None and now >= meta["expires"]:
            bury(app, folder.name, "expired", meta.get("session_id", ""))
            expired += 1
    if not app.locked:
        for row in app.repo.publications():
            exp = _dt(row["expires_at"])
            if exp is not None and exp <= _now():
                # Every computer works out an expiry for itself: no new version to sync.
                app.repo.mark_unpublished(row["id"], exp.isoformat(), touch=False)
                history.log(app, row["session_id"], "publication_expired", publication=row["id"])
                live_publications.retire_later(app, row)  # its bridge stops, its room goes
    return expired


def forget_session(app: AppContext, session_id: str):
    """Before a meeting is deleted: what to do once it is (its publications' local files
    removed, hosted ones deleted in the background, best effort)."""
    rows = app.repo.session_publications(session_id)
    if not rows:
        return None

    def after() -> None:
        for row in rows:
            # Not for a publication this computer never held files of (it reached it by sync).
            if pub_dir(app, row["id"]).exists() or not row.get("origin"):
                bury(app, row["id"], "deleted", session_id)
            if row["place"] == "host" and row["host_url"] and not row["unpublished_at"]:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    logger.warning("Publication %s stays on %s", row["id"], row["host_url"])
                    continue
                loop.create_task(delete_quietly(app, row))

    return after


async def delete_quietly(app: AppContext, row: dict) -> None:
    """The hosted copy deleted in the background (the meeting was deleted, or the publication
    lost a library-sync merge), its live room first. Never raises."""
    try:
        await live_publications.retire(app, row)  # never raises
        await _host_delete(app, row["host_url"], row["id"], row["token"])
    except Exception:
        logger.warning(
            "Could not delete publication %s from %s", row["id"], row["host_url"], exc_info=True
        )
