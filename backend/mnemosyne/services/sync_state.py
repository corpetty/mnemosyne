"""A meeting as one portable document, and writing one in (library sync).

`export_session` reads everything that travels (models/sync_session.py); `write_session` is the
apply path for a document that arrived, or a merge of it with ours. The merge itself lives
elsewhere: this module only reads and writes.
"""

from __future__ import annotations

import json
from datetime import datetime

from ..models.live_flag import LiveFlag
from ..models.session import (
    AgendaItem,
    CopilotNotes,
    ExternalNotes,
    Recording,
    SummaryData,
)
from ..models.sync_session import (
    SyncAsset,
    SyncBookmark,
    SyncComment,
    SyncEvent,
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
    return repo.apply_synced(s, origin=origin, vv=vv, bump=bump)


def same_meeting(a_created_at: datetime, b_created_at: datetime) -> bool:
    """Whether two meetings with one id are one meeting (they started within two seconds of
    each other) and not an id clash, which is reported and never merged."""
    return abs((a_created_at - b_created_at).total_seconds()) <= CLASH_SECONDS
