"""A meeting as one portable document, and writing one in (library sync).

`export_session` reads everything that travels (models/sync_session.py); `write_session` is the
apply path for a document that arrived, or a merge of it with ours. The merge itself lives
elsewhere: this module only reads and writes.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime

from ..models.live_flag import LiveFlag
from ..models.session import (
    AgendaItem,
    CopilotNotes,
    ExternalNotes,
    NotesItem,
    Recording,
    SummaryData,
)
from ..models.sync_session import (
    SyncAsset,
    SyncBookmark,
    SyncComment,
    SyncEvent,
    SyncPeerShare,
    SyncPublication,
    SyncSession,
    event_travels,
)
from ..models.transcript import TranscriptSegment
from ..storage.sqlite import SessionRepository

# Two meetings with the same id are one meeting when they started this close together.
CLASH_SECONDS = 2.0


def export_session(repo: SessionRepository, session_id: str, device_id: str) -> SyncSession | None:
    """The meeting as the portable document, or None when there is no such meeting. Its
    origin is `device_id` (this machine's sync id) when it was recorded here ('' in the
    database), so the receiver can tell whose audio it is."""
    rows = repo.sync_rows(session_id)
    if rows is None:
        return None
    r = rows["session"]
    notes_doc = repo.notes_doc(session_id)
    return SyncSession(
        id=r["id"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
        name=r["name"],
        summary=r["summary"],
        summary_data=(
            SummaryData.model_validate_json(r["summary_data"]) if r["summary_data"] else None
        ),
        notes=r["notes"],
        notes_doc=(
            base64.b64encode(notes_doc["snapshot"]).decode("ascii")
            if notes_doc and notes_doc["snapshot"] is not None
            else None
        ),
        notes_items=repo.notes_items(session_id) or [],
        participants=json.loads(r["participants"]),
        attendees=json.loads(r["attendees"]),
        local_only=bool(r["local_only"]),
        copilot_notes=(
            CopilotNotes.model_validate_json(r["copilot_notes"]) if r["copilot_notes"] else None
        ),
        speakers_reviewed=bool(r["speakers_reviewed"]),
        agenda=[AgendaItem.model_validate(a) for a in json.loads(r["agenda"] or "[]")],
        meeting_type=r["meeting_type"],
        owner_id=r["owner_id"],
        origin_audio=r["audio_file"],
        transcript=[
            TranscriptSegment(
                text=x["text"],
                speaker=x["speaker"],
                start=x["start"],
                end=x["end"],
                words=json.loads(x["words"]) if x["words"] else None,
            )
            for x in rows["segments"]
        ],
        recordings=[
            Recording(
                id=x["id"],
                source=x["source"],
                device_id=x["device_id"],
                device_name=x["device_name"],
                path=x["path"],
                created_at=x["created_at"],
                part=x["part"],
                offset=x["part_offset"],
            )
            for x in rows["recordings"]
        ],
        bookmarks=[
            SyncBookmark(
                id=x["id"],
                part=x["part"],
                seconds=x["seconds"],
                note=x["note"],
                created_at=x["created_at"],
            )
            for x in rows["bookmarks"]
        ],
        external_notes=[
            ExternalNotes(
                id=x["id"],
                source=x["source"],
                text=x["text"],
                filename=x["filename"],
                added_at=x["added_at"],
            )
            for x in rows["external_notes"]
        ],
        live_flags=[
            LiveFlag(
                id=x["id"],
                source=x["source"],
                start=x["start"],
                end=x["end"],
                text=x["text"],
                part=x["part"],
                created_at=x["created_at"],
            )
            for x in rows["live_flags"]
        ],
        comments=[
            SyncComment(**{k: x[k] for k in SyncComment.model_fields}) for x in rows["comments"]
        ],
        publications=[
            SyncPublication(
                id=x["id"],
                key=base64.b64encode(bytes(x["key"])).decode("ascii"),
                token=x["token"],
                where=x["place"],
                host_url=x["host_url"],
                version=x["version"],
                selection=json.loads(x["selection"]),
                created_at=x["created_at"],
                updated_at=x["updated_at"],
                expires_at=x["expires_at"],
                unpublished_at=x["unpublished_at"],
                clip_blobs=json.loads(x["clip_blobs"] or "{}"),
                published_digest=x["published_digest"],
                published_by=x["published_by"],
                origin=x["origin"] or device_id,
            )
            for x in rows["publications"]
        ],
        peer_shares=[
            SyncPeerShare(
                person=x["person"],
                parts=json.loads(x["parts"] or "{}"),
                by=x["by"],
                at=x["at"],
                updated_at=x["updated_at"],
                revoked_at=x["revoked_at"],
            )
            for x in rows["peer_shares"]
        ],
        speaker_embeddings={x["label"]: json.loads(x["embedding"]) for x in rows["embeddings"]},
        assets=[
            SyncAsset(
                id=x["id"],
                kind=x["kind"],
                title=x["title"],
                url=x["url"],
                filename=x["filename"],
                size=x["size"],
                source=x["source"],
                text=x["text"],
                created_at=x["created_at"],
                added_at=x["linked_at"],
            )
            for x in rows["assets"]
        ],
        events=[
            SyncEvent(
                uid=x["uid"],
                at=x["at"],
                kind=x["kind"],
                part=x["part"],
                detail=json.loads(x["detail"] or "{}"),
            )
            for x in rows["events"]
            if x["uid"] and event_travels(x["kind"])
        ],
        origin=r["origin"] or device_id,
        vv=json.loads(r["sync_vv"] or "{}"),
    )


def base_json(doc: SyncSession) -> str:
    """The document as kept for a peer's later merge (`sync_served`): without the notes
    document, which is merged by union and needs no base (the notes text stays for the
    three-way merge)."""
    return doc.model_copy(update={"notes_doc": None}).model_dump_json()


def write_session(
    repo: SessionRepository,
    s: SyncSession,
    *,
    local_device_id: str,
    vv: dict[str, int],
    bump: bool,
) -> str:
    """Write a meeting that arrived (or a merge of it with ours) into the library; returns
    its id. See `SessionRepository.apply_synced` for what is written and what is left alone.

    The sender's origin is its own sync id for a meeting it recorded; here that is '' when it
    is our own id (the meeting came home), else the sender's id stays. `vv` is the version
    vector to store (the incoming one for a fast-forward, the merged one for a merge).
    `bump=True` (a merge) counts a new revision of ours in it; `bump=False` stores `vv` as
    given. Both advance the meeting in our change log, so a third machine hears of it.
    `local_device_id` must be the repository's own `device_id` when `bump` is set: the
    revision is counted under it."""
    if bump and repo.device_id != local_device_id:
        raise ValueError("local_device_id is not this repository's device id")
    origin = "" if s.origin == local_device_id else s.origin
    sid = repo.apply_synced(s, origin=origin, vv=vv, bump=bump)
    if s.notes_items is not None:  # an older peer's document has none: ours stay
        repo.set_notes_items(sid, settle_notes_items(repo.notes_items(sid) or [], s.notes_items))
    return sid


NOTES_ITEM_STATE = ("follow", "due", "issue_url", "task_id")


def settle_notes_items(current: list[NotesItem], arrived: list[NotesItem]) -> list[NotesItem]:
    """The notes items to store once a document arrived: what the notes document says now
    (`current`, derived by our own fold, possibly after a union that came first) with the state
    fields (follow, due, issue_url, task_id) of the arriving item of the same id. An arriving
    item our document has no line for yet is kept as it came: the fold after the document
    arrives derives it again and carries its state by id."""
    state = {i.id: i for i in arrived}
    out = [
        i.model_copy(update={k: getattr(state[i.id], k) for k in NOTES_ITEM_STATE})
        if i.id in state
        else i
        for i in current
    ]
    have = {i.id for i in current}
    return out + [i.model_copy(deep=True) for i in arrived if i.id not in have]


def same_meeting(a_created_at: datetime, b_created_at: datetime) -> bool:
    """Whether two meetings with one id are one meeting (they started within two seconds of
    each other) and not an id clash, which is reported and never merged."""
    return abs((a_created_at - b_created_at).total_seconds()) <= CLASH_SECONDS
