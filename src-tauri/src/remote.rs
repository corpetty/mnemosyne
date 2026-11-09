//! Remote access from this computer to a Mnemosyne backend on another machine (the `link` crate).
//!
//! Pairing redeems an invite from the other machine's Settings (`<ticket>#<code>`) with this
//! computer's own iroh key (`<app data>/remote.key`). The home ticket and the local port are kept
//! in `<app data>/remote.json`. While paired, a tunnel listens on 127.0.0.1 and the UI's
//! connection points at it, carrying the API token the pairing returned; the tunnel restarts
//! with the app.
//!
//! This computer's own backend is told about the main computer (`PUT /api/system/main`: the
//! tunnel's URL, the token and the main computer's name) whenever the tunnel comes up, at
//! pairing and at each launch, and forgets it (`DELETE`) when the pairing is forgotten. Its
//! recordings then go to the main computer. The token is kept in remote.json (mode 0600) for
//! that; a pairing made before it was kept is told nothing until it is paired again.

use std::{
    net::SocketAddr,
    path::PathBuf,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc,
    },
};

use log::{info, warn};
use mnemosyne_link::{
    bind, load_or_create_key, serve_local, EndpointTicket, Invite, Relays, Remote,
};
use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager};
use tokio::{net::TcpListener, sync::Mutex};

/// Tried first so the connection URL the UI saved stays valid across launches.
const PREFERRED_PORT: u16 = 8009;

#[derive(Clone, Serialize, Deserialize)]
struct Saved {
    ticket: String,
    port: u16,
    /// The API token the pairing returned, for telling this computer's backend (None: paired
    /// before it was kept).
    #[serde(default)]
    token: Option<String>,
}

/// Bumped by every tell and forget: a tell still waiting for the backend gives up when a newer
/// one (or a forget) came after it.
static TELL: AtomicU64 = AtomicU64::new(0);

struct Tunnel {
    remote: Arc<Remote>,
    task: tauri::async_runtime::JoinHandle<()>,
    url: String,
    addr: SocketAddr,
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
    use std::io::Write;
    let path = dir(app)?.join("remote.json");
    let mut options = std::fs::OpenOptions::new();
    options.write(true).create(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
        options.mode(0o600); // it holds the token
        if path.exists() {
            let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600));
        }
    }
    options
        .open(&path)
        .and_then(|mut f| f.write_all(&serde_json::to_vec(saved).unwrap()))
        .map_err(|e| format!("{path:?}: {e}"))
}

/// Tell this computer's backend about the main computer behind the tunnel at `tunnel`, once the
/// backend answers (at launch it may still be starting, or installing on a first run). The
/// name is the main computer's host name, asked through the tunnel, else its short id.
fn tell_backend(tunnel: SocketAddr, token: String, fallback_name: String) {
    let generation = TELL.fetch_add(1, Ordering::SeqCst) + 1;
    let spawned = std::thread::Builder::new()
        .name("tell-main".into())
        .spawn(move || {
            let current = || TELL.load(Ordering::SeqCst) == generation;
            let deadline = std::time::Instant::now() + std::time::Duration::from_secs(30 * 60);
            while crate::backend_request("GET", "/health", "").is_none() {
                if !current() || std::time::Instant::now() > deadline {
                    return;
                }
                std::thread::sleep(std::time::Duration::from_secs(2));
            }
            let name = crate::http_request(tunnel, "GET", "/health", "")
                .and_then(|h| h.get("host").and_then(|v| v.as_str()).map(str::to_string))
                .filter(|n| !n.is_empty())
                .unwrap_or(fallback_name);
            let body = serde_json::json!({
                "url": format!("http://{tunnel}"),
                "token": token,
                "name": name,
            })
            .to_string();
            for _ in 0..5 {
                if !current() {
                    return;
                }
                if crate::backend_request("PUT", "/api/system/main", &body).is_some() {
                    info!("Remote access: told the backend its main computer is {name}");
                    return;
                }
                std::thread::sleep(std::time::Duration::from_secs(3));
            }
            warn!("Remote access: could not tell the backend about the main computer");
        });
    if let Err(e) = spawned {
        warn!("Remote access: {e}");
    }
}

/// The pairing was forgotten: this computer's backend has no main computer any more.
fn untell_backend() {
    TELL.fetch_add(1, Ordering::SeqCst); // a tell still waiting gives up
    let _ = std::thread::Builder::new()
        .name("untell-main".into())
        .spawn(|| {
            if crate::backend_request("DELETE", "/api/system/main", "").is_none() {
                warn!("Remote access: could not tell the backend the main computer is gone");
            }
        });
}

/// After the tunnel is up: tell the backend, when the token is known.
fn tell_if_known(saved: Option<&Saved>, tunnel: SocketAddr) {
    let Some(saved) = saved else { return };
    let name = short_id(&saved.ticket).unwrap_or_else(|| "Main computer".into());
    match saved.token.clone() {
        Some(token) if !token.is_empty() => tell_backend(tunnel, token, name),
        _ => info!(
            "Remote access: paired before the token was kept; pair again so this computer's \
             recordings go to {name}"
        ),
    }
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
        addr,
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
    // Home's ticket names its relays: n0's public ones, or ones it runs itself.
    let endpoint = bind(key, &Relays::for_ticket(&ticket))
        .await
        .map_err(|e| format!("{e:#}"))?;
    let remote = Arc::new(Remote::new(endpoint, ticket));
    let url = start_with(app, &mut tunnel, remote, saved.port).await?;
    if let Some(t) = tunnel.as_ref() {
        tell_if_known(load(app).as_ref(), t.addr);
    }
    Ok(Some(url))
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
    let endpoint = bind(key, &Relays::for_ticket(&invite.ticket))
        .await
        .map_err(|e| format!("{e:#}"))?;
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
            token: Some(token.clone()),
        },
    )?;
    let url = start_with(&app, &mut tunnel, remote, PREFERRED_PORT).await?;
    if let Some(t) = tunnel.as_ref() {
        tell_if_known(load(&app).as_ref(), t.addr);
    }
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
    untell_backend();
    let path = dir(&app)?.join("remote.json");
    match std::fs::remove_file(&path) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(format!("{path:?}: {e}")),
    }
}
