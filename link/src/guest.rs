//! Guest notes over iroh: one endpoint per room, reached by guests' browsers (guest-wasm)
//! through one relay, each guest's bi stream proxied to the room's WebSocket on the embedded
//! loopback host ([`crate::host`]).
//!
//! A room endpoint only speaks [`GUEST_ALPN`]: it never serves the tunnel or pairing streams of
//! [`crate::ALPN`], and it has its own key (`<data_dir>/guest_rooms/keys/<room>.key`), so its
//! endpoint id says nothing about this machine's `link.key`. The key outlives restarts, so a
//! guest link stays valid; [`GuestRooms::stop`] deletes it.
//!
//! The stream carries [`frame`]s: tag text = a WebSocket text message, tag binary = a binary
//! one, in both directions, in the protocol of publish-host/src/live.rs. The host's WebSocket
//! close `(code, reason)` becomes the QUIC application close of the guest's connection (after
//! the stream is finished, so every frame before it arrives); the guest finishing its stream
//! closes the WebSocket with 1000. WebSocket pings are answered here and never forwarded.

pub mod frame;

use std::{
    collections::HashMap,
    net::SocketAddr,
    path::PathBuf,
    sync::{Arc, LazyLock},
    time::Duration,
};

use anyhow::{anyhow, bail, Context, Result};
use futures_util::{
    stream::{SplitSink, SplitStream},
    SinkExt, StreamExt,
};
use iroh::{
    endpoint::{presets, Builder, Connection, ReadExactError, RecvStream, SendStream, VarInt},
    Endpoint, EndpointId, RelayMode, RelayUrl, SecretKey, Watcher,
};
use mnemosyne_publish_host::store::valid_id;
use tokio::{net::TcpStream, sync::Mutex, task::JoinHandle, time::timeout};
use tokio_tungstenite::{
    tungstenite::{
        protocol::{frame::coding::CloseCode, CloseFrame},
        Message,
    },
    MaybeTlsStream, WebSocketStream,
};

use crate::{handle_with, load_or_create_key, ControlHandler, ControlRequest, Relays};

pub const GUEST_ALPN: &[u8] = b"mnemosyne/guest/1";

/// How long a guest has, once connected, to open its stream (its join is the first frame).
const ACCEPT_STREAM: Duration = Duration::from_secs(15);
/// How long a closing side gets to deliver what is in flight.
const DRAIN: Duration = Duration::from_secs(3);
/// How long [`pick_relay`] and [`GuestRooms::start`] wait for a relay.
const ONLINE: Duration = Duration::from_secs(10);

// Close codes this proxy sends itself (the host's own come through unchanged).
const CLOSE_NORMAL: u16 = 1000;
const CLOSE_PROTOCOL: u16 = 1002;
const CLOSE_BAD_TEXT: u16 = 1007;
const CLOSE_HOST_LOST: u16 = 1011;
const CLOSE_UNAUTHORIZED: u16 = 4401;
const CLOSE_TOO_LARGE: u16 = 4413;

/// A last adjustment to every endpoint builder (tests trust a local relay's certificate).
pub type Adjust = Arc<dyn Fn(Builder) -> Builder + Send + Sync>;

/// One relay for every room of this process: the only one configured, or the home relay a
/// throwaway endpoint settles on among several (or n0's). Remembered per process, per relay
/// configuration. Fails when no relay answers within 10 s, or with [`Relays::Off`].
pub async fn pick_relay(relays: &Relays) -> Result<RelayUrl> {
    pick_relay_with(relays, |b| b).await
}

/// [`pick_relay`] with a last adjustment to the throwaway endpoint's builder.
pub async fn pick_relay_with(
    relays: &Relays,
    adjust: impl FnOnce(Builder) -> Builder,
) -> Result<RelayUrl> {
    static PICKED: LazyLock<Mutex<HashMap<String, RelayUrl>>> =
        LazyLock::new(|| Mutex::new(HashMap::new()));
    let mode = match relays {
        Relays::Off => bail!("guest rooms need a relay"),
        Relays::Custom(urls) if urls.len() == 1 => return Ok(urls[0].clone()),
        Relays::Custom(urls) if urls.is_empty() => bail!("guest rooms need a relay"),
        Relays::Custom(urls) => RelayMode::Custom(urls.iter().cloned().collect()),
        Relays::N0 => RelayMode::Default,
    };
    // Held while picking, so concurrent first rooms share one throwaway endpoint.
    let mut picked = PICKED.lock().await;
    let key = format!("{relays:?}");
    if let Some(url) = picked.get(&key) {
        return Ok(url.clone());
    }
    let endpoint = adjust(Endpoint::builder(presets::Minimal).relay_mode(mode))
        .bind()
        .await
        .context("binding an endpoint to pick a relay")?;
    let online = timeout(ONLINE, endpoint.online()).await.is_ok();
    let url = endpoint
        .home_relay_status()
        .get()
        .into_iter()
        .find(|s| s.is_connected())
        .map(|s| s.url().clone());
    endpoint.close().await;
    let url = match (online, url) {
        (true, Some(url)) => url,
        _ => bail!("no relay reachable within {} s", ONLINE.as_secs()),
    };
    tracing::info!(%url, "relay for guest rooms");
    picked.insert(key, url.clone());
    Ok(url)
}

/// A room's endpoint: only [`GUEST_ALPN`], only `relay`, no local-network announcement.
pub async fn bind_guest(key: SecretKey, relay: &RelayUrl) -> Result<Endpoint> {
    bind_guest_with(key, relay, |b| b).await
}

/// [`bind_guest`] with a last adjustment to the builder.
pub async fn bind_guest_with(
    key: SecretKey,
    relay: &RelayUrl,
    adjust: impl FnOnce(Builder) -> Builder,
) -> Result<Endpoint> {
    let builder = Endpoint::builder(presets::Minimal)
        .relay_mode(RelayMode::Custom(std::iter::once(relay.clone()).collect()))
        .secret_key(key)
        .alpns(vec![GUEST_ALPN.to_vec()]);
    Ok(adjust(builder).bind().await?)
}

/// How a guest reaches a room.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RoomInfo {
    pub room: String,
    pub endpoint_id: EndpointId,
    pub relay: RelayUrl,
}

impl RoomInfo {
    fn json(&self) -> serde_json::Value {
        serde_json::json!({
            "room": self.room,
            "endpoint_id": self.endpoint_id.to_string(),
            "relay": self.relay.to_string(),
        })
    }
}

/// What [`GuestRooms`] needs.
#[derive(Debug, Clone)]
pub struct GuestOptions {
    /// The backend's data directory: room keys go in `<data_dir>/guest_rooms/keys`.
    pub data_dir: PathBuf,
    /// The loopback host ([`crate::host::start_loopback_host`]).
    pub host: SocketAddr,
    /// Where [`pick_relay`] picks from when a room is started without a relay.
    pub relays: Relays,
}

struct Room {
    endpoint: Endpoint,
    relay: RelayUrl,
    task: JoinHandle<()>,
}

impl Room {
    fn info(&self, room: &str) -> RoomInfo {
        RoomInfo {
            room: room.to_string(),
            endpoint_id: self.endpoint.id(),
            relay: self.relay.clone(),
        }
    }
}

/// The rooms this process serves over iroh, driven by the `rooms` control channel.
pub struct GuestRooms {
    opts: GuestOptions,
    adjust: Adjust,
    rooms: Mutex<HashMap<String, Room>>,
}

impl GuestRooms {
    pub fn new(opts: GuestOptions) -> Self {
        Self::with_adjust(opts, Arc::new(|b| b))
    }

    /// [`GuestRooms::new`] with a last adjustment to every endpoint's builder.
    pub fn with_adjust(opts: GuestOptions, adjust: Adjust) -> Self {
        GuestRooms {
            opts,
            adjust,
            rooms: Mutex::new(HashMap::new()),
        }
    }

    fn key_path(&self, room: &str) -> PathBuf {
        self.opts
            .data_dir
            .join("guest_rooms")
            .join("keys")
            .join(format!("{room}.key"))
    }

    /// Serve `room` on its endpoint (the same id across restarts) through `relay`, or the
    /// process's [`pick_relay`] when none is given. Pass the relay stored from an earlier
    /// start, so the guest link stays valid. A room already served is returned as it is.
    pub async fn start(&self, room: &str, relay: Option<RelayUrl>) -> Result<RoomInfo> {
        if !valid_id(room) {
            bail!("invalid room id {room:?}");
        }
        if let Some(r) = self.rooms.lock().await.get(room) {
            if !r.task.is_finished() {
                return Ok(r.info(room));
            }
        }
        let relay = match relay {
            Some(relay) => relay,
            None => pick_relay_with(&self.opts.relays, |b| (self.adjust)(b)).await?,
        };
        let key = load_or_create_key(&self.key_path(room)).context("loading the room key")?;
        let endpoint = bind_guest_with(key, &relay, |b| (self.adjust)(b)).await?;
        if timeout(ONLINE, endpoint.online()).await.is_err() {
            tracing::warn!(room, %relay, "relay not reachable yet; guests cannot connect until it is");
        }
        let mut rooms = self.rooms.lock().await;
        if let Some(r) = rooms.get(room) {
            if !r.task.is_finished() {
                // Started meanwhile by a concurrent request: keep that one.
                endpoint.close().await;
                return Ok(r.info(room));
            }
        }
        let task = tokio::spawn(serve_guests(
            endpoint.clone(),
            self.opts.host,
            room.to_string(),
        ));
        let r = Room {
            endpoint,
            relay,
            task,
        };
        let info = r.info(room);
        if let Some(old) = rooms.insert(room.to_string(), r) {
            old.endpoint.close().await;
        }
        tracing::info!(room, endpoint = %info.endpoint_id.fmt_short(), "guest room up");
        Ok(info)
    }

    /// Stop serving `room`: its guests are disconnected and its key is deleted (a later start
    /// gives a new endpoint id). Returns whether it was served.
    pub async fn stop(&self, room: &str) -> bool {
        if !valid_id(room) {
            return false;
        }
        let served = self.rooms.lock().await.remove(room);
        if let Err(e) = std::fs::remove_file(self.key_path(room)) {
            if e.kind() != std::io::ErrorKind::NotFound {
                tracing::warn!(room, "cannot delete the room key: {e}");
            }
        }
        let Some(r) = served else { return false };
        r.endpoint.close().await;
        r.task.abort();
        tracing::info!(room, "guest room down");
        true
    }

    /// The rooms served, by room id.
    pub async fn list(&self) -> Vec<RoomInfo> {
        let rooms = self.rooms.lock().await;
        let mut out: Vec<RoomInfo> = rooms
            .iter()
            .filter(|(_, r)| !r.task.is_finished())
            .map(|(id, r)| r.info(id))
            .collect();
        out.sort_by(|a, b| a.room.cmp(&b.room));
        out
    }

    /// Close every endpoint, keeping the keys (shutdown).
    pub async fn close_all(&self) {
        let rooms: Vec<Room> = self.rooms.lock().await.drain().map(|(_, r)| r).collect();
        for r in rooms {
            r.endpoint.close().await;
            r.task.abort();
        }
    }

    /// Answer one control line, as [`crate::Control::handle`] does for `home`.
    pub async fn handle(&self, line: &str) -> serde_json::Value {
        handle_with(line, |request| self.run(request)).await
    }

    async fn run(
        &self,
        request: ControlRequest,
    ) -> Result<serde_json::Map<String, serde_json::Value>> {
        let reply = match request {
            ControlRequest::RoomStart { room, relay } => {
                let relay = relay
                    .as_deref()
                    .map(str::trim)
                    .filter(|r| !r.is_empty())
                    .map(|r| {
                        r.parse::<RelayUrl>()
                            .with_context(|| format!("invalid relay URL {r}"))
                    })
                    .transpose()?;
                let info = self.start(&room, relay).await?;
                serde_json::json!({
                    "endpoint_id": info.endpoint_id.to_string(),
                    "relay": info.relay.to_string(),
                })
            }
            ControlRequest::RoomStop { room } => {
                serde_json::json!({ "closed": self.stop(&room).await })
            }
            ControlRequest::RoomList => {
                let rooms: Vec<_> = self.list().await.iter().map(RoomInfo::json).collect();
                serde_json::json!({ "rooms": rooms })
            }
            _ => bail!("not supported by `mnemosyne-link rooms` (that is `home`)"),
        };
        match reply {
            serde_json::Value::Object(map) => Ok(map),
            _ => Err(anyhow!("replies are objects")),
        }
    }
}

impl ControlHandler for GuestRooms {
    async fn answer(&self, line: &str) -> serde_json::Value {
        self.handle(line).await
    }
}

/// Accept guests until the endpoint is closed: one proxy task per connection.
async fn serve_guests(endpoint: Endpoint, host: SocketAddr, room: String) {
    while let Some(incoming) = endpoint.accept().await {
        let Ok(accepting) = incoming.accept() else {
            continue;
        };
        let room = room.clone();
        tokio::spawn(async move {
            match accepting.await {
                Ok(conn) if conn.alpn() == GUEST_ALPN => proxy(conn, host, &room).await,
                Ok(conn) => conn.close(VarInt::from_u32(CLOSE_PROTOCOL.into()), b"wrong alpn"),
                Err(e) => tracing::debug!(room, "guest connection failed: {e}"),
            }
        });
    }
}

type Ws = WebSocketStream<MaybeTlsStream<TcpStream>>;
type WsSink = SplitSink<Ws, Message>;
type WsSource = SplitStream<Ws>;

/// How the guest-to-host half ended.
enum Up {
    /// The guest finished its stream (the host was sent a 1000 close).
    Finished,
    /// The guest's connection is gone.
    Lost,
    /// The host's WebSocket failed.
    HostLost,
    /// The guest broke the framing: close it with this code and reason, after sending it
    /// `{"type":"error","code":<third>}` when there is one.
    Refuse(u16, &'static str, Option<&'static str>),
}

/// How the host-to-guest half ended.
enum Down {
    /// The host closed the WebSocket with this code and reason.
    HostClosed(u16, String),
    HostLost,
    GuestLost,
    /// The host sent a message over the frame cap.
    TooLarge,
}

/// One guest: its first bi stream, proxied both ways to the room's WebSocket. Never panics on
/// what the guest sends; every ending closes both sides.
async fn proxy(conn: Connection, host: SocketAddr, room: &str) {
    let peer = conn.remote_id();
    let (mut send, mut recv) = match timeout(ACCEPT_STREAM, conn.accept_bi()).await {
        Ok(Ok(streams)) => streams,
        Ok(Err(e)) => {
            tracing::debug!(room, peer = %peer.fmt_short(), "guest left before its stream: {e}");
            return;
        }
        Err(_) => {
            close_conn(&conn, CLOSE_UNAUTHORIZED, "join_timeout");
            return;
        }
    };
    let url = format!("ws://{host}/live/{room}");
    let ws = match tokio_tungstenite::connect_async_with_config(url, None, true).await {
        Ok((ws, _)) => ws,
        Err(e) => {
            tracing::warn!(room, "loopback host not reachable: {e}");
            close_guest(&conn, &mut send, CLOSE_HOST_LOST, "host_unreachable").await;
            return;
        }
    };
    tracing::debug!(room, peer = %peer.fmt_short(), "guest joined");
    let (mut ws_tx, mut ws_rx) = ws.split();
    let ending = {
        let up = guest_to_host(&mut recv, &mut ws_tx);
        let down = host_to_guest(&mut ws_rx, &mut send);
        tokio::pin!(up, down);
        tokio::select! {
            end = &mut up => match end {
                // The host answers our 1000 with its own close: pass that on.
                Up::Finished => match timeout(DRAIN, &mut down).await {
                    Ok(Down::HostClosed(code, reason)) => Err(Down::HostClosed(code, reason)),
                    _ => Ok(Up::Finished),
                },
                end => Ok(end),
            },
            end = &mut down => Err(end),
        }
    };
    drop(ws_rx);
    match ending {
        Ok(Up::Finished) => {
            close_ws(&mut ws_tx, "").await;
            close_guest(&conn, &mut send, CLOSE_NORMAL, "").await;
        }
        Ok(Up::Lost) | Err(Down::GuestLost) => {
            close_ws(&mut ws_tx, "").await;
            close_conn(&conn, CLOSE_NORMAL, "");
        }
        Ok(Up::HostLost) | Err(Down::HostLost) => {
            close_guest(&conn, &mut send, CLOSE_HOST_LOST, "host_lost").await;
        }
        Ok(Up::Refuse(code, reason, error)) => {
            close_ws(&mut ws_tx, reason).await;
            if let Some(error) = error {
                let text = serde_json::json!({"type": "error", "code": error}).to_string();
                if let Ok(bytes) = frame::encode(frame::TAG_TEXT, text.as_bytes()) {
                    send.write_all(&bytes).await.ok();
                }
            }
            close_guest(&conn, &mut send, code, reason).await;
        }
        Err(Down::HostClosed(code, reason)) => {
            // Completes the closing handshake the host started.
            timeout(DRAIN, ws_tx.close()).await.ok();
            close_guest(&conn, &mut send, code, &reason).await;
        }
        Err(Down::TooLarge) => {
            close_ws(&mut ws_tx, "frame_too_large").await;
            close_guest(&conn, &mut send, CLOSE_TOO_LARGE, "frame_too_large").await;
        }
    }
    tracing::debug!(room, peer = %peer.fmt_short(), "guest left");
}

/// Frames from the guest to the host, until either side ends.
async fn guest_to_host(recv: &mut RecvStream, ws: &mut WsSink) -> Up {
    loop {
        let mut header = [0u8; frame::HEADER_LEN];
        match recv.read_exact(&mut header).await {
            Ok(()) => {}
            Err(ReadExactError::FinishedEarly(0)) => {
                let normal = CloseFrame {
                    code: CloseCode::Normal,
                    reason: "".into(),
                };
                ws.send(Message::Close(Some(normal))).await.ok();
                return Up::Finished;
            }
            Err(ReadExactError::FinishedEarly(_)) => {
                return Up::Refuse(CLOSE_PROTOCOL, "truncated_frame", None)
            }
            Err(ReadExactError::ReadError(_)) => return Up::Lost,
        }
        let len = match frame::body_len(header) {
            Ok(len) => len,
            Err(frame::FrameError::TooLarge(_)) => {
                return Up::Refuse(CLOSE_TOO_LARGE, "frame_too_large", Some("frame_too_large"))
            }
            Err(_) => return Up::Refuse(CLOSE_PROTOCOL, "bad_frame", None),
        };
        let mut body = vec![0u8; len];
        match recv.read_exact(&mut body).await {
            Ok(()) => {}
            Err(ReadExactError::FinishedEarly(_)) => {
                return Up::Refuse(CLOSE_PROTOCOL, "truncated_frame", None)
            }
            Err(ReadExactError::ReadError(_)) => return Up::Lost,
        }
        let message = match frame::split_body(&body) {
            Ok((frame::TAG_TEXT, payload)) => match std::str::from_utf8(payload) {
                Ok(text) => Message::text(text),
                Err(_) => return Up::Refuse(CLOSE_BAD_TEXT, "bad_text", None),
            },
            Ok((_, payload)) => Message::binary(payload.to_vec()),
            Err(_) => return Up::Refuse(CLOSE_PROTOCOL, "bad_frame", None),
        };
        if ws.send(message).await.is_err() {
            return Up::HostLost;
        }
    }
}

/// Messages from the host to the guest, until either side ends. Pings are answered by the
/// WebSocket itself while it is read.
async fn host_to_guest(ws: &mut WsSource, send: &mut SendStream) -> Down {
    while let Some(message) = ws.next().await {
        let bytes = match message {
            Ok(Message::Text(text)) => frame::encode(frame::TAG_TEXT, text.as_bytes()),
            Ok(Message::Binary(bytes)) => frame::encode(frame::TAG_BINARY, &bytes),
            Ok(Message::Close(close)) => {
                let (code, reason) = close
                    .map(|c| (u16::from(c.code), c.reason.to_string()))
                    .unwrap_or((CLOSE_NORMAL, String::new()));
                // 1005 means "no code given"; it is never sent on.
                let code = if code == 1005 { CLOSE_NORMAL } else { code };
                return Down::HostClosed(code, reason);
            }
            Ok(_) => continue,
            Err(_) => return Down::HostLost,
        };
        let Ok(bytes) = bytes else {
            return Down::TooLarge;
        };
        if send.write_all(&bytes).await.is_err() {
            return Down::GuestLost;
        }
    }
    Down::HostLost
}

/// Tell the host this guest is gone (1000), without waiting long.
async fn close_ws(ws: &mut WsSink, reason: &str) {
    let normal = CloseFrame {
        code: CloseCode::Normal,
        reason: reason.to_string().into(),
    };
    timeout(DRAIN, async {
        ws.send(Message::Close(Some(normal))).await.ok();
        ws.close().await.ok();
    })
    .await
    .ok();
}

/// Finish the guest's stream, let what was written arrive, then close the connection with
/// `code`: the guest reads every frame first and then the close.
async fn close_guest(conn: &Connection, send: &mut SendStream, code: u16, reason: &str) {
    if send.finish().is_ok() {
        timeout(DRAIN, send.stopped()).await.ok();
    }
    close_conn(conn, code, reason);
}

fn close_conn(conn: &Connection, code: u16, reason: &str) {
    conn.close(VarInt::from_u32(code.into()), reason.as_bytes());
}
