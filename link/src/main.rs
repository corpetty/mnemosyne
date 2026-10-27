//! `mnemosyne-link`: the home side of remote access (started by the backend), plus client
//! commands for pairing and tunnelling from a terminal. Machine-readable output is one JSON
//! line on stdout; logs go to stderr (RUST_LOG).

use std::{net::SocketAddr, path::PathBuf, sync::Arc, time::Duration};

use anyhow::Result;
use clap::{Parser, Subcommand};
use iroh_tickets::endpoint::EndpointTicket;
use mnemosyne_link::guest::{GuestOptions, GuestRooms};
use mnemosyne_link::host::{start_loopback_host, HostOptions};
use mnemosyne_link::{
    bind, bind_with, load_or_create_key, serve_control, serve_home, serve_local, Control, Home,
    Invite, Lan, Relays, Remote,
};
use tokio::net::TcpListener;

#[derive(Parser)]
#[command(version, about = "Remote access to a Mnemosyne backend over iroh")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Accept paired devices and forward them to the backend on this machine.
    ///
    /// After the ready line, stdin takes control commands (JSON lines: dial, hangup, pair; see
    /// the README), each answered by one JSON line on stdout. Closing stdin keeps serving.
    Home {
        /// The backend's address.
        #[arg(long, default_value = "127.0.0.1:8008")]
        backend: SocketAddr,
        /// The backend's data directory: holds link.key and paired_devices.json.
        #[arg(long)]
        data_dir: PathBuf,
        /// A relay to use instead of n0's public ones (repeat for several). Devices follow
        /// the relays in home's ticket, so they need no setting of their own.
        #[arg(long = "relay")]
        relays: Vec<String>,
        /// Use no relay at all: only direct connections, which still work on the local network
        /// (home keeps announcing itself by mDNS). Without it, n0's public relays are used when
        /// no `--relay` is given.
        #[arg(long)]
        no_relay: bool,
        /// Do not announce this machine on the local network either (full isolation: with
        /// `--no-relay`, nothing can reach home but a device that has its direct addresses).
        #[arg(long)]
        no_lan: bool,
    },
    /// Serve guest-notes rooms over iroh, proxied to an embedded host on loopback.
    ///
    /// Prints one ready line `{"listen": "127.0.0.1:<port>", "relay_kind": "n0"|"custom"|"none"}`
    /// (the loopback host; "none" with `--no-relay`: no room is served over iroh), then stdin takes control commands (JSON lines: room_start, room_stop,
    /// room_list; see the README), each answered by one JSON line on stdout. Closing stdin keeps
    /// serving.
    Rooms {
        /// The backend's data directory: the host stores under guest_rooms/host, room keys go
        /// in guest_rooms/keys.
        #[arg(long)]
        data_dir: PathBuf,
        /// The built guest page.
        #[arg(long)]
        guest_dir: PathBuf,
        /// The built publication viewer.
        #[arg(long)]
        viewer_dir: PathBuf,
        /// A file holding the token that creates rooms on the host (not world-readable).
        #[arg(long)]
        upload_token_file: PathBuf,
        /// A relay to use instead of n0's public ones (repeat for several: one is picked).
        #[arg(long = "relay")]
        relays: Vec<String>,
        /// Use no relay: the loopback host still runs (a tunnel can front it), but `room_start`
        /// is refused, since a room's iroh endpoint needs a relay. Never falls back to n0's.
        #[arg(long)]
        no_relay: bool,
    },
    /// Pair this machine with a home backend using an invite from its Settings.
    Pair {
        invite: String,
        #[arg(long)]
        key_file: PathBuf,
        #[arg(long, default_value = "Laptop")]
        name: String,
        #[arg(long)]
        no_relay: bool,
    },
    /// Markdown notes: merges and the shared notes document (stdin to stdout, no network).
    Notes {
        #[command(subcommand)]
        command: NotesCommand,
    },
    /// Serve the home backend on a local port.
    Connect {
        ticket: EndpointTicket,
        #[arg(long)]
        key_file: PathBuf,
        #[arg(long, default_value = "127.0.0.1:0")]
        listen: SocketAddr,
        #[arg(long)]
        no_relay: bool,
    },
    /// Check that a relay carries traffic: two throwaway endpoints talk through it.
    ///
    /// Prints `{"ok":true,"relay":...,"ms":N}` and exits 0, or `{"ok":false,"error":...}` and
    /// exits 1.
    RelayCheck {
        #[arg(long)]
        relay: String,
        #[arg(long, default_value_t = 20)]
        timeout_secs: u64,
    },
    /// Dev only: a plain-HTTP iroh relay for the browser tests (feature `dev-relay`).
    #[cfg(feature = "dev-relay")]
    DevRelay {
        #[arg(long, default_value = "127.0.0.1:8038")]
        listen: SocketAddr,
    },
}

#[derive(Subcommand)]
enum NotesCommand {
    /// Three-way merge a note through its history. Reads JSON on stdin and prints one line.
    ///
    /// Single request: `{"history": <base64 or null>, "app": <text>, "vault": <text or null>}`
    /// gives `{"history": <base64>, "text": <text>}`. On an error: a message on stderr, exit 1.
    ///
    /// Optional fields: `"base": <text>` instead of `history` is a one-off three-way merge (a
    /// history is started from `base`, then `app` and `vault` are merged into it; `base` with
    /// `history` is an error). `"rebase": true` with a `history` first sets the app branch to
    /// the combined one, so `app` is diffed against the combined text and a rendering that
    /// echoes the user's edits does not double them (ignored without a history).
    ///
    /// Batch: a JSON array of request objects gives one line, an array of results in order,
    /// each `{"history", "text"}` or `{"error": <message>}` for a request that failed. A batch
    /// exits 0 whatever its items did; only unparsable top-level JSON exits 1.
    Merge,
    /// The live shared notes document (notes-core). Reads one JSON request on stdin and
    /// prints one line; on an error: a message on stderr, exit 1.
    ///
    /// `{"op":"fold","notes":<origin hex>,"snapshot":<b64 or null>,"frames":[<b64>]}` gives
    /// `{"snapshot","markdown","plain","comments"}`; `"op":"set_text"` with `"markdown"` also
    /// gives `"frames"` (0xAF artifacts, then one 0xDE delta, or `[]`); `"op":"merge"` takes
    /// `"snapshot"` and `"other"`. See notes-core's `api` module.
    Doc,
}

#[derive(serde::Deserialize)]
struct MergeRequest {
    history: Option<String>,
    base: Option<String>,
    app: String,
    vault: Option<String>,
    #[serde(default)]
    rebase: bool,
}

fn merge_one(request: MergeRequest) -> Result<serde_json::Value> {
    use anyhow::Context;
    use base64::{engine::general_purpose::STANDARD, Engine};
    use mnemosyne_link::notes;

    let vault = request.vault.as_deref();
    let merged = match (request.history, request.base) {
        (Some(_), Some(_)) => anyhow::bail!("base and history cannot both be given"),
        (None, Some(base)) => notes::merge_from_base(&base, &request.app, vault)?,
        (history, None) => {
            let history = history
                .map(|h| STANDARD.decode(h.trim()).context("history is not base64"))
                .transpose()?;
            notes::merge_with(history.as_deref(), &request.app, vault, request.rebase)?
        }
    };
    Ok(serde_json::json!({
        "history": STANDARD.encode(&merged.history),
        "text": merged.text,
    }))
}

fn notes_merge() -> Result<()> {
    use anyhow::Context;
    use std::io::Read;

    let mut input = String::new();
    std::io::stdin()
        .read_to_string(&mut input)
        .context("reading stdin")?;
    let value: serde_json::Value = serde_json::from_str(&input).context("invalid request")?;
    match value {
        serde_json::Value::Array(items) => {
            let results: Vec<_> = items
                .into_iter()
                .map(|item| {
                    serde_json::from_value::<MergeRequest>(item)
                        .map_err(anyhow::Error::from)
                        .context("invalid request")
                        .and_then(merge_one)
                        .unwrap_or_else(|e| serde_json::json!({ "error": format!("{e:#}") }))
                })
                .collect();
            print_json(serde_json::Value::Array(results));
        }
        value => {
            let request: MergeRequest = serde_json::from_value(value).context("invalid request")?;
            print_json(merge_one(request)?);
        }
    }
    Ok(())
}

fn notes_doc() -> Result<()> {
    use anyhow::Context;
    use std::io::Read;

    let mut input = String::new();
    std::io::stdin()
        .read_to_string(&mut input)
        .context("reading stdin")?;
    println!("{}", notes_core::api::handle_str(&input)?);
    Ok(())
}

fn print_json(value: serde_json::Value) {
    println!("{value}");
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .with_ansi(std::io::IsTerminal::is_terminal(&std::io::stderr()))
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "mnemosyne_link=info".into()),
        )
        .init();
    match Cli::parse().command {
        Command::Notes {
            command: NotesCommand::Merge,
        } => notes_merge()?,
        Command::Notes {
            command: NotesCommand::Doc,
        } => notes_doc()?,
        Command::Home {
            backend,
            data_dir,
            relays,
            no_relay,
            no_lan,
        } => {
            let key = load_or_create_key(&data_dir.join("link.key"))?;
            let relays = if no_relay {
                Relays::Off
            } else {
                Relays::from_urls(&relays)?
            };
            // Home announces itself on the local network so paired computers there connect
            // directly, without any relay (also with `--no-relay`).
            let lan = home_lan(no_lan);
            let endpoint = bind_with(key, &relays, lan, |b| b).await?;
            if relays != Relays::Off
                && tokio::time::timeout(Duration::from_secs(10), endpoint.online())
                    .await
                    .is_err()
            {
                tracing::warn!("no relay reachable yet; direct connections still work");
            }
            let ticket = EndpointTicket::new(endpoint.addr());
            print_json(serde_json::json!({
                "endpoint_id": endpoint.id().to_string(),
                "ticket": ticket.to_string(),
            }));
            let home = Home {
                backend,
                devices_file: data_dir.join("paired_devices.json"),
            };
            let control = Arc::new(Control::new(endpoint.clone()));
            tokio::spawn(serve_control(
                control.clone(),
                stdin_lines(),
                tokio::io::stdout(),
            ));
            tokio::select! {
                r = serve_home(endpoint.clone(), home) => r?,
                _ = shutdown() => {}
            }
            control.close_all().await;
            endpoint.close().await;
        }
        Command::Rooms {
            data_dir,
            guest_dir,
            viewer_dir,
            upload_token_file,
            relays,
            no_relay,
        } => {
            let upload_token = read_secret(&upload_token_file)?;
            let relays = if no_relay {
                Relays::Off
            } else {
                Relays::from_urls(&relays)?
            };
            let relay_origins = match &relays {
                Relays::Custom(urls) => urls.iter().filter_map(relay_origin).collect(),
                _ => Vec::new(),
            };
            let (listen, _host) = start_loopback_host(HostOptions {
                data_dir: data_dir.clone(),
                guest_dir,
                viewer_dir,
                upload_token,
                relay_origins,
            })
            .await?;
            let relay_kind = match relays {
                Relays::N0 => "n0",
                Relays::Off => "none",
                Relays::Custom(_) => "custom",
            };
            print_json(serde_json::json!({
                "listen": listen.to_string(),
                "relay_kind": relay_kind,
            }));
            let rooms = Arc::new(GuestRooms::new(GuestOptions {
                data_dir,
                host: listen,
                relays,
            }));
            tokio::spawn(serve_control(
                rooms.clone(),
                stdin_lines(),
                tokio::io::stdout(),
            ));
            shutdown().await;
            rooms.close_all().await;
        }
        Command::RelayCheck {
            relay,
            timeout_secs,
        } => {
            use mnemosyne_link::relay_check::{check, extra_roots_from_env, trust_extra_roots};
            // MNEMOSYNE_RELAY_CA: a PEM file of extra trusted roots (a relay behind a private CA).
            let roots = match extra_roots_from_env() {
                Ok(roots) => roots,
                Err(e) => {
                    print_json(serde_json::json!({"ok": false, "error": format!("{e:#}")}));
                    std::process::exit(1);
                }
            };
            let outcome = match roots {
                Some(roots) => {
                    check(
                        &relay,
                        Duration::from_secs(timeout_secs),
                        trust_extra_roots(roots),
                    )
                    .await
                }
                None => check(&relay, Duration::from_secs(timeout_secs), |b| b).await,
            };
            match outcome {
                Ok(ms) => print_json(serde_json::json!({"ok": true, "relay": relay, "ms": ms})),
                Err(e) => {
                    print_json(serde_json::json!({"ok": false, "error": format!("{e:#}")}));
                    std::process::exit(1);
                }
            }
        }
        #[cfg(feature = "dev-relay")]
        Command::DevRelay { listen } => {
            use iroh_relay::server::{RelayConfig, Server, ServerConfig};
            let mut config = ServerConfig::default();
            config.relay = Some(RelayConfig::new(listen));
            let server = Server::spawn(config).await?;
            let addr = server.http_addr().unwrap_or(listen);
            print_json(serde_json::json!({"relay": format!("http://{addr}")}));
            shutdown().await;
            server.shutdown().await?;
        }
        Command::Pair {
            invite,
            key_file,
            name,
            no_relay,
        } => {
            let invite: Invite = invite.parse()?;
            let relays = if no_relay {
                Relays::Off
            } else {
                Relays::for_ticket(&invite.ticket)
            };
            let endpoint = bind(load_or_create_key(&key_file)?, &relays).await?;
            let remote = Remote::new(endpoint.clone(), invite.ticket.clone());
            let (token, _sync, card) = remote
                .pair_with_peer(&invite.code, &name, None, None)
                .await?;
            let mut out = serde_json::json!({"token": token, "ticket": invite.ticket.to_string()});
            if let Some(card) = card {
                out["card"] = card;
            }
            print_json(out);
            endpoint.close().await;
        }
        Command::Connect {
            ticket,
            key_file,
            listen,
            no_relay,
        } => {
            let relays = if no_relay {
                Relays::Off
            } else {
                Relays::for_ticket(&ticket)
            };
            let endpoint = bind(load_or_create_key(&key_file)?, &relays).await?;
            let listener = TcpListener::bind(listen).await?;
            print_json(serde_json::json!({"listen": listener.local_addr()?.to_string()}));
            let remote = Arc::new(Remote::new(endpoint.clone(), ticket));
            tokio::select! {
                r = serve_local(remote, listener) => r?,
                _ = shutdown() => {}
            }
            endpoint.close().await;
        }
    }
    Ok(())
}

/// How home uses the local network: announced unless `--no-lan`.
fn home_lan(no_lan: bool) -> Lan {
    if no_lan {
        Lan::Off
    } else {
        Lan::Advertise
    }
}

/// A secret from a file only its owner can read (trimmed; refused when empty).
fn read_secret(path: &std::path::Path) -> Result<String> {
    use anyhow::Context;
    let meta = std::fs::metadata(path)
        .with_context(|| format!("upload token file {} is missing", path.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if meta.permissions().mode() & 0o004 != 0 {
            anyhow::bail!(
                "upload token file {} is world-readable: chmod 600 it",
                path.display()
            );
        }
    }
    #[cfg(not(unix))]
    let _ = meta;
    let token = std::fs::read_to_string(path)
        .with_context(|| format!("reading {}", path.display()))?
        .trim()
        .to_string();
    if token.is_empty() {
        anyhow::bail!("upload token file {} is empty", path.display());
    }
    Ok(token)
}

/// `host[:port]` of a relay, for the guest page's CSP.
fn relay_origin(url: &iroh::RelayUrl) -> Option<String> {
    let host = url.host_str()?;
    Some(match url.port() {
        Some(port) => format!("{host}:{port}"),
        None => host.to_string(),
    })
}

/// Lines from stdin, read on a plain thread: tokio's stdin cannot cancel a blocked read, which
/// would hold up the runtime's shutdown for as long as the backend keeps the pipe open.
fn stdin_lines() -> tokio::sync::mpsc::Receiver<String> {
    let (tx, rx) = tokio::sync::mpsc::channel(64);
    std::thread::spawn(move || {
        use std::io::BufRead;
        for line in std::io::stdin().lock().lines() {
            let Ok(line) = line else { break };
            if tx.blocking_send(line).is_err() {
                break;
            }
        }
    });
    rx
}

async fn shutdown() {
    #[cfg(unix)]
    {
        let mut term =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).unwrap();
        tokio::select! {
            _ = term.recv() => {}
            _ = tokio::signal::ctrl_c() => {}
        }
    }
    #[cfg(not(unix))]
    tokio::signal::ctrl_c().await.ok();
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `--no-relay` must not turn the local network off: only `--no-lan` does.
    #[test]
    fn home_announces_on_the_lan_unless_no_lan() {
        assert_eq!(home_lan(false), Lan::Advertise);
        assert_eq!(home_lan(true), Lan::Off);
    }

    #[test]
    fn no_relay_does_not_imply_no_lan() {
        let cli = Cli::try_parse_from(["mnemosyne-link", "home", "--data-dir", "/x", "--no-relay"])
            .unwrap();
        match cli.command {
            Command::Home {
                no_relay, no_lan, ..
            } => assert!(no_relay && home_lan(no_lan) == Lan::Advertise),
            _ => panic!("not home"),
        }
    }
}
