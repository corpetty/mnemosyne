//! `mnemosyne-link`: the home side of remote access (started by the backend), plus client
//! commands for pairing and tunnelling from a terminal. Machine-readable output is one JSON
//! line on stdout; logs go to stderr (RUST_LOG).

use std::{net::SocketAddr, path::PathBuf, sync::Arc, time::Duration};

use anyhow::Result;
use clap::{Parser, Subcommand};
use iroh_tickets::endpoint::EndpointTicket;
use mnemosyne_link::{
    bind, bind_with, load_or_create_key, serve_home, serve_local, Home, Invite, Lan, Relays, Remote,
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
            tokio::select! {
                r = serve_home(endpoint.clone(), home) => r?,
                _ = shutdown() => {}
            }
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
