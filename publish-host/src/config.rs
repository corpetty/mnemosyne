//! Flags, each with an environment fallback (`PUBLISH_*`), so a container needs no arguments.

use std::net::SocketAddr;
use std::path::PathBuf;

use clap::Parser;

const MIB: u64 = 1024 * 1024;

#[derive(Parser, Debug, Clone)]
#[command(
    name = "mnemosyne-publish-host",
    version,
    about = "Stores encrypted Mnemosyne publications and serves the static viewer"
)]
pub struct Config {
    /// Address to listen on (plain HTTP: put a TLS proxy in front).
    #[arg(long, env = "PUBLISH_LISTEN", default_value = "127.0.0.1:8080")]
    pub listen: SocketAddr,
    /// Where publications are stored: `<dir>/<id>/{meta.json, manifest, blobs/<sha256>}`.
    #[arg(long, env = "PUBLISH_DATA_DIR", default_value = "./data")]
    pub data_dir: PathBuf,
    /// The built viewer: `index.html` plus its asset folders.
    #[arg(long, env = "PUBLISH_VIEWER_DIR", default_value = "./viewer")]
    pub viewer_dir: PathBuf,
    /// Closed host: every first write to a new publication must also send this as `X-Upload-Token`.
    #[arg(long, env = "PUBLISH_UPLOAD_TOKEN", hide_env_values = true)]
    pub upload_token: Option<String>,
    /// Take the client address from the last `X-Forwarded-For` entry (set only behind a proxy you control).
    #[arg(long, env = "PUBLISH_TRUST_PROXY")]
    pub trust_proxy: bool,
    /// Furthest expiry a publication may ask for, in days.
    #[arg(long, env = "PUBLISH_MAX_DAYS", default_value_t = 365)]
    pub max_days: u64,
    /// Largest manifest.
    #[arg(long, env = "PUBLISH_MANIFEST_MAX_BYTES", default_value_t = MIB)]
    pub manifest_max_bytes: u64,
    /// Largest blob.
    #[arg(long, env = "PUBLISH_BLOB_MAX_BYTES", default_value_t = 20 * MIB)]
    pub blob_max_bytes: u64,
    /// Most one publication may hold (manifest plus blobs, uploaded-but-unreferenced ones included).
    #[arg(long, env = "PUBLISH_PUBLICATION_MAX_BYTES", default_value_t = 32 * MIB)]
    pub publication_max_bytes: u64,
    /// Total store size; writes answer 507 when it is full.
    #[arg(long, env = "PUBLISH_QUOTA_BYTES", default_value_t = 10 * 1024 * MIB)]
    pub quota_bytes: u64,
    /// Writes allowed at once per client address (token bucket size).
    #[arg(long, env = "PUBLISH_WRITE_BURST", default_value_t = 20)]
    pub write_burst: u32,
    /// Writes per minute per client address, refilled steadily. 0 turns the limit off.
    #[arg(long, env = "PUBLISH_WRITE_PER_MINUTE", default_value_t = 30)]
    pub write_per_minute: u32,
    /// Reads allowed at once per client address.
    #[arg(long, env = "PUBLISH_READ_BURST", default_value_t = 120)]
    pub read_burst: u32,
    /// Reads per minute per client address. 0 turns the limit off.
    #[arg(long, env = "PUBLISH_READ_PER_MINUTE", default_value_t = 600)]
    pub read_per_minute: u32,
    /// Seconds between sweeps for expired publications and old tombstones.
    #[arg(long, env = "PUBLISH_SWEEP_SECS", default_value_t = 300)]
    pub sweep_secs: u64,
    /// Days a deleted or expired publication keeps answering 410 before it answers 404.
    #[arg(long, env = "PUBLISH_TOMBSTONE_DAYS", default_value_t = 30)]
    pub tombstone_days: u64,
    /// Hours a publication that never got a manifest (blobs only) is kept.
    #[arg(long, env = "PUBLISH_PENDING_HOURS", default_value_t = 24)]
    pub pending_hours: u64,
    /// The built guest-notes page (`/n/<room>`): `index.html` plus its asset folders.
    #[arg(long, env = "PUBLISH_GUEST_DIR", default_value = "./guest")]
    pub guest_dir: PathBuf,
    /// Largest sealed frame a guest-notes room accepts (a 2 MiB paste plus its envelope).
    #[arg(long, env = "PUBLISH_ROOM_FRAME_MAX_BYTES", default_value_t = 2 * MIB + 1024)]
    pub room_frame_max_bytes: u64,
    /// Most a room's log may hold; the owner's snapshots prune it.
    #[arg(long, env = "PUBLISH_ROOM_LOG_MAX_BYTES", default_value_t = 8 * MIB)]
    pub room_log_max_bytes: u64,
    /// Largest room snapshot.
    #[arg(long, env = "PUBLISH_ROOM_SNAPSHOT_MAX_BYTES", default_value_t = 8 * MIB)]
    pub room_snapshot_max_bytes: u64,
    /// Furthest expiry a room may ask for, in days from now.
    #[arg(long, env = "PUBLISH_ROOM_MAX_DAYS", default_value_t = 7)]
    pub room_max_days: u64,
    /// Hours a room that has not ended is kept after its last frame.
    #[arg(long, env = "PUBLISH_ROOM_IDLE_HOURS", default_value_t = 24)]
    pub room_idle_hours: u64,
    /// Live connections one room accepts at once.
    #[arg(long, env = "PUBLISH_ROOM_MAX_CONNECTIONS", default_value_t = 64)]
    pub room_max_connections: usize,
    /// Extra iroh relay hosts the guest page may connect to (`https://H` and `wss://H` join its
    /// `connect-src`), comma separated: `host[:port]` or a full URL. n0's public relays are always
    /// allowed.
    #[arg(
        long,
        env = "PUBLISH_RELAY_ORIGINS",
        value_delimiter = ',',
        value_parser = parse_relay_origin
    )]
    pub relay_origins: Vec<String>,
}

/// `https://relay.example.com:443/relay` or `relay.example.com` becomes `relay.example.com[:443]`;
/// an `http://` origin (a loopback dev relay) keeps its scheme: `http://127.0.0.1:8038`.
/// Only characters of a host name or address are accepted, so nothing can break out of the header.
fn parse_relay_origin(raw: &str) -> Result<String, String> {
    let s = raw.trim();
    let (plain, s) = match s.split_once("://") {
        Some((scheme, rest)) => (scheme.eq_ignore_ascii_case("http"), rest),
        None => (false, s),
    };
    let s = s.split(['/', '?', '#']).next().unwrap_or("");
    let ok = !s.is_empty()
        && s.chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | ':' | '[' | ']' | '*'));
    if ok {
        let host = s.to_ascii_lowercase();
        Ok(if plain {
            format!("http://{host}")
        } else {
            host
        })
    } else {
        Err(format!("not a relay host or URL: {raw:?}"))
    }
}

impl Config {
    /// Defaults, as if no flag or variable was given (for tests and embedding).
    pub fn defaults() -> Config {
        Config::parse_from(["mnemosyne-publish-host"])
    }
}
