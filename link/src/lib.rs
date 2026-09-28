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
//! iroh encrypts end to end between the two endpoint keys; a relay in between only forwards
//! packets. Every stream starts with one byte naming its kind: [`TUNNEL`] or [`PAIR`].

use std::{
    net::SocketAddr,
    path::{Path, PathBuf},
    str::FromStr,
    sync::Arc,
    time::Duration,
};

use anyhow::{anyhow, bail, Context, Result};
use iroh::{
    endpoint::{presets, Connection, RecvStream, SendStream},
    Endpoint, EndpointAddr, EndpointId, RelayMode, SecretKey,
};
use serde::{Deserialize, Serialize};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    sync::Mutex,
};

pub use iroh_tickets::endpoint::EndpointTicket;

pub const ALPN: &[u8] = b"mnemosyne/link/1";
/// A stream carrying one TCP connection to the backend.
pub const TUNNEL: u8 = b'T';
/// A stream carrying one pairing request: JSON `{code, name}` in, [`PairReply`] out.
pub const PAIR: u8 = b'P';
/// How often an open connection re-checks that its device is still paired.
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

/// Bind an endpoint. With `relay` it uses n0's public relays and address lookup, so it can
/// be reached from anywhere; without, only directly on this machine (tests, debugging).
pub async fn bind(key: SecretKey, relay: bool) -> Result<Endpoint> {
    let builder = if relay {
        Endpoint::builder(presets::N0)
    } else {
        Endpoint::builder(presets::Minimal)
            .relay_mode(RelayMode::Disabled)
            .bind_addr("127.0.0.1:0")?
    };
    Ok(builder
        .secret_key(key)
        .alpns(vec![ALPN.to_vec()])
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
}

/// The home side's answer to a pairing request.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PairReply {
    pub ok: bool,
    /// The device's own API token (on success).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub token: Option<String>,
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
            let tcp = TcpStream::connect(home.backend)
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
    let body =
        serde_json::json!({"code": req.code, "name": req.name, "endpoint_id": peer.to_string()});
    match post_json(backend, "/api/pairing/redeem", &body).await {
        Ok((200, reply)) => PairReply {
            ok: true,
            token: reply
                .get("token")
                .and_then(|t| t.as_str())
                .map(String::from),
            error: None,
        },
        Ok((_, reply)) => PairReply {
            ok: false,
            token: None,
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
    let mut tcp = TcpStream::connect(addr)
        .await
        .context("backend not reachable")?;
    let head = format!(
        "POST {path} HTTP/1.1\r\nHost: {addr}\r\nContent-Type: application/json\r\n\
         Content-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    );
    tcp.write_all(head.as_bytes()).await?;
    tcp.write_all(&body).await?;
    let mut raw = Vec::new();
    tcp.read_to_end(&mut raw).await?;
    let split = raw
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .ok_or_else(|| anyhow!("malformed response"))?;
    let status_line = std::str::from_utf8(&raw[..split])?
        .lines()
        .next()
        .unwrap_or_default()
        .to_string();
    let status: u16 = status_line
        .split_whitespace()
        .nth(1)
        .and_then(|s| s.parse().ok())
        .ok_or_else(|| anyhow!("malformed status line: {status_line}"))?;
    let json = serde_json::from_slice(&raw[split + 4..]).unwrap_or(serde_json::Value::Null);
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
}

impl Remote {
    pub fn new(endpoint: Endpoint, home: impl Into<EndpointAddr>) -> Self {
        Remote {
            endpoint,
            home: home.into(),
            conn: Mutex::new(None),
        }
    }

    pub fn endpoint(&self) -> &Endpoint {
        &self.endpoint
    }

    async fn connection(&self) -> Result<Connection> {
        let mut conn = self.conn.lock().await;
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
        let (mut send, mut recv) = self.connection().await?.open_bi().await?;
        send.write_all(&[PAIR]).await?;
        send.write_all(&serde_json::to_vec(&PairRequest {
            code: code.to_string(),
            name: name.to_string(),
        })?)
        .await?;
        send.finish()?;
        let reply: PairReply = serde_json::from_slice(&recv.read_to_end(64 * 1024).await?)?;
        match (reply.ok, reply.token) {
            (true, Some(token)) => Ok(token),
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
