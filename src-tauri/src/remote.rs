//! Remote access from this computer to a Mnemosyne backend on another machine (the `link` crate).
//!
//! Pairing redeems an invite from the other machine's Settings (`<ticket>#<code>`) with this
//! computer's own iroh key (`<app data>/remote.key`). The home ticket and the local port are kept
//! in `<app data>/remote.json`. While paired, a tunnel listens on 127.0.0.1 and the UI's
//! connection points at it, carrying the API token the pairing returned; the tunnel restarts
//! with the app.

use std::{path::PathBuf, sync::Arc};

use log::{info, warn};
use mnemosyne_link::{bind, load_or_create_key, serve_local, EndpointTicket, Invite, Remote};
use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager};
use tokio::{net::TcpListener, sync::Mutex};

/// Tried first so the connection URL the UI saved stays valid across launches.
const PREFERRED_PORT: u16 = 8009;

#[derive(Clone, Serialize, Deserialize)]
struct Saved {
    ticket: String,
    port: u16,
}

struct Tunnel {
    remote: Arc<Remote>,
    task: tauri::async_runtime::JoinHandle<()>,
    url: String,
}

#[derive(Default)]
pub struct RemoteState(Mutex<Option<Tunnel>>);

#[derive(Serialize)]
pub struct RemoteStatus {
    /// Paired with a home backend.
    configured: bool,
    running: bool,
    /// The local tunnel address the connection should use.
    url: Option<String>,
    /// A short form of home's endpoint id, to show which machine this is paired with.
    home: Option<String>,
}

#[derive(Serialize)]
pub struct Paired {
    url: String,
    token: String,
}

fn dir(app: &AppHandle) -> Result<PathBuf, String> {
    let dir = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("app data dir: {e}"))?;
    std::fs::create_dir_all(&dir).map_err(|e| format!("create {dir:?}: {e}"))?;
    Ok(dir)
}

fn load(app: &AppHandle) -> Option<Saved> {
    let text = std::fs::read_to_string(dir(app).ok()?.join("remote.json")).ok()?;
    serde_json::from_str(&text).ok()
}

fn save(app: &AppHandle, saved: &Saved) -> Result<(), String> {
    let path = dir(app)?.join("remote.json");
    std::fs::write(&path, serde_json::to_vec(saved).unwrap()).map_err(|e| format!("{path:?}: {e}"))
}

fn short_id(ticket: &str) -> Option<String> {
    let ticket: EndpointTicket = ticket.parse().ok()?;
    Some(ticket.endpoint_addr().id.fmt_short().to_string())
}

async fn listen(port: u16) -> Result<TcpListener, String> {
    match TcpListener::bind(("127.0.0.1", port)).await {
        Ok(l) => Ok(l),
        Err(e) => {
            warn!("Remote access: port {port} unavailable ({e}), using another");
            TcpListener::bind("127.0.0.1:0")
                .await
                .map_err(|e| format!("local port: {e}"))
        }
    }
}

/// Serve `remote` on a local port (unless a tunnel already runs) and return its URL.
async fn start_with(
    app: &AppHandle,
    tunnel: &mut Option<Tunnel>,
    remote: Arc<Remote>,
    port: u16,
) -> Result<String, String> {
    let listener = listen(port).await?;
    let addr = listener.local_addr().map_err(|e| e.to_string())?;
    let url = format!("http://{addr}");
    let task = tauri::async_runtime::spawn({
        let remote = remote.clone();
        async move {
            if let Err(e) = serve_local(remote, listener).await {
                warn!("Remote access tunnel stopped: {e:#}");
            }
        }
    });
    info!("Remote access: tunnel on {url}");
    if let Some(mut saved) = load(app) {
        if saved.port != addr.port() {
            saved.port = addr.port();
            save(app, &saved)?;
        }
    }
    *tunnel = Some(Tunnel {
        remote,
        task,
        url: url.clone(),
    });
    Ok(url)
}

async fn stop(tunnel: &mut Option<Tunnel>) {
    if let Some(t) = tunnel.take() {
        t.task.abort();
        t.remote.endpoint().close().await;
    }
}

/// Start the tunnel if this computer is paired and it is not running yet. Returns its URL.
async fn ensure_started(app: &AppHandle) -> Result<Option<String>, String> {
    let Some(saved) = load(app) else {
        return Ok(None);
    };
    let state = app.state::<RemoteState>();
    let mut tunnel = state.0.lock().await;
    if let Some(t) = tunnel.as_ref() {
        return Ok(Some(t.url.clone()));
    }
    let ticket: EndpointTicket = saved.ticket.parse().map_err(|e| format!("ticket: {e}"))?;
    let key = load_or_create_key(&dir(app)?.join("remote.key")).map_err(|e| format!("{e:#}"))?;
    let endpoint = bind(key, true).await.map_err(|e| format!("{e:#}"))?;
    let remote = Arc::new(Remote::new(endpoint, ticket));
    start_with(app, &mut tunnel, remote, saved.port)
        .await
        .map(Some)
}

/// At launch: bring the tunnel up before the UI's first request, if this computer is paired.
pub fn start_at_launch(app: &AppHandle) {
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        if let Err(e) = ensure_started(&app).await {
            warn!("Remote access: {e}");
        }
    });
}

#[tauri::command]
pub async fn remote_status(app: AppHandle) -> RemoteStatus {
    let saved = load(&app);
    let state = app.state::<RemoteState>();
    let tunnel = state.0.lock().await;
    RemoteStatus {
        configured: saved.is_some(),
        running: tunnel.is_some(),
        url: tunnel.as_ref().map(|t| t.url.clone()),
        home: saved.and_then(|s| short_id(&s.ticket)),
    }
}

#[tauri::command]
pub async fn remote_start(app: AppHandle) -> Result<Option<String>, String> {
    ensure_started(&app).await
}

/// Pair with the home backend that made `invite`, then serve it on a local port.
#[tauri::command]
pub async fn remote_pair(app: AppHandle, invite: String, name: String) -> Result<Paired, String> {
    let invite: Invite = invite.parse().map_err(|e| format!("{e:#}"))?;
    let key = load_or_create_key(&dir(&app)?.join("remote.key")).map_err(|e| format!("{e:#}"))?;
    let endpoint = bind(key, true).await.map_err(|e| format!("{e:#}"))?;
    let remote = Arc::new(Remote::new(endpoint, invite.ticket.clone()));
    let token = match remote.pair(&invite.code, &name).await {
        Ok(token) => token,
        Err(e) => {
            remote.endpoint().close().await;
            return Err(format!("{e:#}"));
        }
    };
    let state = app.state::<RemoteState>();
    let mut tunnel = state.0.lock().await;
    stop(&mut tunnel).await;
    save(
        &app,
        &Saved {
            ticket: invite.ticket.to_string(),
            port: PREFERRED_PORT,
        },
    )?;
    let url = start_with(&app, &mut tunnel, remote, PREFERRED_PORT).await?;
    info!(
        "Remote access: paired with {}",
        invite.ticket.endpoint_addr().id.fmt_short()
    );
    Ok(Paired { url, token })
}

/// Stop the tunnel and forget the home backend (this computer keeps its key).
#[tauri::command]
pub async fn remote_forget(app: AppHandle) -> Result<(), String> {
    let state = app.state::<RemoteState>();
    stop(&mut *state.0.lock().await).await;
    let path = dir(&app)?.join("remote.json");
    match std::fs::remove_file(&path) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(format!("{path:?}: {e}")),
    }
}
