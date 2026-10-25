"""SQLite-backed session repository.

One file, WAL mode, stdlib sqlite3. Transcript segments live in their own
table so listing sessions never deserializes transcripts.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import sqlite3
import threading
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4

from .. import access
from ..models.ask import Ask, Citation, Passage, PassageLine
from ..models.comment import TARGET_LABELS, Comment
from ..models.digest import Digest
from ..models.live_flag import LiveFlag
from ..models.search import CommentHit, SearchHit, SegmentHit
from ..models.session import (
    AgendaItem,
    Asset,
    Bookmark,
    CopilotNotes,
    ExternalNotes,
    Recording,
    Session,
    SessionStatus,
    SessionSummary,
    SummaryData,
)
from ..models.shared_meeting import SharedMeeting
from ..models.speaker import SpeakerProfile
from ..models.sync_session import SyncPeerShare, SyncSession, event_travels, peer_share_rank
from ..models.transcript import TranscriptSegment

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 17

# Fields of a meeting that do not travel in library sync: changing only these is no new version.
# Meta key prefix of the last of my notes frames the owner accepted, per meeting shared with me.
PUSHED_SEQ_PREFIX = "share_pushed_seq:"
REFUSED_NOTES_PREFIX = "share_notes_refused:"  # ... and why the owner refused them

LOCAL_FIELDS = frozenset({"status", "audio_file"})

# The search index follows its tables by rowid: an index row has the rowid of the row it
# indexes, so removing one is a lookup. (Matching on session_id and idx, UNINDEXED columns,
# scanned the whole index once per removed line: rewriting a long transcript took minutes.)
FTS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS segments_ai AFTER INSERT ON segments BEGIN
    INSERT INTO segments_fts(rowid, text, session_id, idx)
    VALUES (new.rowid, new.text, new.session_id, new.idx);
END;
CREATE TRIGGER IF NOT EXISTS segments_ad AFTER DELETE ON segments BEGIN
    DELETE FROM segments_fts WHERE rowid = old.rowid;
END;
CREATE TRIGGER IF NOT EXISTS segments_au AFTER UPDATE OF text ON segments BEGIN
    DELETE FROM segments_fts WHERE rowid = old.rowid;
    INSERT INTO segments_fts(rowid, text, session_id, idx)
    VALUES (new.rowid, new.text, new.session_id, new.idx);
END;
CREATE TRIGGER IF NOT EXISTS sessions_ai AFTER INSERT ON sessions BEGIN
    INSERT INTO sessions_fts(rowid, name, summary, notes, session_id)
    VALUES (new.rowid, new.name, new.summary, new.notes, new.id);
END;
CREATE TRIGGER IF NOT EXISTS sessions_au AFTER UPDATE OF name, summary, notes ON sessions BEGIN
    DELETE FROM sessions_fts WHERE rowid = old.rowid;
    INSERT INTO sessions_fts(rowid, name, summary, notes, session_id)
    VALUES (new.rowid, new.name, new.summary, new.notes, new.id);
END;
CREATE TRIGGER IF NOT EXISTS sessions_ad AFTER DELETE ON sessions BEGIN
    DELETE FROM sessions_fts WHERE rowid = old.rowid;
END;
"""

# Keyword search over comments (text + the quote they were written about); a deleted comment is
# not indexed. Own constant: it needs the comments table, which comes late in SCHEMA.
COMMENT_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS comments_fts USING fts5(
    text, quote, session_id UNINDEXED, comment_id UNINDEXED,
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS comments_ai AFTER INSERT ON comments WHEN new.deleted = 0 BEGIN
    INSERT INTO comments_fts(rowid, text, quote, session_id, comment_id)
    VALUES (new.rowid, new.text, new.quote, new.session_id, new.id);
END;
CREATE TRIGGER IF NOT EXISTS comments_au AFTER UPDATE OF text, quote, deleted, session_id
ON comments BEGIN
    DELETE FROM comments_fts WHERE rowid = old.rowid;
    INSERT INTO comments_fts(rowid, text, quote, session_id, comment_id)
    SELECT new.rowid, new.text, new.quote, new.session_id, new.id WHERE new.deleted = 0;
END;
CREATE TRIGGER IF NOT EXISTS comments_ad AFTER DELETE ON comments BEGIN
    DELETE FROM comments_fts WHERE rowid = old.rowid;
END;
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    audio_file TEXT,
    summary TEXT NOT NULL DEFAULT '',
    summary_data TEXT,
    notes TEXT NOT NULL DEFAULT '',
    participants TEXT NOT NULL DEFAULT '[]',
    attendees TEXT NOT NULL DEFAULT '[]',
    local_only INTEGER NOT NULL DEFAULT 0,
    copilot_notes TEXT,
    speakers_reviewed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created_at DESC);
CREATE TABLE IF NOT EXISTS segments (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    text TEXT NOT NULL,
    speaker TEXT NOT NULL,
    start REAL NOT NULL,
    "end" REAL NOT NULL,
    words TEXT,
    PRIMARY KEY (session_id, idx)
);
CREATE TABLE IF NOT EXISTS recordings (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    device_id INTEGER NOT NULL,
    device_name TEXT NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    part INTEGER NOT NULL DEFAULT 0,
    part_offset REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_recordings_session ON recordings(session_id);
CREATE TABLE IF NOT EXISTS speakers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    embedding TEXT NOT NULL,
    sample_count INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
    text, session_id UNINDEXED, idx UNINDEXED,
    tokenize='unicode61 remove_diacritics 2'
);
CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
    name, summary, notes, session_id UNINDEXED,
    tokenize='unicode61 remove_diacritics 2'
);
{FTS_TRIGGERS}CREATE TABLE IF NOT EXISTS asks (
    id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    citations TEXT NOT NULL DEFAULT '[]',
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS digests (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    markdown TEXT NOT NULL,
    session_ids TEXT NOT NULL DEFAULT '[]',
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    path TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunk_vectors (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,           -- lines | summary
    first_idx INTEGER NOT NULL,   -- -1 for a summary
    last_idx INTEGER NOT NULL,
    text_hash TEXT NOT NULL,
    vec BLOB NOT NULL,            -- float16, unit length
    PRIMARY KEY (session_id, kind, first_idx)
);
CREATE TABLE IF NOT EXISTS session_speakers (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    embedding TEXT NOT NULL,
    PRIMARY KEY (session_id, label)
);

-- A bookmark is kept as (part, seconds into it): a part's place on the meeting's timeline is
-- known only once its recording is saved (services/parts.py).
CREATE TABLE IF NOT EXISTS bookmarks (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    part INTEGER NOT NULL DEFAULT 0,
    seconds REAL NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bookmarks_session ON bookmarks(session_id);

-- Comments travel inside their meeting (library sync). `anchor` is JSON: {part, start, end} for
-- a transcript span, {exact, prefix, suffix} for summary text; `target_id` names a summary
-- block. A deleted comment with replies stays as an empty row (`deleted`).
CREATE TABLE IF NOT EXISTS comments (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    parent_id TEXT NOT NULL DEFAULT '',
    target TEXT NOT NULL DEFAULT '',
    target_id TEXT NOT NULL DEFAULT '',
    anchor TEXT NOT NULL DEFAULT '{}',
    quote TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    author_id TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    mentions TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT NOT NULL DEFAULT '',
    deleted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS comments_session ON comments(session_id);
{COMMENT_FTS}
-- Live lines marked "Not said" while recording (keyboard noise heard as "yeah"): the final
-- transcript drops its lines that overlap them (services/live_flags.py). start/"end" are the
-- live transcript's own seconds, i.e. into `part`'s recording, as the live_segment events had.
CREATE TABLE IF NOT EXISTS live_flags (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    start REAL NOT NULL,
    "end" REAL NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    part INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS live_flags_session ON live_flags(session_id);

-- Resources (links, files) in a library; a meeting lists those attached to it.
-- path: a file's place under <data_dir>/assets (relative, so backups move), maybe .enc.
CREATE TABLE IF NOT EXISTS assets (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    filename TEXT,
    size INTEGER,
    path TEXT,
    text TEXT,
    source TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS external_notes (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    text TEXT NOT NULL,
    filename TEXT,
    added_at TEXT NOT NULL
);
-- What happened to a meeting (services/history.py): recordings started and stopped, parts
-- saved or recovered, capture problems, imports, transcriptions. detail is a JSON object.
CREATE TABLE IF NOT EXISTS session_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    part INTEGER,
    detail TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS session_events_session ON session_events(session_id);
-- The meeting note last written to the Obsidian vault (services/vault_sync.py write_note):
-- the merge history of its text (export/note_merge.py), the checkbox statuses and the hash of
-- the bytes written, so the next export can tell what the user changed. boxes is JSON.
CREATE TABLE IF NOT EXISTS vault_notes (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    history BLOB NOT NULL,
    boxes TEXT NOT NULL,
    written_sha TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- A meeting summary as a mergeable document (services/summary_doc.py): per-block merge
-- histories, the LLM's last output as the base of three-way rules, deleted ids, and what the
-- last Obsidian export rendered. JSON; summary_data stays the projection read everywhere.
CREATE TABLE IF NOT EXISTS summary_docs (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    doc TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- Live shared notes (services/shared_notes.py): the frames clients sent, in arrival order, as
-- the relay got them (opaque hashseq bytes: 0xDE deltas, 0xAF artifacts), with who sent each.
-- `folded` frames are in the snapshot; their rows stay for per-frame authorship.
CREATE TABLE IF NOT EXISTS shared_notes_ops (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    frame BLOB NOT NULL,
    author_id TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL,
    folded INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, seq)
);
-- The folded document (the sidecar's snapshot, up to `folded_seq`), its markdown as last
-- folded (Session.notes differing from it = written around the document, which catches up),
-- its plain text and each comment mark's range in it as last folded (`comment_ranges`, JSON
-- {comment id: [start, end) | null}, code points; services/comments.py places notes comments
-- from them), and the last edit shown.
CREATE TABLE IF NOT EXISTS shared_notes (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    snapshot BLOB,
    folded_seq INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    last_by TEXT NOT NULL DEFAULT '',
    last_at TEXT,
    markdown TEXT,
    plain TEXT,
    comment_ranges TEXT
);
CREATE TABLE IF NOT EXISTS session_assets (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    added_at TEXT NOT NULL,
    PRIMARY KEY (session_id, asset_id)
);
-- Records (services/records.py). Earlier transcripts and summaries, kept when replaced.
CREATE TABLE IF NOT EXISTS session_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    reason TEXT NOT NULL,
    by TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    summary_data TEXT
);
CREATE INDEX IF NOT EXISTS session_versions_session ON session_versions(session_id);
-- Seals: a hash of the meeting's content and audio files, chained to the previous seal.
CREATE TABLE IF NOT EXISTS session_seals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    reason TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    audio TEXT NOT NULL DEFAULT '{}',
    prev TEXT NOT NULL DEFAULT '',
    chain TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS session_seals_session ON session_seals(session_id);
-- Flagged phrases (services/supervision.py): transcript lines with a review phrase, and
-- reviewers' sign-offs. A meeting is reviewed when a review is newer than its newest flag.
CREATE TABLE IF NOT EXISTS supervision_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    start REAL NOT NULL,
    speaker TEXT NOT NULL DEFAULT '',
    phrase TEXT NOT NULL,
    text TEXT NOT NULL,
    found_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS supervision_flags_session ON supervision_flags(session_id);
CREATE TABLE IF NOT EXISTS supervision_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    by_id TEXT NOT NULL DEFAULT '',
    by TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS supervision_reviews_session ON supervision_reviews(session_id);
-- A team server's per-person preferences (services/prefs.py), as JSON.
CREATE TABLE IF NOT EXISTS user_prefs (
    user_id TEXT PRIMARY KEY,
    data TEXT NOT NULL DEFAULT '{}'
);
-- Meetings shared on a team server (access.py): read access for user_id, or everyone ("*").
CREATE TABLE IF NOT EXISTS session_shares (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    by TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL,
    PRIMARY KEY (session_id, user_id)
);
CREATE INDEX IF NOT EXISTS session_shares_user ON session_shares(user_id);
-- Organizations (services/organizations.py): people grouped by who they are with. Members are
-- people by name (as in transcripts and calendars), with an email when known.
CREATE TABLE IF NOT EXISTS organizations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    hubspot_company_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS organization_members (
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    PRIMARY KEY (organization_id, name)
);
-- Meetings and audio deleted: who, when, why. Outlives what it describes.
CREATE TABLE IF NOT EXISTS deletions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    session_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    what TEXT NOT NULL,
    by TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT ''
);
-- Publications (services/publishing.py): a meeting's curated, encrypted copy, readable with a
-- link that carries `key`. `token` authorizes updates on a publish host; `place` is "local" or
-- "host"; selection, clip_blobs (span -> sealed clip, reused by later versions) are JSON;
-- published_digest is a hash of what the last version showed (`behind`). One live (not
-- unpublished) row per meeting. A change here is a new version of the meeting for library sync
-- (the row travels in SyncSession.publications); `origin` is the computer whose files serve a
-- "local" publication ('' = this one, else its sync id).
CREATE TABLE IF NOT EXISTS publications (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    key BLOB NOT NULL,
    token TEXT NOT NULL,
    place TEXT NOT NULL,
    host_url TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL,
    selection TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    unpublished_at TEXT,
    clip_blobs TEXT NOT NULL DEFAULT '{}',
    published_digest TEXT NOT NULL DEFAULT '',
    published_by TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS publications_session ON publications(session_id);
CREATE UNIQUE INDEX IF NOT EXISTS publications_live ON publications(session_id)
    WHERE unpublished_at IS NULL;
-- Guest notes rooms (phase 9): one per meeting. `key` is the room's secret, `token` the
-- publisher token on the host, `room_token` the room's own token. Belongs to the machine that
-- started it: never travels in library sync and is not a new version of the meeting.
CREATE TABLE IF NOT EXISTS guest_rooms (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    room TEXT NOT NULL,
    key BLOB NOT NULL,
    token TEXT NOT NULL,
    room_token TEXT NOT NULL,
    host_url TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_by TEXT NOT NULL DEFAULT '',
    ended_at TEXT NOT NULL DEFAULT '',
    deleted_at TEXT NOT NULL DEFAULT '',
    expires_at TEXT NOT NULL DEFAULT '',
    auto_end_at TEXT NOT NULL DEFAULT '',
    last_seq INTEGER NOT NULL DEFAULT 0
);
-- Library sync (services/sync): the states served to each peer, the last few per meeting (the
-- base of a three-way merge), and meetings deleted. No foreign key: a tombstone outlives its
-- meeting, that is its job.
CREATE TABLE IF NOT EXISTS sync_served (
    session_id TEXT NOT NULL,
    peer_id TEXT NOT NULL,
    rev INTEGER NOT NULL,
    state TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (session_id, peer_id, rev)
);
CREATE TABLE IF NOT EXISTS sync_tombstones (
    session_id TEXT PRIMARY KEY,
    at TEXT NOT NULL,
    device TEXT NOT NULL,
    name TEXT NOT NULL
);
-- Contacts (services/contacts.py): other people's keys, global to the library, not meeting data.
-- `card` is their signed card, `secret` the pair's shared secret, `machines` runtime state per
-- endpoint (the dialed loopback address), `cursors` pull cursors per endpoint. A removed
-- contact keeps its row (removed_at) so the removal can travel in library sync.
CREATE TABLE IF NOT EXISTS contacts (
    person TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    my_name TEXT NOT NULL DEFAULT '',
    card TEXT NOT NULL,
    secret TEXT NOT NULL,
    machines TEXT NOT NULL DEFAULT '{}',
    cursors TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    removed_at TEXT NOT NULL DEFAULT ''
);
-- Meetings shared with a contact (phase 8; services/shares.py): `person` is the contact's key,
-- `parts` the chosen parts as JSON. A revoked share keeps its row (`revoked_at`) so the
-- revocation travels in library sync and reaches the contact as a tombstone.
CREATE TABLE IF NOT EXISTS peer_shares (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    person TEXT NOT NULL,
    parts TEXT NOT NULL,
    by TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revoked_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (session_id, person)
);
CREATE INDEX IF NOT EXISTS peer_shares_person ON peer_shares(person);
-- Who had a share of a meeting when it was deleted: the cascade removes peer_shares, and the
-- contact still has to be told. Local bookkeeping, not synced (each machine tells its own).
CREATE TABLE IF NOT EXISTS peer_share_tombstones (
    session_id TEXT NOT NULL,
    person TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (session_id, person)
);
-- Signatures over a subject (a record id or a frame hash): who signed, and the signature.
-- Also the idempotency check for contributions that arrive twice.
CREATE TABLE IF NOT EXISTS signatures (
    subject TEXT PRIMARY KEY,
    signer TEXT NOT NULL,
    sig TEXT NOT NULL,
    at TEXT NOT NULL
);
-- A meeting someone shared with this person, as last received (services/share_pull.py): the
-- sharer's key, the parts they chose (JSON), the SharedMeeting's digest (an equal one is not
-- applied again) and who else it is shared with (JSON list of names). Local, never synced.
CREATE TABLE IF NOT EXISTS shared_meetings (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    person TEXT NOT NULL,
    parts TEXT NOT NULL,
    digest TEXT NOT NULL,
    shared_with TEXT NOT NULL DEFAULT '[]',
    received_at TEXT NOT NULL
);

-- What I did on a meeting someone shared with me, waiting to be sent to its owner's computer
-- (services/share_push.py): `id` is the signed record's id (a resend is the same record), `person`
-- the owner. Comments and action-item flags only; my typed notes frames are read from the notes
-- log past a per-meeting cursor (meta `share_pushed_seq:<session>`). status: pending | accepted |
-- rejected (`reason` says why). Local, never synced.
CREATE TABLE IF NOT EXISTS share_outbox (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    person TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    frame_hashes TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    reason TEXT NOT NULL DEFAULT '',
    pushed_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_share_outbox_session ON share_outbox(session_id, status);
CREATE INDEX IF NOT EXISTS idx_share_outbox_person ON share_outbox(person, status);
"""
SCHEMA = SCHEMA.replace("{FTS_TRIGGERS}", FTS_TRIGGERS).replace("{COMMENT_FTS}", COMMENT_FTS)


def _ranges_json(comments: dict | None) -> str | None:
    """A fold's comment ranges ({id: [start, end] | None}) as stored; None = not known."""
    return None if comments is None else json.dumps(comments, sort_keys=True)


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


_TERM = re.compile(r"[\w']+", re.UNICODE)


# Words that carry no retrieval signal in questions about meetings.
STOPWORDS = frozenset(
    """a about above after again against all am an and any are as at be because been before
    being below between both but by can could did do does doing down during each few for from
    further had has have having he her here hers him his how i if in into is it its itself
    just me more most my no nor not now of off on once only or other our ours out over own
    same she should so some such than that the their theirs them then there these they this
    those through to too under until up very was we were what when where which while who whom
    why will with would you your yours yourself anything something everything tell said say
    says talk talked talking discuss discussed meeting meetings call calls did we us last
    week weeks month months day days ago recently""".split()
)


def fts_any_query(question: str) -> str:
    """OR-query of the question's meaningful words (prefix-matched when long enough),
    for ranking passages by relevance rather than requiring every word."""
    terms = []
    for t in _TERM.findall(question.lower()):
        t = t.strip("'")
        if len(t) < 3 or t in STOPWORDS or t in terms:
            continue
        terms.append(t)
    return " OR ".join(f'"{t}"*' if len(t) >= 4 else f'"{t}"' for t in terms[:24])


def fts_query(user_query: str) -> str:
    """Turn free text into a safe FTS5 query: every term quoted and required,
    the last one as a prefix so results appear while typing."""
    terms = _TERM.findall(user_query)
    if not terms:
        return ""
    quoted = [f'"{t}"' for t in terms]
    quoted[-1] = quoted[-1] + "*"
    return " ".join(quoted)


def _connect(db_path: Path, key: bytes | None):
    """A connection to the database; with `key`, through SQLCipher (encrypted at rest)."""
    if key is None:
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn
    import sqlcipher3

    conn = sqlcipher3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlcipher3.Row
    conn.execute(f"PRAGMA key = \"x'{key.hex()}'\"")
    conn.execute("SELECT count(*) FROM sqlite_master")  # fails here on a wrong key
    return conn


def convert_database(src: Path, dst: Path, src_key: bytes | None, dst_key: bytes | None) -> None:
    """Copy a database into a new file, encrypting or decrypting it (sqlcipher_export)."""
    import sqlcipher3

    dst.unlink(missing_ok=True)
    conn = sqlcipher3.connect(str(src))
    try:
        if src_key is not None:
            conn.execute(f"PRAGMA key = \"x'{src_key.hex()}'\"")
        target = f"x'{dst_key.hex()}'" if dst_key is not None else ""
        conn.execute(f'ATTACH DATABASE ? AS converted KEY "{target}"', (str(dst),))
        conn.execute("SELECT sqlcipher_export('converted')")
        conn.execute("DETACH DATABASE converted")
    finally:
        conn.close()


def _asset(r) -> Asset:
    return Asset(
        id=r["id"],
        kind=r["kind"],
        title=r["title"],
        url=r["url"],
        filename=r["filename"],
        size=r["size"],
        created_at=_dt(r["created_at"]),
        source=r["source"],
        has_text=r["text"] is not None,
    )


def _local_naive(dt: datetime) -> datetime:
    """Local meetings store naive local times; a timezone-aware one (shared in) becomes the same."""
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo is not None else dt


class SessionRepository:
    def __init__(self, db_path: Path, key: bytes | None = None):
        self._lock = threading.RLock()
        # Called with (session id, owner id) after a new meeting is saved (services/prefs.py).
        self.on_new_session: Callable[[str, str], None] | None = None
        # This machine's id in version vectors (services/sync sets it at startup). '' until
        # then: writes still bump the change counter and count under the key "" in sync_vv.
        self.device_id = ""
        self._open(db_path, key)

    def reopen(self, db_path: Path, key: bytes | None = None) -> None:
        """Switch this repository (and everything holding it) to another database file or key:
        after unlocking, or when encryption is turned on or off."""
        with self._lock:
            self._conn.close()
            self._open(db_path, key)

    def _open(self, db_path: Path, key: bytes | None) -> None:
        self.db_path = db_path
        self.encrypted = key is not None
        if str(db_path) != ":memory:":
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = _connect(db_path, key)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._migrate_columns()
        self._migrate_fts_rowids()
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()
        self._backfill_fts()

    def _migrate_columns(self) -> None:
        """Additive column migrations for databases created by older schemas."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(sessions)")}
        if "summary_data" not in cols:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN summary_data TEXT")
        if "attendees" not in cols:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN attendees TEXT NOT NULL DEFAULT '[]'"
            )
        if "local_only" not in cols:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN local_only INTEGER NOT NULL DEFAULT 0"
            )
        if "copilot_notes" not in cols:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN copilot_notes TEXT")
        rec_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(recordings)")}
        if "part" not in rec_cols:
            self._conn.execute("ALTER TABLE recordings ADD COLUMN part INTEGER NOT NULL DEFAULT 0")
            self._conn.execute(
                "ALTER TABLE recordings ADD COLUMN part_offset REAL NOT NULL DEFAULT 0"
            )
        if "meeting_type" not in cols:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN meeting_type TEXT NOT NULL DEFAULT ''"
            )
        if "agenda" not in cols:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN agenda TEXT NOT NULL DEFAULT '[]'")
        if "speakers_reviewed" not in cols:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN speakers_reviewed INTEGER NOT NULL DEFAULT 0"
            )
            # Only meetings transcribed from now on ask "who is who".
            self._conn.execute("UPDATE sessions SET speakers_reviewed = 1")
        # Who a meeting, a saved question or a digest belongs to on a team server (access.py).
        if "owner_id" not in cols:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''")
        for table in ("asks", "digests"):
            if "owner_id" not in {
                r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")
            }:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''"
                )
        # A version's structured summary (topics, items...), outside its records hash, so an
        # earlier summary can be restored whole (services/summary_edit.py).
        if "summary_data" not in {
            r["name"] for r in self._conn.execute("PRAGMA table_info(session_versions)")
        }:
            self._conn.execute("ALTER TABLE session_versions ADD COLUMN summary_data TEXT")
        # Not on the Session model: calendar attendees' addresses (name -> email) and what a
        # meeting became in a CRM (services/hubspot.py), keyed by CRM.
        if "attendee_emails" not in cols:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN attendee_emails TEXT NOT NULL DEFAULT '{}'"
            )
        if "crm" not in cols:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN crm TEXT NOT NULL DEFAULT '{}'")
        # A legal hold's reason ("" = none): no deleting the meeting or its audio (records.py).
        if "legal_hold" not in cols:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN legal_hold TEXT NOT NULL DEFAULT ''"
            )
        # Library sync. origin: the machine that recorded the meeting ('' = this one);
        # sync_vv: version vector {device id: revision}; sync_seq: position in this machine's
        # change log (meta 'sync_seq' is the counter). Meetings that exist when the columns
        # appear get consecutive sequence numbers in the order they were last changed, so a
        # first sync lists them; their vectors stay empty (the sync service claims them).
        if "sync_seq" not in cols:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN origin TEXT NOT NULL DEFAULT ''")
            self._conn.execute("ALTER TABLE sessions ADD COLUMN sync_vv TEXT NOT NULL DEFAULT '{}'")
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN sync_seq INTEGER NOT NULL DEFAULT 0"
            )
            ids = [
                r["id"]
                for r in self._conn.execute("SELECT id FROM sessions ORDER BY updated_at, id")
            ]
            self._conn.executemany(
                "UPDATE sessions SET sync_seq=? WHERE id=?",
                [(n, sid) for n, sid in enumerate(ids, start=1)],
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('sync_seq', ?)", (str(len(ids)),)
            )
        self._conn.execute("CREATE INDEX IF NOT EXISTS sessions_sync_seq ON sessions(sync_seq)")
        # A history event's identity across machines (the union of two histories is by uid).
        event_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(session_events)")}
        if "uid" not in event_cols:
            self._conn.execute("ALTER TABLE session_events ADD COLUMN uid TEXT")
        self._conn.execute(
            "UPDATE session_events SET uid = lower(hex(randomblob(8))) WHERE uid IS NULL"
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS session_events_uid ON session_events(uid)")
        if "markdown" not in {
            r["name"] for r in self._conn.execute("PRAGMA table_info(shared_notes)")
        }:
            self._conn.execute("ALTER TABLE shared_notes ADD COLUMN markdown TEXT")
        notes_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(shared_notes)")}
        for col in ("plain", "comment_ranges"):
            if col not in notes_cols:
                self._conn.execute(f"ALTER TABLE shared_notes ADD COLUMN {col} TEXT")
        # A meeting someone shared WITH this person: the sharer's person key (64 hex); '' for
        # our own meetings. Never travels in library sync (each computer pulls it from the owner).
        if "shared_from" not in cols:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN shared_from TEXT NOT NULL DEFAULT ''"
            )
        # Who may read a meeting beyond its owner: team-mode shares and live peer shares, as
        # (session_id, user_id). A contact reads as the Principal "p:" + person[:16].
        # Recreated on every open: the definition may change.
        self._conn.execute("DROP VIEW IF EXISTS share_grants")
        self._conn.execute(
            "CREATE VIEW share_grants(session_id, user_id) AS"
            " SELECT session_id, user_id FROM session_shares"
            " UNION ALL"
            " SELECT session_id, 'p:' || substr(person, 1, 16) FROM peer_shares"
            " WHERE revoked_at = ''"
        )
        pub_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(publications)")}
        if "origin" not in pub_cols:
            self._conn.execute(
                "ALTER TABLE publications ADD COLUMN origin TEXT NOT NULL DEFAULT ''"
            )
        self._conn.commit()

    def _migrate_fts_rowids(self) -> None:
        """Databases from before the index followed rowids: new triggers, rebuilt index. Once."""
        done = self._conn.execute("SELECT value FROM meta WHERE key='fts_rowids'").fetchone()
        if done is not None:
            return
        with self._conn:
            for trigger in (
                "segments_ai",
                "segments_ad",
                "segments_au",
                "sessions_ai",
                "sessions_au",
                "sessions_ad",
            ):
                self._conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            self._conn.execute("DELETE FROM segments_fts")
            self._conn.execute("DELETE FROM sessions_fts")
        self._conn.executescript(FTS_TRIGGERS)
        with self._conn:
            self._conn.execute(
                "INSERT INTO segments_fts(rowid, text, session_id, idx)"
                " SELECT rowid, text, session_id, idx FROM segments"
            )
            self._conn.execute(
                "INSERT INTO sessions_fts(rowid, name, summary, notes, session_id)"
                " SELECT rowid, name, summary, notes, id FROM sessions"
            )
            self._conn.execute("INSERT INTO meta(key, value) VALUES ('fts_rowids', '1')")

    def _backfill_fts(self) -> None:
        """Index rows that predate the FTS tables (databases from schema < 3)."""
        with self._lock, self._conn:
            if self._conn.execute("SELECT count(*) FROM segments_fts").fetchone()[0] == 0:
                self._conn.execute(
                    "INSERT INTO segments_fts(rowid, text, session_id, idx)"
                    " SELECT rowid, text, session_id, idx FROM segments"
                )
            if self._conn.execute("SELECT count(*) FROM sessions_fts").fetchone()[0] == 0:
                self._conn.execute(
                    "INSERT INTO sessions_fts(rowid, name, summary, notes, session_id)"
                    " SELECT rowid, name, summary, notes, id FROM sessions"
                )
            if self._conn.execute("SELECT count(*) FROM comments_fts").fetchone()[0] == 0:
                self._conn.execute(
                    "INSERT INTO comments_fts(rowid, text, quote, session_id, comment_id)"
                    " SELECT rowid, text, quote, session_id, id FROM comments WHERE deleted=0"
                )
            self._conn.execute(
                "UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),)
            )

    # ---- contacts (services/contacts.py); no meeting is touched ---------------------------

    @staticmethod
    def _contact(row) -> dict:
        d = dict(row)
        d["card"] = json.loads(d["card"])
        d["machines"] = json.loads(d["machines"])
        d["cursors"] = json.loads(d["cursors"])
        return d

    def contacts(self, include_removed: bool = False) -> list[dict]:
        sql = "SELECT * FROM contacts"
        if not include_removed:
            sql += " WHERE removed_at=''"
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY created_at, person").fetchall()
        return [self._contact(r) for r in rows]

    def contact(self, person: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM contacts WHERE person=?", (person,)).fetchone()
        return self._contact(row) if row else None

    def put_contact(self, row: dict) -> None:
        """Insert or replace a contact row (a dict as `contact()` returns it)."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO contacts (person, name, my_name, card, secret, machines,"
                " cursors, created_at, updated_at, removed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    row["person"],
                    row["name"],
                    row.get("my_name", ""),
                    json.dumps(row["card"], sort_keys=True),
                    row["secret"],
                    json.dumps(row.get("machines") or {}, sort_keys=True),
                    json.dumps(row.get("cursors") or {}, sort_keys=True),
                    row["created_at"],
                    row["updated_at"],
                    row.get("removed_at", ""),
                ),
            )

    def remove_contact(self, person: str, at: str) -> bool:
        """Mark a contact removed (the row stays, so the removal can travel)."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE contacts SET removed_at=?, updated_at=? WHERE person=?", (at, at, person)
            )
        return cur.rowcount > 0

    def set_contact_runtime(
        self, person: str, machines: dict | None = None, cursors: dict | None = None
    ) -> None:
        """Replace a contact's per-machine runtime state and/or pull cursors (not a change that
        travels: updated_at stays)."""
        with self._lock, self._conn:
            if machines is not None:
                self._conn.execute(
                    "UPDATE contacts SET machines=? WHERE person=?",
                    (json.dumps(machines, sort_keys=True), person),
                )
            if cursors is not None:
                self._conn.execute(
                    "UPDATE contacts SET cursors=? WHERE person=?",
                    (json.dumps(cursors, sort_keys=True), person),
                )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def exclusive(self):
        """Hold every other reader and writer off (the database is being swapped: they would
        find it closed). Reentrant: close() and reopen() work inside."""
        return self._lock

    def rebase_paths(self, old: str, new: str) -> int:
        """Move stored audio paths from one recordings folder to another (after a restore).
        Never bumps the meeting's version (a path is local), and leaves the recordings rows of
        meetings recorded elsewhere alone: their paths are the origin machine's."""
        prefix = old.rstrip("/") + "/"
        target = new.rstrip("/") + "/"
        n = len(prefix)
        with self._lock, self._conn:
            a = self._conn.execute(
                "UPDATE sessions SET audio_file = ? || substr(audio_file, ?)"
                " WHERE substr(audio_file, 1, ?) = ?",
                (target, n + 1, n, prefix),
            ).rowcount
            b = self._conn.execute(
                "UPDATE recordings SET path = ? || substr(path, ?) WHERE substr(path, 1, ?) = ?"
                " AND session_id IN (SELECT id FROM sessions WHERE origin = '')",
                (target, n + 1, n, prefix),
            ).rowcount
        return a + b

    def snapshot(self, dest: Path) -> None:
        """A consistent copy of the database file, as stored (encrypted when it is): the WAL is
        folded in and nothing writes while the file is copied."""
        import shutil

        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            shutil.copyfile(self.db_path, dest)

    # ---- who may see and change what (access.py) ----------------------

    def session_owner(self, session_id: str) -> str | None:
        return self._owner(session_id)

    def _owner(self, session_id: str) -> str | None:
        """The meeting's owner id ("" for nobody's), or None for an unknown meeting."""
        with self._lock:
            row = self._conn.execute(
                "SELECT owner_id FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
        return None if row is None else row["owner_id"]

    # A member reads their own meetings and those shared with them, alone or with everyone.
    _SEES = "(owner_id=? OR id IN (SELECT session_id FROM share_grants WHERE user_id IN (?, '*')))"
    _SEES_S = _SEES.replace("(owner_id", "(s.owner_id").replace(" id IN", " s.id IN")  # sessions s

    def _readable(self, session_id: str) -> bool:
        only = access.read_owner()
        if only is None:
            return True
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._SEES} AS ok FROM sessions WHERE id=?",
                (only, only, session_id),
            ).fetchone()
        return row is None or bool(row["ok"])

    def _check_write(self, session_id: str) -> None:
        owner = self._owner(session_id)
        if owner is not None and not access.can_write(owner):
            if not self._readable(session_id):
                raise access.Hidden("Session not found")
            raise access.Forbidden("This is someone else's meeting")
        if not access.is_internal() and self._shared_from(session_id):
            raise access.Forbidden(
                f"Shared by {self._sharer_name(session_id)}: you can comment, tick action items"
                " and type in the notes"
            )

    def shared_from(self, session_id: str) -> str:
        """The sharer's key of a meeting shared with us; '' for our own or an unknown one."""
        return self._shared_from(session_id)

    def _shared_from(self, session_id: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT shared_from FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
        return row["shared_from"] if row else ""

    def _sharer_name(self, session_id: str) -> str:
        person = self._shared_from(session_id)
        contact = self.contact(person) if person else None
        if contact is None:
            return "someone"
        return contact.get("my_name") or contact.get("name") or "someone"

    def visible_ids(self) -> set[str] | None:
        """The meetings the caller may see, or None for all of them."""
        only = access.read_owner()
        if only is None:
            return None
        with self._lock:
            rows = self._conn.execute(f"SELECT id FROM sessions WHERE {self._SEES}", (only, only))
            return {r["id"] for r in rows}

    def owner_of(self, session_id: str) -> str | None:
        return self._owner(session_id)

    # ---- reads ---------------------------------------------------------

    def exists(self, session_id: str) -> bool:
        return self._owner(session_id) is not None and self._readable(session_id)

    def list_summaries(self) -> list[SessionSummary]:
        sql = """
        SELECT s.id, s.name, s.status, s.created_at, s.updated_at, s.participants, s.local_only,
               length(s.summary) > 0 AS has_summary,
               s.audio_file IS NOT NULL AS has_audio, s.owner_id, s.origin, s.shared_from,
               EXISTS(SELECT 1 FROM segments g WHERE g.session_id = s.id) AS has_transcript
        FROM sessions s {where} ORDER BY s.created_at DESC
        """
        only = access.read_owner()
        with self._lock:
            if only is None:
                rows = self._conn.execute(sql.format(where="")).fetchall()
            else:
                rows = self._conn.execute(
                    sql.format(where="WHERE " + self._SEES_S), (only, only)
                ).fetchall()
        return [
            SessionSummary(
                id=r["id"],
                name=r["name"],
                status=SessionStatus(r["status"]),
                created_at=_dt(r["created_at"]),
                updated_at=_dt(r["updated_at"]),
                has_transcript=bool(r["has_transcript"]),
                has_summary=bool(r["has_summary"]),
                has_audio=bool(r["has_audio"]),
                participant_count=len(json.loads(r["participants"])),
                local_only=bool(r["local_only"]),
                owner_id=r["owner_id"],
                origin=r["origin"],
                shared_from=r["shared_from"],
            )
            for r in rows
        ]

    def get(self, session_id: str) -> Session | None:
        """The meeting, or None when there is none or the caller may not see it."""
        only = access.read_owner()
        with self._lock:
            row = self._conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                return None
        if only is not None and row["owner_id"] != only and not self._readable(session_id):
            return None
        with self._lock:
            seg_rows = self._conn.execute(
                "SELECT * FROM segments WHERE session_id=? ORDER BY idx", (session_id,)
            ).fetchall()
            rec_rows = self._conn.execute(
                "SELECT * FROM recordings WHERE session_id=? ORDER BY created_at", (session_id,)
            ).fetchall()
            mark_rows = self._conn.execute(
                "SELECT * FROM bookmarks WHERE session_id=?", (session_id,)
            ).fetchall()
            note_rows = self._conn.execute(
                "SELECT * FROM external_notes WHERE session_id=? ORDER BY added_at",
                (session_id,),
            ).fetchall()
            asset_rows = self._conn.execute(
                "SELECT a.* FROM assets a JOIN session_assets sa ON sa.asset_id = a.id"
                " WHERE sa.session_id=? ORDER BY sa.added_at",
                (session_id,),
            ).fetchall()
        part_starts: dict[int, float] = {}
        for r in rec_rows:
            part_starts.setdefault(r["part"], r["part_offset"])
        return Session(
            id=row["id"],
            name=row["name"],
            status=SessionStatus(row["status"]),
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
            audio_file=row["audio_file"],
            summary=row["summary"],
            summary_data=(
                SummaryData.model_validate_json(row["summary_data"])
                if row["summary_data"]
                else None
            ),
            notes=row["notes"],
            participants=json.loads(row["participants"]),
            attendees=json.loads(row["attendees"]),
            local_only=bool(row["local_only"]),
            copilot_notes=(
                CopilotNotes.model_validate_json(row["copilot_notes"])
                if row["copilot_notes"]
                else None
            ),
            speakers_reviewed=bool(row["speakers_reviewed"]),
            agenda=[AgendaItem.model_validate(a) for a in json.loads(row["agenda"] or "[]")],
            meeting_type=row["meeting_type"],
            owner_id=row["owner_id"],
            legal_hold=row["legal_hold"],
            origin=row["origin"],
            shared_from=row["shared_from"],
            assets=[_asset(r) for r in asset_rows],
            external_notes=[
                ExternalNotes(
                    id=r["id"],
                    source=r["source"],
                    text=r["text"],
                    filename=r["filename"],
                    added_at=_dt(r["added_at"]),
                )
                for r in note_rows
            ],
            transcript=[
                TranscriptSegment(
                    text=r["text"],
                    speaker=r["speaker"],
                    start=r["start"],
                    end=r["end"],
                    words=json.loads(r["words"]) if r["words"] else None,
                )
                for r in seg_rows
            ],
            recordings=[
                Recording(
                    id=r["id"],
                    source=r["source"],
                    device_id=r["device_id"],
                    device_name=r["device_name"],
                    path=r["path"],
                    created_at=_dt(r["created_at"]),
                    part=r["part"],
                    offset=r["part_offset"],
                )
                for r in rec_rows
            ],
            bookmarks=sorted(
                (
                    Bookmark(
                        id=r["id"],
                        at=round(part_starts.get(r["part"], 0.0) + r["seconds"], 2),
                        note=r["note"],
                        created_at=_dt(r["created_at"]),
                    )
                    for r in mark_rows
                ),
                key=lambda b: b.at,
            ),
        )

    # ---- writes --------------------------------------------------------

    def save(self, session: Session) -> Session:
        """Insert or fully replace a session including segments and recordings. A new meeting
        belongs to whoever creates it (access.py); an existing one keeps its owner."""
        self._check_write(session.id)
        owner = self._owner(session.id)
        session.owner_id = owner if owner is not None else (session.owner_id or access.user_id())
        session.updated_at = datetime.now()
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO sessions(id, name, status, created_at, updated_at, audio_file,
                                        summary, summary_data, notes, participants,
                                        attendees, local_only, copilot_notes,
                                        speakers_reviewed, agenda, meeting_type, owner_id,
                                        origin)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     name=excluded.name, status=excluded.status, updated_at=excluded.updated_at,
                     audio_file=excluded.audio_file, summary=excluded.summary,
                     summary_data=excluded.summary_data,
                     notes=excluded.notes, participants=excluded.participants,
                     attendees=excluded.attendees, local_only=excluded.local_only,
                     copilot_notes=excluded.copilot_notes,
                     speakers_reviewed=excluded.speakers_reviewed,
                     agenda=excluded.agenda, meeting_type=excluded.meeting_type""",
                (
                    session.id,
                    session.name,
                    session.status.value,
                    session.created_at.isoformat(),
                    session.updated_at.isoformat(),
                    session.audio_file,
                    session.summary,
                    session.summary_data.model_dump_json() if session.summary_data else None,
                    session.notes,
                    json.dumps(session.participants),
                    json.dumps(session.attendees),
                    int(session.local_only),
                    session.copilot_notes.model_dump_json() if session.copilot_notes else None,
                    int(session.speakers_reviewed),
                    json.dumps([a.model_dump() for a in session.agenda]),
                    session.meeting_type,
                    session.owner_id,
                    session.origin,
                ),
            )
            self._write_segments(session.id, session.transcript)
            self._write_recordings(session.id, session.recordings)
            self._touch(session.id, session.updated_at.isoformat())
        if owner is None and self.on_new_session is not None:
            self.on_new_session(session.id, session.owner_id)
        return session

    def update_fields(self, session_id: str, **fields) -> Session | None:
        """Cheap metadata update. Valid keys: name, status, audio_file, summary, notes,
        participants."""
        self._check_write(session_id)
        allowed = {
            "name",
            "status",
            "audio_file",
            "summary",
            "summary_data",
            "notes",
            "participants",
            "attendees",
            "local_only",
            "copilot_notes",
            "speakers_reviewed",
            "agenda",
            "meeting_type",
        }
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"Cannot update fields: {sorted(bad)}")
        if not fields:
            return self.get(session_id)
        if "summary" in fields:
            before = self.get(session_id)
            if before is not None and before.summary and before.summary != fields["summary"]:
                self.keep_version(before, "summarized again")
        values = []
        sets = []
        for key, value in fields.items():
            if key == "status" and isinstance(value, SessionStatus):
                value = value.value
            if key in ("participants", "attendees"):
                value = json.dumps(value)
            if key == "agenda":
                value = json.dumps([a if isinstance(a, dict) else a.model_dump() for a in value])
            if key in ("local_only", "speakers_reviewed"):
                value = int(bool(value))
            if key in ("summary_data", "copilot_notes") and value is not None:
                value = value if isinstance(value, str) else value.model_dump_json()
            sets.append(f"{key}=?")
            values.append(value)
        now = datetime.now().isoformat()
        sets.append("updated_at=?")
        values.append(now)
        values.append(session_id)
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"UPDATE sessions SET {', '.join(sets)} WHERE id=?", tuple(values)
            )
            if cur.rowcount == 0:
                return None
            # A status is per machine and the audio file is local: neither travels.
            if set(fields) - LOCAL_FIELDS:
                self._touch(session_id, now)
        return self.get(session_id)

    def replace_segments(self, session_id: str, segments: list[TranscriptSegment]) -> None:
        self._check_write(session_id)
        before = self.get(session_id)
        if before is not None and before.transcript and before.transcript != segments:
            self.keep_version(before, "transcribed again")
        with self._lock, self._conn:
            self._write_segments(session_id, segments)
            self._touch(session_id)

    def add_recordings(
        self, session_id: str, recordings: list[Recording], *, quiet: bool = False
    ) -> None:
        """Record audio sources. `quiet`: a path-only rewrite (encryption), not a new version
        of the meeting for library sync. Adding nothing is never a change."""
        self._check_write(session_id)
        with self._lock, self._conn:
            self._insert_recordings(session_id, recordings)
            if recordings and not quiet:
                self._touch(session_id)

    def _insert_recordings(self, session_id: str, recordings: list[Recording]) -> None:
        self._conn.executemany(
            """INSERT OR REPLACE INTO recordings
               (id, session_id, source, device_id, device_name, path, created_at, part,
                part_offset)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            [
                (
                    r.id,
                    session_id,
                    r.source,
                    r.device_id,
                    r.device_name,
                    r.path,
                    r.created_at.isoformat(),
                    r.part,
                    r.offset,
                )
                for r in recordings
            ],
        )

    def clear_audio(self, session_id: str) -> bool:
        """Forget a session's audio (recordings rows + mixed file path). Files are the
        caller's job. Returns False for an unknown session."""
        self._check_write(session_id)
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE sessions SET audio_file=NULL, updated_at=? WHERE id=?",
                (datetime.now().isoformat(), session_id),
            )
            if cur.rowcount == 0:
                return False
            self._conn.execute("DELETE FROM recordings WHERE session_id=?", (session_id,))
            self._touch(session_id)
        return True

    # ---- other assistants' notes ---------------------------------------

    def add_external_notes(self, session_id: str, notes: ExternalNotes) -> None:
        self._check_write(session_id)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO external_notes (id, session_id, source, text, filename, added_at)"
                " VALUES (?,?,?,?,?,?)",
                (
                    notes.id,
                    session_id,
                    notes.source,
                    notes.text,
                    notes.filename,
                    notes.added_at.isoformat(),
                ),
            )
            self._touch(session_id)

    def delete_external_notes(self, session_id: str, notes_id: str) -> bool:
        self._check_write(session_id)
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM external_notes WHERE id=? AND session_id=?", (notes_id, session_id)
            )
            if cur.rowcount:
                self._touch(session_id)
        return cur.rowcount > 0

    # ---- assets (resources) --------------------------------------------

    def add_asset(self, asset: Asset, path: str | None = None, text: str | None = None) -> Asset:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO assets (id, kind, title, url, filename, size, path, text, source,"
                " created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    asset.id,
                    asset.kind,
                    asset.title,
                    asset.url,
                    asset.filename,
                    asset.size,
                    path,
                    text,
                    asset.source,
                    asset.created_at.isoformat(),
                ),
            )
        return self.get_asset(asset.id)

    def get_asset(self, asset_id: str) -> Asset | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        return _asset(row) if row else None

    def asset_file(self, asset_id: str) -> tuple[str | None, str | None]:
        """A file asset's stored path (relative to the assets folder) and its text."""
        with self._lock:
            row = self._conn.execute(
                "SELECT path, text FROM assets WHERE id=?", (asset_id,)
            ).fetchone()
        return (row["path"], row["text"]) if row else (None, None)

    def find_link(self, url: str) -> Asset | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM assets WHERE kind='link' AND url=?", (url,)
            ).fetchone()
        return _asset(row) if row else None

    def list_assets(self, query: str = "", limit: int = 100) -> list[tuple[Asset, int]]:
        """The library, newest first, with how many meetings use each asset."""
        like = f"%{query.strip()}%"
        with self._lock:
            rows = self._conn.execute(
                "SELECT a.*, (SELECT count(*) FROM session_assets sa WHERE sa.asset_id = a.id)"
                " AS used FROM assets a WHERE a.title LIKE ? OR ifnull(a.url, '') LIKE ?"
                " OR ifnull(a.filename, '') LIKE ? ORDER BY a.created_at DESC LIMIT ?",
                (like, like, like, limit),
            ).fetchall()
        return [(_asset(r), r["used"]) for r in rows]

    def asset_texts(self, session_id: str) -> list[tuple[Asset, str]]:
        if not self._readable(session_id):
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT a.* FROM assets a JOIN session_assets sa ON sa.asset_id = a.id"
                " WHERE sa.session_id=? AND a.text IS NOT NULL ORDER BY sa.added_at",
                (session_id,),
            ).fetchall()
        return [(_asset(r), r["text"]) for r in rows]

    def attach_asset(self, session_id: str, asset_id: str) -> None:
        self._check_write(session_id)
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO session_assets (session_id, asset_id, added_at)"
                " VALUES (?,?,?)",
                (session_id, asset_id, datetime.now().isoformat()),
            )
            if cur.rowcount:
                self._touch(session_id)

    def detach_asset(self, session_id: str, asset_id: str) -> bool:
        self._check_write(session_id)
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM session_assets WHERE session_id=? AND asset_id=?",
                (session_id, asset_id),
            )
            if cur.rowcount:
                self._touch(session_id)
        return cur.rowcount > 0

    def update_asset(self, asset_id: str, **fields) -> Asset | None:
        allowed = {"title", "path"}
        if set(fields) - allowed:
            raise ValueError(f"Cannot update: {sorted(set(fields) - allowed)}")
        with self._lock, self._conn:
            for key, value in fields.items():
                self._conn.execute(f"UPDATE assets SET {key}=? WHERE id=?", (value, asset_id))
            if fields:
                self._touch_asset_users(asset_id)
        return self.get_asset(asset_id)

    def delete_asset(self, asset_id: str) -> str | None:
        """Remove an asset from the library (and every meeting). Returns its file's path."""
        path, _ = self.asset_file(asset_id)
        with self._lock, self._conn:
            users = self._asset_users(asset_id)  # the cascade removes the links
            cur = self._conn.execute("DELETE FROM assets WHERE id=?", (asset_id,))
            if cur.rowcount:
                for sid in users:
                    self._touch(sid)
        return path if cur.rowcount else None

    def _asset_users(self, asset_id: str) -> list[str]:
        return [
            r["session_id"]
            for r in self._conn.execute(
                "SELECT session_id FROM session_assets WHERE asset_id=?", (asset_id,)
            )
        ]

    def _touch_asset_users(self, asset_id: str) -> None:
        for sid in self._asset_users(asset_id):
            self._touch(sid)

    def asset_paths(self) -> list[tuple[str, str]]:
        """(id, path) of every file asset: for encryption on and off."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, path FROM assets WHERE path IS NOT NULL"
            ).fetchall()
        return [(r["id"], r["path"]) for r in rows]

    # ---- bookmarks -----------------------------------------------------

    def add_bookmark(self, session_id: str, part: int, seconds: float, note: str = "") -> str:
        from uuid import uuid4

        self._check_write(session_id)
        bookmark_id = str(uuid4())[:8]
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO bookmarks (id, session_id, part, seconds, note, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (
                    bookmark_id,
                    session_id,
                    part,
                    max(0.0, seconds),
                    note,
                    datetime.now().isoformat(),
                ),
            )
            self._touch(session_id)
        return bookmark_id

    def update_bookmark(self, session_id: str, bookmark_id: str, note: str) -> bool:
        self._check_write(session_id)
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE bookmarks SET note=? WHERE id=? AND session_id=?",
                (note, bookmark_id, session_id),
            )
            if cur.rowcount:
                self._touch(session_id)
        return cur.rowcount > 0

    def delete_bookmark(self, session_id: str, bookmark_id: str) -> bool:
        self._check_write(session_id)
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM bookmarks WHERE id=? AND session_id=?", (bookmark_id, session_id)
            )
            if cur.rowcount:
                self._touch(session_id)
        return cur.rowcount > 0

    def move_bookmarks(self, moves: list[tuple[str, int, str, int]]) -> None:
        """(session, part) -> (session, part), all at once: meetings combined
        (services/combine.py) renumber their parts."""
        with self._lock, self._conn:
            rows = self._conn.execute("SELECT id, session_id, part FROM bookmarks").fetchall()
            where = {(old_s, old_p): (new_s, new_p) for old_s, old_p, new_s, new_p in moves}
            moved = False
            for r in rows:
                if (dest := where.get((r["session_id"], r["part"]))) is not None:
                    self._conn.execute(
                        "UPDATE bookmarks SET session_id=?, part=? WHERE id=?", (*dest, r["id"])
                    )
                    moved = True
            if moved:
                self._touch_all(moves)

    # ---- comments ------------------------------------------------------

    _COMMENT_FIELDS = frozenset(
        {
            "id", "parent_id", "target", "target_id", "anchor", "quote", "text", "author_id",
            "author", "mentions", "created_at", "updated_at",
        }
    )  # fmt: skip

    @staticmethod
    def _comment(r: sqlite3.Row) -> Comment:
        return Comment(
            id=r["id"],
            session_id=r["session_id"],
            parent_id=r["parent_id"],
            target=r["target"],
            target_id=r["target_id"],
            anchor=json.loads(r["anchor"] or "{}"),
            quote=r["quote"],
            text=r["text"],
            author_id=r["author_id"],
            author=r["author"],
            mentions=json.loads(r["mentions"] or "[]"),
            created_at=_dt(r["created_at"]),
            updated_at=_dt(r["updated_at"]),
            resolved_at=_dt(r["resolved_at"]) if r["resolved_at"] else None,
            resolved_by=r["resolved_by"],
            deleted=bool(r["deleted"]),
        )

    def _comment_session(self, comment_id: str) -> str | None:
        """The meeting a comment is in, or None; Hidden when the caller may not see it."""
        with self._lock:
            row = self._conn.execute(
                "SELECT session_id FROM comments WHERE id=?", (comment_id,)
            ).fetchone()
        if row is None:
            return None
        if not self._readable(row["session_id"]):
            raise access.Hidden("Session not found")
        return row["session_id"]

    def comment_rows(self, session_id: str) -> list[dict]:
        """A meeting's comments as stored (JSON columns decoded), no access check: for library
        sync, which sends whole meetings."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM comments WHERE session_id=? ORDER BY created_at, id", (session_id,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["anchor"] = json.loads(d["anchor"] or "{}")
            d["mentions"] = json.loads(d["mentions"] or "[]")
            d["deleted"] = bool(d["deleted"])
            out.append(d)
        return out

    def open_comment_counts(self, visible: set[str] | None = None) -> Counter:
        """Open (not resolved, not deleted) root threads on action items and decisions, by
        (session_id, target, target_id), over the meetings in `visible` (None = all)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT session_id, target, target_id, COUNT(*) AS n FROM comments"
                " WHERE parent_id='' AND deleted=0 AND resolved_at IS NULL"
                " AND target IN ('item','decision') AND target_id!=''"
                " GROUP BY session_id, target, target_id"
            ).fetchall()
        out: Counter = Counter()
        for r in rows:
            if visible is None or r["session_id"] in visible:
                out[(r["session_id"], r["target"], r["target_id"])] += r["n"]
        return out

    def list_comments(self, session_id: str) -> list[Comment]:
        """A meeting's comments, oldest first, deleted ones included (they hold threads
        together). Empty for a meeting that is not there; Hidden when the caller may not
        see it."""
        if self._owner(session_id) is None:
            return []
        if not self._readable(session_id):
            raise access.Hidden("Session not found")
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM comments WHERE session_id=? ORDER BY created_at, id", (session_id,)
            ).fetchall()
        return [self._comment(r) for r in rows]

    def get_comment(self, comment_id: str) -> Comment | None:
        if self._comment_session(comment_id) is None:
            return None
        with self._lock:
            row = self._conn.execute("SELECT * FROM comments WHERE id=?", (comment_id,)).fetchone()
        return None if row is None else self._comment(row)

    def add_comment(self, session_id: str, **fields) -> str:
        """Add a comment (any of `_COMMENT_FIELDS`; `text` is required) and return its id (minted
        here, unless `id` is given: a notes comment's mark carries it). Readers may comment: who
        may do what beyond that is the service's rule."""
        unknown = set(fields) - self._COMMENT_FIELDS
        if unknown:
            raise TypeError(f"Unknown comment fields: {', '.join(sorted(unknown))}")
        if "text" not in fields:
            raise TypeError("A comment needs text")
        if not self._readable(session_id):
            raise access.Hidden("Session not found")
        now = datetime.now().isoformat()
        comment_id = fields.get("id") or uuid4().hex[:12]
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO comments (id, session_id, parent_id, target, target_id, anchor,"
                " quote, text, author_id, author, mentions, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    comment_id,
                    session_id,
                    fields.get("parent_id", ""),
                    fields.get("target", ""),
                    fields.get("target_id", ""),
                    json.dumps(fields.get("anchor") or {}, sort_keys=True),
                    fields.get("quote", ""),
                    fields["text"],
                    fields.get("author_id", ""),
                    fields.get("author", ""),
                    json.dumps(fields.get("mentions") or []),
                    fields.get("created_at") or now,
                    fields.get("updated_at") or now,
                ),
            )
            self._touch(session_id)
        return comment_id

    def update_comment_text(
        self, comment_id: str, text: str, mentions: list[str] | None = None
    ) -> bool:
        """Edit a comment (and its @mentions when given). False when it is gone or deleted."""
        session_id = self._comment_session(comment_id)
        if session_id is None:
            return False
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE comments SET text=?, updated_at=?,"
                " mentions=CASE WHEN ? IS NULL THEN mentions ELSE ? END"
                " WHERE id=? AND deleted=0",
                (
                    text,
                    datetime.now().isoformat(),
                    None if mentions is None else json.dumps(mentions),
                    None if mentions is None else json.dumps(mentions),
                    comment_id,
                ),
            )
            if cur.rowcount:
                self._touch(session_id)
        return cur.rowcount > 0

    def set_comment_resolved(
        self, comment_id: str, resolved_at: datetime | None, resolved_by: str = ""
    ) -> bool:
        """Resolve a comment (when, by whom) or reopen it (`resolved_at=None`)."""
        session_id = self._comment_session(comment_id)
        if session_id is None:
            return False
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE comments SET resolved_at=?, resolved_by=? WHERE id=?",
                (
                    None if resolved_at is None else resolved_at.isoformat(),
                    "" if resolved_at is None else resolved_by,
                    comment_id,
                ),
            )
            if cur.rowcount:
                self._touch(session_id)
        return cur.rowcount > 0

    def delete_comment(self, comment_id: str) -> bool:
        """Remove a comment: the row goes when nothing replies to it, else it stays as an empty
        `deleted` one so the replies keep their thread. False when it is gone or already deleted."""
        session_id = self._comment_session(comment_id)
        if session_id is None:
            return False
        with self._lock, self._conn:
            replied = self._conn.execute(
                "SELECT 1 FROM comments WHERE parent_id=? LIMIT 1", (comment_id,)
            ).fetchone()
            if replied is None:
                cur = self._conn.execute("DELETE FROM comments WHERE id=?", (comment_id,))
            else:
                cur = self._conn.execute(
                    "UPDATE comments SET deleted=1, text='', mentions='[]', updated_at=?"
                    " WHERE id=? AND deleted=0",
                    (datetime.now().isoformat(), comment_id),
                )
            if cur.rowcount:
                self._touch(session_id)
        return cur.rowcount > 0

    def move_comments(self, moves: list[tuple[str, int, str, int]]) -> None:
        """(session, part) -> (session, part), all at once, like move_bookmarks: meetings
        combined (services/combine.py) renumber their parts, and a transcript comment's anchor
        carries its `part`. Comments of a meeting that is absorbed and name no part (on the
        summary) come along to the one meeting it joins; one on a part that is not in `moves`
        comes along without its place (it shows as detached, with its quote)."""
        where = {(old_s, old_p): (new_s, new_p) for old_s, old_p, new_s, new_p in moves}
        targets = {new_s for _, _, new_s, _ in moves}
        sources = {old_s for old_s, _, _, _ in moves} - targets
        join = next(iter(targets)) if len(targets) == 1 else None
        with self._lock, self._conn:
            rows = self._conn.execute("SELECT id, session_id, anchor FROM comments").fetchall()
            moved = False
            for r in rows:
                anchor = json.loads(r["anchor"] or "{}")
                part = anchor.get("part")
                if isinstance(part, int):
                    if (dest := where.get((r["session_id"], part))) is not None:
                        anchor["part"] = dest[1]
                        new_session = dest[0]
                    elif r["session_id"] in sources and join is not None:
                        for key in ("part", "start", "end"):
                            anchor.pop(key, None)
                        new_session = join
                    else:
                        continue
                elif r["session_id"] in sources and join is not None:
                    new_session = join
                else:
                    continue
                self._conn.execute(
                    "UPDATE comments SET session_id=?, anchor=? WHERE id=?",
                    (new_session, json.dumps(anchor, sort_keys=True), r["id"]),
                )
                moved = True
            if moved:
                self._touch_all(moves)

    # ---- live flags ----------------------------------------------------

    def add_live_flag(
        self, session_id: str, source: str, start: float, end: float, text: str, part: int = 0
    ) -> LiveFlag:
        from uuid import uuid4

        self._check_write(session_id)
        flag = LiveFlag(
            id=str(uuid4())[:8],
            source=source,
            start=start,
            end=end,
            text=text,
            part=part,
            created_at=datetime.now(),
        )
        with self._lock, self._conn:
            self._conn.execute(
                'INSERT INTO live_flags (id, session_id, source, start, "end", text, created_at,'
                " part) VALUES (?,?,?,?,?,?,?,?)",
                (
                    flag.id,
                    session_id,
                    source,
                    start,
                    end,
                    text,
                    flag.created_at.isoformat(),
                    part,
                ),
            )
            self._touch(session_id)
        return flag

    def list_live_flags(self, session_id: str) -> list[LiveFlag]:
        """The flags of a meeting; empty when there is none or the caller may not see it."""
        if self._owner(session_id) is None or not self._readable(session_id):
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM live_flags WHERE session_id=? ORDER BY part, start, created_at",
                (session_id,),
            ).fetchall()
        return [
            LiveFlag(
                id=r["id"],
                source=r["source"],
                start=r["start"],
                end=r["end"],
                text=r["text"],
                part=r["part"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    def delete_live_flag(self, session_id: str, flag_id: str) -> bool:
        self._check_write(session_id)
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM live_flags WHERE id=? AND session_id=?", (flag_id, session_id)
            )
            if cur.rowcount:
                self._touch(session_id)
        return cur.rowcount > 0

    # ---- history -------------------------------------------------------

    def add_event(
        self, session_id: str, kind: str, part: int | None = None, detail: dict | None = None
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO session_events (session_id, at, kind, part, detail, uid)"
                " VALUES (?,?,?,?,?,?)",
                (
                    session_id,
                    datetime.now().isoformat(),
                    kind,
                    part,
                    json.dumps(detail or {}),
                    uuid4().hex[:16],
                ),
            )

    # ---- vault notes (internal bookkeeping: no access checks, like events being written) --

    def get_vault_note(self, session_id: str) -> dict | None:
        """{path, history (bytes), boxes (dict), written_sha, updated_at} or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT path, history, boxes, written_sha, updated_at FROM vault_notes"
                " WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "path": row["path"],
            "history": bytes(row["history"]),
            "boxes": json.loads(row["boxes"]),
            "written_sha": row["written_sha"],
            "updated_at": row["updated_at"],
        }

    def put_vault_note(
        self, session_id: str, path: str, history: bytes, boxes: dict, written_sha: str
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO vault_notes"
                " (session_id, path, history, boxes, written_sha, updated_at) VALUES (?,?,?,?,?,?)",
                (
                    session_id,
                    path,
                    history,
                    json.dumps(boxes),
                    written_sha,
                    datetime.now().isoformat(),
                ),
            )

    def delete_vault_note(self, session_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM vault_notes WHERE session_id=?", (session_id,))

    # ---- summary documents (internal bookkeeping: no access checks, like vault notes) -----

    def get_summary_doc(self, session_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT doc FROM summary_docs WHERE session_id=?", (session_id,)
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["doc"])

    def put_summary_doc(self, session_id: str, doc: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO summary_docs (session_id, doc, updated_at) VALUES (?,?,?)",
                (session_id, json.dumps(doc), datetime.now().isoformat()),
            )

    def delete_summary_doc(self, session_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM summary_docs WHERE session_id=?", (session_id,))

    # ---- publications (services/publishing.py; no access checks: the caller checks) -----

    _PUB_COLS = (
        "id, session_id, key, token, place, host_url, version, selection, created_at,"
        " updated_at, expires_at, unpublished_at, clip_blobs, published_digest, published_by,"
        " origin"
    )

    @staticmethod
    def _publication(r: sqlite3.Row) -> dict:
        row = dict(r)
        row["key"] = bytes(row["key"])
        row["selection"] = json.loads(row["selection"])
        row["clip_blobs"] = json.loads(row["clip_blobs"] or "{}")
        return row

    def live_publication(self, session_id: str) -> dict | None:
        """The meeting's publication that is not unpublished, as a dict of its columns."""
        with self._lock:
            r = self._conn.execute(
                f"SELECT {self._PUB_COLS} FROM publications"
                " WHERE session_id=? AND unpublished_at IS NULL",
                (session_id,),
            ).fetchone()
        return None if r is None else self._publication(r)

    def get_publication(self, pub_id: str) -> dict | None:
        with self._lock:
            r = self._conn.execute(
                f"SELECT {self._PUB_COLS} FROM publications WHERE id=?", (pub_id,)
            ).fetchone()
        return None if r is None else self._publication(r)

    def publications(self, live_only: bool = True) -> list[dict]:
        where = " WHERE unpublished_at IS NULL" if live_only else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._PUB_COLS} FROM publications{where} ORDER BY updated_at DESC"
            ).fetchall()
        return [self._publication(r) for r in rows]

    def session_publications(self, session_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._PUB_COLS} FROM publications WHERE session_id=?"
                " ORDER BY created_at, id",
                (session_id,),
            ).fetchall()
        return [self._publication(r) for r in rows]

    @staticmethod
    def _publication_values(row: dict) -> tuple:
        return (
            row["id"],
            row["session_id"],
            row["key"],
            row["token"],
            row["place"],
            row.get("host_url", ""),
            row["version"],
            json.dumps(row["selection"]),
            row["created_at"],
            row["updated_at"],
            row.get("expires_at"),
            row.get("unpublished_at"),
            json.dumps(row.get("clip_blobs") or {}),
            row.get("published_digest", ""),
            row.get("published_by", ""),
            row.get("origin", ""),
        )

    _PUB_UPSERT = (
        f"INSERT INTO publications ({_PUB_COLS}) VALUES ({','.join('?' * 16)})"
        " ON CONFLICT(id) DO UPDATE SET "
        + ", ".join(
            f"{c.strip()}=excluded.{c.strip()}"
            for c in _PUB_COLS.split(",")
            if c.strip() not in ("id", "session_id")
        )
    )

    def put_publication(self, row: dict) -> None:
        """Insert or replace a publication (every column of `_PUB_COLS`). A new version of the
        meeting for library sync (`_touch`): the row travels."""
        with self._lock, self._conn:
            self._conn.execute(self._PUB_UPSERT, self._publication_values(row))
            self._touch(row["session_id"])

    # ---- guest notes rooms (services/guest_notes.py; no access checks, no `_touch`) -----

    _GUEST_COLS = (
        "session_id, room, key, token, room_token, host_url, created_at, started_by, ended_at,"
        " deleted_at, expires_at, auto_end_at, last_seq"
    )
    _GUEST_FIELDS = frozenset(c.strip() for c in _GUEST_COLS.split(",")) - {"session_id"}

    @staticmethod
    def _guest_room(r: sqlite3.Row) -> dict:
        row = dict(r)
        row["key"] = bytes(row["key"])
        return row

    def guest_room(self, session_id: str) -> dict | None:
        with self._lock:
            r = self._conn.execute(
                f"SELECT {self._GUEST_COLS} FROM guest_rooms WHERE session_id=?", (session_id,)
            ).fetchone()
        return None if r is None else self._guest_room(r)

    def put_guest_room(self, row: dict) -> None:
        """Insert or replace the meeting's room (missing optional columns take their defaults)."""
        defaults = {
            "started_by": "",
            "ended_at": "",
            "deleted_at": "",
            "expires_at": "",
            "auto_end_at": "",
            "last_seq": 0,
        }
        full = {**defaults, **row}
        cols = [c.strip() for c in self._GUEST_COLS.split(",")]
        with self._lock, self._conn:
            self._conn.execute(
                f"INSERT OR REPLACE INTO guest_rooms ({self._GUEST_COLS})"
                f" VALUES ({','.join('?' * len(cols))})",
                tuple(full[c] for c in cols),
            )

    def live_guest_rooms(self) -> list[dict]:
        """Rooms not deleted, ended ones included (they still need expiry handling)."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._GUEST_COLS} FROM guest_rooms WHERE deleted_at=''"
                " ORDER BY created_at, session_id"
            ).fetchall()
        return [self._guest_room(r) for r in rows]

    def set_guest_room(self, session_id: str, **fields) -> None:
        if not fields:
            return
        bad = set(fields) - self._GUEST_FIELDS
        if bad:
            raise ValueError(f"unknown guest room fields: {sorted(bad)}")
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE guest_rooms SET {sets} WHERE session_id=?",
                (*fields.values(), session_id),
            )

    def mark_unpublished(self, pub_id: str, at: str, *, touch: bool = True) -> None:
        """`touch=False` for what every machine works out for itself (an expiry)."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE publications SET unpublished_at=?, updated_at=? WHERE id=?",
                (at, at, pub_id),
            )
            if touch:
                r = self._conn.execute(
                    "SELECT session_id FROM publications WHERE id=?", (pub_id,)
                ).fetchone()
                if r is not None:
                    self._touch(r["session_id"])

    def store_synced_publications(self, session_id: str, publications: list) -> None:
        """Library sync's write of a meeting's publications (`SyncPublication`s, already merged
        with ours): written as given, unpublished ones first so a row going dead never meets
        the live one that replaces it (one live row per meeting). The caller's transaction
        counts the new version. Rows we have that are not listed stay. Their `origin` is the
        sender's view: its own sync id means that computer."""
        ordered = sorted(publications, key=lambda p: not p.unpublished_at)
        for p in ordered:
            row = {
                "id": p.id,
                "session_id": session_id,
                "key": base64.b64decode(p.key),
                "token": p.token,
                "place": p.where,
                "host_url": p.host_url,
                "version": p.version,
                "selection": p.selection.model_dump(mode="json"),
                "created_at": p.created_at,
                "updated_at": p.updated_at,
                "expires_at": p.expires_at,
                "unpublished_at": p.unpublished_at,
                "clip_blobs": p.clip_blobs,
                "published_digest": p.published_digest,
                "published_by": p.published_by,
                "origin": "" if p.origin == self.device_id else p.origin,
            }
            self._conn.execute(self._PUB_UPSERT, self._publication_values(row))

    # ---- live shared notes (services/shared_notes.py; no access checks: the caller checks) --

    def notes_doc(self, session_id: str) -> dict | None:
        """The folded document: {snapshot (bytes or None), folded_seq, updated_at, last_by,
        last_at, markdown}, or None when the meeting has none yet."""
        with self._lock:
            row = self._conn.execute(
                "SELECT snapshot, folded_seq, updated_at, last_by, last_at, markdown"
                " FROM shared_notes"
                " WHERE session_id=?",
                (session_id,),
            ).fetchone()
        return None if row is None else dict(row)

    def notes_source(self, session_id: str) -> tuple[str, str] | None:
        """(Session.notes, where the meeting is from) without loading the meeting, or None for an
        unknown one. The second is its `origin`, or for a meeting shared with us the sharer's key
        (`shared_from`): either way not here, so its notes document comes from there and is never
        seeded here (services/shared_notes.py `seed_blocking`, `needs_doc_from_sync`)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT notes, origin, shared_from FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
        if row is None:
            return None
        return (row["notes"] or "", row["origin"] or row["shared_from"] or "")

    def has_notes_doc(self, session_id: str) -> bool:
        """A document exists: a stored snapshot or any frame."""
        with self._lock:
            row = self._conn.execute(
                "SELECT EXISTS(SELECT 1 FROM shared_notes WHERE session_id=?"
                " AND snapshot IS NOT NULL)"
                " OR EXISTS(SELECT 1 FROM shared_notes_ops WHERE session_id=?) AS has",
                (session_id, session_id),
            ).fetchone()
        return bool(row["has"])

    def notes_catchup(self, session_id: str) -> tuple[dict | None, list[tuple[int, bytes]]]:
        """The folded document and the frames after it, read together: a fold committing between
        two separate reads would hand out the old snapshot without the frames it just folded."""
        with self._lock:
            row = self._conn.execute(
                "SELECT snapshot, folded_seq, updated_at, last_by, last_at, markdown"
                " FROM shared_notes WHERE session_id=?",
                (session_id,),
            ).fetchone()
            doc = None if row is None else dict(row)
            rows = self._conn.execute(
                "SELECT seq, frame FROM shared_notes_ops WHERE session_id=? AND seq>? ORDER BY seq",
                (session_id, doc["folded_seq"] if doc else 0),
            ).fetchall()
        return doc, [(r["seq"], bytes(r["frame"])) for r in rows]

    def has_unfolded_notes(self, session_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM shared_notes_ops WHERE session_id=? AND folded=0 LIMIT 1",
                (session_id,),
            ).fetchone()
        return row is not None

    def notes_ops(self, session_id: str) -> list[dict]:
        """Every frame with its author, in log order (folded ones too)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, frame, author_id, author, at, folded FROM shared_notes_ops"
                " WHERE session_id=? ORDER BY seq",
                (session_id,),
            ).fetchall()
        return [{**dict(r), "frame": bytes(r["frame"])} for r in rows]

    def append_notes_ops(
        self, session_id: str, frames: list[bytes], author_id: str, author: str, last_edit: bool
    ) -> list[int]:
        """Append frames to the log; their sequence numbers. `last_edit`: show `author` as the
        last to edit."""
        if not frames:
            return []
        now = datetime.now().isoformat()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS top FROM shared_notes_ops WHERE session_id=?",
                (session_id,),
            ).fetchone()
            seqs = list(range(row["top"] + 1, row["top"] + 1 + len(frames)))
            self._conn.executemany(
                "INSERT INTO shared_notes_ops (session_id, seq, frame, author_id, author, at)"
                " VALUES (?,?,?,?,?,?)",
                [
                    (session_id, s, f, author_id, author, now)
                    for s, f in zip(seqs, frames, strict=True)
                ],
            )
            self._conn.execute(
                "INSERT INTO shared_notes (session_id, updated_at, last_by, last_at)"
                " VALUES (?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET"
                " last_by=CASE WHEN ? THEN excluded.last_by ELSE last_by END,"
                " last_at=CASE WHEN ? THEN excluded.last_at ELSE last_at END",
                (
                    session_id,
                    now,
                    author if last_edit else "",
                    now if last_edit else None,
                    int(last_edit),
                    int(last_edit),
                ),
            )
        return seqs

    def store_notes_fold(
        self,
        session_id: str,
        snapshot: bytes,
        upto_seq: int,
        markdown: str | None,
        plain: str | None = None,
        comments: dict | None = None,
    ) -> None:
        """The snapshot now holds every frame up to `upto_seq` and renders as `markdown` (None:
        not known, e.g. a snapshot merged in by sync before its fold; no catching up then).
        `plain` and `comments` are the fold's plain text and each comment mark's range in it
        (the sidecar's answer; None = not known, then notes comments show as detached).

        A snapshot that differs from the stored one (the document's first, or formatting, comment
        marks and artifacts that leave the markdown alone) is a new version of the meeting for
        library sync (`_touch`), or it would never travel. Not when `Session.notes` is about to
        change anyway: the caller's write of it counts, once. Unchanged: no new version."""
        now = datetime.now().isoformat()
        with self._lock, self._conn:
            before = self._conn.execute(
                "SELECT snapshot FROM shared_notes WHERE session_id=?", (session_id,)
            ).fetchone()
            self._conn.execute(
                "INSERT INTO shared_notes (session_id, snapshot, folded_seq, updated_at, markdown,"
                " plain, comment_ranges) VALUES (?,?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE"
                " SET snapshot=excluded.snapshot, folded_seq=excluded.folded_seq,"
                " updated_at=excluded.updated_at, markdown=excluded.markdown,"
                " plain=excluded.plain, comment_ranges=excluded.comment_ranges",
                (session_id, snapshot, upto_seq, now, markdown, plain, _ranges_json(comments)),
            )
            self._conn.execute(
                "UPDATE shared_notes_ops SET folded=1 WHERE session_id=? AND seq<=? AND folded=0",
                (session_id, upto_seq),
            )
            old = (
                None if before is None or before["snapshot"] is None else bytes(before["snapshot"])
            )
            if old != bytes(snapshot):
                row = self._conn.execute(
                    "SELECT notes FROM sessions WHERE id=?", (session_id,)
                ).fetchone()
                if row is not None and (markdown is None or (row["notes"] or "") == markdown):
                    self._touch(session_id)

    def store_synced_notes_doc(
        self,
        session_id: str,
        snapshot: bytes,
        markdown: str | None,
        frames: list[bytes],
        author: str,
        notes: str | None = None,
        plain: str | None = None,
        comments: dict | None = None,
    ) -> list[int]:
        """A notes document that came by library sync (services/sync_notes.py), one transaction:
        `frames` (what the local snapshot lacked of it, for the connected editors) are appended
        to the log as `author`, already folded; `snapshot` (the union, or the peer's document
        adopted where there was none) becomes the stored one with its `markdown`; `notes`, when
        given, becomes `Session.notes` in the same breath; `plain` and `comments` as in
        `store_notes_fold`. No new version (`_touch`): the sync
        apply path counts it. Frames that arrived from editors meanwhile (unfolded, so not in
        `snapshot`) stay unfolded. Returns the appended frames' sequence numbers."""
        now = datetime.now().isoformat()
        with self._lock, self._conn:
            top = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS top FROM shared_notes_ops WHERE session_id=?",
                (session_id,),
            ).fetchone()["top"]
            seqs = list(range(top + 1, top + 1 + len(frames)))
            self._conn.executemany(
                "INSERT INTO shared_notes_ops (session_id, seq, frame, author_id, author, at,"
                " folded) VALUES (?,?,?,?,?,?,1)",
                [(session_id, q, f, "", author, now) for q, f in zip(seqs, frames, strict=True)],
            )
            self._conn.execute(
                "INSERT INTO shared_notes (session_id, snapshot, folded_seq, updated_at, markdown,"
                " plain, comment_ranges) VALUES (?,?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE"
                " SET snapshot=excluded.snapshot, updated_at=excluded.updated_at,"
                " markdown=excluded.markdown, plain=excluded.plain,"
                " comment_ranges=excluded.comment_ranges,"
                " folded_seq=MAX(folded_seq, excluded.folded_seq)",
                (
                    session_id,
                    snapshot,
                    seqs[-1] if seqs else 0,
                    now,
                    markdown,
                    plain,
                    _ranges_json(comments),
                ),
            )
            if notes is not None:
                self._conn.execute("UPDATE sessions SET notes=? WHERE id=?", (notes, session_id))
        return seqs

    def notes_marks(self, session_id: str) -> tuple[str, dict[str, list[int] | None]] | None:
        """(plain text, {comment id: [start, end) | None}) of the document as last folded, for
        placing notes comments; None when nothing was stored (no document, or folded before the
        ranges were kept, or by a sidecar that did not report them)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT plain, comment_ranges FROM shared_notes WHERE session_id=?", (session_id,)
            ).fetchone()
        if row is None or row["plain"] is None or row["comment_ranges"] is None:
            return None
        try:
            ranges = json.loads(row["comment_ranges"])
        except ValueError:
            return None
        return row["plain"], ranges if isinstance(ranges, dict) else {}

    def set_notes_markdown(self, session_id: str, markdown: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE shared_notes SET markdown=? WHERE session_id=?", (markdown, session_id)
            )

    def notes_drift(self, session_id: str) -> str | None:
        """Session.notes when it differs from the document's last folded markdown (it was
        written around the document), else None. None too without a folded document."""
        with self._lock:
            row = self._conn.execute(
                "SELECT s.notes FROM sessions s JOIN shared_notes n ON n.session_id = s.id"
                " WHERE s.id=? AND n.markdown IS NOT NULL AND s.notes IS NOT n.markdown",
                (session_id,),
            ).fetchone()
        return None if row is None else (row["notes"] or "")

    def events(self, session_id: str) -> list[dict]:
        if not self._readable(session_id):
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT at, kind, part, detail FROM session_events WHERE session_id=?"
                " ORDER BY at, id",
                (session_id,),
            ).fetchall()
        return [
            {
                "at": _dt(r["at"]),
                "kind": r["kind"],
                "part": r["part"],
                "detail": json.loads(r["detail"]),
            }
            for r in rows
        ]

    def move_events(self, moves: list[tuple[str, int, str, int]]) -> None:
        """Like move_bookmarks: a combined meeting keeps the history of both, parts renumbered.
        Events of the other meeting that name no part come along too."""
        where = {(old_s, old_p): (new_s, new_p) for old_s, old_p, new_s, new_p in moves}
        with self._lock, self._conn:
            rows = self._conn.execute("SELECT id, session_id, part FROM session_events").fetchall()
            targets = {new_s for _, _, new_s, _ in moves}
            sources = {old_s for old_s, _, _, _ in moves}
            moved = False
            for r in rows:
                if (dest := where.get((r["session_id"], r["part"]))) is not None:
                    self._conn.execute(
                        "UPDATE session_events SET session_id=?, part=? WHERE id=?",
                        (*dest, r["id"]),
                    )
                    moved = True
                elif (
                    r["part"] is None and r["session_id"] in sources - targets and len(targets) == 1
                ):
                    self._conn.execute(
                        "UPDATE session_events SET session_id=? WHERE id=?",
                        (next(iter(targets)), r["id"]),
                    )
                    moved = True
            if moved:
                self._touch_all(moves)

    def delete(
        self, session_id: str, *, device_id: str | None = None, tombstone: bool = True
    ) -> bool:
        """Delete a meeting and everything of it. Library sync learns of it from a tombstone
        (who deleted it, when, its name): `tombstone=False` for a delete that arrived from a
        peer, which has its tombstone already."""
        self._check_write(session_id)
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT name, shared_from FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            # The cascade removes the meeting's peer_shares: keep who had one, so the contact
            # still hears the meeting is gone (`shared_tombstones_since`).
            self._conn.execute(
                "INSERT OR REPLACE INTO peer_share_tombstones(session_id, person, at)"
                " SELECT session_id, person, ? FROM peer_shares WHERE session_id=?",
                (datetime.now().isoformat(), session_id),
            )
            cur = self._conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            if cur.rowcount:
                self._conn.execute("DELETE FROM sync_served WHERE session_id=?", (session_id,))
                self._conn.execute(
                    "DELETE FROM meta WHERE key IN (?, ?)",
                    (f"{PUSHED_SEQ_PREFIX}{session_id}", f"{REFUSED_NOTES_PREFIX}{session_id}"),
                )
                # A meeting shared with us is the owner's to delete: it leaves no tombstone, or
                # our other computers would delete their copy and we could not pull it again.
                if tombstone and row is not None and not row["shared_from"]:
                    self._conn.execute(
                        "INSERT OR REPLACE INTO sync_tombstones(session_id, at, device, name)"
                        " VALUES (?,?,?,?)",
                        (
                            session_id,
                            datetime.now().isoformat(),
                            self.device_id if device_id is None else device_id,
                            row["name"],
                        ),
                    )
        return cur.rowcount > 0

    # ---- editing -------------------------------------------------------

    def edit_segments(
        self, session_id: str, edit: Callable[[list[TranscriptSegment]], list[TranscriptSegment]]
    ) -> Session | None:
        """Apply `edit` to the transcript, then recompute participants."""
        self._check_write(session_id)
        session = self.get(session_id)
        if session is None:
            return None
        before = list(session.transcript)
        segments = edit(list(before))
        if segments != before:
            self.keep_version(session, "edited")
        speakers = list(dict.fromkeys(s.speaker for s in segments if s.speaker != "UNKNOWN"))
        # Keep participants that still appear, in their existing order, then new ones.
        participants = [p for p in session.participants if p in speakers]
        participants += [s for s in speakers if s not in participants]
        with self._lock, self._conn:
            if len(segments) == len(before):  # lines changed in place: write only those
                for i, (old, new) in enumerate(zip(before, segments, strict=True)):
                    if old != new:
                        words = [w.model_dump() for w in new.words] if new.words else None
                        self._conn.execute(
                            """UPDATE segments SET text=?, speaker=?, start=?, "end"=?, words=?
                               WHERE session_id=? AND idx=?""",
                            (
                                new.text,
                                new.speaker,
                                new.start,
                                new.end,
                                json.dumps(words) if words else None,
                                session_id,
                                i,
                            ),
                        )
            else:
                self._write_segments(session_id, segments)
            self._conn.execute(
                "UPDATE sessions SET participants=?, updated_at=? WHERE id=?",
                (json.dumps(participants), datetime.now().isoformat(), session_id),
            )
            self._touch(session_id)
        return self.get(session_id)

    # ---- records (services/records.py) -------------------------------

    def keep_version(self, before: Session | None, reason: str) -> None:
        """Keep a meeting's transcript and summary as they were `before` a change (the caller
        knows the content does change); nothing when that is already the last version."""
        from .records import canonical, content_hash, content_of

        if before is None or not (before.transcript or before.summary):
            return
        session_id = before.id
        self._check_write(session_id)  # callers may keep a version before their own write
        content = content_of(before)
        digest = content_hash(content)
        data = before.summary_data.model_dump_json() if before.summary_data else None
        who = access.principal()
        with self._lock, self._conn:
            last = self._conn.execute(
                "SELECT content_hash, summary_data FROM session_versions WHERE session_id=?"
                " ORDER BY id DESC",
                (session_id,),
            ).fetchone()
            if last is not None and last["content_hash"] == digest and last["summary_data"] == data:
                return
            self._conn.execute(
                "INSERT INTO session_versions (session_id, at, reason, by, content, content_hash,"
                " summary_data) VALUES (?,?,?,?,?,?,?)",
                (
                    session_id,
                    datetime.now().isoformat(),
                    reason,
                    who.name if who else "",
                    canonical(content).decode(),
                    digest,
                    data,
                ),
            )

    def versions(self, session_id: str, with_content: bool = False) -> list[dict]:
        if not self._readable(session_id):
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, at, reason, by, content_hash, content, summary_data"
                " FROM session_versions WHERE session_id=? ORDER BY id",
                (session_id,),
            ).fetchall()
        out = []
        for r in rows:
            v = {k: r[k] for k in ("id", "at", "reason", "by", "content_hash")}
            content = json.loads(r["content"])
            v["has_summary"] = bool(content.get("summary") or r["summary_data"])
            if with_content:
                v["content"] = content
                v["summary_data"] = json.loads(r["summary_data"]) if r["summary_data"] else None
            out.append(v)
        return out

    def add_seal(
        self, session_id: str, reason: str, content: str, audio: dict, *, force: bool = False
    ) -> dict | None:
        """Chain a new seal to the meeting's last one; None when nothing changed since (unless
        `force`: a seal that records an event, services/records.py EVENT_SEALS)."""
        from .records import chain_hash

        at = datetime.now().isoformat()
        with self._lock, self._conn:
            last = self._conn.execute(
                "SELECT content_hash, audio, chain FROM session_seals WHERE session_id=?"
                " ORDER BY id DESC",
                (session_id,),
            ).fetchone()
            if (
                not force
                and last is not None
                and last["content_hash"] == content
                and json.loads(last["audio"]) == audio
            ):
                return None
            prev = last["chain"] if last is not None else ""
            chain = chain_hash(prev, content, audio, at, reason)
            self._conn.execute(
                "INSERT INTO session_seals (session_id, at, reason, content_hash, audio, prev,"
                " chain) VALUES (?,?,?,?,?,?,?)",
                (session_id, at, reason, content, json.dumps(audio, sort_keys=True), prev, chain),
            )
        return {"at": at, "reason": reason, "content_hash": content, "chain": chain}

    def seals(self, session_id: str) -> list[dict]:
        if not self._readable(session_id):
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT at, reason, content_hash, audio, prev, chain FROM session_seals"
                " WHERE session_id=? ORDER BY id",
                (session_id,),
            ).fetchall()
        return [{**dict(r), "audio": json.loads(r["audio"])} for r in rows]

    def protected_ids(self, since: str) -> set[str]:
        """Meetings on legal hold, and (with `since`, an ISO date) created since then."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM sessions WHERE legal_hold<>'' OR (?<>'' AND created_at>=?)",
                (since, since),
            ).fetchall()
        return {r["id"] for r in rows}

    def set_legal_hold(self, session_id: str, reason: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE sessions SET legal_hold=? WHERE id=?", (reason.strip(), session_id)
            )

    def log_deletion(self, session: Session, what: str, by: str, role: str, reason: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO deletions (at, session_id, name, created_at, what, by, role, reason)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    datetime.now().isoformat(),
                    session.id,
                    session.name,
                    session.created_at.isoformat(),
                    what,
                    by,
                    role,
                    reason,
                ),
            )

    def deletions(self, start: str = "", end: str = "") -> list[dict]:
        """Deleted meetings and audio (at within [start, end), ISO dates; blank = open)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT at, session_id, name, created_at, what, by, role, reason FROM deletions"
                " WHERE (?='' OR at>=?) AND (?='' OR at<?) ORDER BY at",
                (start, start, end, end),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- supervision (services/supervision.py) --------------------------

    def set_flags(self, session_id: str, flags: list[dict], replace: bool) -> int:
        """Store flagged lines ({idx, start, speaker, phrase, text}). A flag already there
        keeps when it was found, so a meeting reviewed stays reviewed. `replace` drops the
        ones no longer found (a new transcript from the audio); otherwise they stay, so an
        edit never hides what was said. Returns how many flags are new."""
        # By what was said and when, not the line's number: deleting or merging other lines
        # renumbers it without making it a new flag.
        key = lambda f: (f["phrase"], f["text"], round(f["start"], 2))  # noqa: E731
        now = datetime.now().isoformat()
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT id, start, phrase, text FROM supervision_flags WHERE session_id=?",
                (session_id,),
            ).fetchall()
            have = {key(r): r["id"] for r in rows}
            wanted = {key(f): f for f in flags}
            if replace:
                gone = [i for k, i in have.items() if k not in wanted]
                self._conn.executemany(
                    "DELETE FROM supervision_flags WHERE id=?", [(i,) for i in gone]
                )
            self._conn.executemany(
                "UPDATE supervision_flags SET idx=?, speaker=? WHERE id=?",
                [(f["idx"], f["speaker"], have[k]) for k, f in wanted.items() if k in have],
            )
            new = [f for k, f in wanted.items() if k not in have]
            self._conn.executemany(
                "INSERT INTO supervision_flags (session_id, idx, start, speaker, phrase, text,"
                " found_at) VALUES (?,?,?,?,?,?,?)",
                [
                    (session_id, f["idx"], f["start"], f["speaker"], f["phrase"], f["text"], now)
                    for f in new
                ],
            )
        return len(new)

    def flags(self, session_id: str) -> list[dict]:
        if not self._readable(session_id):
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT idx, start, speaker, phrase, text, found_at FROM supervision_flags"
                " WHERE session_id=? ORDER BY start, idx, id",
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def add_review(self, session_id: str, note: str) -> None:
        who = access.principal()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO supervision_reviews (session_id, at, by_id, by, note)"
                " VALUES (?,?,?,?,?)",
                (
                    session_id,
                    datetime.now().isoformat(),
                    who.user_id if who else "",
                    who.name if who else "",
                    note,
                ),
            )

    def reviews(self, session_id: str) -> list[dict]:
        if not self._readable(session_id):
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT at, by_id, by, note FROM supervision_reviews WHERE session_id=?"
                " ORDER BY id",
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def supervision_queue(self) -> list[dict]:
        """Every visible meeting with flags: its flags' phrases, when the newest was found,
        and its last review."""
        sql = """
        SELECT s.id, s.name, s.created_at, s.owner_id,
               count(f.id) AS flags, group_concat(DISTINCT f.phrase) AS phrases,
               max(f.found_at) AS last_found_at,
               (SELECT r.at FROM supervision_reviews r WHERE r.session_id = s.id
                ORDER BY r.id DESC LIMIT 1) AS reviewed_at,
               (SELECT r.by FROM supervision_reviews r WHERE r.session_id = s.id
                ORDER BY r.id DESC LIMIT 1) AS reviewed_by,
               (SELECT r.note FROM supervision_reviews r WHERE r.session_id = s.id
                ORDER BY r.id DESC LIMIT 1) AS note
        FROM sessions s JOIN supervision_flags f ON f.session_id = s.id
        GROUP BY s.id ORDER BY s.created_at DESC
        """
        visible = self.visible_ids()
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [dict(r) for r in rows if visible is None or r["id"] in visible]

    def set_action_item_done(self, session_id: str, idx: int, done: bool) -> bool:
        """Tick an action item done (or not). Anyone who can read the meeting may: someone it
        is shared with ticks off what they were asked to do. False: no such meeting or item."""
        return self._set_action_item_flag(session_id, idx, "done", done)

    def set_action_item_follow(self, session_id: str, idx: int, follow: bool) -> bool:
        """Star an action item (or not), i.e. follow what somebody else was given. Readers may,
        like ticking; the flag is kept with the meeting, not per person."""
        return self._set_action_item_flag(session_id, idx, "follow", follow)

    def _set_action_item_flag(self, session_id: str, idx: int, flag: str, value: bool) -> bool:
        if not self._readable(session_id):
            return False
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT summary_data FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            if row is None or not row["summary_data"]:
                return False
            data = SummaryData.model_validate_json(row["summary_data"])
            if not 0 <= idx < len(data.action_items):
                return False
            setattr(data.action_items[idx], flag, value)
            self._conn.execute(
                "UPDATE sessions SET summary_data=?, updated_at=? WHERE id=?",
                (data.model_dump_json(), datetime.now().isoformat(), session_id),
            )
            self._touch(session_id)
        return True

    # By item id (ActionItem.id) rather than list position, for writes that reach several
    # meetings at once (services/task_links.py): positions shift under re-summaries, ids do not.
    _BY_ID_FIELDS = frozenset({"done", "follow", "task_id", "issue_url"})
    _FAN_OUT_FIELDS = frozenset({"done", "follow", "issue_url"})

    def set_action_item_flag_by_id(
        self, session_id: str, item_id: str, flag: str, value: bool
    ) -> bool:
        """`set_action_item_done` / `_follow` by item id: `flag` is "done" or "follow". Read
        access suffices, as for the index-based ones."""
        if flag not in ("done", "follow"):
            raise ValueError(f"Not an action item flag: {flag}")
        return self.set_action_item_fields_by_id(session_id, item_id, **{flag: value})

    def set_action_item_fields_by_id(self, session_id: str, item_id: str, **fields) -> bool:
        """Set `done`, `follow`, `task_id` and/or `issue_url` on the action item with this id.
        `issue_url` needs write access (as creating an issue does, Forbidden/Hidden otherwise);
        the others read access, like ticking: a reader links a task they were given. True
        when the item exists (the meeting is only bumped when a value changed), False for no
        such (visible) meeting or item."""
        bad = set(fields) - self._BY_ID_FIELDS
        if bad:
            raise ValueError(f"Cannot set action item fields: {sorted(bad)}")
        if "issue_url" in fields:
            self._check_write(session_id)
        if not self._readable(session_id):
            return False
        return self._write_item_by_id(session_id, item_id, fields)

    def fan_out_action_item_fields(self, session_id: str, item_id: str, **fields) -> bool:
        """INTERNAL, for services/task_links.py only: the same write WITHOUT any access check,
        so a task's done/follow/issue_url reaches every meeting that mentions it, also on a
        team server where the caller cannot see some of them (it is the same task; the history
        line names who changed it). Only `done`, `follow` and `issue_url`: never text, owner or
        the link itself. Callers must have checked that the caller may change the task."""
        bad = set(fields) - self._FAN_OUT_FIELDS
        if bad:
            raise ValueError(f"Cannot fan out action item fields: {sorted(bad)}")
        return self._write_item_by_id(session_id, item_id, fields)

    def _write_item_by_id(self, session_id: str, item_id: str, fields: dict) -> bool:
        if not item_id:
            return False
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT summary_data FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            if row is None or not row["summary_data"]:
                return False
            data = SummaryData.model_validate_json(row["summary_data"])
            item = next((a for a in data.action_items if a.id == item_id), None)
            if item is None:
                return False
            if all(getattr(item, k) == v for k, v in fields.items()):
                return True  # already so: no new version
            for k, v in fields.items():
                setattr(item, k, v)
            self._conn.execute(
                "UPDATE sessions SET summary_data=?, updated_at=? WHERE id=?",
                (data.model_dump_json(), datetime.now().isoformat(), session_id),
            )
            self._touch(session_id)
        return True

    def set_decision_links(self, session_id: str, changes: dict[str, str]) -> bool:
        """Set (`{decision id: object id}`) or remove (object id "") links in the meeting's
        `summary_data.decision_links`, for services/decision_links.py; the decision texts are
        never touched. Read access suffices (a reader links a decision they were given, as
        with `task_id`). Only decisions the meeting has (by `decision_ids`) get a link. True
        when the meeting exists and is visible (it is only bumped when something changed)."""
        if not self._readable(session_id):
            return False
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT summary_data FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            if row is None or not row["summary_data"]:
                return False
            data = SummaryData.model_validate_json(row["summary_data"])
            links = dict(data.decision_links)
            live = set(data.decision_ids)
            for decision_id, object_id in changes.items():
                if object_id and decision_id in live:
                    links[decision_id] = object_id
                else:
                    links.pop(decision_id, None)
            if links == data.decision_links:
                return True
            data.decision_links = links
            self._conn.execute(
                "UPDATE sessions SET summary_data=?, updated_at=? WHERE id=?",
                (data.model_dump_json(), datetime.now().isoformat(), session_id),
            )
            self._touch(session_id)
        return True

    def adopt_unowned(self, owner_id: str) -> int:
        """Meetings that belong to nobody (made before team mode) become this person's."""
        with self._lock, self._conn:
            return self._conn.execute(
                "UPDATE sessions SET owner_id=? WHERE owner_id=''", (owner_id,)
            ).rowcount

    # ---- per-person preferences (services/prefs.py) ------------------------

    def user_prefs(self, user_id: str) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM user_prefs WHERE user_id=?", (user_id,)
            ).fetchone()
        return json.loads(row["data"]) if row else {}

    def set_user_prefs(self, user_id: str, data: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO user_prefs (user_id, data) VALUES (?,?)"
                " ON CONFLICT(user_id) DO UPDATE SET data=excluded.data",
                (user_id, json.dumps(data)),
            )

    # ---- sharing (services/sharing.py) -------------------------------------

    def shares(self, session_id: str) -> list[dict]:
        """Who a meeting is shared with: user ids, "*" for everyone; with who shared and when."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_id, by, at FROM session_shares WHERE session_id=? ORDER BY at",
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_shares(self, session_id: str, user_ids: set[str], by: str) -> tuple[set, set]:
        """Share with exactly these (user ids, "*"); returns (added, removed)."""
        now = datetime.now().isoformat()
        with self._lock, self._conn:
            have = {
                r["user_id"]
                for r in self._conn.execute(
                    "SELECT user_id FROM session_shares WHERE session_id=?", (session_id,)
                )
            }
            added, removed = user_ids - have, have - user_ids
            self._conn.executemany(
                "DELETE FROM session_shares WHERE session_id=? AND user_id=?",
                [(session_id, u) for u in removed],
            )
            self._conn.executemany(
                "INSERT INTO session_shares (session_id, user_id, by, at) VALUES (?,?,?,?)",
                [(session_id, u, by, now) for u in added],
            )
        return added, removed

    # ---- sharing with contacts (phase 8; services/shares.py) -----------------------------
    # No access checks here: the caller (a route, the hub) has applied the rules.

    @staticmethod
    def _peer_share(row) -> dict:
        d = dict(row)
        d["parts"] = json.loads(d["parts"] or "{}")
        return d

    def peer_shares(self, session_id: str, include_revoked: bool = False) -> list[dict]:
        """Who a meeting is shared with (contacts' person keys), oldest first; revoked rows
        only when asked for."""
        sql = "SELECT * FROM peer_shares WHERE session_id=?"
        if not include_revoked:
            sql += " AND revoked_at=''"
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY at, person", (session_id,)).fetchall()
        return [self._peer_share(r) for r in rows]

    def peer_share(self, session_id: str, person: str) -> dict | None:
        """The share row for one contact, revoked or not; None when there never was one."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM peer_shares WHERE session_id=? AND person=?", (session_id, person)
            ).fetchone()
        return self._peer_share(row) if row else None

    def put_peer_share(self, session_id: str, person: str, parts: dict, by: str) -> dict:
        """Share (or change what is shared of) a meeting with a contact. Clears a revocation.
        The meeting moves in the change feed and its version vector (`_touch`, without moving
        `updated_at`), so the change reaches library sync and the contact's next pull.
        Raises KeyError for an unknown meeting."""
        now = datetime.now().isoformat()
        with self._lock, self._conn:
            if (
                self._conn.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone()
                is None
            ):
                raise KeyError(session_id)
            # `at` is when it was first shared; sharing again after a revocation starts anew.
            self._conn.execute(
                "INSERT INTO peer_shares (session_id, person, parts, by, at, updated_at)"
                " VALUES (?,?,?,?,?,?) ON CONFLICT(session_id, person) DO UPDATE SET"
                " parts=excluded.parts, by=excluded.by, updated_at=excluded.updated_at,"
                " at=CASE WHEN revoked_at != '' THEN excluded.at ELSE at END, revoked_at=''",
                (session_id, person, json.dumps(parts, sort_keys=True), by, now, now),
            )
            self._touch(session_id, keep_updated=True)
        return self.peer_share(session_id, person) or {}

    def revoke_peer_share(self, session_id: str, person: str) -> bool:
        """Stop sharing a meeting with a contact (the row stays, marked revoked); False when
        there was no live share."""
        now = datetime.now().isoformat()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE peer_shares SET revoked_at=?, updated_at=?"
                " WHERE session_id=? AND person=? AND revoked_at=''",
                (now, now, session_id, person),
            )
            if cur.rowcount:
                self._touch(session_id, keep_updated=True)
        return cur.rowcount > 0

    def shares_for_person(self, person: str) -> list[dict]:
        """The live shares a contact has, newest first (each with its `session_id`)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM peer_shares WHERE person=? AND revoked_at='' ORDER BY at DESC",
                (person,),
            ).fetchall()
        return [self._peer_share(r) for r in rows]

    def shared_changes_since(
        self, person: str, since_seq: int, limit: int = 200
    ) -> tuple[list[tuple[str, int]], int, bool]:
        """The meetings live-shared with `person` that changed after `since_seq`, oldest change
        first: ([(id, sync_seq)], the seq to ask from next time, whether there are more)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.id, s.sync_seq FROM sessions s JOIN peer_shares p ON p.session_id = s.id"
                " WHERE p.person=? AND p.revoked_at='' AND s.shared_from='' AND s.sync_seq > ?"
                " ORDER BY s.sync_seq LIMIT ?",
                (person, since_seq, limit + 1),
            ).fetchall()
        more = len(rows) > limit
        changes = [(r["id"], r["sync_seq"]) for r in rows[:limit]]
        return changes, (changes[-1][1] if changes else since_seq), more

    def shared_tombstones_since(self, person: str, since_at: str = "") -> list[dict]:
        """What `person` must forget after the ISO time `since_at` ("" = ever), oldest first:
        [{session_id, at, reason}] with reason "unshared" (the share was revoked) or "deleted"
        (the meeting was deleted)."""
        with self._lock:
            unshared = self._conn.execute(
                "SELECT session_id, revoked_at AS at FROM peer_shares"
                " WHERE person=? AND revoked_at != '' AND revoked_at > ?",
                (person, since_at),
            ).fetchall()
            deleted = self._conn.execute(
                "SELECT session_id, at FROM peer_share_tombstones WHERE person=? AND at > ?",
                (person, since_at),
            ).fetchall()
        out = [
            {"session_id": r["session_id"], "at": r["at"], "reason": "unshared"} for r in unshared
        ]
        out += [
            {"session_id": r["session_id"], "at": r["at"], "reason": "deleted"} for r in deleted
        ]
        return sorted(out, key=lambda t: (t["at"], t["session_id"]))

    def add_signature(self, subject: str, signer: str, sig: str) -> bool:
        """Remember who signed `subject`; True when it is new, False when `subject` was already
        signed (the first signature stands: a record arriving twice is stored once)."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO signatures (subject, signer, sig, at) VALUES (?,?,?,?)",
                (subject, signer, sig, datetime.now().isoformat()),
            )
        return cur.rowcount > 0

    def has_signature(self, subject: str) -> bool:
        with self._lock:
            return (
                self._conn.execute(
                    "SELECT 1 FROM signatures WHERE subject=?", (subject,)
                ).fetchone()
                is not None
            )

    def signature_of(self, subject: str) -> dict | None:
        """The kept signature of `subject` ({subject, signer, sig, at}), or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT subject, signer, sig, at FROM signatures WHERE subject=?", (subject,)
            ).fetchone()
        return dict(row) if row else None

    # ---- meetings shared WITH this person (services/share_pull.py) -------------------------
    # The owner's computer is the truth: a SharedMeeting replaces the copy whole. No `_touch`:
    # these rows never enter the change feed (changes_since skips shared_from rows anyway) and
    # never travel in library sync; each of this person's computers pulls them itself.

    def shared_meeting(self, session_id: str) -> dict | None:
        """{session_id, person, parts (dict), digest, shared_with (list), received_at} of a
        meeting shared with us, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM shared_meetings WHERE session_id=?", (session_id,)
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["parts"] = json.loads(d["parts"] or "{}")
        d["shared_with"] = json.loads(d["shared_with"] or "[]")
        return d

    def shared_from_ids(self, person: str) -> list[str]:
        """The meetings `person` shares with us that we hold a copy of."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM sessions WHERE shared_from=? ORDER BY created_at", (person,)
            ).fetchall()
        return [r["id"] for r in rows]

    def apply_shared(self, sm: SharedMeeting, person: str) -> str:
        """Write a meeting `person` shares with us (a verified SharedMeeting) as its copy here,
        one transaction, no access checks (the caller verified the signature). Returns "new"
        (first arrival), "parts" (the chosen parts changed), "updated" (anything else) or
        "clash" (a meeting of our own, or someone else's copy, has this id: nothing written).

        The sessions row gets the meeting's name, times, summary, attendees, participants and
        notes, status completed, no audio, no owner or origin, `shared_from` = person; the
        transcript, comments and (when notes are not shared) the notes document are replaced
        whole: what is not in `sm` is not shared any more. Search indexes follow by triggers."""
        sid = sm.id
        parts = sm.parts.model_dump()
        now = datetime.now().isoformat()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT shared_from FROM sessions WHERE id=?", (sid,)
            ).fetchone()
            if row is not None and row["shared_from"] != person:
                return "clash"
            held = self._conn.execute(
                "SELECT parts FROM shared_meetings WHERE session_id=?", (sid,)
            ).fetchone()
            common = (
                sm.name,
                _local_naive(sm.created_at).isoformat(),
                _local_naive(sm.updated_at).isoformat(),
                sm.summary,
                sm.summary_data.model_dump_json() if sm.summary_data else None,
                sm.notes,
                json.dumps(sm.participants),
                json.dumps(sm.attendees),
            )
            if row is None:
                self._conn.execute(
                    "INSERT INTO sessions(name, created_at, updated_at, summary, summary_data,"
                    " notes, participants, attendees, id, status, audio_file, owner_id, origin,"
                    " shared_from) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,'','',?)",
                    (*common, sid, SessionStatus.COMPLETED.value, person),
                )
            else:
                self._conn.execute(
                    "UPDATE sessions SET name=?, created_at=?, updated_at=?, summary=?,"
                    " summary_data=?, notes=?, participants=?, attendees=?, status=?,"
                    " audio_file=NULL, owner_id='', origin='' WHERE id=?",
                    (*common, SessionStatus.COMPLETED.value, sid),
                )
            self._write_segments(
                sid,
                [
                    TranscriptSegment(text=t.text, speaker=t.speaker, start=t.start, end=t.end)
                    for t in sm.transcript
                ],
            )
            self._conn.execute("DELETE FROM comments WHERE session_id=?", (sid,))
            self._conn.executemany(
                "INSERT OR REPLACE INTO comments (id, session_id, parent_id, target, target_id,"
                " anchor, quote, text, author_id, author, mentions, created_at, updated_at,"
                " resolved_at, resolved_by, deleted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        c.id,
                        sid,
                        c.parent_id,
                        c.target,
                        c.target_id,
                        json.dumps(c.anchor),
                        c.quote,
                        c.text,
                        c.author_id,
                        c.author,
                        json.dumps(c.mentions),
                        c.created_at,
                        c.updated_at,
                        c.resolved_at,
                        c.resolved_by,
                        int(c.deleted),
                    )
                    for c in sm.comments
                ],
            )
            if not sm.parts.notes:
                self._conn.execute("DELETE FROM shared_notes_ops WHERE session_id=?", (sid,))
                self._conn.execute("DELETE FROM shared_notes WHERE session_id=?", (sid,))
                # The log starts over: what was sent of it counts for nothing now.
                self._conn.execute(
                    "DELETE FROM meta WHERE key IN (?, ?)",
                    (f"{PUSHED_SEQ_PREFIX}{sid}", f"{REFUSED_NOTES_PREFIX}{sid}"),
                )
            self._conn.execute(
                "INSERT OR REPLACE INTO shared_meetings (session_id, person, parts, digest,"
                " shared_with, received_at) VALUES (?,?,?,?,?,?)",
                (
                    sid,
                    person,
                    json.dumps(parts, sort_keys=True),
                    sm.digest,
                    json.dumps(sm.shared_with),
                    now,
                ),
            )
        if row is None:
            return "new"
        if held is None or json.loads(held["parts"] or "{}") != parts:
            return "parts"
        return "updated"

    # ---- organizations (services/organizations.py) ---------------------------

    def organizations(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, name, hubspot_company_id, created_at FROM organizations ORDER BY name"
            ).fetchall()
            members = self._conn.execute(
                "SELECT organization_id, name, email, source FROM organization_members"
                " ORDER BY rowid"
            ).fetchall()
        out = {r["id"]: {**dict(r), "members": []} for r in rows}
        for m in members:
            if m["organization_id"] in out:
                out[m["organization_id"]]["members"].append(
                    {"name": m["name"], "email": m["email"], "source": m["source"]}
                )
        return list(out.values())

    def save_organization(
        self, organization_id: str, name: str, members: list[dict], hubspot_company_id: str = ""
    ) -> None:
        """Create or replace an organization. A person belongs to one organization: adding them here
        takes them out of any other."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO organizations (id, name, hubspot_company_id, created_at)"
                " VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,"
                " hubspot_company_id=excluded.hubspot_company_id",
                (organization_id, name, hubspot_company_id, datetime.now().isoformat()),
            )
            self._conn.execute(
                "DELETE FROM organization_members WHERE organization_id=?", (organization_id,)
            )
            for m in members:
                self._conn.execute(
                    "DELETE FROM organization_members WHERE lower(name)=lower(?)", (m["name"],)
                )
                self._conn.execute(
                    "INSERT INTO organization_members (organization_id, name, email, source)"
                    " VALUES (?,?,?,?)",
                    (organization_id, m["name"], m.get("email", ""), m.get("source", "manual")),
                )

    def delete_organization(self, organization_id: str) -> bool:
        with self._lock, self._conn:
            return (
                self._conn.execute(
                    "DELETE FROM organizations WHERE id=?", (organization_id,)
                ).rowcount
                > 0
            )

    # ---- search --------------------------------------------------------

    def search(self, query: str, limit: int = 50, per_session: int = 5) -> list[SearchHit]:
        """Full-text search over transcript segments and session name/summary/notes."""
        fts = fts_query(query)
        if not fts:
            return []
        with self._lock:
            seg_rows = self._conn.execute(
                """SELECT session_id, idx, snippet(segments_fts, 0, '[[', ']]', '…', 12) AS snip,
                          bm25(segments_fts) AS score
                   FROM segments_fts WHERE segments_fts MATCH ?
                   ORDER BY score LIMIT ?""",
                (fts, limit * 4),
            ).fetchall()
            sess_rows = self._conn.execute(
                """SELECT session_id, snippet(sessions_fts, -1, '[[', ']]', '…', 12) AS snip,
                          bm25(sessions_fts) AS score
                   FROM sessions_fts WHERE sessions_fts MATCH ?
                   ORDER BY score LIMIT ?""",
                (fts, limit),
            ).fetchall()
            com_rows = self._conn.execute(
                """SELECT c.session_id, c.id, c.target, c.target_id, c.author, c.quote,
                          snippet(comments_fts, -1, '[[', ']]', '…', 12) AS snip,
                          bm25(comments_fts) AS score
                   FROM comments_fts JOIN comments c ON c.rowid = comments_fts.rowid
                   WHERE comments_fts MATCH ?
                   ORDER BY score LIMIT ?""",
                (fts, limit * 2),
            ).fetchall()
            # speaker/start for segment hits
            details = {}
            for r in seg_rows:
                d = self._conn.execute(
                    "SELECT speaker, start FROM segments WHERE session_id=? AND idx=?",
                    (r["session_id"], r["idx"]),
                ).fetchone()
                if d:
                    details[(r["session_id"], r["idx"])] = d

        summaries = {x.id: x for x in self.list_summaries()}
        hits: dict[str, SearchHit] = {}
        order: list[str] = []

        def hit(session_id: str) -> SearchHit | None:
            if session_id not in summaries:
                return None
            if session_id not in hits:
                sm = summaries[session_id]
                hits[session_id] = SearchHit(
                    session_id=session_id,
                    session_name=sm.name,
                    created_at=sm.created_at,
                    score=0.0,
                )
                order.append(session_id)
            return hits[session_id]

        for r in sess_rows:
            h = hit(r["session_id"])
            if h is not None:
                h.session_snippet = r["snip"]
                h.score += -float(r["score"])
        for r in seg_rows:
            h = hit(r["session_id"])
            if h is None or len(h.segments) >= per_session:
                continue
            d = details.get((r["session_id"], r["idx"]))
            h.segments.append(
                SegmentHit(
                    idx=r["idx"],
                    speaker=d["speaker"] if d else "",
                    start=float(d["start"]) if d else 0.0,
                    snippet=r["snip"],
                )
            )
            h.score += -float(r["score"])
        for r in com_rows:
            h = hit(r["session_id"])
            if h is None or len(h.comments) >= per_session:
                continue
            h.comments.append(
                CommentHit(
                    id=r["id"],
                    author=r["author"],
                    target=r["target"],
                    label=TARGET_LABELS.get(r["target"], ""),
                    quote=r["quote"],
                    target_id=r["target_id"],
                    snippet=r["snip"],
                )
            )
            h.score += -float(r["score"])
        results = [hits[i] for i in order]
        results.sort(key=lambda h: h.score, reverse=True)
        return results[:limit]

    # ---- retrieval for ask ---------------------------------------------

    def retrieve(
        self, question: str, limit: int = 20, context: int = 2, per_session: int = 6
    ) -> list[Passage]:
        """Best-matching transcript windows (each hit plus `context` lines either side,
        overlapping windows merged) and session summaries, most relevant first."""
        fts = fts_any_query(question)
        if not fts:
            return []
        with self._lock:
            seg_hits = self._conn.execute(
                """SELECT session_id, idx, bm25(segments_fts) AS score FROM segments_fts
                   WHERE segments_fts MATCH ? ORDER BY score LIMIT ?""",
                (fts, limit * 4),
            ).fetchall()
            sess_hits = self._conn.execute(
                """SELECT session_id, bm25(sessions_fts) AS score FROM sessions_fts
                   WHERE sessions_fts MATCH ? ORDER BY score LIMIT ?""",
                (fts, max(3, limit // 4)),
            ).fetchall()
            visible = self.visible_ids()
            meta = {
                r["id"]: r
                for r in self._conn.execute(
                    "SELECT id, name, created_at, summary FROM sessions"
                ).fetchall()
                if visible is None or r["id"] in visible
            }

            # Merge hit windows per session, keeping each window's best score.
            windows: dict[str, list[list]] = {}
            counts: dict[str, int] = {}
            for h in seg_hits:
                sid, idx, score = h["session_id"], h["idx"], -float(h["score"])
                if sid not in meta:
                    continue
                lo, hi = idx - context, idx + context
                spans = windows.setdefault(sid, [])
                for span in spans:
                    if lo <= span[1] + 1 and hi >= span[0] - 1:
                        span[0], span[1] = min(span[0], lo), max(span[1], hi)
                        if score > span[2]:
                            span[2], span[3] = score, idx
                        break
                else:
                    if counts.get(sid, 0) >= per_session:
                        continue
                    counts[sid] = counts.get(sid, 0) + 1
                    spans.append([lo, hi, score, idx])

            passages: list[Passage] = []
            for sid, spans in windows.items():
                m = meta[sid]
                for lo, hi, score, focus in spans:
                    rows = self._conn.execute(
                        """SELECT idx, speaker, start, text FROM segments
                           WHERE session_id=? AND idx BETWEEN ? AND ? ORDER BY idx""",
                        (sid, max(lo, 0), hi),
                    ).fetchall()
                    passages.append(
                        Passage(
                            session_id=sid,
                            session_name=m["name"],
                            created_at=_dt(m["created_at"]),
                            lines=[
                                PassageLine(
                                    idx=r["idx"],
                                    speaker=r["speaker"],
                                    start=r["start"],
                                    text=r["text"],
                                )
                                for r in rows
                            ],
                            score=score,
                            focus_idx=focus,
                        )
                    )
            for h in sess_hits:
                m = meta.get(h["session_id"])
                if m is not None and m["summary"].strip():
                    passages.append(
                        Passage(
                            session_id=m["id"],
                            session_name=m["name"],
                            created_at=_dt(m["created_at"]),
                            kind="summary",
                            text=m["summary"].strip(),
                            score=-float(h["score"]),
                        )
                    )
        passages.sort(key=lambda p: p.score, reverse=True)
        return passages[:limit]

    # ---- asks ------------------------------------------------------------

    def save_ask(self, ask: Ask) -> Ask:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO asks(id, question, answer, citations, provider, model,
                                               created_at, owner_id) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    ask.id,
                    ask.question,
                    ask.answer,
                    json.dumps([c.model_dump(mode="json") for c in ask.citations]),
                    ask.provider,
                    ask.model,
                    ask.created_at.isoformat(),
                    access.user_id(),
                ),
            )
        return ask

    @staticmethod
    def _ask_from_row(r) -> Ask:
        return Ask(
            id=r["id"],
            question=r["question"],
            answer=r["answer"],
            citations=[Citation.model_validate(c) for c in json.loads(r["citations"])],
            provider=r["provider"],
            model=r["model"],
            created_at=_dt(r["created_at"]),
        )

    def list_asks(self, limit: int = 50) -> list[Ask]:
        """Saved questions: a signed-in user's own (access.py), else all."""
        user = access.user_id()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM asks WHERE ? = '' OR owner_id = ? ORDER BY created_at DESC LIMIT ?",
                (user, user, limit),
            ).fetchall()
        return [self._ask_from_row(r) for r in rows]

    def get_ask(self, ask_id: str) -> Ask | None:
        """One saved question, under the same access rule as `list_asks`."""
        user = access.user_id()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM asks WHERE id = ? AND (? = '' OR owner_id = ?)",
                (ask_id, user, user),
            ).fetchone()
        return self._ask_from_row(row) if row else None

    def delete_ask(self, ask_id: str) -> bool:
        user = access.user_id()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM asks WHERE id=? AND (? = '' OR owner_id = ?)", (ask_id, user, user)
            )
        return cur.rowcount > 0

    # ---- action items ----------------------------------------------------

    def list_action_items(self):
        """Every action item of every summarized session, newest meeting first."""
        from ..services.tasks import TaskItem

        visible = self.visible_ids()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, name, created_at, summary_data, owner_id FROM sessions"
                " WHERE summary_data IS NOT NULL ORDER BY created_at DESC"
            ).fetchall()
        out = []
        for r in rows:
            if visible is not None and r["id"] not in visible:
                continue
            data = SummaryData.model_validate_json(r["summary_data"])
            writable = access.can_write(r["owner_id"])
            for i, a in enumerate(data.action_items):
                out.append(
                    TaskItem(
                        session_id=r["id"],
                        session_name=r["name"],
                        created_at=_dt(r["created_at"]),
                        idx=i,
                        text=a.text,
                        owner=a.owner,
                        done=a.done,
                        issue_url=a.issue_url,
                        due=a.due,
                        follow=a.follow,
                        can_edit=writable,
                    )
                )
        return out

    def action_item_sources(self) -> list[dict]:
        """Every summarized meeting, newest first, as {id, name, created_at, owner_id,
        attendees, shared_from, summary_data (SummaryData)}, NOT filtered by access: for
        services/task_links.py, which needs every mention of a task and filters by
        `visible_ids()` itself. Same cost as `list_action_items`."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, name, created_at, owner_id, attendees, summary_data, shared_from"
                " FROM sessions WHERE summary_data IS NOT NULL ORDER BY created_at DESC"
            ).fetchall()
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "created_at": _dt(r["created_at"]),
                "owner_id": r["owner_id"],
                "attendees": json.loads(r["attendees"] or "[]"),
                "shared_from": r["shared_from"],
                "summary_data": SummaryData.model_validate_json(r["summary_data"]),
            }
            for r in rows
        ]

    def meeting_meta(self):
        """Name, date, attendees and summary of every session, without transcripts."""
        from ..services.brief import MeetingMeta

        visible = self.visible_ids()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, name, created_at, attendees, summary, summary_data FROM sessions"
            ).fetchall()
        rows = [r for r in rows if visible is None or r["id"] in visible]
        return [
            MeetingMeta(
                id=r["id"],
                name=r["name"],
                created_at=_dt(r["created_at"]),
                attendees=json.loads(r["attendees"] or "[]"),
                summary=r["summary"] or "",
                summary_data=(
                    SummaryData.model_validate_json(r["summary_data"])
                    if r["summary_data"]
                    else None
                ),
            )
            for r in rows
        ]

    # ---- the outbox: what I did on meetings shared with me (services/share_push.py) --------

    OUTBOX_KEEP = timedelta(days=30)  # accepted and rejected rows are kept this long

    def outbox_add(
        self,
        record_id: str,
        session_id: str,
        person: str,
        kind: str,
        payload: dict,
        created_at: str,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO share_outbox (id, session_id, person, kind, payload,"
                " created_at) VALUES (?,?,?,?,?,?)",
                (record_id, session_id, person, kind, json.dumps(payload), created_at),
            )

    @staticmethod
    def _outbox_row(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["payload"] = json.loads(d["payload"] or "{}")
        d["frame_hashes"] = json.loads(d["frame_hashes"] or "[]")
        return d

    def outbox_pending(self, person: str = "", session_id: str = "") -> list[dict]:
        """Pending rows, first made first (then in the order they were added), of one owner
        and/or one meeting (blank: any)."""
        sql, args = "SELECT * FROM share_outbox WHERE status='pending'", []
        if person:
            sql, args = sql + " AND person=?", [*args, person]
        if session_id:
            sql, args = sql + " AND session_id=?", [*args, session_id]
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY created_at, rowid", args).fetchall()
        return [self._outbox_row(r) for r in rows]

    def outbox_drop_person(self, person: str) -> int:
        """Forget everything in the outbox for one owner (a removed contact): returns the rows."""
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM share_outbox WHERE person=?", (person,))
        return cur.rowcount

    def outbox_people(self) -> list[str]:
        """The owners something is waiting for."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT person FROM share_outbox WHERE status='pending'"
            ).fetchall()
        return [r["person"] for r in rows]

    def outbox_counts(self, session_id: str) -> dict[str, int]:
        """Pending rows of a meeting by kind."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, COUNT(*) AS n FROM share_outbox WHERE session_id=?"
                " AND status='pending' GROUP BY kind",
                (session_id,),
            ).fetchall()
        return {r["kind"]: r["n"] for r in rows}

    def outbox_mark(self, results: list[tuple[str, bool, str]]) -> None:
        """Settle rows: (id, accepted, reason). A row not pending any more is left as it is."""
        now = datetime.now(UTC).isoformat()
        with self._lock, self._conn:
            for record_id, ok, reason in results:
                self._conn.execute(
                    "UPDATE share_outbox SET status=?, reason=?, pushed_at=?"
                    " WHERE id=? AND status='pending'",
                    ("accepted" if ok else "rejected", "" if ok else reason, now, record_id),
                )
            old = (datetime.now(UTC) - self.OUTBOX_KEEP).isoformat()
            self._conn.execute(
                "DELETE FROM share_outbox WHERE status!='pending' AND pushed_at!=''"
                " AND pushed_at<?",
                (old,),
            )

    def outbox_rejected(self, session_id: str, limit: int = 20) -> list[dict]:
        """The rows the owner refused, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM share_outbox WHERE session_id=? AND status='rejected'"
                " ORDER BY pushed_at DESC, rowid DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [self._outbox_row(r) for r in rows]

    def notes_ops_after(
        self, session_id: str, after_seq: int, author_id: str
    ) -> list[tuple[int, bytes]]:
        """(seq, frame) of the frames `author_id` typed here after `after_seq`, in log order."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, frame FROM shared_notes_ops WHERE session_id=? AND seq>?"
                " AND author_id=? ORDER BY seq",
                (session_id, after_seq, author_id),
            ).fetchall()
        return [(r["seq"], bytes(r["frame"])) for r in rows]

    def pushed_seq(self, session_id: str) -> int:
        """The last of my notes frames the owner accepted (0: none)."""
        value = self.get_meta(f"{PUSHED_SEQ_PREFIX}{session_id}")
        try:
            return int(value) if value else 0
        except ValueError:
            return 0

    def set_pushed_seq(self, session_id: str, seq: int) -> None:
        if self._shared_from(session_id):  # a meeting deleted meanwhile leaves no key behind
            self.set_meta(f"{PUSHED_SEQ_PREFIX}{session_id}", str(seq))

    def notes_rejection(self, session_id: str) -> str:
        """Why the owner refused my typed notes last ('' = not refused)."""
        return self.get_meta(f"{REFUSED_NOTES_PREFIX}{session_id}") or ""

    def set_notes_rejection(self, session_id: str, reason: str) -> None:
        if reason or self.get_meta(f"{REFUSED_NOTES_PREFIX}{session_id}"):
            self.set_meta(f"{REFUSED_NOTES_PREFIX}{session_id}", reason)

    # ---- semantic index ------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            r = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def vector_hashes(self, session_id: str) -> dict[tuple[str, int], str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, first_idx, text_hash FROM chunk_vectors WHERE session_id=?",
                (session_id,),
            ).fetchall()
        return {(r["kind"], r["first_idx"]): r["text_hash"] for r in rows}

    def replace_vectors(self, session_id: str, rows: list[tuple]) -> None:
        """rows: (kind, first_idx, last_idx, text_hash, vec_bytes)."""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM chunk_vectors WHERE session_id=?", (session_id,))
            self._conn.executemany(
                "INSERT INTO chunk_vectors(session_id, kind, first_idx, last_idx, text_hash, vec)"
                " VALUES (?,?,?,?,?,?)",
                [(session_id, *r) for r in rows],
            )

    def clear_vectors(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM chunk_vectors")
            self._conn.execute("DELETE FROM meta WHERE key LIKE 'vecstamp:%'")

    def session_stamp(self, session_id: str) -> str | None:
        """Changes whenever the session or its transcript is written (updated_at)."""
        with self._lock:
            r = self._conn.execute(
                "SELECT updated_at FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
        return r["updated_at"] if r else None

    def all_vectors(self) -> list:
        visible = self.visible_ids()
        with self._lock:
            rows = self._conn.execute(
                "SELECT session_id, kind, first_idx, last_idx, vec FROM chunk_vectors"
            ).fetchall()
        return rows if visible is None else [r for r in rows if r["session_id"] in visible]

    def indexed_session_count(self) -> int:
        with self._lock:
            r = self._conn.execute(
                "SELECT COUNT(DISTINCT session_id) AS n FROM chunk_vectors"
            ).fetchone()
        return int(r["n"])

    def session_ids(self) -> list[str]:
        visible = self.visible_ids()
        with self._lock:
            ids = [r["id"] for r in self._conn.execute("SELECT id FROM sessions").fetchall()]
        return ids if visible is None else [i for i in ids if i in visible]

    def passage_window(self, session_id: str, lo: int, hi: int, focus: int, score: float):
        """A transcript Passage for lines lo..hi of a session, or None."""
        if not self._readable(session_id):
            return None
        with self._lock:
            m = self._conn.execute(
                "SELECT id, name, created_at FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            if m is None:
                return None
            rows = self._conn.execute(
                """SELECT idx, speaker, start, text FROM segments
                   WHERE session_id=? AND idx BETWEEN ? AND ? ORDER BY idx""",
                (session_id, max(lo, 0), hi),
            ).fetchall()
        if not rows:
            return None
        return Passage(
            session_id=session_id,
            session_name=m["name"],
            created_at=_dt(m["created_at"]),
            lines=[
                PassageLine(idx=r["idx"], speaker=r["speaker"], start=r["start"], text=r["text"])
                for r in rows
            ],
            score=score,
            focus_idx=focus,
        )

    def summary_passage(self, session_id: str, score: float):
        if not self._readable(session_id):
            return None
        with self._lock:
            m = self._conn.execute(
                "SELECT id, name, created_at, summary FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
        if m is None or not (m["summary"] or "").strip():
            return None
        return Passage(
            session_id=session_id,
            session_name=m["name"],
            created_at=_dt(m["created_at"]),
            kind="summary",
            text=m["summary"].strip(),
            score=score,
        )

    def attendee_emails(self, session_id: str) -> dict[str, str]:
        if not self._readable(session_id):
            return {}
        with self._lock:
            row = self._conn.execute(
                "SELECT attendee_emails FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
        return json.loads(row["attendee_emails"] or "{}") if row else {}

    def set_attendee_emails(self, session_id: str, emails: dict[str, str]) -> None:
        self._check_write(session_id)
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE sessions SET attendee_emails=? WHERE id=?", (json.dumps(emails), session_id)
            )

    def crm_state(self, session_id: str, crm: str) -> dict | None:
        if not self._readable(session_id):
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT crm FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
        return json.loads(row["crm"] or "{}").get(crm) if row else None

    def set_crm_state(self, session_id: str, crm: str, state: dict) -> None:
        self._check_write(session_id)
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT crm FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            if row is None:
                return
            data = json.loads(row["crm"] or "{}")
            data[crm] = state
            self._conn.execute(
                "UPDATE sessions SET crm=? WHERE id=?", (json.dumps(data), session_id)
            )

    def local_only_ids(self) -> set[str]:
        with self._lock:
            rows = self._conn.execute("SELECT id FROM sessions WHERE local_only=1").fetchall()
        return {r["id"] for r in rows}

    def people_rows(self) -> list:
        """id, name, created_at, participants, attendees, summary_data, attendee_emails and crm
        of every session."""
        visible = self.visible_ids()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, name, created_at, participants, attendees, summary_data,"
                " attendee_emails, crm FROM sessions ORDER BY created_at DESC"
            ).fetchall()
        return rows if visible is None else [r for r in rows if r["id"] in visible]

    # ---- digests ---------------------------------------------------------

    def sessions_between(self, start: date, end: date) -> list[Session]:
        """Full sessions created on start..end (inclusive), oldest first."""
        lo = datetime.combine(start, time.min).isoformat()
        hi = datetime.combine(end + timedelta(days=1), time.min).isoformat()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM sessions WHERE created_at >= ? AND created_at < ?"
                " ORDER BY created_at",
                (lo, hi),
            ).fetchall()
        return [s for s in (self.get(r["id"]) for r in rows) if s is not None]

    def save_digest(self, digest: Digest) -> Digest:
        """Save, replacing any earlier digest of the same range (label) and owner: on a team
        server each person has their own (access.py)."""
        owner = access.user_id()
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM digests WHERE label=? AND id<>? AND owner_id=?",
                (digest.label, digest.id, owner),
            )
            self._conn.execute(
                """INSERT OR REPLACE INTO digests(id, label, start, end, markdown, session_ids,
                                                  provider, model, path, created_at, owner_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    digest.id,
                    digest.label,
                    digest.start.isoformat(),
                    digest.end.isoformat(),
                    digest.markdown,
                    json.dumps(digest.session_ids),
                    digest.provider,
                    digest.model,
                    digest.path,
                    digest.created_at.isoformat(),
                    owner,
                ),
            )
        return digest

    @staticmethod
    def _digest(r) -> Digest:
        return Digest(
            id=r["id"],
            label=r["label"],
            start=date.fromisoformat(r["start"]),
            end=date.fromisoformat(r["end"]),
            markdown=r["markdown"],
            session_ids=json.loads(r["session_ids"]),
            provider=r["provider"],
            model=r["model"],
            path=r["path"],
            created_at=_dt(r["created_at"]),
        )

    # A signed-in user's own digests; everyone's for the desktop app and the admin token.
    _MINE = "(? = '' OR owner_id = ?)"

    def list_digests(self, limit: int = 50) -> list[Digest]:
        u = access.user_id()
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM digests WHERE {self._MINE}"
                " ORDER BY start DESC, created_at DESC LIMIT ?",
                (u, u, limit),
            ).fetchall()
        return [self._digest(r) for r in rows]

    def get_digest(self, digest_id: str) -> Digest | None:
        u = access.user_id()
        with self._lock:
            r = self._conn.execute(
                f"SELECT * FROM digests WHERE id=? AND {self._MINE}", (digest_id, u, u)
            ).fetchone()
        return self._digest(r) if r else None

    def has_digest(self, label: str) -> bool:
        with self._lock:
            r = self._conn.execute(
                "SELECT 1 FROM digests WHERE label=? AND owner_id=?", (label, access.user_id())
            ).fetchone()
        return r is not None

    def delete_digest(self, digest_id: str) -> bool:
        u = access.user_id()
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"DELETE FROM digests WHERE id=? AND {self._MINE}", (digest_id, u, u)
            )
        return cur.rowcount > 0

    # ---- speakers ------------------------------------------------------

    def set_session_embeddings(self, session_id: str, embeddings: dict[str, list[float]]) -> None:
        self._check_write(session_id)
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO session_speakers(session_id, label, embedding)"
                " VALUES (?,?,?)",
                [(session_id, label, json.dumps(vec)) for label, vec in embeddings.items()],
            )
            if embeddings:
                self._touch(session_id)

    def get_session_embeddings(self, session_id: str) -> dict[str, list[float]]:
        if not self._readable(session_id):
            return {}
        with self._lock:
            rows = self._conn.execute(
                "SELECT label, embedding FROM session_speakers WHERE session_id=?", (session_id,)
            ).fetchall()
        return {r["label"]: json.loads(r["embedding"]) for r in rows}

    def relabel_session_speaker(self, session_id: str, old: str, new: str) -> Session | None:
        """Rename a speaker label everywhere in one session (segments, words,
        participants, stored embedding)."""
        self._check_write(session_id)
        session = self.get(session_id)
        if session is None:
            return None
        if old != new and any(s.speaker == old for s in session.transcript):
            self.keep_version(session, f"speaker {old} renamed")
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE segments SET speaker=? WHERE session_id=? AND speaker=?",
                (new, session_id, old),
            )
            self._conn.execute(
                "UPDATE OR REPLACE session_speakers SET label=? WHERE session_id=? AND label=?",
                (new, session_id, old),
            )
            participants = [new if p == old else p for p in session.participants]
            participants = list(dict.fromkeys(participants))
            self._conn.execute(
                "UPDATE sessions SET participants=?, updated_at=? WHERE id=?",
                (json.dumps(participants), datetime.now().isoformat(), session_id),
            )
            self._rename_item_owners(session_id, old, new)
            self._touch(session_id)
        return self.get(session_id)

    def _rename_item_owners(self, session_id: str, old: str, new: str) -> None:
        """Action items owned by the speaker label `old` now belong to `new`."""
        from ..services.tasks import rename_owner

        row = self._conn.execute(
            "SELECT summary_data FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        if row is None or not row["summary_data"]:
            return
        data = SummaryData.model_validate_json(row["summary_data"])
        changed = False
        for item in data.action_items:
            renamed = rename_owner(item.owner, old, new)
            if renamed != item.owner:
                item.owner, changed = renamed, True
        if changed:
            self._conn.execute(
                "UPDATE sessions SET summary_data=? WHERE id=?",
                (data.model_dump_json(), session_id),
            )

    def list_speakers(self) -> list[SpeakerProfile]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM speakers ORDER BY name").fetchall()
        return [self._speaker(r) for r in rows]

    def get_speaker(self, speaker_id: str) -> SpeakerProfile | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM speakers WHERE id=?", (speaker_id,)).fetchone()
        return self._speaker(row) if row else None

    def get_speaker_by_name(self, name: str) -> SpeakerProfile | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM speakers WHERE name=?", (name,)).fetchone()
        return self._speaker(row) if row else None

    def upsert_speaker_sample(self, name: str, embedding: list[float]) -> SpeakerProfile:
        """Add one voice sample to `name`, keeping a running mean embedding."""
        existing = self.get_speaker_by_name(name)
        now = datetime.now().isoformat()
        with self._lock, self._conn:
            if existing is None:
                profile = SpeakerProfile(name=name, embedding=embedding, sample_count=1)
                self._conn.execute(
                    "INSERT INTO speakers"
                    "(id, name, embedding, sample_count, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (profile.id, name, json.dumps(embedding), 1, now, now),
                )
                return profile
            n = existing.sample_count
            if len(existing.embedding) == len(embedding):
                mean = [
                    (o * n + v) / (n + 1)
                    for o, v in zip(existing.embedding, embedding, strict=True)
                ]
            else:
                mean = embedding  # embedding model changed; start over
                n = 0
            self._conn.execute(
                "UPDATE speakers SET embedding=?, sample_count=?, updated_at=? WHERE id=?",
                (json.dumps(mean), n + 1, now, existing.id),
            )
        return self.get_speaker(existing.id)

    def rename_speaker(self, speaker_id: str, name: str) -> SpeakerProfile | None:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE speakers SET name=?, updated_at=? WHERE id=?",
                (name, datetime.now().isoformat(), speaker_id),
            )
        return self.get_speaker(speaker_id) if cur.rowcount else None

    def delete_speaker(self, speaker_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM speakers WHERE id=?", (speaker_id,))
        return cur.rowcount > 0

    @staticmethod
    def _speaker(row) -> SpeakerProfile:
        return SpeakerProfile(
            id=row["id"],
            name=row["name"],
            embedding=json.loads(row["embedding"]),
            sample_count=row["sample_count"],
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    # ---- library sync (services/sync) ----------------------------------

    def _touch(self, session_id: str, at: str | None = None, *, keep_updated: bool = False) -> None:
        """A meeting got a new version: move it to the head of the change log (`sync_seq`
        from the global counter in meta), count one more revision for this machine in its
        version vector (under `device_id`, "" until the sync service names this machine) and
        set `updated_at`. Called inside the writer's own transaction, so a failed write leaves
        no trace. A meeting that does not exist is left alone. `keep_updated`: leave `updated_at`
        alone (a change to who the meeting is shared with is not a change to the meeting: it
        must not move the sidebar order, a digest window or "publication behind")."""
        row = self._conn.execute(
            "SELECT sync_vv FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        if row is None:
            return
        counter = self._conn.execute("SELECT value FROM meta WHERE key='sync_seq'").fetchone()
        seq = (int(counter["value"]) if counter else 0) + 1
        self._conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('sync_seq', ?)", (str(seq),)
        )
        vv = json.loads(row["sync_vv"] or "{}")
        vv[self.device_id] = vv.get(self.device_id, 0) + 1
        if keep_updated:
            self._conn.execute(
                "UPDATE sessions SET sync_seq=?, sync_vv=? WHERE id=?",
                (seq, json.dumps(vv, sort_keys=True), session_id),
            )
            return
        self._conn.execute(
            "UPDATE sessions SET sync_seq=?, sync_vv=?, updated_at=? WHERE id=?",
            (seq, json.dumps(vv, sort_keys=True), at or datetime.now().isoformat(), session_id),
        )

    def _touch_all(self, moves: list[tuple[str, int, str, int]]) -> None:
        """Every meeting a move names, source and target, once."""
        for sid in dict.fromkeys(s for old_s, _, new_s, _ in moves for s in (old_s, new_s)):
            self._touch(sid)

    def changes_since(
        self, seq: int, limit: int = 200
    ) -> tuple[list[tuple[str, int, dict[str, int]]], int, bool]:
        """Meetings changed after `seq`, oldest change first: ([(id, sync_seq, sync_vv)],
        the seq to ask from next time, whether there are more than `limit`)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, sync_seq, sync_vv FROM sessions WHERE sync_seq > ?"
                " AND shared_from = '' ORDER BY sync_seq LIMIT ?",
                (seq, limit + 1),
            ).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        changes = [(r["id"], r["sync_seq"], json.loads(r["sync_vv"] or "{}")) for r in rows]
        return changes, (changes[-1][1] if changes else seq), more

    def current_sync_seq(self) -> int:
        """The newest change number handed out (0 for none)."""
        value = self.get_meta("sync_seq")
        return int(value) if value else 0

    def tombstones_since(self, at: str = "") -> list[dict]:
        """Meetings deleted after the ISO time `at` ("" = all), oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT session_id, at, device, name FROM sync_tombstones WHERE at > ?"
                " ORDER BY at, session_id",
                (at,),
            ).fetchall()
        return [dict(r) for r in rows]

    def has_tombstone(self, session_id: str) -> bool:
        with self._lock:
            return (
                self._conn.execute(
                    "SELECT 1 FROM sync_tombstones WHERE session_id=?", (session_id,)
                ).fetchone()
                is not None
            )

    def add_tombstone(self, session_id: str, at: str, device: str, name: str) -> None:
        """A deletion learned from a peer (kept so it is passed on and the meeting stays gone)."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO sync_tombstones(session_id, at, device, name)"
                " VALUES (?,?,?,?)",
                (session_id, at, device, name),
            )

    def sync_state(self, session_id: str) -> tuple[str, dict[str, int]] | None:
        """(origin, version vector) of a meeting; None when there is no such meeting."""
        with self._lock:
            row = self._conn.execute(
                "SELECT origin, sync_vv FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
        return (row["origin"], json.loads(row["sync_vv"] or "{}")) if row else None

    def set_sync_state(self, session_id: str, vv: dict[str, int], origin: str) -> None:
        """Set a meeting's version vector and origin as they are (no new version)."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE sessions SET sync_vv=?, origin=? WHERE id=?",
                (json.dumps(vv, sort_keys=True), origin, session_id),
            )

    SERVED_KEPT = 3

    def serve_snapshot(self, session_id: str, peer_id: str, rev: int, state_json: str) -> None:
        """Remember the state served to a peer at our revision `rev`; only the last
        `SERVED_KEPT` revisions per (meeting, peer) are kept: the base of a later merge."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO sync_served(session_id, peer_id, rev, state, at)"
                " VALUES (?,?,?,?,?)",
                (session_id, peer_id, rev, state_json, datetime.now().isoformat()),
            )
            self._conn.execute(
                "DELETE FROM sync_served WHERE session_id=? AND peer_id=? AND rev NOT IN"
                " (SELECT rev FROM sync_served WHERE session_id=? AND peer_id=?"
                "  ORDER BY rev DESC LIMIT ?)",
                (session_id, peer_id, session_id, peer_id, self.SERVED_KEPT),
            )

    def rename_sync_peer(self, old: str, new: str, cursor_key: str) -> None:
        """A sync peer's id changed: its served states and tombstone cursor follow."""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM sync_served WHERE peer_id=?", (new,))
            self._conn.execute("UPDATE sync_served SET peer_id=? WHERE peer_id=?", (new, old))
            self._conn.execute("DELETE FROM meta WHERE key=?", (cursor_key + new,))
            self._conn.execute(
                "UPDATE meta SET key=? WHERE key=?", (cursor_key + new, cursor_key + old)
            )

    def served_snapshot(self, session_id: str, peer_id: str, rev: int) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT state FROM sync_served WHERE session_id=? AND peer_id=? AND rev=?",
                (session_id, peer_id, rev),
            ).fetchone()
        return row["state"] if row else None

    def sync_rows(self, session_id: str) -> dict | None:
        """Everything of a meeting that library sync reads, as raw rows from one consistent
        snapshot, without access checks (the sync service is the machine itself, not a
        request): None when there is no such meeting. Keys: session, segments, recordings,
        bookmarks, live_flags, external_notes, comments (`comment_rows`), publications,
        peer_shares, embeddings, assets (with `linked_at`), events (with `uid`, oldest first)."""

        def rows(sql: str) -> list[dict]:
            return [dict(r) for r in self._conn.execute(sql, (session_id,)).fetchall()]

        with self._lock:
            row = self._conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                return None
            return {
                "session": dict(row),
                "segments": rows("SELECT * FROM segments WHERE session_id=? ORDER BY idx"),
                "recordings": rows(
                    "SELECT * FROM recordings WHERE session_id=? ORDER BY created_at, id"
                ),
                "bookmarks": rows("SELECT * FROM bookmarks WHERE session_id=? ORDER BY id"),
                "live_flags": rows(
                    "SELECT * FROM live_flags WHERE session_id=? ORDER BY part, start, id"
                ),
                "external_notes": rows(
                    "SELECT * FROM external_notes WHERE session_id=? ORDER BY added_at, id"
                ),
                "comments": self.comment_rows(session_id),
                "publications": [
                    dict(r)
                    for r in self._conn.execute(
                        f"SELECT {self._PUB_COLS} FROM publications WHERE session_id=?"
                        " ORDER BY created_at, id",
                        (session_id,),
                    ).fetchall()
                ],
                "peer_shares": rows("SELECT * FROM peer_shares WHERE session_id=? ORDER BY person"),
                "embeddings": rows("SELECT * FROM session_speakers WHERE session_id=?"),
                "assets": rows(
                    "SELECT a.*, sa.added_at AS linked_at FROM assets a"
                    " JOIN session_assets sa ON sa.asset_id = a.id WHERE sa.session_id=?"
                    " ORDER BY sa.added_at, a.id"
                ),
                "events": rows(
                    "SELECT uid, at, kind, part, detail FROM session_events"
                    " WHERE session_id=? ORDER BY id"
                ),
            }

    def apply_synced(self, s: SyncSession, *, origin: str, vv: dict[str, int], bump: bool) -> str:
        """Write a meeting that arrived from a peer (or a merge of it with ours): the apply
        path of library sync, apart from `save` (which stamps `updated_at` and fires
        `on_new_session`) and from every access check. One transaction. `origin` is
        the meeting's origin as THIS machine stores it ('' = recorded here).

        Sets the session row (not `status` or `audio_file` of a meeting recorded here; for any
        other: `completed` when it has a transcript, else `created`, and no audio file), replaces
        the transcript, bookmarks, external notes, live flags, comments and speaker embeddings, the
        recordings rows (not for a meeting recorded here: the paths are ours and encryption
        rewrites them quietly), the asset links (assets we lack are added, without file bytes;
        a link we already have under another id is reused), and adds the history events whose
        uid we lack. Search indexes follow by their triggers; nothing else is touched.

        `sync_vv` becomes `vv` as given. `bump`: this is a merge, a new version made here: our
        revision in the vector counts one more (`_touch`, which also sets `updated_at` to now).
        Without it (a fast-forward) the vector stays as given and `updated_at` is the document's,
        so the meeting is identical on both machines; either way `sync_seq` advances, so a third
        machine asking us for changes hears of the meeting. Returns the meeting id."""
        sid = s.id
        here = origin == ""
        vv_json = json.dumps(dict(sorted(vv.items())))
        with self._lock, self._conn:
            existing = self._conn.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone()
            common = (
                s.name,
                s.updated_at.isoformat(),
                s.summary,
                s.summary_data.model_dump_json() if s.summary_data else None,
                s.notes,
                json.dumps(s.participants),
                json.dumps(s.attendees),
                int(s.local_only),
                s.copilot_notes.model_dump_json() if s.copilot_notes else None,
                int(s.speakers_reviewed),
                json.dumps([a.model_dump() for a in s.agenda]),
                s.meeting_type,
                s.owner_id,
                origin,
                vv_json,
            )
            status = SessionStatus.COMPLETED if s.transcript else SessionStatus.CREATED
            if existing is None:
                self._conn.execute(
                    "INSERT INTO sessions(name, updated_at, summary, summary_data, notes,"
                    " participants, attendees, local_only, copilot_notes, speakers_reviewed,"
                    " agenda, meeting_type, owner_id, origin, sync_vv, id, created_at, status,"
                    " audio_file) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                    (*common, sid, s.created_at.isoformat(), status.value),
                )
            else:
                sets = (
                    "name=?, updated_at=?, summary=?, summary_data=?, notes=?, participants=?,"
                    " attendees=?, local_only=?, copilot_notes=?, speakers_reviewed=?, agenda=?,"
                    " meeting_type=?, owner_id=?, origin=?, sync_vv=?, created_at=?"
                )
                if here:
                    self._conn.execute(
                        f"UPDATE sessions SET {sets} WHERE id=?",
                        (*common, s.created_at.isoformat(), sid),
                    )
                else:
                    self._conn.execute(
                        f"UPDATE sessions SET {sets}, status=?, audio_file=NULL WHERE id=?",
                        (*common, s.created_at.isoformat(), status.value, sid),
                    )
            self._write_segments(sid, s.transcript)
            if not here:
                self._write_recordings(sid, s.recordings)
            self._conn.execute("DELETE FROM bookmarks WHERE session_id=?", (sid,))
            self._conn.executemany(
                "INSERT OR REPLACE INTO bookmarks (id, session_id, part, seconds, note,"
                " created_at) VALUES (?,?,?,?,?,?)",
                [
                    (b.id, sid, b.part, b.seconds, b.note, b.created_at.isoformat())
                    for b in s.bookmarks
                ],
            )
            self._conn.execute("DELETE FROM external_notes WHERE session_id=?", (sid,))
            self._conn.executemany(
                "INSERT OR REPLACE INTO external_notes (id, session_id, source, text, filename,"
                " added_at) VALUES (?,?,?,?,?,?)",
                [
                    (n.id, sid, n.source, n.text, n.filename, n.added_at.isoformat())
                    for n in s.external_notes
                ],
            )
            self._conn.execute("DELETE FROM live_flags WHERE session_id=?", (sid,))
            self._conn.executemany(
                'INSERT OR REPLACE INTO live_flags (id, session_id, source, start, "end", text,'
                " created_at, part) VALUES (?,?,?,?,?,?,?,?)",
                [
                    (f.id, sid, f.source, f.start, f.end, f.text, f.created_at.isoformat(), f.part)
                    for f in s.live_flags
                ],
            )
            self._conn.execute("DELETE FROM comments WHERE session_id=?", (sid,))
            self._conn.executemany(
                "INSERT OR REPLACE INTO comments (id, session_id, parent_id, target, target_id,"
                " anchor, quote, text, author_id, author, mentions, created_at, updated_at,"
                " resolved_at, resolved_by, deleted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        c.id,
                        sid,
                        c.parent_id,
                        c.target,
                        c.target_id,
                        json.dumps(c.anchor),
                        c.quote,
                        c.text,
                        c.author_id,
                        c.author,
                        json.dumps(c.mentions),
                        c.created_at,
                        c.updated_at,
                        c.resolved_at,
                        c.resolved_by,
                        int(c.deleted),
                    )
                    for c in s.comments
                ],
            )
            self._conn.execute("DELETE FROM session_speakers WHERE session_id=?", (sid,))
            self._conn.executemany(
                "INSERT INTO session_speakers(session_id, label, embedding) VALUES (?,?,?)",
                [(sid, label, json.dumps(vec)) for label, vec in s.speaker_embeddings.items()],
            )
            self._link_synced_assets(sid, s)
            self.store_synced_publications(sid, s.publications)
            self._store_synced_peer_shares(sid, s.peer_shares)
            have = {
                r["uid"]
                for r in self._conn.execute(
                    "SELECT uid FROM session_events WHERE session_id=?", (sid,)
                )
            }
            for e in s.events:
                if e.uid in have or not event_travels(e.kind):
                    continue
                have.add(e.uid)
                self._conn.execute(
                    "INSERT INTO session_events (session_id, at, kind, part, detail, uid)"
                    " VALUES (?,?,?,?,?,?)",
                    (sid, e.at, e.kind, e.part, json.dumps(e.detail), e.uid),
                )
            if bump:
                self._touch(sid)
            else:
                self._advance_seq(sid)
        return sid

    def _store_synced_peer_shares(self, session_id: str, shares: list[SyncPeerShare]) -> None:
        """Library sync's write of a meeting's shares: by person, the later row wins (as in
        `merge_peer_shares`), and none is ever removed, so an older peer that does not send the
        field wipes nothing. Inside the caller's transaction."""
        for p in shares:
            have = self._conn.execute(
                "SELECT * FROM peer_shares WHERE session_id=? AND person=?",
                (session_id, p.person),
            ).fetchone()
            if have is not None:
                mine = SyncPeerShare(
                    **{k: have[k] for k in SyncPeerShare.model_fields if k != "parts"},
                    parts=json.loads(have["parts"] or "{}"),
                )
                if peer_share_rank(p) <= peer_share_rank(mine):
                    continue
            self._conn.execute(
                "INSERT OR REPLACE INTO peer_shares (session_id, person, parts, by, at,"
                " updated_at, revoked_at) VALUES (?,?,?,?,?,?,?)",
                (
                    session_id,
                    p.person,
                    json.dumps(p.parts, sort_keys=True),
                    p.by,
                    p.at,
                    p.updated_at,
                    p.revoked_at,
                ),
            )

    def _link_synced_assets(self, session_id: str, s: SyncSession) -> None:
        """The meeting's asset links become the incoming ones. A link asset is found by its
        URL first (as `find_link`), then by id; one we lack is added with no file path."""
        self._conn.execute("DELETE FROM session_assets WHERE session_id=?", (session_id,))
        for a in s.assets:
            row = None
            if a.kind == "link" and a.url:
                row = self._conn.execute(
                    "SELECT id FROM assets WHERE kind='link' AND url=?", (a.url,)
                ).fetchone()
            if row is None:
                row = self._conn.execute(
                    "SELECT id, kind, url, filename FROM assets WHERE id=?", (a.id,)
                ).fetchone()
                if row is not None and (row["kind"], row["url"], row["filename"]) != (
                    a.kind,
                    a.url,
                    a.filename,
                ):
                    row = None  # the id is another asset's here
                    a = a.model_copy(update={"id": uuid4().hex[:8]})
            if row is None:
                self._conn.execute(
                    "INSERT INTO assets (id, kind, title, url, filename, size, path, text,"
                    " source, created_at) VALUES (?,?,?,?,?,?,NULL,?,?,?)",
                    (
                        a.id,
                        a.kind,
                        a.title,
                        a.url,
                        a.filename,
                        a.size,
                        a.text,
                        a.source,
                        a.created_at.isoformat(),
                    ),
                )
                asset_id = a.id
            else:
                asset_id = row["id"]
            self._conn.execute(
                "INSERT OR IGNORE INTO session_assets (session_id, asset_id, added_at)"
                " VALUES (?,?,?)",
                (session_id, asset_id, a.added_at.isoformat()),
            )

    def _advance_seq(self, session_id: str) -> None:
        """Move a meeting to the head of the change log without a new version (its vector and
        `updated_at` stay): a fast-forward is news to a third machine."""
        counter = self._conn.execute("SELECT value FROM meta WHERE key='sync_seq'").fetchone()
        seq = (int(counter["value"]) if counter else 0) + 1
        self._conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('sync_seq', ?)", (str(seq),)
        )
        self._conn.execute("UPDATE sessions SET sync_seq=? WHERE id=?", (seq, session_id))

    # ---- internals -----------------------------------------------------

    def _write_segments(self, session_id: str, segments: list[TranscriptSegment]) -> None:
        self._conn.execute("DELETE FROM segments WHERE session_id=?", (session_id,))
        self._conn.executemany(
            """INSERT INTO segments(session_id, idx, text, speaker, start, "end", words)
               VALUES (?,?,?,?,?,?,?)""",
            [
                (
                    session_id,
                    i,
                    s.text,
                    s.speaker,
                    s.start,
                    s.end,
                    json.dumps([w.model_dump() for w in s.words]) if s.words else None,
                )
                for i, s in enumerate(segments)
            ],
        )

    def _write_recordings(self, session_id: str, recordings: list[Recording]) -> None:
        self._conn.execute("DELETE FROM recordings WHERE session_id=?", (session_id,))
        self._insert_recordings(session_id, recordings)


# ---- migration from the v2 JSON-per-session layout ------------------------


def import_json_sessions(repo: SessionRepository, sessions_dir: Path) -> int:
    """Import data/sessions/*.json files that are not yet in the database.

    Files are left in place. Returns the number imported.
    """
    if not sessions_dir.is_dir():
        return 0
    imported = 0
    for path in sorted(sessions_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
            if repo.exists(data.get("id", "")):
                continue
            status = data.get("status", "created")
            if status == "processing":
                status = "completed" if data.get("transcript") else "created"
            data["status"] = status
            session = Session.model_validate(data)
            repo.save(session)
            imported += 1
        except Exception:
            logger.warning("Skipping unreadable legacy session file: %s", path, exc_info=True)
    if imported:
        logger.info("Imported %d legacy JSON session(s) from %s", imported, sessions_dir)
    return imported
