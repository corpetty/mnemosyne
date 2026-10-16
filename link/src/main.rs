//! `mnemosyne-link`: the home side of remote access (started by the backend), plus client
//! commands for pairing and tunnelling from a terminal. Machine-readable output is one JSON
//! line on stdout; logs go to stderr (RUST_LOG).

use std::{net::SocketAddr, path::PathBuf, sync::Arc, time::Duration};

use anyhow::Result;
use clap::{Parser, Subcommand};
use iroh_tickets::endpoint::EndpointTicket;
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
        /// Only accept direct connections on this machine (no relays).
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
    /// Merge markdown notes (stdin to stdout, no network).
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
        Command::Home {
            backend,
            data_dir,
            relays,
            no_relay,
        } => {
            let key = load_or_create_key(&data_dir.join("link.key"))?;
            let relays = if no_relay {
                Relays::Off
            } else {
                Relays::from_urls(&relays)?
            };
            // Home announces itself on the local network so paired computers there connect
            // directly, without any relay.
            let lan = if no_relay { Lan::Off } else { Lan::Advertise };
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
            let token = remote.pair(&invite.code, &name).await?;
            print_json(serde_json::json!({"token": token, "ticket": invite.ticket.to_string()}));
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
