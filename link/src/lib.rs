//! Remote access to a Mnemosyne backend over iroh.
//!
//! The home side ([`serve_home`]) accepts iroh connections and turns each bidirectional stream
//! from a paired device into a TCP connection to the backend on the same machine, so the
//! backend and its token auth see ordinary local HTTP. A device that is not paired yet may only
//! open a pairing stream: it redeems a one-time code with the backend (`POST
//! /api/pairing/redeem`), which binds the device's endpoint id to a new paired device. The
//! client side ([`Remote`], [`serve_local`]) listens on a local port and carries each TCP
//! connection over one iroh connection to home.
//!
//! For sync between two desktops, a running home also dials its peers from the same endpoint
//! ([`Control`], driven by JSON lines on the sidecar's stdin through [`serve_control`]).
//!
//! iroh encrypts end to end between the two endpoint keys; a relay in between only forwards
//! packets. Every stream starts with one byte naming its kind: [`TUNNEL`] or [`PAIR`].

pub mod guest;
pub mod host;
pub mod notes;

use std::{
    collections::HashMap,
    net::SocketAddr,
    path::{Path, PathBuf},
    str::FromStr,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::Duration,
};

use anyhow::{anyhow, bail, Context, Result};
use iroh::{
    endpoint::{presets, Connection, RecvStream, SendStream},
    Endpoint, EndpointAddr, EndpointId, RelayMode, RelayUrl, SecretKey,
};
use serde::{Deserialize, Serialize};
use tokio::{
    io::{AsyncWrite, AsyncWriteExt},
    net::{TcpListener, TcpSocket, TcpStream},
    sync::{mpsc, Mutex},
    task::JoinHandle,
};

pub use iroh_tickets::endpoint::EndpointTicket;

pub const ALPN: &[u8] = b"mnemosyne/link/1";
/// A stream carrying one TCP connection to the backend.
pub const TUNNEL: u8 = b'T';
/// A stream carrying one pairing request: JSON `{code, name, peer?, card?}` in, [`PairReply`] out.
pub const PAIR: u8 = b'P';
/// How often an open connection re-checks that its device is still paired.
/// The source address of every connection made to the backend for a remote peer. The backend
/// tells these from the desktop app's own requests (127.0.0.1) by it. If it cannot be bound (not
/// Linux, or the loopback /8 is not routed) the connection comes from 127.0.0.1 and the backend
/// cannot tell the two apart: a warning is logged once.
pub const TUNNEL_SOURCE: &str = "127.0.0.2";

/// Connect to the backend on behalf of a remote peer, from `TUNNEL_SOURCE`.
async fn connect_backend(backend: SocketAddr) -> std::io::Result<TcpStream> {
    static WARNED: std::sync::Once = std::sync::Once::new();
    if backend.is_ipv4() {
        let attempt = async {
            let socket = TcpSocket::new_v4()?;
            socket.bind(SocketAddr::new(TUNNEL_SOURCE.parse().unwrap(), 0))?;
            socket.connect(backend).await
        };
        match attempt.await {
            Ok(tcp) => return Ok(tcp),
            Err(e)
                if e.kind() == std::io::ErrorKind::AddrNotAvailable
                    || e.kind() == std::io::ErrorKind::PermissionDenied
                    || e.kind() == std::io::ErrorKind::InvalidInput =>
            {
                WARNED.call_once(|| {
                    tracing::warn!(
                        "cannot connect from {TUNNEL_SOURCE} ({e}); tunnelled requests will \
                         look like this machine's own to the backend"
                    )
                });
            }
            Err(e) => return Err(e),
        }
    }
    TcpStream::connect(backend).await
}

const RECHECK: Duration = Duration::from_secs(5);

/// Load the endpoint's secret key from `path` (hex), creating it (mode 0600) if missing.
pub fn load_or_create_key(path: &Path) -> Result<SecretKey> {
    if let Ok(text) = std::fs::read_to_string(path) {
        let bytes = decode_hex(text.trim()).context("invalid key file")?;
        let bytes: [u8; 32] = bytes
            .try_into()
            .map_err(|_| anyhow!("key must be 32 bytes"))?;
        return Ok(SecretKey::from_bytes(&bytes));
    }
    let key = SecretKey::generate();
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let tmp = path.with_extension("tmp");
    std::fs::write(&tmp, encode_hex(&key.to_bytes()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o600))?;
    }
    std::fs::rename(&tmp, path)?;
    Ok(key)
}

fn encode_hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn decode_hex(s: &str) -> Result<Vec<u8>> {
    if !s.len().is_multiple_of(2) {
        bail!("odd length");
    }
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).map_err(Into::into))
        .collect()
}

/// Which relays an endpoint uses to be reachable and to reach others.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Relays {
    /// None: only direct connections on this machine (tests, debugging).
    Off,
    /// n0's public relays, with n0's address lookup.
    N0,
    /// Relays we or the user run (iroh-relay); no n0 service is used at all.
    Custom(Vec<RelayUrl>),
}

impl Relays {
    /// From a list of relay URLs; an empty list means n0's public relays.
    pub fn from_urls<S: AsRef<str>>(urls: &[S]) -> Result<Self> {
        let urls = urls
            .iter()
            .map(|u| u.as_ref().trim())
            .filter(|u| !u.is_empty())
            .map(|u| u.parse().with_context(|| format!("invalid relay URL {u}")))
            .collect::<Result<Vec<RelayUrl>>>()?;
        Ok(if urls.is_empty() {
            Relays::N0
        } else {
            Relays::Custom(urls)
        })
    }

    /// What a device uses to reach the home in `ticket`: the home's own relays, unless they
    /// are n0's (then n0's, with address lookup, in case home moves to another n0 relay).
    pub fn for_ticket(ticket: &EndpointTicket) -> Self {
        let urls: Vec<RelayUrl> = ticket.endpoint_addr().relay_urls().cloned().collect();
        if urls.is_empty() || urls.iter().any(is_n0) {
            Relays::N0
        } else {
            Relays::Custom(urls)
        }
    }
}

fn is_n0(url: &RelayUrl) -> bool {
    url.host_str()
        .is_some_and(|h| h.trim_end_matches('.').ends_with(".iroh.link"))
}

/// Local first: how an endpoint uses the local network (mDNS) to find the other side without
/// any relay or lookup service, so machines on the same network connect directly.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Lan {
    /// Not at all (tests that must stay isolated).
    Off,
    /// Look for home on the local network, without announcing this device (a laptop on
    /// someone else's Wi-Fi need not tell it anything).
    Discover,
    /// Announce this endpoint on the local network too (home). The record carries only the
    /// endpoint ID and its addresses, under iroh's generic service name.
    Advertise,
}

/// Bind an endpoint using `relays` that looks for others on the local network.
pub async fn bind(key: SecretKey, relays: &Relays) -> Result<Endpoint> {
    bind_with(key, relays, Lan::Discover, |b| b).await
}

/// [`bind`] with a choice of [`Lan`] and a last adjustment to the builder (tests trust a local
/// relay's certificate).
pub async fn bind_with(
    key: SecretKey,
    relays: &Relays,
    lan: Lan,
    adjust: impl FnOnce(iroh::endpoint::Builder) -> iroh::endpoint::Builder,
) -> Result<Endpoint> {
    let mut builder = match relays {
        Relays::N0 => Endpoint::builder(presets::N0),
        Relays::Custom(urls) => Endpoint::builder(presets::Minimal)
            .relay_mode(RelayMode::Custom(urls.iter().cloned().collect())),
        Relays::Off => Endpoint::builder(presets::Minimal)
            .relay_mode(RelayMode::Disabled)
            .bind_addr("127.0.0.1:0")?,
    };
    if lan != Lan::Off {
        builder = builder.address_lookup(
            iroh_mdns_address_lookup::MdnsAddressLookup::builder().advertise(lan == Lan::Advertise),
        );
    }
    Ok(adjust(builder.secret_key(key).alpns(vec![ALPN.to_vec()]))
        .bind()
        .await?)
}

/// What a device needs to pair: how to reach home, and the one-time code.
/// Written as `<endpoint ticket>#<code>`.
#[derive(Debug, Clone)]
pub struct Invite {
    pub ticket: EndpointTicket,
    pub code: String,
}

impl std::fmt::Display for Invite {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}#{}", self.ticket, self.code)
    }
}

impl FromStr for Invite {
    type Err = anyhow::Error;
    fn from_str(s: &str) -> Result<Self> {
        let (ticket, code) = s
            .trim()
            .rsplit_once('#')
            .ok_or_else(|| anyhow!("not a pairing invite: expected <ticket>#<code>"))?;
        Ok(Invite {
            ticket: ticket.parse().context("invalid ticket in the invite")?,
            code: code.to_string(),
        })
    }
}

#[derive(Debug, Serialize, Deserialize)]
struct PairRequest {
    code: String,
    name: String,
    /// Sync pairing: how home reaches this machine back. Passed to the backend unchanged.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    peer: Option<PeerInfo>,
    /// An opaque signed JSON card, passed to the backend unchanged.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    card: Option<serde_json::Value>,
}

/// What a machine pairing for sync tells home about itself, so home can dial it back: its
/// sync id and name, its endpoint ticket and the API token home should use on it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PeerInfo {
    /// The machine's library sync id (its backend's `sync_peers.json` self id).
    #[serde(default)]
    pub id: String,
    pub name: String,
    pub ticket: String,
    pub token: String,
}

/// The home side's answer to a pairing request.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PairReply {
    pub ok: bool,
    /// The device's own API token (on success).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub token: Option<String>,
    /// For a sync pairing: home's own library sync `{id, name}`, passed through from its
    /// backend's redeem reply.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sync: Option<serde_json::Value>,
    /// An opaque card from home's backend redeem reply, passed through unchanged.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub card: Option<serde_json::Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

// ---------------------------------------------------------------------------------------------
// Home side
// ---------------------------------------------------------------------------------------------

/// Where the home side forwards to, and where it learns which devices are paired.
#[derive(Debug, Clone)]
pub struct Home {
    /// The backend's HTTP address (normally 127.0.0.1:8008).
    pub backend: SocketAddr,
    /// The backend's `paired_devices.json` (services/pairing.py).
    pub devices_file: PathBuf,
}

#[derive(Deserialize)]
struct DevicesFile {
    #[serde(default)]
    devices: Vec<DeviceEntry>,
}

#[derive(Deserialize)]
struct DeviceEntry {
    #[serde(default)]
    endpoint_id: Option<String>,
}

impl Home {
    /// Whether `peer` belongs to a paired device. Read fresh each time, so removing a device
    /// in Settings takes effect without restarting anything.
    pub fn is_paired(&self, peer: &EndpointId) -> bool {
        let Ok(text) = std::fs::read_to_string(&self.devices_file) else {
            return false;
        };
        let Ok(file) = serde_json::from_str::<DevicesFile>(&text) else {
            return false;
        };
        let peer = peer.to_string();
        file.devices
            .iter()
            .any(|d| d.endpoint_id.as_deref() == Some(peer.as_str()))
    }
}

/// Accept connections until the endpoint is closed.
pub async fn serve_home(endpoint: Endpoint, home: Home) -> Result<()> {
    let home = Arc::new(home);
    while let Some(incoming) = endpoint.accept().await {
        let Ok(accepting) = incoming.accept() else {
            continue;
        };
        let home = home.clone();
        tokio::spawn(async move {
            match accepting.await {
                Ok(conn) => serve_connection(conn, home).await,
                Err(e) => tracing::debug!("connection failed: {e}"),
            }
        });
    }
    Ok(())
}

async fn serve_connection(conn: Connection, home: Arc<Home>) {
    let peer = conn.remote_id();
    tracing::info!(peer = %peer.fmt_short(), paired = home.is_paired(&peer), "connection");
    // Close the connection once its device is removed, or after a minute if it never pairs.
    let watch = {
        let (conn, home) = (conn.clone(), home.clone());
        tokio::spawn(async move {
            let mut grace = 12; // an unpaired device gets a minute to pair
            loop {
                tokio::time::sleep(RECHECK).await;
                if home.is_paired(&peer) {
                    grace = 0;
                } else if grace == 0 {
                    conn.close(1u32.into(), b"not paired");
                    return;
                } else {
                    grace -= 1;
                }
            }
        })
    };
    while let Ok((send, recv)) = conn.accept_bi().await {
        let home = home.clone();
        tokio::spawn(async move {
            if let Err(e) = serve_stream(peer, send, recv, &home).await {
                tracing::debug!(peer = %peer.fmt_short(), "stream ended: {e:#}");
            }
        });
    }
    watch.abort();
}

async fn serve_stream(
    peer: EndpointId,
    mut send: SendStream,
    mut recv: RecvStream,
    home: &Home,
) -> Result<()> {
    let mut kind = [0u8; 1];
    recv.read_exact(&mut kind).await?;
    match kind[0] {
        TUNNEL if home.is_paired(&peer) => {
            let tcp = connect_backend(home.backend)
                .await
                .with_context(|| format!("backend at {} is not reachable", home.backend))?;
            forward(tcp, send, recv).await
        }
        TUNNEL => {
            send.reset(1u32.into()).ok();
            bail!("tunnel from a device that is not paired")
        }
        PAIR => {
            let body = recv.read_to_end(4096).await?;
            let reply = match serde_json::from_slice::<PairRequest>(&body) {
                Ok(req) => redeem(home.backend, &req, &peer).await,
                Err(e) => PairReply {
                    ok: false,
                    token: None,
                    sync: None,
                    card: None,
                    error: Some(format!("bad request: {e}")),
                },
            };
            send.write_all(&serde_json::to_vec(&reply)?).await?;
            send.finish()?;
            // Let the reply reach the device before the stream is dropped.
            send.stopped().await.ok();
            Ok(())
        }
        other => bail!("unknown stream kind {other}"),
    }
}

/// Ask the backend to redeem a pairing code for `peer`.
async fn redeem(backend: SocketAddr, req: &PairRequest, peer: &EndpointId) -> PairReply {
    let mut body =
        serde_json::json!({"code": req.code, "name": req.name, "endpoint_id": peer.to_string()});
    if let Some(info) = &req.peer {
        body["peer"] = serde_json::json!(info);
    }
    if let Some(card) = &req.card {
        body["card"] = card.clone();
    }
    match post_json(backend, "/api/pairing/redeem", &body).await {
        Ok((200, reply)) => PairReply {
            ok: true,
            token: reply
                .get("token")
                .and_then(|t| t.as_str())
                .map(String::from),
            sync: reply.get("sync").filter(|v| !v.is_null()).cloned(),
            card: reply.get("card").filter(|v| !v.is_null()).cloned(),
            error: None,
        },
        Ok((_, reply)) => PairReply {
            ok: false,
            token: None,
            sync: None,
            card: None,
            error: Some(
                reply
                    .get("detail")
                    .and_then(|d| d.as_str())
                    .unwrap_or("pairing refused")
                    .to_string(),
            ),
        },
        Err(e) => PairReply {
            ok: false,
            token: None,
            sync: None,
            card: None,
            error: Some(format!("{e:#}")),
        },
    }
}

/// A minimal HTTP/1.1 POST to the local backend (it answers with a Content-Length body).
async fn post_json(
    addr: SocketAddr,
    path: &str,
    body: &serde_json::Value,
) -> Result<(u16, serde_json::Value)> {
    let body = serde_json::to_vec(body)?;
    let tcp = connect_backend(addr)
        .await
        .context("backend not reachable")?;
    let (status, raw) = host::exchange(
        tcp,
        addr,
        "POST",
        path,
        &[("Content-Type", "application/json")],
        &body,
    )
    .await?;
    let json = serde_json::from_slice(&raw).unwrap_or(serde_json::Value::Null);
    Ok((status, json))
}

/// Copy bytes both ways between a TCP connection and a QUIC stream until both sides finish.
async fn forward(tcp: TcpStream, mut send: SendStream, mut recv: RecvStream) -> Result<()> {
    let (mut tcp_read, mut tcp_write) = tcp.into_split();
    let up = async {
        tokio::io::copy(&mut tcp_read, &mut send).await?;
        send.finish()?;
        anyhow::Ok(())
    };
    let down = async {
        tokio::io::copy(&mut recv, &mut tcp_write).await?;
        tcp_write.shutdown().await?;
        anyhow::Ok(())
    };
    tokio::try_join!(up, down)?;
    Ok(())
}

// ---------------------------------------------------------------------------------------------
// Client side
// ---------------------------------------------------------------------------------------------

/// A device's view of home: one iroh connection, reopened when it drops.
pub struct Remote {
    endpoint: Endpoint,
    home: EndpointAddr,
    conn: Mutex<Option<Connection>>,
    closed: AtomicBool,
}

impl Remote {
    pub fn new(endpoint: Endpoint, home: impl Into<EndpointAddr>) -> Self {
        Remote {
            endpoint,
            home: home.into(),
            conn: Mutex::new(None),
            closed: AtomicBool::new(false),
        }
    }

    /// Close the connection to home (cutting its tunnels) and never open another.
    pub async fn close(&self) {
        self.closed.store(true, Ordering::SeqCst);
        if let Some(c) = self.conn.lock().await.take() {
            c.close(0u32.into(), b"hangup");
        }
    }

    pub fn endpoint(&self) -> &Endpoint {
        &self.endpoint
    }

    async fn connection(&self) -> Result<Connection> {
        let mut conn = self.conn.lock().await;
        if self.closed.load(Ordering::SeqCst) {
            bail!("hung up");
        }
        if let Some(c) = conn.as_ref() {
            if c.close_reason().is_none() {
                return Ok(c.clone());
            }
        }
        let c = self
            .endpoint
            .connect(self.home.clone(), ALPN)
            .await
            .context("cannot reach home")?;
        *conn = Some(c.clone());
        Ok(c)
    }

    /// Redeem a pairing code: home remembers this device's key and returns its API token.
    pub async fn pair(&self, code: &str, name: &str) -> Result<String> {
        Ok(self.pair_with_peer(code, name, None, None).await?.0)
    }

    /// [`Remote::pair`] for sync: `peer` tells home how to reach this machine back.
    pub async fn pair_with_peer(
        &self,
        code: &str,
        name: &str,
        peer: Option<PeerInfo>,
        card: Option<serde_json::Value>,
    ) -> Result<(String, Option<serde_json::Value>, Option<serde_json::Value>)> {
        let (mut send, mut recv) = self.connection().await?.open_bi().await?;
        send.write_all(&[PAIR]).await?;
        send.write_all(&serde_json::to_vec(&PairRequest {
            code: code.to_string(),
            name: name.to_string(),
            peer,
            card,
        })?)
        .await?;
        send.finish()?;
        let reply: PairReply = serde_json::from_slice(&recv.read_to_end(64 * 1024).await?)?;
        match (reply.ok, reply.token.clone()) {
            (true, Some(token)) => Ok((token, reply.sync, reply.card)),
            _ => bail!(reply.error.unwrap_or_else(|| "pairing refused".into())),
        }
    }

    async fn open_tunnel(&self) -> Result<(SendStream, RecvStream)> {
        let (mut send, recv) = self.connection().await?.open_bi().await?;
        send.write_all(&[TUNNEL]).await?;
        Ok((send, recv))
    }
}

/// Carry every TCP connection accepted on `listener` to the backend at home.
pub async fn serve_local(remote: Arc<Remote>, listener: TcpListener) -> Result<()> {
    loop {
        let (tcp, _) = listener.accept().await?;
        let remote = remote.clone();
        tokio::spawn(async move {
            let result = match remote.open_tunnel().await {
                Ok((send, recv)) => forward(tcp, send, recv).await,
                Err(e) => Err(e),
            };
            if let Err(e) = result {
                tracing::debug!("tunnel ended: {e:#}");
            }
        });
    }
}

// ---------------------------------------------------------------------------------------------
// Control channel (sync peers)
// ---------------------------------------------------------------------------------------------

/// Commands the backend sends a running `home` over its stdin: dial sync peers from home's own
/// endpoint (the same key, so peers see this machine's paired endpoint id), hang up, and pair
/// with another machine. One JSON object per line, answered by one JSON line (see the README).
pub struct Control {
    endpoint: Endpoint,
    dials: Mutex<HashMap<EndpointId, Dial>>,
}

struct Dial {
    listen: SocketAddr,
    remote: Arc<Remote>,
    task: JoinHandle<()>,
}

/// One control line, by its `op` (see the README). `home` serves dial, hangup and pair;
/// `rooms` serves room_start, room_stop and room_list; each answers the others with an error.
#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "lowercase")]
pub enum ControlRequest {
    Dial {
        ticket: String,
    },
    Hangup {
        peer: String,
    },
    Pair {
        invite: String,
        name: String,
        #[serde(default)]
        peer: Option<PeerInfo>,
        #[serde(default)]
        card: Option<serde_json::Value>,
    },
    /// Serve a guest-notes room over iroh (`relay`: the one stored from an earlier start).
    #[serde(rename = "room_start")]
    RoomStart {
        room: String,
        #[serde(default)]
        relay: Option<String>,
    },
    #[serde(rename = "room_stop")]
    RoomStop {
        room: String,
    },
    #[serde(rename = "room_list")]
    RoomList,
}

/// What [`serve_control`] answers lines with: [`Control`] for `home`,
/// [`guest::GuestRooms`] for `rooms`.
pub trait ControlHandler: Send + Sync + 'static {
    /// One JSON reply for one line; never fails (errors are `{"ok": false, "error"}`).
    fn answer(&self, line: &str) -> impl std::future::Future<Output = serde_json::Value> + Send;
}

impl ControlHandler for Control {
    async fn answer(&self, line: &str) -> serde_json::Value {
        self.handle(line).await
    }
}

/// Parse `line`, run it with `run` and build the reply: it echoes the request's `id` and `op`
/// (null when the line has none), with `ok: true` and the fields, or `ok: false` and `error`.
pub(crate) async fn handle_with<F, Fut>(line: &str, run: F) -> serde_json::Value
where
    F: FnOnce(ControlRequest) -> Fut,
    Fut: std::future::Future<Output = Result<serde_json::Map<String, serde_json::Value>>>,
{
    let (id, op, result) = match serde_json::from_str::<serde_json::Value>(line) {
        Err(e) => (
            serde_json::Value::Null,
            serde_json::Value::Null,
            Err(anyhow!("invalid JSON: {e}")),
        ),
        Ok(request) => {
            let id = request.get("id").cloned().unwrap_or_default();
            let op = request.get("op").cloned().unwrap_or_default();
            let result = match serde_json::from_value::<ControlRequest>(request) {
                Ok(request) => run(request).await,
                Err(e) => Err(anyhow!("bad request: {e}")),
            };
            (id, op, result)
        }
    };
    let mut reply = serde_json::Map::new();
    reply.insert("id".into(), id);
    reply.insert("op".into(), op);
    match result {
        Ok(fields) => {
            reply.insert("ok".into(), true.into());
            reply.extend(fields);
        }
        Err(e) => {
            reply.insert("ok".into(), false.into());
            reply.insert("error".into(), format!("{e:#}").into());
        }
    }
    serde_json::Value::Object(reply)
}

impl Control {
    pub fn new(endpoint: Endpoint) -> Self {
        Control {
            endpoint,
            dials: Mutex::new(HashMap::new()),
        }
    }

    /// Serve the peer in `ticket` on a loopback port: every TCP connection accepted there is
    /// carried to that peer's backend. Dialing a peer again returns the listener it has. The
    /// endpoint's relays stay home's own; the peer's relay and addresses come from the ticket.
    pub async fn dial(&self, ticket: EndpointTicket) -> Result<(EndpointId, SocketAddr)> {
        let peer = ticket.endpoint_addr().id;
        if peer == self.endpoint.id() {
            bail!("that ticket is this machine's own");
        }
        let mut dials = self.dials.lock().await;
        if let Some(dial) = dials.get(&peer) {
            if !dial.task.is_finished() {
                return Ok((peer, dial.listen));
            }
        }
        let listener = TcpListener::bind("127.0.0.1:0").await?;
        let listen = listener.local_addr()?;
        let remote = Arc::new(Remote::new(self.endpoint.clone(), ticket));
        let task = tokio::spawn({
            let remote = remote.clone();
            async move {
                if let Err(e) = serve_local(remote, listener).await {
                    tracing::warn!(peer = %peer.fmt_short(), "listener ended: {e:#}");
                }
            }
        });
        tracing::info!(peer = %peer.fmt_short(), %listen, "dialing");
        dials.insert(
            peer,
            Dial {
                listen,
                remote,
                task,
            },
        );
        Ok((peer, listen))
    }

    /// Stop serving `peer`: its listener closes and its open tunnels are cut. Returns whether
    /// it was dialed.
    pub async fn hangup(&self, peer: &EndpointId) -> bool {
        let Some(dial) = self.dials.lock().await.remove(peer) else {
            return false;
        };
        dial.task.abort();
        // Wait for the listener to be dropped, so the port is closed once we answer.
        dial.task.await.ok();
        dial.remote.close().await;
        tracing::info!(peer = %peer.fmt_short(), "hung up");
        true
    }

    /// Hang up on every peer (shutdown).
    pub async fn close_all(&self) {
        let peers: Vec<EndpointId> = self.dials.lock().await.keys().copied().collect();
        for peer in peers {
            self.hangup(&peer).await;
        }
    }

    /// Pair with the home in `invite` from this endpoint, as [`Remote::pair_with_peer`].
    /// Returns the token, home's library sync `{id, name}` and the card (if its backend sent them).
    pub async fn pair(
        &self,
        invite: &Invite,
        name: &str,
        peer: Option<PeerInfo>,
        card: Option<serde_json::Value>,
    ) -> Result<(String, Option<serde_json::Value>, Option<serde_json::Value>)> {
        let remote = Remote::new(self.endpoint.clone(), invite.ticket.clone());
        let result = remote.pair_with_peer(&invite.code, name, peer, card).await;
        remote.close().await;
        result
    }

    /// Answer one control line. Never fails: errors become `{"ok": false, "error": ...}`. The
    /// reply echoes the request's `id` and `op` (null when the line has none).
    pub async fn handle(&self, line: &str) -> serde_json::Value {
        handle_with(line, |request| self.run(request)).await
    }

    async fn run(
        &self,
        request: ControlRequest,
    ) -> Result<serde_json::Map<String, serde_json::Value>> {
        let reply = match request {
            ControlRequest::Dial { ticket } => {
                let ticket: EndpointTicket = ticket.trim().parse().context("invalid ticket")?;
                let (peer, listen) = self.dial(ticket).await?;
                serde_json::json!({"peer": peer.to_string(), "listen": listen.to_string()})
            }
            ControlRequest::Hangup { peer } => {
                let peer: EndpointId = peer.trim().parse().context("invalid endpoint id")?;
                serde_json::json!({"peer": peer.to_string(), "closed": self.hangup(&peer).await})
            }
            ControlRequest::Pair {
                invite,
                name,
                peer,
                card,
            } => {
                let invite: Invite = invite.parse()?;
                let (token, sync, card) = self.pair(&invite, &name, peer, card).await?;
                serde_json::json!({
                    "token": token,
                    "sync": sync,
                    "card": card,
                    "ticket": invite.ticket.to_string(),
                    "peer": invite.ticket.endpoint_addr().id.to_string(),
                })
            }
            ControlRequest::RoomStart { .. }
            | ControlRequest::RoomStop { .. }
            | ControlRequest::RoomList => {
                bail!("not supported by `mnemosyne-link home` (guest rooms are `rooms`)")
            }
        };
        match reply {
            serde_json::Value::Object(map) => Ok(map),
            _ => unreachable!("replies are objects"),
        }
    }
}

/// Answer every line from `lines` on `out`, one JSON line each. Requests run concurrently (a
/// slow pairing does not hold up a dial), so replies may come out of order: match them by
/// `id`. Returns once `lines` ends and every reply is written; what was dialed keeps running.
pub async fn serve_control<H, W>(control: Arc<H>, mut lines: mpsc::Receiver<String>, mut out: W)
where
    H: ControlHandler,
    W: AsyncWrite + Unpin + Send + 'static,
{
    let (tx, mut rx) = mpsc::unbounded_channel::<serde_json::Value>();
    let writer = tokio::spawn(async move {
        while let Some(reply) = rx.recv().await {
            let mut line = reply.to_string();
            line.push('\n');
            if out.write_all(line.as_bytes()).await.is_err() || out.flush().await.is_err() {
                break;
            }
        }
    });
    while let Some(line) = lines.recv().await {
        if line.trim().is_empty() {
            continue;
        }
        let (control, tx) = (control.clone(), tx.clone());
        tokio::spawn(async move {
            tx.send(control.answer(&line).await).ok();
        });
    }
    drop(tx);
    writer.await.ok();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn relay_choice() {
        assert_eq!(Relays::from_urls::<&str>(&[]).unwrap(), Relays::N0);
        assert_eq!(Relays::from_urls(&[" ", ""]).unwrap(), Relays::N0);
        let own = Relays::from_urls(&["https://relay.example.org"]).unwrap();
        assert!(matches!(own, Relays::Custom(ref u) if u.len() == 1));
        assert!(Relays::from_urls(&["not a url"]).is_err());

        let id = SecretKey::generate().public();
        let ticket = |url: &str| {
            EndpointTicket::new(EndpointAddr::new(id).with_relay_url(url.parse().unwrap()))
        };
        // A home on n0's relays: devices use n0 too, with its address lookup.
        assert_eq!(
            Relays::for_ticket(&ticket("https://euc1-1.relay.n0.iroh.link.")),
            Relays::N0
        );
        // A home on its own relay: devices use exactly that relay.
        assert_eq!(
            Relays::for_ticket(&ticket("https://relay.example.org")),
            Relays::Custom(vec!["https://relay.example.org".parse().unwrap()])
        );
        assert_eq!(
            Relays::for_ticket(&EndpointTicket::new(EndpointAddr::new(id))),
            Relays::N0
        );
    }
}
