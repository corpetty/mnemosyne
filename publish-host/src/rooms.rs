//! Guest-notes rooms on disk: `<data_dir>/rooms/<room>/{meta.json, log, snapshot}`.
//!
//! A room is a mailbox of sealed frames: the host never holds the key, so it cannot read,
//! merge or check what it relays. The log is a run of records `seq u64 BE || len u32 BE ||
//! bytes`, appended and synced one by one; the snapshot is the owner's latest sealed fold,
//! covering every frame up to `snapshot_seq`, and posting one drops those frames from the log.
//!
//! Loaded rooms live in a table, one lock each. Every write to a room, and every read that
//! must line up with the live relay (`join`), holds that room's lock, so a frame is either in
//! what a joining client replays or in what it receives live, never in neither. The table's
//! lock is only taken before a room's lock, never while holding one.
//!
//! `meta.json` is rewritten when the owner acts (create, snapshot, end, delete), by the
//! sweeper and once its `updated` lags by a minute while frames arrive; after a crash the log's last
//! record is the truth for `last_seq`, and a half-written record at its end is cut off.

use std::collections::{HashMap, HashSet};
use std::io;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use axum::http::StatusCode;
use serde::{Deserialize, Serialize};
use serde_json::json;
use tokio::fs;
use tokio::io::AsyncWriteExt;
use tokio::sync::{broadcast, Mutex};

use crate::clock::Clock;
use crate::config::Config;
use crate::error::ApiError;
use crate::store::{secrets_equal, sha256_hex, valid_id, valid_sha, write_atomic, Auth};

/// Bytes in front of every log record: seq (8) and length (4).
const RECORD_HEADER: u64 = 12;
/// Events a slow listener may fall behind by before it sees `Lagged` (and should rejoin).
const CHANNEL: usize = 256;
/// While frames arrive, `meta.json` is rewritten once its idle timer (`updated`) is this
/// far behind, so a crash costs a room at most this much of its idle time.
const META_FLUSH_SECS: u64 = 60;

#[derive(Serialize, Deserialize, Clone, Debug, Default, PartialEq, Eq)]
#[serde(default)]
pub struct RoomMeta {
    /// sha256 (hex) of the publisher token, as publications store it; empty in a tombstone.
    pub token_hash: String,
    /// sha256 (hex) of the room token guests prove the link with; empty in a tombstone.
    pub room_token_hash: String,
    pub created: u64,
    /// Unix seconds of the last frame (or of the creation): the idle timer.
    pub updated: u64,
    pub expires: u64,
    /// Set by `end`: the room is read-only until it expires.
    pub ended_at: Option<u64>,
    /// The last frame the snapshot covers (0 with a snapshot: it covers none).
    pub snapshot_seq: u64,
    /// The snapshot's size; 0 while there is none.
    pub snapshot_bytes: u64,
    /// The last frame appended (seqs start at 1).
    pub last_seq: u64,
    /// Set when deleted, expired or idle: a re-create of the id is refused for a while.
    pub deleted_at: Option<u64>,
}

/// What a room relays to everyone connected to it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Event {
    /// A frame appended to the log. `from` is the connection that sent it (0: none), so the
    /// relay does not echo it back.
    Frame {
        seq: u64,
        from: u64,
        bytes: Vec<u8>,
    },
    /// Relayed, never logged.
    Presence {
        from: u64,
        bytes: Vec<u8>,
    },
    /// Connections now open.
    Count(usize),
    Ended {
        ended_at: u64,
        expires: u64,
    },
    /// Deleted by the owner or the sweeper: close.
    Deleted,
}

/// `(snapshot as (snapshot_seq, bytes), frames as (seq, bytes) in order)`.
pub type Replay = (Option<(u64, Vec<u8>)>, Vec<(u64, Vec<u8>)>);

/// What `join_with_meta` hands a live connection.
pub struct Joined {
    pub rx: broadcast::Receiver<Arc<Event>>,
    pub replay: Replay,
    pub meta: RoomMeta,
    pub connections: usize,
}

pub struct RoomState {
    meta: RoomMeta,
    tx: broadcast::Sender<Arc<Event>>,
    connections: usize,
    /// Bytes in the log file (whole records only).
    log_bytes: u64,
    /// `meta` has changes `meta.json` does not.
    dirty: bool,
    /// `meta.updated` as `meta.json` has it.
    saved_updated: u64,
}

impl RoomState {
    fn new(meta: RoomMeta, log_bytes: u64) -> RoomState {
        RoomState {
            saved_updated: meta.updated,
            meta,
            tx: broadcast::channel(CHANNEL).0,
            connections: 0,
            log_bytes,
            dirty: false,
        }
    }

    pub fn meta(&self) -> &RoomMeta {
        &self.meta
    }

    pub fn connections(&self) -> usize {
        self.connections
    }

    pub fn subscribe(&self) -> broadcast::Receiver<Arc<Event>> {
        self.tx.subscribe()
    }

    fn send(&self, event: Event) {
        // No receivers is not an error: nobody is connected.
        let _ = self.tx.send(Arc::new(event));
    }
}

#[derive(Debug, Default, PartialEq, Eq)]
pub struct RoomSweepStats {
    pub expired: usize,
    pub idle: usize,
    pub tombstones_removed: usize,
}

pub struct RoomStore {
    cfg: Arc<Config>,
    clock: Clock,
    root: PathBuf,
    rooms: Mutex<HashMap<String, Arc<Mutex<RoomState>>>>,
    next_connection: AtomicU64,
}

fn parse_log(data: &[u8]) -> (Vec<(u64, &[u8])>, usize) {
    let mut records = Vec::new();
    let mut at = 0;
    while data.len() - at >= RECORD_HEADER as usize {
        let seq = u64::from_be_bytes(data[at..at + 8].try_into().unwrap_or([0; 8]));
        let len = u32::from_be_bytes(data[at + 8..at + 12].try_into().unwrap_or([0; 4])) as usize;
        let end = at + RECORD_HEADER as usize + len;
        if end > data.len() {
            break;
        }
        records.push((seq, &data[at + RECORD_HEADER as usize..end]));
        at = end;
    }
    (records, at)
}

fn record(seq: u64, len: u32, bytes: &[u8]) -> Vec<u8> {
    let mut r = Vec::with_capacity(RECORD_HEADER as usize + bytes.len());
    r.extend_from_slice(&seq.to_be_bytes());
    r.extend_from_slice(&len.to_be_bytes());
    r.extend_from_slice(bytes);
    r
}

async fn room_ids(root: &Path) -> Vec<String> {
    let mut ids = Vec::new();
    if let Ok(mut rd) = fs::read_dir(root).await {
        while let Ok(Some(e)) = rd.next_entry().await {
            let name = e.file_name().to_string_lossy().into_owned();
            if valid_id(&name) {
                ids.push(name);
            }
        }
    }
    ids
}

fn not_found() -> ApiError {
    ApiError::new(StatusCode::NOT_FOUND, "not_found")
}

fn gone() -> ApiError {
    ApiError::new(StatusCode::GONE, "gone")
}

fn too_large(code: &'static str) -> ApiError {
    ApiError::new(StatusCode::PAYLOAD_TOO_LARGE, code)
}

fn authorize(meta: &RoomMeta, auth: &Auth) -> Result<(), ApiError> {
    if secrets_equal(&meta.token_hash, &auth.token_hash) {
        Ok(())
    } else {
        Err(ApiError::new(StatusCode::FORBIDDEN, "wrong_token"))
    }
}

impl RoomStore {
    /// Opens (creating) `<data_dir>/rooms` and drops half-written temporary files. Rooms are
    /// loaded on first use.
    pub async fn open(cfg: Arc<Config>, clock: Clock) -> io::Result<RoomStore> {
        let root = cfg.data_dir.join("rooms");
        fs::create_dir_all(&root).await?;
        for id in room_ids(&root).await {
            if let Ok(mut rd) = fs::read_dir(root.join(&id)).await {
                while let Ok(Some(e)) = rd.next_entry().await {
                    if e.file_name().to_string_lossy().starts_with(".tmp-") {
                        let _ = fs::remove_file(e.path()).await;
                    }
                }
            }
        }
        Ok(RoomStore {
            cfg,
            clock,
            root,
            rooms: Mutex::new(HashMap::new()),
            next_connection: AtomicU64::new(1),
        })
    }

    fn dir(&self, room: &str) -> PathBuf {
        self.root.join(room)
    }

    fn max_expiry(&self, now: u64) -> u64 {
        now + self.cfg.room_max_days * 86_400
    }

    fn in_grace(&self, deleted_at: u64, now: u64) -> bool {
        now < deleted_at + self.cfg.tombstone_days * 86_400
    }

    /// Deleted or past its expiry (the sweeper may not have caught up yet).
    fn check_live(&self, meta: &RoomMeta) -> Result<(), ApiError> {
        if meta.deleted_at.is_some() || meta.expires <= self.clock.now() {
            Err(gone())
        } else {
            Ok(())
        }
    }

    async fn read_meta(&self, room: &str) -> Result<Option<RoomMeta>, ApiError> {
        match fs::read(self.dir(room).join("meta.json")).await {
            Ok(bytes) => serde_json::from_slice(&bytes).map(Some).map_err(|e| {
                tracing::error!("meta.json of room {room} unreadable: {e}");
                ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, "storage_error")
            }),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(e.into()),
        }
    }

    async fn write_meta(&self, room: &str, meta: &RoomMeta) -> io::Result<()> {
        let bytes = serde_json::to_vec(meta)?;
        write_atomic(&self.dir(room).join("meta.json"), &bytes).await
    }

    async fn save(&self, room: &str, s: &mut RoomState) -> io::Result<()> {
        self.write_meta(room, &s.meta).await?;
        s.dirty = false;
        s.saved_updated = s.meta.updated;
        Ok(())
    }

    /// A room from disk: 404 when there is none (or only a tombstone past its grace), 410
    /// for a deleted or expired one. Cuts a half-written record off the log's end.
    async fn load(&self, room: &str) -> Result<RoomState, ApiError> {
        let mut meta = self.read_meta(room).await?.ok_or_else(not_found)?;
        let now = self.clock.now();
        if let Some(at) = meta.deleted_at {
            return Err(if self.in_grace(at, now) {
                gone()
            } else {
                not_found()
            });
        }
        if meta.expires <= now {
            return Err(gone());
        }
        let path = self.dir(room).join("log");
        let mut log_bytes = 0;
        match fs::read(&path).await {
            Ok(data) => {
                let (records, valid) = parse_log(&data);
                if valid < data.len() {
                    tracing::warn!(
                        "room {room}: dropping {} bytes of a half-written record",
                        data.len() - valid
                    );
                    let f = fs::OpenOptions::new().write(true).open(&path).await?;
                    f.set_len(valid as u64).await?;
                    f.sync_all().await?;
                }
                if let Some((seq, _)) = records.last() {
                    meta.last_seq = meta.last_seq.max(*seq);
                }
                log_bytes = valid as u64;
            }
            Err(e) if e.kind() == io::ErrorKind::NotFound => {}
            Err(e) => return Err(e.into()),
        }
        meta.last_seq = meta.last_seq.max(meta.snapshot_seq);
        Ok(RoomState::new(meta, log_bytes))
    }

    /// The loaded room, loading it from disk the first time (see `load` for the errors).
    pub async fn open_room(&self, room: &str) -> Result<Arc<Mutex<RoomState>>, ApiError> {
        if !valid_id(room) {
            return Err(ApiError::bad("bad_id"));
        }
        let mut rooms = self.rooms.lock().await;
        if let Some(state) = rooms.get(room) {
            return Ok(state.clone());
        }
        let state = Arc::new(Mutex::new(self.load(room).await?));
        rooms.insert(room.to_string(), state.clone());
        Ok(state)
    }

    /// Creates a room, or (same publisher token, same room token) moves its expiry. The
    /// expiry is clamped to `room_max_days` from now. `true` when it was new.
    pub async fn create(
        &self,
        room: &str,
        auth: &Auth,
        room_token_hash: &str,
        expires: u64,
    ) -> Result<(RoomMeta, bool), ApiError> {
        if !valid_id(room) {
            return Err(ApiError::bad("bad_id"));
        }
        if !valid_sha(room_token_hash) {
            return Err(ApiError::bad("bad_room_token"));
        }
        let now = self.clock.now();
        if expires <= now {
            return Err(ApiError::bad("expiry_out_of_range")
                .with(json!({ "now": now, "max_days": self.cfg.room_max_days })));
        }
        let expires = expires.min(self.max_expiry(now));

        let mut rooms = self.rooms.lock().await;
        let existing = match rooms.get(room) {
            Some(state) => Some(state.clone()),
            None => match self.load(room).await {
                Ok(s) => {
                    let state = Arc::new(Mutex::new(s));
                    rooms.insert(room.to_string(), state.clone());
                    Some(state)
                }
                Err(e) if e.status == StatusCode::NOT_FOUND => None,
                Err(e) => return Err(e),
            },
        };
        if let Some(state) = existing {
            let mut s = state.lock().await;
            self.check_live(&s.meta)?;
            authorize(&s.meta, auth)?;
            if !secrets_equal(&s.meta.room_token_hash, room_token_hash) {
                return Err(ApiError::new(StatusCode::CONFLICT, "room_exists"));
            }
            s.meta.expires = expires;
            self.save(room, &mut s).await?;
            return Ok((s.meta.clone(), false));
        }
        if !auth.upload_ok {
            return Err(ApiError::new(
                StatusCode::FORBIDDEN,
                "upload_token_required",
            ));
        }
        // Nothing, or a tombstone past its grace: start clean.
        let dir = self.dir(room);
        let _ = fs::remove_dir_all(&dir).await;
        fs::create_dir_all(&dir).await?;
        let meta = RoomMeta {
            token_hash: auth.token_hash.clone(),
            room_token_hash: room_token_hash.to_string(),
            created: now,
            updated: now,
            expires,
            ..RoomMeta::default()
        };
        self.write_meta(room, &meta).await?;
        rooms.insert(
            room.to_string(),
            Arc::new(Mutex::new(RoomState::new(meta.clone(), 0))),
        );
        Ok((meta, true))
    }

    /// A guest's proof of the link: the room token, compared by its hash in constant time.
    pub async fn check_guest(&self, room: &str, room_token: &str) -> Result<RoomMeta, ApiError> {
        let state = self.open_room(room).await?;
        let s = state.lock().await;
        self.check_live(&s.meta)?;
        if !secrets_equal(&sha256_hex(room_token.as_bytes()), &s.meta.room_token_hash) {
            return Err(ApiError::new(StatusCode::FORBIDDEN, "wrong_room_token"));
        }
        Ok(s.meta.clone())
    }

    /// The publisher's proof of ownership.
    pub async fn check_publisher(&self, room: &str, auth: &Auth) -> Result<RoomMeta, ApiError> {
        let state = self.open_room(room).await?;
        let s = state.lock().await;
        self.check_live(&s.meta)?;
        authorize(&s.meta, auth)?;
        Ok(s.meta.clone())
    }

    /// Counts a live connection in (refused at `room_max_connections`). Returns its id, for
    /// `append`/`presence`'s `from`, and the new count, which is also relayed.
    pub async fn connect(&self, room: &str) -> Result<(u64, usize), ApiError> {
        let state = self.open_room(room).await?;
        let mut s = state.lock().await;
        self.check_live(&s.meta)?;
        if s.connections >= self.cfg.room_max_connections {
            return Err(ApiError::new(StatusCode::SERVICE_UNAVAILABLE, "room_full")
                .with(json!({ "max_connections": self.cfg.room_max_connections })));
        }
        s.connections += 1;
        let id = self.next_connection.fetch_add(1, Ordering::Relaxed);
        s.send(Event::Count(s.connections));
        Ok((id, s.connections))
    }

    /// Counts a connection out. A room no longer loaded (deleted) is left alone.
    pub async fn disconnect(&self, room: &str) {
        let rooms = self.rooms.lock().await;
        if let Some(state) = rooms.get(room) {
            let mut s = state.lock().await;
            s.connections = s.connections.saturating_sub(1);
            s.send(Event::Count(s.connections));
        }
    }

    /// Subscribes to the room's events and reads what came before, under one lock: every
    /// frame is in the replay or arrives on the receiver, exactly once.
    pub async fn join(
        &self,
        room: &str,
        since: u64,
    ) -> Result<(broadcast::Receiver<Arc<Event>>, Replay), ApiError> {
        let joined = self.join_with_meta(room, since).await?;
        Ok((joined.rx, joined.replay))
    }

    /// `join`, plus the room's metadata and connection count as of the same moment (what a
    /// live client's `welcome` says), so they line up with the replay.
    pub async fn join_with_meta(&self, room: &str, since: u64) -> Result<Joined, ApiError> {
        let state = self.open_room(room).await?;
        let s = state.lock().await;
        self.check_live(&s.meta)?;
        let rx = s.subscribe();
        let replay = self.replay_locked(room, &s.meta, since).await?;
        Ok(Joined {
            rx,
            replay,
            meta: s.meta.clone(),
            connections: s.connections,
        })
    }

    /// What a client that has every frame up to `since` (0: none) is missing: the snapshot
    /// when it covers more than that (or the client has nothing), then the frames after
    /// both.
    pub async fn replay(&self, room: &str, since: u64) -> Result<Replay, ApiError> {
        let state = self.open_room(room).await?;
        let s = state.lock().await;
        self.check_live(&s.meta)?;
        self.replay_locked(room, &s.meta, since).await
    }

    async fn replay_locked(
        &self,
        room: &str,
        meta: &RoomMeta,
        since: u64,
    ) -> Result<Replay, ApiError> {
        let dir = self.dir(room);
        let snapshot = if meta.snapshot_bytes > 0 && (since == 0 || since < meta.snapshot_seq) {
            Some((meta.snapshot_seq, fs::read(dir.join("snapshot")).await?))
        } else {
            None
        };
        let floor = match &snapshot {
            Some((seq, _)) => since.max(*seq),
            None => since,
        };
        let frames = match fs::read(dir.join("log")).await {
            Ok(data) => parse_log(&data)
                .0
                .into_iter()
                .filter(|(seq, _)| *seq > floor)
                .map(|(seq, bytes)| (seq, bytes.to_vec()))
                .collect(),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Vec::new(),
            Err(e) => return Err(e.into()),
        };
        Ok((snapshot, frames))
    }

    /// Appends a sealed frame to the log, syncs it and relays it. Refused when the room ended
    /// (409 `room_ended`), is gone (410), the frame is over `room_frame_max_bytes` (413
    /// `frame_too_large`) or the log would pass `room_log_max_bytes` (413 `log_full`).
    pub async fn append(&self, room: &str, from: u64, bytes: &[u8]) -> Result<u64, ApiError> {
        if bytes.is_empty() {
            return Err(ApiError::bad("empty_frame"));
        }
        let len = u32::try_from(bytes.len())
            .ok()
            .filter(|n| u64::from(*n) <= self.cfg.room_frame_max_bytes)
            .ok_or_else(|| too_large("frame_too_large"))?;
        let state = self.open_room(room).await?;
        let mut s = state.lock().await;
        self.check_live(&s.meta)?;
        if s.meta.ended_at.is_some() {
            return Err(ApiError::new(StatusCode::CONFLICT, "room_ended"));
        }
        let rec_len = RECORD_HEADER + u64::from(len);
        if s.log_bytes + rec_len > self.cfg.room_log_max_bytes {
            return Err(too_large("log_full"));
        }
        let seq = s.meta.last_seq + 1;
        let mut f = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(self.dir(room).join("log"))
            .await?;
        let written = async {
            f.write_all(&record(seq, len, bytes)).await?;
            f.sync_data().await
        }
        .await;
        if let Err(e) = written {
            // Never leave a torn record for the next one to land behind.
            let _ = f.set_len(s.log_bytes).await;
            return Err(e.into());
        }
        drop(f);
        let now = self.clock.now();
        s.log_bytes += rec_len;
        s.meta.last_seq = seq;
        s.meta.updated = now;
        s.dirty = true;
        if now >= s.saved_updated + META_FLUSH_SECS {
            if let Err(e) = self.save(room, &mut s).await {
                tracing::warn!("room {room}: meta.json not written: {e}");
            }
        }
        s.send(Event::Frame {
            seq,
            from,
            bytes: bytes.to_vec(),
        });
        Ok(seq)
    }

    /// Relays a presence frame (who is here, who is typing) without logging it.
    pub async fn presence(&self, room: &str, from: u64, bytes: &[u8]) -> Result<(), ApiError> {
        if bytes.len() as u64 > self.cfg.room_frame_max_bytes {
            return Err(too_large("frame_too_large"));
        }
        let state = self.open_room(room).await?;
        let s = state.lock().await;
        self.check_live(&s.meta)?;
        s.send(Event::Presence {
            from,
            bytes: bytes.to_vec(),
        });
        Ok(())
    }

    /// Stores the owner's snapshot covering frames `..= since_seq` and drops those from the
    /// log. `since_seq` may not pass the last frame (409 `since_ahead`) nor go below the
    /// current snapshot's (409 `snapshot_stale`: those frames are already pruned).
    pub async fn put_snapshot(
        &self,
        room: &str,
        auth: &Auth,
        bytes: &[u8],
        since_seq: u64,
    ) -> Result<RoomMeta, ApiError> {
        if bytes.is_empty() {
            return Err(ApiError::bad("empty_snapshot"));
        }
        if bytes.len() as u64 > self.cfg.room_snapshot_max_bytes {
            return Err(too_large("snapshot_too_large"));
        }
        let state = self.open_room(room).await?;
        let mut s = state.lock().await;
        self.check_live(&s.meta)?;
        authorize(&s.meta, auth)?;
        if since_seq > s.meta.last_seq {
            return Err(ApiError::new(StatusCode::CONFLICT, "since_ahead")
                .with(json!({ "last_seq": s.meta.last_seq })));
        }
        if since_seq < s.meta.snapshot_seq {
            return Err(ApiError::new(StatusCode::CONFLICT, "snapshot_stale")
                .with(json!({ "snapshot_seq": s.meta.snapshot_seq })));
        }
        let dir = self.dir(room);
        // Snapshot, then meta, then the log: a crash between leaves frames the snapshot
        // already covers, which replay skips (and the CRDT would not mind).
        write_atomic(&dir.join("snapshot"), bytes).await?;
        s.meta.snapshot_seq = since_seq;
        s.meta.snapshot_bytes = bytes.len() as u64;
        self.save(room, &mut s).await?;
        let log = match fs::read(dir.join("log")).await {
            Ok(data) => Some(data),
            Err(e) if e.kind() == io::ErrorKind::NotFound => None,
            Err(e) => return Err(e.into()),
        };
        if let Some(data) = log {
            let (records, valid) = parse_log(&data);
            let keep: Vec<u8> = records
                .iter()
                .filter(|(seq, _)| *seq > since_seq)
                .flat_map(|(seq, b)| record(*seq, b.len() as u32, b))
                .collect();
            if keep.len() != valid {
                write_atomic(&dir.join("log"), &keep).await?;
            }
            s.log_bytes = keep.len() as u64;
        }
        Ok(s.meta.clone())
    }

    /// Ends the room: read-only until `expires` (clamped to `room_max_days` from now). Again
    /// on an ended room only moves the expiry.
    pub async fn end(&self, room: &str, auth: &Auth, expires: u64) -> Result<RoomMeta, ApiError> {
        let now = self.clock.now();
        if expires <= now {
            return Err(ApiError::bad("expiry_out_of_range")
                .with(json!({ "now": now, "max_days": self.cfg.room_max_days })));
        }
        let state = self.open_room(room).await?;
        let mut s = state.lock().await;
        self.check_live(&s.meta)?;
        authorize(&s.meta, auth)?;
        let ended_at = *s.meta.ended_at.get_or_insert(now);
        s.meta.expires = expires.min(self.max_expiry(now));
        self.save(room, &mut s).await?;
        s.send(Event::Ended {
            ended_at,
            expires: s.meta.expires,
        });
        Ok(s.meta.clone())
    }

    /// Deletes the room now, keeping a tombstone so the id cannot be created again for
    /// `tombstone_days`.
    pub async fn delete(&self, room: &str, auth: &Auth) -> Result<(), ApiError> {
        if !valid_id(room) {
            return Err(ApiError::bad("bad_id"));
        }
        let mut rooms = self.rooms.lock().await;
        let state = match rooms.get(room) {
            Some(state) => state.clone(),
            None => Arc::new(Mutex::new(self.load(room).await?)),
        };
        let mut s = state.lock().await;
        if s.meta.deleted_at.is_some() {
            return Err(gone());
        }
        authorize(&s.meta, auth)?;
        self.tombstone(room, &mut s, self.clock.now()).await?;
        drop(s);
        rooms.remove(room);
        Ok(())
    }

    async fn write_tombstone(&self, room: &str, at: u64) -> io::Result<RoomMeta> {
        let dir = self.dir(room);
        let tomb = RoomMeta {
            updated: self.clock.now(),
            deleted_at: Some(at),
            ..RoomMeta::default()
        };
        self.write_meta(room, &tomb).await?;
        let _ = fs::remove_file(dir.join("log")).await;
        let _ = fs::remove_file(dir.join("snapshot")).await;
        Ok(tomb)
    }

    async fn tombstone(&self, room: &str, s: &mut RoomState, at: u64) -> io::Result<()> {
        s.meta = self.write_tombstone(room, at).await?;
        s.dirty = false;
        s.log_bytes = 0;
        s.send(Event::Deleted);
        Ok(())
    }

    /// Why the sweeper should delete a room now (and as of when), if it should.
    fn due(&self, meta: &RoomMeta, now: u64) -> Option<(u64, bool)> {
        if meta.deleted_at.is_some() {
            None
        } else if meta.expires <= now {
            Some((meta.expires, false))
        } else if meta.ended_at.is_none() && now >= meta.updated + self.cfg.room_idle_hours * 3600 {
            Some((now, true))
        } else {
            None
        }
    }

    /// Deletes rooms past their expiry, and rooms that have not ended and saw no frame for
    /// `room_idle_hours`; forgets tombstones after `tombstone_days`; writes pending metadata
    /// and unloads rooms nobody is connected to.
    pub async fn sweep(&self, now: u64) -> RoomSweepStats {
        let mut stats = RoomSweepStats::default();
        let mut rooms = self.rooms.lock().await;
        let mut handled = HashSet::new();
        let mut unload = Vec::new();
        for (id, state) in rooms.iter() {
            handled.insert(id.clone());
            let mut s = state.lock().await;
            if let Some((at, idle)) = self.due(&s.meta, now) {
                if self.tombstone(id, &mut s, at).await.is_ok() {
                    if idle {
                        stats.idle += 1;
                    } else {
                        stats.expired += 1;
                    }
                }
                unload.push(id.clone());
                continue;
            }
            if s.dirty {
                if let Err(e) = self.save(id, &mut s).await {
                    tracing::warn!("room {id}: meta.json not written: {e}");
                    continue;
                }
            }
            // Only the table holds it, and nobody is connected: loading again is cheap.
            if s.connections == 0 && Arc::strong_count(state) == 1 {
                unload.push(id.clone());
            }
        }
        for id in unload {
            rooms.remove(&id);
        }
        for id in room_ids(&self.root).await {
            if handled.contains(&id) {
                continue;
            }
            let Ok(Some(meta)) = self.read_meta(&id).await else {
                continue;
            };
            if let Some(at) = meta.deleted_at {
                if !self.in_grace(at, now) && fs::remove_dir_all(self.dir(&id)).await.is_ok() {
                    stats.tombstones_removed += 1;
                }
            } else if let Some((at, idle)) = self.due(&meta, now) {
                if self.write_tombstone(&id, at).await.is_ok() {
                    if idle {
                        stats.idle += 1;
                    } else {
                        stats.expired += 1;
                    }
                }
            }
        }
        stats
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    const T0: u64 = 1_800_000_000;
    const ROOM: &str = "0123456789abcdef";
    const OTHER: &str = "fedcba9876543210";
    const DAY: u64 = 86_400;

    struct Rig {
        tmp: TempDir,
        clock: Clock,
        cfg: Arc<Config>,
        rooms: RoomStore,
    }

    async fn rig_with(tweak: impl FnOnce(&mut Config)) -> Rig {
        let tmp = tempfile::tempdir().unwrap();
        let mut cfg = Config::defaults();
        cfg.data_dir = tmp.path().join("data");
        tweak(&mut cfg);
        let cfg = Arc::new(cfg);
        let clock = Clock::manual(T0);
        let rooms = RoomStore::open(cfg.clone(), clock.clone()).await.unwrap();
        Rig {
            tmp,
            clock,
            cfg,
            rooms,
        }
    }

    async fn rig() -> Rig {
        rig_with(|_| {}).await
    }

    fn owner() -> Auth {
        auth("publisher-token")
    }

    fn auth(token: &str) -> Auth {
        Auth {
            token_hash: sha256_hex(token.as_bytes()),
            upload_ok: true,
        }
    }

    fn room_hash() -> String {
        sha256_hex(b"room-token")
    }

    impl Rig {
        async fn create(&self, room: &str) -> RoomMeta {
            self.rooms
                .create(room, &owner(), &room_hash(), T0 + DAY)
                .await
                .unwrap()
                .0
        }

        fn dir(&self, room: &str) -> PathBuf {
            self.tmp.path().join("data/rooms").join(room)
        }

        async fn loaded(&self) -> usize {
            self.rooms.rooms.lock().await.len()
        }

        /// Drops every in-memory room, as a sweep does for rooms nobody is connected to.
        async fn unload(&self) {
            self.rooms.rooms.lock().await.clear();
        }
    }

    fn code(e: ApiError) -> (StatusCode, &'static str) {
        (e.status, e.code)
    }

    #[tokio::test]
    async fn create_and_open() {
        let r = rig().await;
        let (meta, created) = r
            .rooms
            .create(ROOM, &owner(), &room_hash(), T0 + DAY)
            .await
            .unwrap();
        assert!(created);
        assert_eq!(meta.expires, T0 + DAY);
        assert_eq!((meta.created, meta.last_seq, meta.ended_at), (T0, 0, None));
        let disk: RoomMeta =
            serde_json::from_slice(&std::fs::read(r.dir(ROOM).join("meta.json")).unwrap()).unwrap();
        assert_eq!(disk, meta);

        // Same tokens again: the expiry moves (clamped to room_max_days).
        let (again, created) = r
            .rooms
            .create(ROOM, &owner(), &room_hash(), T0 + 30 * DAY)
            .await
            .unwrap();
        assert!(!created);
        assert_eq!(again.expires, T0 + r.cfg.room_max_days * DAY);

        let e = r
            .rooms
            .create(ROOM, &auth("someone-else"), &room_hash(), T0 + DAY)
            .await
            .unwrap_err();
        assert_eq!(code(e), (StatusCode::FORBIDDEN, "wrong_token"));
        let e = r
            .rooms
            .create(ROOM, &owner(), &sha256_hex(b"other"), T0 + DAY)
            .await
            .unwrap_err();
        assert_eq!(code(e), (StatusCode::CONFLICT, "room_exists"));
        let e = r
            .rooms
            .create("0123", &owner(), &room_hash(), T0 + DAY)
            .await
            .unwrap_err();
        assert_eq!(code(e), (StatusCode::BAD_REQUEST, "bad_id"));
        let e = r
            .rooms
            .create(OTHER, &owner(), "nothex", T0 + DAY)
            .await
            .unwrap_err();
        assert_eq!(code(e), (StatusCode::BAD_REQUEST, "bad_room_token"));
        let e = r
            .rooms
            .create(OTHER, &owner(), &room_hash(), T0)
            .await
            .unwrap_err();
        assert_eq!(code(e), (StatusCode::BAD_REQUEST, "expiry_out_of_range"));
        let closed = Auth {
            upload_ok: false,
            ..owner()
        };
        let e = r
            .rooms
            .create(OTHER, &closed, &room_hash(), T0 + DAY)
            .await
            .unwrap_err();
        assert_eq!(code(e), (StatusCode::FORBIDDEN, "upload_token_required"));
        // An existing room's owner needs no upload token.
        r.rooms
            .create(ROOM, &closed, &room_hash(), T0 + DAY)
            .await
            .unwrap();

        assert_eq!(
            r.rooms
                .check_guest(ROOM, "room-token")
                .await
                .unwrap()
                .expires,
            T0 + DAY
        );
        let e = r.rooms.check_guest(ROOM, "guess").await.unwrap_err();
        assert_eq!(code(e), (StatusCode::FORBIDDEN, "wrong_room_token"));
        r.rooms.check_publisher(ROOM, &owner()).await.unwrap();
        let e = r.rooms.check_publisher(ROOM, &auth("x")).await.unwrap_err();
        assert_eq!(code(e), (StatusCode::FORBIDDEN, "wrong_token"));
        let e = r.rooms.check_guest(OTHER, "room-token").await.unwrap_err();
        assert_eq!(code(e), (StatusCode::NOT_FOUND, "not_found"));
        let e = r.rooms.open_room("../etc").await.err().unwrap();
        assert_eq!(code(e), (StatusCode::BAD_REQUEST, "bad_id"));
    }

    #[tokio::test]
    async fn append_seqs_are_monotonic_and_relayed() {
        let r = rig().await;
        r.create(ROOM).await;
        let (mut rx, (snap, frames)) = r.rooms.join(ROOM, 0).await.unwrap();
        assert!(snap.is_none() && frames.is_empty());
        for (i, f) in [b"one".as_slice(), b"two", b"three"].iter().enumerate() {
            assert_eq!(r.rooms.append(ROOM, 7, f).await.unwrap(), i as u64 + 1);
        }
        let first = rx.recv().await.unwrap();
        assert_eq!(
            *first,
            Event::Frame {
                seq: 1,
                from: 7,
                bytes: b"one".to_vec()
            }
        );
        r.rooms.presence(ROOM, 7, b"typing").await.unwrap();
        let mut last = None;
        while let Ok(ev) = rx.try_recv() {
            last = Some(ev);
        }
        assert_eq!(
            *last.unwrap(),
            Event::Presence {
                from: 7,
                bytes: b"typing".to_vec()
            }
        );
        let e = r.rooms.append(ROOM, 7, b"").await.unwrap_err();
        assert_eq!(code(e), (StatusCode::BAD_REQUEST, "empty_frame"));
        // The log on disk is exactly the three records.
        let data = std::fs::read(r.dir(ROOM).join("log")).unwrap();
        let (recs, valid) = parse_log(&data);
        assert_eq!(valid, data.len());
        assert_eq!(
            recs.iter()
                .map(|(s, b)| (*s, b.to_vec()))
                .collect::<Vec<_>>(),
            vec![
                (1, b"one".to_vec()),
                (2, b"two".to_vec()),
                (3, b"three".to_vec())
            ]
        );
        assert_eq!(&data[..12], &[0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 3]);
    }

    #[tokio::test]
    async fn replay_with_and_without_snapshot() {
        let r = rig().await;
        r.create(ROOM).await;
        for f in [b"a", b"b", b"c", b"d"] {
            r.rooms.append(ROOM, 0, f).await.unwrap();
        }
        let (snap, frames) = r.rooms.replay(ROOM, 0).await.unwrap();
        assert!(snap.is_none());
        assert_eq!(frames.iter().map(|f| f.0).collect::<Vec<_>>(), [1, 2, 3, 4]);
        let (_, frames) = r.rooms.replay(ROOM, 2).await.unwrap();
        assert_eq!(frames.iter().map(|f| f.0).collect::<Vec<_>>(), [3, 4]);

        r.rooms
            .put_snapshot(ROOM, &owner(), b"SNAP@2", 2)
            .await
            .unwrap();
        // Behind the snapshot: the snapshot, then what follows it.
        let (snap, frames) = r.rooms.replay(ROOM, 1).await.unwrap();
        assert_eq!(snap, Some((2, b"SNAP@2".to_vec())));
        assert_eq!(frames, vec![(3, b"c".to_vec()), (4, b"d".to_vec())]);
        let (snap, frames) = r.rooms.replay(ROOM, 0).await.unwrap();
        assert_eq!(snap.map(|s| s.0), Some(2));
        assert_eq!(frames.len(), 2);
        // At or past it: no snapshot, only frames after `since`.
        let (snap, frames) = r.rooms.replay(ROOM, 2).await.unwrap();
        assert!(snap.is_none());
        assert_eq!(frames.len(), 2);
        let (snap, frames) = r.rooms.replay(ROOM, 3).await.unwrap();
        assert!(snap.is_none());
        assert_eq!(frames, vec![(4, b"d".to_vec())]);
        let (snap, frames) = r.rooms.replay(ROOM, 9).await.unwrap();
        assert!(snap.is_none() && frames.is_empty());
    }

    #[tokio::test]
    async fn snapshot_before_any_frame_reaches_new_clients() {
        let r = rig().await;
        r.create(ROOM).await;
        r.rooms
            .put_snapshot(ROOM, &owner(), b"initial notes", 0)
            .await
            .unwrap();
        r.rooms.append(ROOM, 0, b"x").await.unwrap();
        let (snap, frames) = r.rooms.replay(ROOM, 0).await.unwrap();
        assert_eq!(snap, Some((0, b"initial notes".to_vec())));
        assert_eq!(frames, vec![(1, b"x".to_vec())]);
        let (snap, _) = r.rooms.replay(ROOM, 1).await.unwrap();
        assert!(snap.is_none());
    }

    #[tokio::test]
    async fn snapshot_prunes_the_log() {
        let r = rig().await;
        r.create(ROOM).await;
        for f in [b"a", b"b", b"c"] {
            r.rooms.append(ROOM, 0, f).await.unwrap();
        }
        let e = r
            .rooms
            .put_snapshot(ROOM, &owner(), b"S", 4)
            .await
            .unwrap_err();
        assert_eq!(code(e), (StatusCode::CONFLICT, "since_ahead"));
        let e = r
            .rooms
            .put_snapshot(ROOM, &auth("x"), b"S", 2)
            .await
            .unwrap_err();
        assert_eq!(code(e), (StatusCode::FORBIDDEN, "wrong_token"));
        let e = r
            .rooms
            .put_snapshot(ROOM, &owner(), b"", 2)
            .await
            .unwrap_err();
        assert_eq!(code(e), (StatusCode::BAD_REQUEST, "empty_snapshot"));

        let meta = r
            .rooms
            .put_snapshot(ROOM, &owner(), b"S2", 2)
            .await
            .unwrap();
        assert_eq!(
            (meta.snapshot_seq, meta.snapshot_bytes, meta.last_seq),
            (2, 2, 3)
        );
        let data = std::fs::read(r.dir(ROOM).join("log")).unwrap();
        let (recs, _) = parse_log(&data);
        assert_eq!(recs, vec![(3, b"c".as_slice())]);
        assert_eq!(std::fs::read(r.dir(ROOM).join("snapshot")).unwrap(), b"S2");

        let e = r
            .rooms
            .put_snapshot(ROOM, &owner(), b"S1", 1)
            .await
            .unwrap_err();
        assert_eq!(code(e), (StatusCode::CONFLICT, "snapshot_stale"));
        // Covering everything empties the log; seqs keep counting after it.
        r.rooms
            .put_snapshot(ROOM, &owner(), b"S3", 3)
            .await
            .unwrap();
        assert_eq!(std::fs::read(r.dir(ROOM).join("log")).unwrap(), b"");
        assert_eq!(r.rooms.append(ROOM, 0, b"d").await.unwrap(), 4);
        // ... also after a reload, with the log holding only seq 4.
        r.unload().await;
        assert_eq!(r.rooms.append(ROOM, 0, b"e").await.unwrap(), 5);
    }

    #[tokio::test]
    async fn caps_are_refused() {
        let r = rig_with(|c| {
            c.room_frame_max_bytes = 10;
            c.room_log_max_bytes = 3 * (12 + 10);
            c.room_snapshot_max_bytes = 5;
        })
        .await;
        r.create(ROOM).await;
        let e = r.rooms.append(ROOM, 0, &[1; 11]).await.unwrap_err();
        assert_eq!(code(e), (StatusCode::PAYLOAD_TOO_LARGE, "frame_too_large"));
        let e = r.rooms.presence(ROOM, 0, &[1; 11]).await.unwrap_err();
        assert_eq!(code(e), (StatusCode::PAYLOAD_TOO_LARGE, "frame_too_large"));
        for _ in 0..3 {
            r.rooms.append(ROOM, 0, &[1; 10]).await.unwrap();
        }
        let e = r.rooms.append(ROOM, 0, &[1]).await.unwrap_err();
        assert_eq!(code(e), (StatusCode::PAYLOAD_TOO_LARGE, "log_full"));
        let e = r
            .rooms
            .put_snapshot(ROOM, &owner(), &[1; 6], 3)
            .await
            .unwrap_err();
        assert_eq!(
            code(e),
            (StatusCode::PAYLOAD_TOO_LARGE, "snapshot_too_large")
        );
        // A snapshot makes room again.
        r.rooms
            .put_snapshot(ROOM, &owner(), &[1; 5], 3)
            .await
            .unwrap();
        assert_eq!(r.rooms.append(ROOM, 0, &[1; 10]).await.unwrap(), 4);
    }

    #[tokio::test]
    async fn end_refuses_appends() {
        let r = rig().await;
        r.create(ROOM).await;
        r.rooms.append(ROOM, 0, b"a").await.unwrap();
        let (mut rx, _) = r.rooms.join(ROOM, 0).await.unwrap();
        let e = r.rooms.end(ROOM, &auth("x"), T0 + DAY).await.unwrap_err();
        assert_eq!(code(e), (StatusCode::FORBIDDEN, "wrong_token"));
        r.clock.advance(60);
        let meta = r.rooms.end(ROOM, &owner(), T0 + 30 * DAY).await.unwrap();
        assert_eq!(meta.ended_at, Some(T0 + 60));
        assert_eq!(meta.expires, T0 + 60 + r.cfg.room_max_days * DAY);
        assert_eq!(
            *rx.recv().await.unwrap(),
            Event::Ended {
                ended_at: T0 + 60,
                expires: meta.expires
            }
        );
        let e = r.rooms.append(ROOM, 0, b"b").await.unwrap_err();
        assert_eq!(code(e), (StatusCode::CONFLICT, "room_ended"));
        // Still readable and joinable; the owner may still post the final snapshot.
        assert_eq!(r.rooms.replay(ROOM, 0).await.unwrap().1.len(), 1);
        r.rooms.connect(ROOM).await.unwrap();
        r.rooms
            .put_snapshot(ROOM, &owner(), b"final", 1)
            .await
            .unwrap();
        // Ending again keeps the first end, moves the expiry.
        let again = r.rooms.end(ROOM, &owner(), T0 + 2 * DAY).await.unwrap();
        assert_eq!(
            (again.ended_at, again.expires),
            (Some(T0 + 60), T0 + 2 * DAY)
        );
        // Persisted.
        r.unload().await;
        let meta = r.rooms.check_publisher(ROOM, &owner()).await.unwrap();
        assert_eq!(meta.ended_at, Some(T0 + 60));
        let e = r.rooms.append(ROOM, 0, b"b").await.unwrap_err();
        assert_eq!(code(e), (StatusCode::CONFLICT, "room_ended"));
    }

    #[tokio::test]
    async fn delete_keeps_a_tombstone_that_refuses_recreate() {
        let r = rig().await;
        r.create(ROOM).await;
        r.rooms.append(ROOM, 0, b"a").await.unwrap();
        r.rooms.put_snapshot(ROOM, &owner(), b"S", 1).await.unwrap();
        let (mut rx, _) = r.rooms.join(ROOM, 0).await.unwrap();
        let e = r.rooms.delete(ROOM, &auth("x")).await.unwrap_err();
        assert_eq!(code(e), (StatusCode::FORBIDDEN, "wrong_token"));
        r.rooms.delete(ROOM, &owner()).await.unwrap();
        assert_eq!(*rx.recv().await.unwrap(), Event::Deleted);
        assert!(!r.dir(ROOM).join("log").exists());
        assert!(!r.dir(ROOM).join("snapshot").exists());
        let tomb: RoomMeta =
            serde_json::from_slice(&std::fs::read(r.dir(ROOM).join("meta.json")).unwrap()).unwrap();
        assert_eq!(tomb.deleted_at, Some(T0));
        assert!(tomb.token_hash.is_empty() && tomb.room_token_hash.is_empty());

        for e in [
            r.rooms
                .create(ROOM, &owner(), &room_hash(), T0 + DAY)
                .await
                .unwrap_err(),
            r.rooms.append(ROOM, 0, b"b").await.unwrap_err(),
            r.rooms.check_guest(ROOM, "room-token").await.unwrap_err(),
            r.rooms.delete(ROOM, &owner()).await.unwrap_err(),
        ] {
            assert_eq!(code(e), (StatusCode::GONE, "gone"));
        }

        // After the grace, the sweeper forgets it and the id is free again.
        r.clock.advance(r.cfg.tombstone_days * DAY);
        let stats = r.rooms.sweep(r.clock.now()).await;
        assert_eq!(stats.tombstones_removed, 1);
        assert!(!r.dir(ROOM).exists());
        let (meta, created) = r
            .rooms
            .create(ROOM, &owner(), &room_hash(), r.clock.now() + DAY)
            .await
            .unwrap();
        assert!(created);
        assert_eq!(meta.last_seq, 0);
    }

    #[tokio::test]
    async fn sweep_removes_expired_and_idle_rooms() {
        let r = rig().await;
        const ENDED: &str = "00000000000000aa";
        const BUSY: &str = "00000000000000bb";
        r.rooms
            .create(ROOM, &owner(), &room_hash(), T0 + 3600)
            .await
            .unwrap();
        r.rooms
            .create(OTHER, &owner(), &room_hash(), T0 + 3 * DAY)
            .await
            .unwrap();
        r.create(ENDED).await;
        r.rooms
            .create(BUSY, &owner(), &room_hash(), T0 + 3 * DAY)
            .await
            .unwrap();
        r.rooms.end(ENDED, &owner(), T0 + 2 * DAY).await.unwrap();
        let (mut rx, _) = r.rooms.join(ROOM, 0).await.unwrap();

        // Expired: refused at once, swept on the next pass.
        r.clock.advance(3600);
        let e = r.rooms.append(ROOM, 0, b"late").await.unwrap_err();
        assert_eq!(code(e), (StatusCode::GONE, "gone"));
        let stats = r.rooms.sweep(r.clock.now()).await;
        assert_eq!(
            stats,
            RoomSweepStats {
                expired: 1,
                ..Default::default()
            }
        );
        assert_eq!(*rx.recv().await.unwrap(), Event::Deleted);
        let tomb: RoomMeta =
            serde_json::from_slice(&std::fs::read(r.dir(ROOM).join("meta.json")).unwrap()).unwrap();
        assert_eq!(tomb.deleted_at, Some(T0 + 3600));

        // A frame keeps BUSY's idle timer fresh; OTHER saw none for room_idle_hours.
        r.clock.advance(r.cfg.room_idle_hours * 3600 - 3600 - 10);
        r.rooms.append(BUSY, 0, b"still here").await.unwrap();
        r.clock.advance(10);
        // Unloaded rooms are swept from disk too, from what meta.json says (`unload` is a
        // crash: nothing is flushed first, yet BUSY's fresh idle timer was written).
        r.unload().await;
        let stats = r.rooms.sweep(r.clock.now()).await;
        assert_eq!(
            stats,
            RoomSweepStats {
                idle: 1,
                ..Default::default()
            }
        );
        let e = r.rooms.check_guest(OTHER, "room-token").await.unwrap_err();
        assert_eq!(code(e), (StatusCode::GONE, "gone"));
        // The ended room does not idle out; it lives to its expiry.
        r.rooms.replay(ENDED, 0).await.unwrap();
        r.rooms.replay(BUSY, 0).await.unwrap();
        r.clock.advance(DAY);
        let stats = r.rooms.sweep(r.clock.now()).await;
        assert_eq!(
            stats,
            RoomSweepStats {
                expired: 1,
                idle: 1,
                ..Default::default()
            }
        );
        assert_eq!(r.loaded().await, 0);
    }

    #[tokio::test]
    async fn rooms_reload_lazily_from_disk() {
        let r = rig().await;
        r.create(ROOM).await;
        r.rooms.append(ROOM, 0, b"one").await.unwrap();
        r.rooms.append(ROOM, 0, b"two").await.unwrap();
        assert_eq!(r.loaded().await, 1);
        // meta.json still says last_seq 0 (flushed at most once a minute): the log decides.
        let disk: RoomMeta =
            serde_json::from_slice(&std::fs::read(r.dir(ROOM).join("meta.json")).unwrap()).unwrap();
        assert_eq!(disk.last_seq, 0);

        // A sweep writes pending metadata and unloads rooms nobody is connected to.
        r.rooms.sweep(r.clock.now()).await;
        assert_eq!(r.loaded().await, 0);
        let disk: RoomMeta =
            serde_json::from_slice(&std::fs::read(r.dir(ROOM).join("meta.json")).unwrap()).unwrap();
        assert_eq!(disk.last_seq, 2);
        assert_eq!(r.rooms.append(ROOM, 0, b"three").await.unwrap(), 3);
        assert_eq!(r.loaded().await, 1);

        // A crash mid-append: a torn record at the end is cut off on load.
        let mut f = std::fs::OpenOptions::new()
            .append(true)
            .open(r.dir(ROOM).join("log"))
            .unwrap();
        std::io::Write::write_all(&mut f, &[0, 0, 0, 0, 0, 0, 0, 4, 0, 0, 0, 9, 1, 2]).unwrap();
        drop(f);
        r.unload().await;
        // A fresh store (a restart) reads the same.
        let again = RoomStore::open(r.cfg.clone(), r.clock.clone())
            .await
            .unwrap();
        let (_, frames) = again.replay(ROOM, 0).await.unwrap();
        assert_eq!(frames.iter().map(|f| f.0).collect::<Vec<_>>(), [1, 2, 3]);
        assert_eq!(again.append(ROOM, 0, b"four").await.unwrap(), 4);
        let (_, frames) = again.replay(ROOM, 3).await.unwrap();
        assert_eq!(frames, vec![(4, b"four".to_vec())]);

        // Connected rooms stay loaded across a sweep.
        again.connect(ROOM).await.unwrap();
        again.sweep(r.clock.now()).await;
        assert_eq!(again.rooms.lock().await.len(), 1);
    }

    #[tokio::test]
    async fn connections_are_capped() {
        let r = rig_with(|c| c.room_max_connections = 2).await;
        r.create(ROOM).await;
        let (mut rx, _) = r.rooms.join(ROOM, 0).await.unwrap();
        let (a, n) = r.rooms.connect(ROOM).await.unwrap();
        assert_eq!(n, 1);
        let (b, n) = r.rooms.connect(ROOM).await.unwrap();
        assert_eq!(n, 2);
        assert_ne!(a, b);
        assert!(a > 0 && b > 0);
        let e = r.rooms.connect(ROOM).await.unwrap_err();
        assert_eq!(code(e), (StatusCode::SERVICE_UNAVAILABLE, "room_full"));
        r.rooms.disconnect(ROOM).await;
        assert_eq!(r.rooms.connect(ROOM).await.unwrap().1, 2);
        let counts: Vec<Event> = std::iter::from_fn(|| rx.try_recv().ok())
            .map(|e| (*e).clone())
            .collect();
        assert_eq!(
            counts,
            vec![
                Event::Count(1),
                Event::Count(2),
                Event::Count(1),
                Event::Count(2)
            ]
        );
        // Other rooms have their own count.
        r.create(OTHER).await;
        assert_eq!(r.rooms.connect(OTHER).await.unwrap().1, 1);
        assert_eq!(
            r.rooms
                .open_room(ROOM)
                .await
                .unwrap()
                .lock()
                .await
                .connections(),
            2
        );
    }
}
