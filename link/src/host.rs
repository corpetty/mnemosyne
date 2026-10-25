//! The publish host, embedded: guest-notes rooms served on loopback, for this sidecar to
//! proxy guests to. Every guest arrives from 127.0.0.1, so the per-address rate limits are
//! off, and only whoever knows the upload token (the backend) can create a room.

use std::net::SocketAddr;
use std::path::PathBuf;

use anyhow::{anyhow, Context, Result};
use mnemosyne_publish_host::app::{router, spawn_sweeper, App, Shared};
use mnemosyne_publish_host::clock::Clock;
use mnemosyne_publish_host::config::Config;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};

/// What the embedded host needs to know.
#[derive(Debug, Clone)]
pub struct HostOptions {
    /// The backend's data directory; the host stores under `<data_dir>/guest_rooms/host`.
    pub data_dir: PathBuf,
    /// The built guest page (`/n/<room>`).
    pub guest_dir: PathBuf,
    /// The built publication viewer (`/p/<id>`).
    pub viewer_dir: PathBuf,
    /// Required to create a room (`X-Upload-Token`): only the backend knows it.
    pub upload_token: String,
    /// Extra relay hosts the guest page's CSP allows.
    pub relay_origins: Vec<String>,
}

/// Starts the host on `127.0.0.1:0` and returns where it listens. The server and the sweeper
/// run as tasks for as long as the runtime lives.
pub async fn start_loopback_host(opts: HostOptions) -> Result<(SocketAddr, Shared)> {
    // Parsed from the (empty) command line, but clap also reads PUBLISH_* variables: every
    // field that matters here is set below.
    let mut cfg = Config::defaults();
    cfg.data_dir = opts.data_dir.join("guest_rooms").join("host");
    cfg.guest_dir = opts.guest_dir;
    cfg.viewer_dir = opts.viewer_dir;
    cfg.upload_token = Some(opts.upload_token);
    cfg.trust_proxy = false;
    // 0 per minute turns a limiter off (publish-host limiter.rs).
    cfg.read_per_minute = 0;
    cfg.write_per_minute = 0;
    cfg.relay_origins = opts.relay_origins;

    let app = App::new(cfg, Clock::system())
        .await
        .context("opening the room store")?;
    app.sweep().await;
    spawn_sweeper(app.clone());

    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .context("binding the loopback host")?;
    let addr = listener.local_addr()?;
    let service = router(app.clone()).into_make_service_with_connect_info::<SocketAddr>();
    tokio::spawn(async move {
        if let Err(e) = axum::serve(listener, service).await {
            tracing::error!("loopback host stopped: {e}");
        }
    });
    Ok((addr, app))
}

/// A minimal HTTP/1.1 request to `addr` from 127.0.0.1 (this machine's own call, unlike
/// [`crate::connect_backend`], which comes from the tunnel source address). Returns the
/// status and the body.
pub async fn request(
    addr: SocketAddr,
    method: &str,
    path: &str,
    headers: &[(&str, &str)],
    body: &[u8],
) -> Result<(u16, Vec<u8>)> {
    let tcp = TcpStream::connect(addr)
        .await
        .with_context(|| format!("{addr} not reachable"))?;
    exchange(tcp, addr, method, path, headers, body).await
}

/// One request and its whole response over an open connection (`Connection: close`).
pub(crate) async fn exchange(
    mut tcp: TcpStream,
    addr: SocketAddr,
    method: &str,
    path: &str,
    headers: &[(&str, &str)],
    body: &[u8],
) -> Result<(u16, Vec<u8>)> {
    let mut head = format!("{method} {path} HTTP/1.1\r\nHost: {addr}\r\n");
    for (k, v) in headers {
        head.push_str(&format!("{k}: {v}\r\n"));
    }
    head.push_str(&format!(
        "Content-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    ));
    tcp.write_all(head.as_bytes()).await?;
    tcp.write_all(body).await?;
    let mut raw = Vec::new();
    tcp.read_to_end(&mut raw).await?;
    let split = raw
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .ok_or_else(|| anyhow!("malformed response"))?;
    let head = std::str::from_utf8(&raw[..split])?;
    let status_line = head.lines().next().unwrap_or_default();
    let status: u16 = status_line
        .split_whitespace()
        .nth(1)
        .and_then(|s| s.parse().ok())
        .ok_or_else(|| anyhow!("malformed status line: {status_line}"))?;
    let chunked = head.lines().skip(1).any(|l| {
        l.split_once(':').is_some_and(|(k, v)| {
            k.trim().eq_ignore_ascii_case("transfer-encoding")
                && v.to_ascii_lowercase().contains("chunked")
        })
    });
    let rest = &raw[split + 4..];
    let body = if chunked {
        dechunk(rest)?
    } else {
        rest.to_vec()
    };
    Ok((status, body))
}

/// Decodes a chunked body (chunk extensions and trailers ignored).
fn dechunk(mut rest: &[u8]) -> Result<Vec<u8>> {
    let mut out = Vec::new();
    loop {
        let eol = rest
            .windows(2)
            .position(|w| w == b"\r\n")
            .ok_or_else(|| anyhow!("malformed chunked body"))?;
        let size_line = std::str::from_utf8(&rest[..eol])?;
        let size_hex = size_line.split(';').next().unwrap_or("").trim();
        let size = usize::from_str_radix(size_hex, 16)
            .map_err(|_| anyhow!("malformed chunk size: {size_line}"))?;
        rest = &rest[eol + 2..];
        if size == 0 {
            return Ok(out);
        }
        if rest.len() < size + 2 {
            return Err(anyhow!("truncated chunked body"));
        }
        out.extend_from_slice(&rest[..size]);
        rest = &rest[size + 2..];
    }
}
