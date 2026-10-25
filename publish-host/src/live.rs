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
//!   "expires":..}`. A deleted room closes with 4410; a client too slow for the relay with 4409
//!   ("lagged": rejoin with `since`).
//! - The client sends `0x01 || envelope` (logged, relayed) and `0x02 || envelope` (relayed).
//!   A frame is acknowledged with `{"type":"ack","seq":n}`, sent in seq order with the frames
//!   relayed to this connection (a client that saw the ack for n has every frame up to n).
//!   Refusals: ended room → close 4403; over the frame cap or a full log → `{"type":"error",
//!   "code":"frame_too_large"|"log_full"}` then close 4413; an empty or unknown frame →
//!   `{"type":"error","code":"bad_frame"}` (the connection stays).
//! - Text `ping` is answered with `pong`. The host pings every `PING_EVERY`; a connection it has
//!   heard nothing from for `SILENT_FOR` is closed (1001, "idle").

use std::time::Duration;

use axum::body::Bytes;
use axum::extract::ws::{CloseFrame, Message, Utf8Bytes, WebSocket};
use axum::http::StatusCode;
use serde::Deserialize;
use serde_json::{json, Value};
use tokio::sync::broadcast::error::RecvError;
use tokio::time::{interval_at, timeout, Instant, MissedTickBehavior};

use crate::app::Shared;
use crate::error::ApiError;
use crate::rooms::{Event, Joined};

pub const JOIN_TIMEOUT: Duration = Duration::from_secs(10);
pub const PING_EVERY: Duration = Duration::from_secs(30);
pub const SILENT_FOR: Duration = Duration::from_secs(90);
/// Room tokens are 43 characters (32 bytes, base64url); anything far longer is not one.
const MAX_TOKEN: usize = 256;

pub const KIND_FRAME: u8 = 0x01;
pub const KIND_PRESENCE: u8 = 0x02;
pub const KIND_SNAPSHOT: u8 = 0x03;

pub const CLOSE_IDLE: u16 = 1001;
pub const CLOSE_SERVER_ERROR: u16 = 1011;
pub const CLOSE_UNAUTHORIZED: u16 = 4401;
pub const CLOSE_ENDED: u16 = 4403;
pub const CLOSE_LAGGED: u16 = 4409;
pub const CLOSE_GONE: u16 = 4410;
pub const CLOSE_TOO_LARGE: u16 = 4413;
pub const CLOSE_FULL: u16 = 4429;

/// A connection counted into its room; counted out when dropped, however the socket ends (or
/// if the upgrade never happens).
pub struct Connection {
    app: Shared,
    room: String,
    id: u64,
}

impl Connection {
    pub fn new(app: Shared, room: String, id: u64) -> Connection {
        Connection { app, room, id }
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
        replay: (snapshot, frames),
        meta,
        connections,
    } = rooms
        .join_with_meta(&conn.room, join.since)
        .await
        .map_err(|e| join_refused(&e))?;

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
    loop {
        tokio::select! {
            event = rx.recv() => match event {
                Ok(event) => relay(socket, &event, conn.id).await?,
                Err(RecvError::Lagged(_)) => return Err(Stop::Close(CLOSE_LAGGED, "lagged")),
                Err(RecvError::Closed) => return Err(Stop::Close(CLOSE_GONE, "gone")),
            },
            msg = socket.recv() => {
                let msg = match msg {
                    None | Some(Err(_)) | Some(Ok(Message::Close(_))) => return Err(Stop::Lost),
                    Some(Ok(msg)) => msg,
                };
                heard = Instant::now();
                incoming(socket, conn, msg).await?;
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

async fn incoming(socket: &mut WebSocket, conn: &Connection, msg: Message) -> Step {
    let rooms = &conn.app.rooms;
    match msg {
        Message::Text(t) if t.as_str() == "ping" => send(socket, Message::text("pong")).await,
        Message::Binary(b) => {
            let result = match b.split_first() {
                Some((&KIND_FRAME, env)) => rooms.append(&conn.room, conn.id, env).await.map(drop),
                Some((&KIND_PRESENCE, env)) => rooms.presence(&conn.room, conn.id, env).await,
                _ => Err(ApiError::bad("bad_frame")),
            };
            match result {
                Ok(()) => Ok(()),
                Err(e) => frame_refused(socket, &e).await,
            }
        }
        // Other text (a second join, say) and protocol pings need nothing from us.
        _ => Ok(()),
    }
}
