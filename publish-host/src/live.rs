//! One live connection to a guest-notes room (`GET /live/<room>`, a WebSocket).
//!
//! The wire, as the guest page and the owner's bridge speak it:
//!
//! - The client's first text message, within `JOIN_TIMEOUT`, is
//!   `{"type":"join","token":"<room token>","since":N}` (`since`: the last seq it has, 0 for
//!   none). Anything else, or a wrong token, closes with 4401; a deleted or expired room 4410.
//! - A room that cannot be joined is refused after the upgrade, never as an HTTP status (a browser
//!   WebSocket cannot read one): an unknown, deleted or expired room closes at once with 4410
//!   ("gone"), a room at its connection cap with 4429 ("room_full": try again later). Only a
//!   malformed room id is an HTTP 400 before the upgrade.
//! - The host answers `{"type":"welcome","seq":<last seq>,"snapshot_seq":<seq or null>,
//!   "ended":bool,"ended_at":<secs or null>,"expires":<secs>,"present":n}`, then binary
//!   `0x03 || snapshot_seq u64 BE || snapshot` when the client is behind the snapshot, then every
//!   logged frame after that as `0x01 || seq u64 BE || envelope`.
//! - Live: other connections' frames `0x01 || seq || envelope`, their presence `0x02 ||
//!   envelope` (never logged), `{"type":"count","present":n}`, `{"type":"ended","ended_at":..,
//!   "expires":..}`. A deleted room closes with 4410; a client too slow for the relay of frames
//!   with 4409 ("lagged": rejoin with `since`).
//! - Presence has its own broadcast channel in the room, apart from frames, counts and the end:
//!   a connection that falls behind on it just misses some presence messages (the next carets
//!   replace them) and is not closed, and presence is not ordered against frames.
//! - The client sends `0x01 || envelope` (logged, relayed) and `0x02 || envelope` (relayed,
//!   at most `PRESENCE_PER_SEC` a second after a burst of `PRESENCE_BURST`; the rest is dropped
//!   without an answer, since the next caret supersedes it).
//!   A frame is acknowledged with `{"type":"ack","seq":n}`, sent in seq order with the frames
//!   relayed to this connection (a client that saw the ack for n has every frame up to n).
//!   Over the client's daily write budget (`write_bytes_per_day`, counted per client address
//!   across every upload) a logged frame is refused with `{"type":"error","code":"write_budget",
//!   "retry_after":<seconds>}` and a close 4429, the code of "try again later" (the guest page
//!   retries it with backoff); the frame is not logged or relayed.
//!   Refusals: ended room → close 4403; over the frame cap or a full log → `{"type":"error",
//!   "code":"frame_too_large"|"log_full"}` then close 4413; an empty or unknown frame →
//!   `{"type":"error","code":"bad_frame"}` (the connection stays).
//! - Media (notes images, sealed by the owner's page or a guest's) travel on the same socket, beside
//!   the log and outside it. A blob's name is 32 bytes (64 lowercase hex characters as text) the
//!   host cannot interpret; a name is stored once and never replaced.
//!   Up: `0x04 || name(32) || envelope` stores the blob (at most `room_media_max_bytes`; the
//!   room's media at most `room_media_total_bytes`) and is answered to the sender with
//!   `{"type":"media","name":"<hex>","ok":true}` (also when the name was already stored), while
//!   every other connection of the room, whoever stored it (a guest, or the owner's bridge over
//!   HTTP), is told `{"type":"media","name":"<hex>"}` so one waiting for it can ask at once.
//!   Down: the text `{"type":"get_media","name":"<hex>"}` is answered with `0x05 || name(32) ||
//!   envelope`. At most `MAX_GETS` gets of one connection are in flight; one more is answered
//!   `media_busy` (ask again after an answer). A malformed get is a `bad_frame`.
//!   Media errors are `{"type":"error","code":..,"name":"<hex>"}` and leave the connection open:
//!   `media_missing` (a get; the room has no such name), `media_too_large`, `room_media_full`,
//!   `media_busy`, `empty_media`. Over the write budget a put is refused like a frame
//!   (`write_budget`, close 4429; the blob's bytes are charged, a stored name's too). In an ended
//!   room a put is `{"type":"error","code":"room_ended"}` and a close 4403, like a frame. A put
//!   shorter than its name is a `bad_frame`.
//! - Text `ping` is answered with `pong`. The host pings every `PING_EVERY`; a connection it has
//!   heard nothing from for `SILENT_FOR` is closed (1001, "idle").

use std::time::{Duration, Instant as StdInstant};

use axum::body::Bytes;
use axum::extract::ws::{CloseFrame, Message, Utf8Bytes, WebSocket};
use axum::http::StatusCode;
use serde::Deserialize;
use serde_json::{json, Value};
use tokio::io::AsyncReadExt;
use tokio::sync::broadcast::error::RecvError;
use tokio::sync::mpsc;
use tokio::time::{interval_at, timeout, Instant, MissedTickBehavior};

use crate::app::{ClientIp, Shared};
use crate::error::ApiError;
use crate::metrics::Metrics;
use crate::rooms::{Event, Joined};
use crate::store::valid_sha;

pub const JOIN_TIMEOUT: Duration = Duration::from_secs(10);
pub const PING_EVERY: Duration = Duration::from_secs(30);
pub const SILENT_FOR: Duration = Duration::from_secs(90);
/// Room tokens are 43 characters (32 bytes, base64url); anything far longer is not one.
const MAX_TOKEN: usize = 256;

/// Presence a connection may relay: `PRESENCE_BURST` at once, then `PRESENCE_PER_SEC` a second;
/// more is dropped silently (no error, no close). Carets are sent several times a second per
/// person and each one is fanned out to every other guest, so an unbounded sender (a stuck
/// key repeat, a buggy or hostile page) would flood a room's listeners; a person's real caret
/// rate stays well under this.
pub const PRESENCE_PER_SEC: f64 = 10.0;
pub const PRESENCE_BURST: f64 = 20.0;

pub const KIND_FRAME: u8 = 0x01;
pub const KIND_PRESENCE: u8 = 0x02;
pub const KIND_SNAPSHOT: u8 = 0x03;
/// Up: `name(32) || envelope`, a media blob to store.
pub const KIND_MEDIA_PUT: u8 = 0x04;
/// Down: `name(32) || envelope`, the answer to a `get_media`.
pub const KIND_MEDIA: u8 = 0x05;
/// A media blob's name in bytes (64 hex characters as text).
pub const NAME_LEN: usize = 32;
/// Media gets of one connection answered at once (each holds a blob, 2 MiB and a bit, in memory
/// until it is sent); more are answered `media_busy`.
pub const MAX_GETS: usize = 20;

pub const CLOSE_IDLE: u16 = 1001;
pub const CLOSE_SERVER_ERROR: u16 = 1011;
pub const CLOSE_UNAUTHORIZED: u16 = 4401;
pub const CLOSE_ENDED: u16 = 4403;
pub const CLOSE_LAGGED: u16 = 4409;
pub const CLOSE_GONE: u16 = 4410;
pub const CLOSE_TOO_LARGE: u16 = 4413;
pub const CLOSE_FULL: u16 = 4429;
/// Over the daily write budget: the same "try again later" code as a full room.
pub const CLOSE_BUDGET: u16 = CLOSE_FULL;

/// A connection counted into its room; counted out when dropped, however the socket ends (or
/// if the upgrade never happens).
pub struct Connection {
    app: Shared,
    room: String,
    id: u64,
    /// The client address the write budget is charged to.
    client: ClientIp,
}

impl Connection {
    pub fn new(app: Shared, room: String, id: u64, client: ClientIp) -> Connection {
        Connection {
            app,
            room,
            id,
            client,
        }
    }
}

impl Drop for Connection {
    fn drop(&mut self) {
        let app = self.app.clone();
        let room = std::mem::take(&mut self.room);
        if let Ok(rt) = tokio::runtime::Handle::try_current() {
            rt.spawn(async move { app.rooms.disconnect(&room).await });
        }
    }
}

#[derive(Deserialize)]
struct Join {
    #[serde(rename = "type")]
    kind: String,
    token: String,
    #[serde(default)]
    since: u64,
}

/// A token bucket for one connection's presence.
struct PresenceBucket {
    tokens: f64,
    last: StdInstant,
}

impl PresenceBucket {
    fn new(now: StdInstant) -> PresenceBucket {
        PresenceBucket {
            tokens: PRESENCE_BURST,
            last: now,
        }
    }

    /// Takes a token if there is one.
    fn allow(&mut self, now: StdInstant) -> bool {
        let elapsed = now.saturating_duration_since(self.last).as_secs_f64();
        self.last = now;
        self.tokens = (self.tokens + elapsed * PRESENCE_PER_SEC).min(PRESENCE_BURST);
        if self.tokens >= 1.0 {
            self.tokens -= 1.0;
            true
        } else {
            false
        }
    }
}

/// How a step of the connection ended, when it did.
enum Stop {
    /// Send this close frame.
    Close(u16, &'static str),
    /// The peer is gone or the socket failed: nothing to send.
    Lost,
}

type Step = Result<(), Stop>;

fn tagged(kind: u8, seq: Option<u64>, bytes: &[u8]) -> Message {
    let mut out = Vec::with_capacity(9 + bytes.len());
    out.push(kind);
    if let Some(seq) = seq {
        out.extend_from_slice(&seq.to_be_bytes());
    }
    out.extend_from_slice(bytes);
    Message::Binary(Bytes::from(out))
}

fn name_hex(raw: &[u8]) -> String {
    raw.iter().map(|b| format!("{b:02x}")).collect()
}

/// The 32 bytes of a name that is 64 lowercase hex characters (`valid_sha`).
fn name_bytes(hex: &str) -> Option<[u8; NAME_LEN]> {
    if !valid_sha(hex) {
        return None;
    }
    let digit = |b: u8| if b <= b'9' { b - b'0' } else { b - b'a' + 10 };
    let mut out = [0u8; NAME_LEN];
    for (byte, pair) in out.iter_mut().zip(hex.as_bytes().chunks(2)) {
        *byte = digit(pair[0]) << 4 | digit(pair[1]);
    }
    Some(out)
}

/// What a media get ended with: the name asked for and the tagged message, or why not.
type Answer = (String, Result<Vec<u8>, ApiError>);

/// The media gets of one connection: answers come back on `tx`'s channel, so the loop goes on
/// relaying while a blob is read.
struct Gets {
    tx: mpsc::Sender<Answer>,
    inflight: usize,
}

impl Gets {
    /// Counts a get in and hands out the sender its task answers on; `None` at `MAX_GETS`.
    fn begin(&mut self) -> Option<mpsc::Sender<Answer>> {
        if self.inflight >= MAX_GETS {
            return None;
        }
        self.inflight += 1;
        Some(self.tx.clone())
    }

    /// An answer came back.
    fn finished(&mut self) {
        self.inflight = self.inflight.saturating_sub(1);
    }
}

async fn read_media(app: Shared, room: String, name: String) -> Result<Vec<u8>, ApiError> {
    let raw = name_bytes(&name).ok_or(ApiError::bad("bad_name"))?;
    let (mut file, len) = app.rooms.open_media(&room, &name).await?;
    let mut out = Vec::with_capacity(1 + NAME_LEN + len as usize);
    out.push(KIND_MEDIA);
    out.extend_from_slice(&raw);
    file.read_to_end(&mut out).await?;
    Ok(out)
}

async fn send(socket: &mut WebSocket, msg: Message) -> Step {
    socket.send(msg).await.map_err(|_| Stop::Lost)
}

async fn send_json(socket: &mut WebSocket, value: Value) -> Step {
    send(socket, Message::Text(value.to_string().into())).await
}

/// A store error met while joining, as a close.
fn join_refused(e: &ApiError) -> Stop {
    match e.status {
        StatusCode::FORBIDDEN => Stop::Close(CLOSE_UNAUTHORIZED, "wrong_room_token"),
        StatusCode::NOT_FOUND | StatusCode::GONE => Stop::Close(CLOSE_GONE, "gone"),
        _ => Stop::Close(CLOSE_SERVER_ERROR, "server_error"),
    }
}

/// A store error met on a frame the client sent.
async fn frame_refused(socket: &mut WebSocket, e: &ApiError) -> Step {
    match e.status {
        StatusCode::CONFLICT => Err(Stop::Close(CLOSE_ENDED, "ended")),
        StatusCode::NOT_FOUND | StatusCode::GONE => Err(Stop::Close(CLOSE_GONE, "gone")),
        StatusCode::PAYLOAD_TOO_LARGE => {
            send_json(socket, json!({ "type": "error", "code": e.code })).await?;
            Err(Stop::Close(CLOSE_TOO_LARGE, e.code))
        }
        StatusCode::BAD_REQUEST => {
            send_json(socket, json!({ "type": "error", "code": "bad_frame" })).await
        }
        _ => {
            send_json(socket, json!({ "type": "error", "code": e.code })).await?;
            Err(Stop::Close(CLOSE_SERVER_ERROR, "server_error"))
        }
    }
}

async fn await_join(socket: &mut WebSocket) -> Result<Join, Stop> {
    let refused = Stop::Close(CLOSE_UNAUTHORIZED, "join_required");
    loop {
        match socket.recv().await {
            None | Some(Err(_)) | Some(Ok(Message::Close(_))) => return Err(Stop::Lost),
            Some(Ok(Message::Ping(_) | Message::Pong(_))) => continue,
            Some(Ok(Message::Binary(_))) => return Err(refused),
            Some(Ok(Message::Text(t))) => {
                return serde_json::from_str::<Join>(t.as_str())
                    .ok()
                    .filter(|j| j.kind == "join" && j.token.len() <= MAX_TOKEN)
                    .ok_or(refused)
            }
        }
    }
}

/// Sends a close frame and waits briefly for the peer's answer, so it sees a clean handshake.
async fn close_with(socket: &mut WebSocket, code: u16, reason: &'static str) {
    let frame = CloseFrame {
        code,
        reason: Utf8Bytes::from_static(reason),
    };
    if socket.send(Message::Close(Some(frame))).await.is_ok() {
        let _ = timeout(Duration::from_secs(2), async {
            while let Some(Ok(m)) = socket.recv().await {
                if matches!(m, Message::Close(_)) {
                    break;
                }
            }
        })
        .await;
    }
}

/// A connection the room refused (gone, full): the upgrade completes and the close follows at
/// once, because a browser cannot see the HTTP status of a refused upgrade.
pub async fn refuse(mut socket: WebSocket, code: u16, reason: &'static str) {
    close_with(&mut socket, code, reason).await;
}

/// Runs the connection to its end; the close (if any) is sent here.
pub async fn run(mut socket: WebSocket, conn: Connection) {
    if let Err(Stop::Close(code, reason)) = serve(&mut socket, &conn).await {
        close_with(&mut socket, code, reason).await;
    }
    drop(conn);
}

async fn serve(socket: &mut WebSocket, conn: &Connection) -> Step {
    let rooms = &conn.app.rooms;
    let join = match timeout(JOIN_TIMEOUT, await_join(socket)).await {
        Ok(join) => join?,
        Err(_) => return Err(Stop::Close(CLOSE_UNAUTHORIZED, "join_timeout")),
    };
    rooms
        .check_guest(&conn.room, &join.token)
        .await
        .map_err(|e| join_refused(&e))?;
    let Joined {
        mut rx,
        mut presence_rx,
        replay: (snapshot, frames),
        meta,
        connections,
    } = rooms
        .join_with_meta(&conn.room, join.since)
        .await
        .map_err(|e| join_refused(&e))?;

    Metrics::inc(&conn.app.metrics.guest_connections);
    send_json(
        socket,
        json!({
            "type": "welcome",
            "seq": meta.last_seq,
            "snapshot_seq": (meta.snapshot_bytes > 0).then_some(meta.snapshot_seq),
            "ended": meta.ended_at.is_some(),
            "ended_at": meta.ended_at,
            "expires": meta.expires,
            "present": connections,
        }),
    )
    .await?;
    if let Some((seq, bytes)) = snapshot {
        send(socket, tagged(KIND_SNAPSHOT, Some(seq), &bytes)).await?;
    }
    for (seq, bytes) in frames {
        send(socket, tagged(KIND_FRAME, Some(seq), &bytes)).await?;
    }

    let mut ping = interval_at(Instant::now() + PING_EVERY, PING_EVERY);
    ping.set_missed_tick_behavior(MissedTickBehavior::Delay);
    let mut heard = Instant::now();
    let mut presence_open = true;
    let mut bucket = PresenceBucket::new(StdInstant::now());
    let (media_tx, mut media_rx) = mpsc::channel::<Answer>(MAX_GETS);
    let mut gets = Gets {
        tx: media_tx,
        inflight: 0,
    };
    loop {
        tokio::select! {
            event = rx.recv() => match event {
                Ok(event) => relay(socket, &event, conn.id).await?,
                Err(RecvError::Lagged(_)) => return Err(Stop::Close(CLOSE_LAGGED, "lagged")),
                Err(RecvError::Closed) => return Err(Stop::Close(CLOSE_GONE, "gone")),
            },
            event = presence_rx.recv(), if presence_open => match event {
                Ok(event) => relay(socket, &event, conn.id).await?,
                // Carets are disposable: skip what was missed and carry on.
                Err(RecvError::Lagged(_)) => continue,
                // Both channels close together; leave the close to the main one, which says
                // why (`Deleted` comes before its end).
                Err(RecvError::Closed) => presence_open = false,
            },
            // `gets` holds a sender, so the channel never closes under us.
            Some((name, answer)) = media_rx.recv() => {
                gets.finished();
                match answer {
                    Ok(msg) => send(socket, Message::Binary(Bytes::from(msg))).await?,
                    Err(e) => media_refused(socket, &name, &e).await?,
                }
            }
            msg = socket.recv() => {
                let msg = match msg {
                    None | Some(Err(_)) | Some(Ok(Message::Close(_))) => return Err(Stop::Lost),
                    Some(Ok(msg)) => msg,
                };
                heard = Instant::now();
                incoming(socket, conn, &mut bucket, &mut gets, msg).await?;
            }
            _ = ping.tick() => {
                if heard.elapsed() >= SILENT_FOR {
                    return Err(Stop::Close(CLOSE_IDLE, "idle"));
                }
                send(socket, Message::Ping(Bytes::new())).await?;
            }
        }
    }
}

async fn relay(socket: &mut WebSocket, event: &Event, me: u64) -> Step {
    match event {
        // Our own frame comes back here, in seq order: that is when it is acknowledged.
        Event::Frame { seq, from, .. } if *from == me => {
            send_json(socket, json!({ "type": "ack", "seq": seq })).await
        }
        Event::Frame { seq, bytes, .. } => {
            send(socket, tagged(KIND_FRAME, Some(*seq), bytes)).await
        }
        Event::Presence { from, .. } if *from == me => Ok(()),
        Event::Presence { bytes, .. } => send(socket, tagged(KIND_PRESENCE, None, bytes)).await,
        Event::Media { from, .. } if *from == me => Ok(()),
        Event::Media { name, .. } => {
            send_json(socket, json!({ "type": "media", "name": name })).await
        }
        Event::Count(n) => send_json(socket, json!({ "type": "count", "present": n })).await,
        Event::Ended { ended_at, expires } => {
            send_json(
                socket,
                json!({ "type": "ended", "ended_at": ended_at, "expires": expires }),
            )
            .await
        }
        Event::Deleted => Err(Stop::Close(CLOSE_GONE, "deleted")),
    }
}

/// Charges `bytes` to the connection's client. Over the daily budget the refusal is sent as
/// an error message and the connection is closed with 4429.
async fn charge(socket: &mut WebSocket, conn: &Connection, bytes: usize) -> Step {
    if let Err(e) = conn.app.charge_write(conn.client, bytes as u64) {
        send_json(
            socket,
            json!({
                "type": "error",
                "code": e.code,
                "retry_after": e.retry_after,
            }),
        )
        .await?;
        return Err(Stop::Close(CLOSE_BUDGET, e.code));
    }
    Ok(())
}

/// A store error met on a media put or get: told with the blob's name, and the connection
/// stays, except where a frame's would close (an ended or gone room).
async fn media_refused(socket: &mut WebSocket, name: &str, e: &ApiError) -> Step {
    let told = json!({ "type": "error", "code": e.code, "name": name });
    match e.status {
        StatusCode::CONFLICT => {
            send_json(socket, told).await?;
            Err(Stop::Close(CLOSE_ENDED, "ended"))
        }
        StatusCode::NOT_FOUND if e.code == "media_missing" => send_json(socket, told).await,
        StatusCode::NOT_FOUND | StatusCode::GONE => Err(Stop::Close(CLOSE_GONE, "gone")),
        StatusCode::PAYLOAD_TOO_LARGE | StatusCode::BAD_REQUEST => send_json(socket, told).await,
        _ => {
            send_json(socket, told).await?;
            Err(Stop::Close(CLOSE_SERVER_ERROR, "server_error"))
        }
    }
}

/// `0x04 || name(32) || envelope`.
async fn media_put(socket: &mut WebSocket, conn: &Connection, rest: &[u8]) -> Step {
    let Some((raw, env)) = rest.split_at_checked(NAME_LEN) else {
        return send_json(socket, json!({ "type": "error", "code": "bad_frame" })).await;
    };
    let name = name_hex(raw);
    charge(socket, conn, env.len()).await?;
    match conn
        .app
        .rooms
        .put_media(&conn.room, conn.id, &name, env)
        .await
    {
        Ok(_) => send_json(socket, json!({ "type": "media", "name": name, "ok": true })).await,
        Err(e) => media_refused(socket, &name, &e).await,
    }
}

#[derive(Deserialize)]
struct MediaRequest {
    #[serde(rename = "type")]
    kind: String,
    #[serde(default)]
    name: String,
}

/// `{"type":"get_media","name":hex}`: read in a task, answered through `gets`. Other text is
/// not ours and needs nothing.
async fn media_get(socket: &mut WebSocket, conn: &Connection, gets: &mut Gets, text: &str) -> Step {
    let Ok(req) = serde_json::from_str::<MediaRequest>(text) else {
        return Ok(());
    };
    if req.kind != "get_media" {
        return Ok(());
    }
    if !valid_sha(&req.name) {
        return send_json(socket, json!({ "type": "error", "code": "bad_frame" })).await;
    }
    let Some(tx) = gets.begin() else {
        return send_json(
            socket,
            json!({ "type": "error", "code": "media_busy", "name": req.name }),
        )
        .await;
    };
    let (app, room) = (conn.app.clone(), conn.room.clone());
    tokio::spawn(async move {
        let answer = read_media(app, room, req.name.clone()).await;
        // The loop is gone when the connection ended: nobody to answer.
        let _ = tx.send((req.name, answer)).await;
    });
    Ok(())
}

async fn incoming(
    socket: &mut WebSocket,
    conn: &Connection,
    bucket: &mut PresenceBucket,
    gets: &mut Gets,
    msg: Message,
) -> Step {
    let rooms = &conn.app.rooms;
    match msg {
        Message::Text(t) if t.as_str() == "ping" => send(socket, Message::text("pong")).await,
        Message::Text(t) => media_get(socket, conn, gets, t.as_str()).await,
        Message::Binary(b) => {
            let result = match b.split_first() {
                Some((&KIND_FRAME, env)) => {
                    charge(socket, conn, env.len()).await?;
                    let appended = rooms.append(&conn.room, conn.id, env).await.map(drop);
                    if appended.is_ok() {
                        Metrics::inc(&conn.app.metrics.frames_relayed);
                    }
                    appended
                }
                Some((&KIND_MEDIA_PUT, rest)) => return media_put(socket, conn, rest).await,
                Some((&KIND_PRESENCE, _)) if !bucket.allow(StdInstant::now()) => return Ok(()),
                Some((&KIND_PRESENCE, env)) => rooms.presence(&conn.room, conn.id, env).await,
                _ => Err(ApiError::bad("bad_frame")),
            };
            match result {
                Ok(()) => Ok(()),
                Err(e) => frame_refused(socket, &e).await,
            }
        }
        // Protocol pings and the like need nothing from us.
        _ => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_round_trip_between_hex_and_bytes() {
        let hex = "00ff10abcdef0123456789abcdef0123456789abcdef0123456789abcdef01aa";
        let raw = name_bytes(hex).unwrap();
        assert_eq!(raw[..3], [0x00, 0xff, 0x10]);
        assert_eq!(name_hex(&raw), hex);
        assert_eq!(name_bytes(&hex.to_uppercase()), None);
        assert_eq!(name_bytes(&hex[..62]), None);
    }

    #[test]
    fn gets_in_flight_are_capped() {
        let mut gets = Gets {
            tx: mpsc::channel(MAX_GETS).0,
            inflight: 0,
        };
        assert!((0..MAX_GETS).all(|_| gets.begin().is_some()));
        assert!(gets.begin().is_none());
        gets.finished();
        assert!(gets.begin().is_some());
        assert!(gets.begin().is_none());
        (0..MAX_GETS + 5).for_each(|_| gets.finished());
        assert_eq!(gets.inflight, 0);
    }

    #[test]
    fn the_bucket_allows_a_burst_then_the_rate() {
        let t0 = StdInstant::now();
        let mut b = PresenceBucket::new(t0);
        let burst = PRESENCE_BURST as usize;
        assert_eq!((0..burst + 10).filter(|_| b.allow(t0)).count(), burst);
        // A tenth of a second later (at 10/s) exactly one more is allowed.
        let t1 = t0 + Duration::from_millis(100);
        assert!(b.allow(t1));
        assert!(!b.allow(t1));
        // A long pause refills to the burst and no further.
        let t2 = t1 + Duration::from_secs(60);
        assert_eq!((0..burst + 10).filter(|_| b.allow(t2)).count(), burst);
    }
}
