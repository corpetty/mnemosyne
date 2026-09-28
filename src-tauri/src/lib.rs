//! Tauri shell: owns the window and supervises the Python backend.
//!
//! Dev:     spawns `uv run uvicorn --reload` from the repo's backend/ dir.
//! Release: the backend ships as source + uv.lock in the resource dir and a
//!          pinned `uv` sidecar. On first launch (or after an update changes
//!          uv.lock) we `uv sync` into a per-user venv, then run that venv's
//!          Python directly. Progress is emitted as `backend-status` events.

use std::fs;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command as StdCommand, Stdio};
use std::sync::Mutex;

use log::{error, info, warn};
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Emitter, Manager, Wry};

const BACKEND_PORT: u16 = 8008;

struct BackendState {
    child: Mutex<Option<Child>>,
    /// A backend we found running and took over instead of spawning one (see
    /// `existing_backend`): stopped by pid on exit, since it is not our child.
    adopted: Mutex<Option<i32>>,
}

#[derive(Clone, serde::Serialize)]
struct BackendStatus {
    stage: String,
    message: String,
}

fn emit_status(app: &AppHandle, stage: &str, message: impl Into<String>) {
    let message = message.into();
    info!("[backend {}] {}", stage, message);
    let _ = app.emit(
        "backend-status",
        BackendStatus { stage: stage.to_string(), message },
    );
}

/// Kill a process and its entire process group (handles `uv run` -> `uvicorn` child).
fn kill_process_tree(child: &mut Child) {
    let pid = child.id() as i32;
    #[cfg(unix)]
    {
        unsafe {
            libc::kill(-pid, libc::SIGTERM);
        }
        std::thread::sleep(std::time::Duration::from_millis(500));
        unsafe {
            libc::kill(-pid, libc::SIGKILL);
        }
    }
    #[cfg(not(unix))]
    {
        let _ = child.kill();
    }
    let _ = child.wait();
}

/// Spawn a command in its own process group so we can kill the whole tree.
fn spawn_in_process_group(cmd: &mut StdCommand) -> std::io::Result<Child> {
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        unsafe {
            cmd.pre_exec(|| {
                libc::setsid();
                Ok(())
            });
        }
    }
    cmd.spawn()
}

async fn wait_for_backend(timeout_secs: u64) -> bool {
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(timeout_secs);
    while std::time::Instant::now() < deadline {
        if tokio::net::TcpStream::connect(("127.0.0.1", BACKEND_PORT))
            .await
            .is_ok()
        {
            return true;
        }
        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    }
    false
}

fn backend_addr() -> std::net::SocketAddr {
    std::net::SocketAddr::from(([127, 0, 0, 1], BACKEND_PORT))
}

fn port_in_use() -> bool {
    std::net::TcpStream::connect_timeout(&backend_addr(), std::time::Duration::from_secs(1)).is_ok()
}

/// A small blocking HTTP/1.0 request to the backend port, for the few calls the shell makes
/// before any window can. Returns the JSON body of a 200 answer.
fn backend_request(method: &str, path: &str, body: &str) -> Option<serde_json::Value> {
    use std::io::{Read, Write};
    let timeout = std::time::Duration::from_secs(5);
    let mut stream = std::net::TcpStream::connect_timeout(&backend_addr(), timeout).ok()?;
    stream.set_read_timeout(Some(timeout)).ok()?;
    stream.set_write_timeout(Some(timeout)).ok()?;
    write!(
        stream,
        "{method} {path} HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\
         Content-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}",
        body.len()
    )
    .ok()?;
    let mut raw = Vec::new();
    stream.read_to_end(&mut raw).ok()?;
    let text = String::from_utf8_lossy(&raw);
    let (head, body) = text.split_once("\r\n\r\n")?;
    if head.split_whitespace().nth(1) != Some("200") {
        return None;
    }
    serde_json::from_str(body).ok()
}

/// What holds the backend port when the app starts.
enum Existing {
    Free,
    /// A Mnemosyne backend: usually one left by an app that crashed or was killed, which keeps
    /// recording for a while so we can pick the recording up (backend/mnemosyne/api/app_watch.py).
    Backend { version: String, pid: Option<i32> },
    /// Some other program.
    Other,
}

fn existing_backend() -> Existing {
    if !port_in_use() {
        return Existing::Free;
    }
    match backend_request("GET", "/health", "") {
        Some(h) if h.get("status").and_then(|s| s.as_str()) == Some("ok") => Existing::Backend {
            version: h.get("version").and_then(|v| v.as_str()).unwrap_or_default().to_string(),
            pid: h
                .get("pid")
                .and_then(|p| p.as_i64())
                .map(|p| p as i32)
                .or_else(find_backend_pid),
        },
        _ => Existing::Other,
    }
}

/// Backends before 0.9.2 do not report their pid: find the process running our command line.
fn find_backend_pid() -> Option<i32> {
    let port = BACKEND_PORT.to_string();
    let own = std::process::id() as i32;
    for entry in fs::read_dir("/proc").ok()?.flatten() {
        let Some(pid) = entry.file_name().to_str().and_then(|s| s.parse::<i32>().ok()) else {
            continue;
        };
        let Ok(raw) = fs::read(entry.path().join("cmdline")) else {
            continue;
        };
        let argv: Vec<&[u8]> = raw.split(|b| *b == 0).collect();
        let has = |arg: &str| argv.iter().any(|a| *a == arg.as_bytes());
        if pid != own && has("main.py") && has("--port") && has(&port) {
            return Some(pid);
        }
    }
    None
}

/// Stop a backend that is not our child and wait for the port to be free. SIGTERM lets it
/// shut down cleanly; a recording it had is recovered by the next backend on startup.
fn stop_backend_pid(pid: i32) -> bool {
    unsafe {
        libc::kill(pid, libc::SIGTERM);
    }
    for i in 0..60 {
        if !port_in_use() {
            return true;
        }
        if i == 40 {
            warn!("Backend {pid} did not stop in 20 s; killing it");
            unsafe {
                libc::kill(pid, libc::SIGKILL);
            }
        }
        std::thread::sleep(std::time::Duration::from_millis(500));
    }
    !port_in_use()
}

/// Use the backend already running instead of starting one, and tell it that it belongs to
/// this app now (it then ends with us, and stops waiting for its old app to come back).
fn adopt_backend(app: &AppHandle, pid: Option<i32>) {
    let body = format!("{{\"pid\": {}}}", std::process::id());
    let ours = backend_request("POST", "/api/system/attach", &body)
        .and_then(|r| r.get("watching").and_then(|w| w.as_bool()))
        .unwrap_or(false);
    info!("Using the backend already running (pid {pid:?}; stopped on exit: {ours})");
    if ours {
        *app.state::<BackendState>().adopted.lock().unwrap() = pid;
    }
    emit_status(app, "ready", format!("Backend ready on port {BACKEND_PORT}"));
    let _ = app.emit("backend-ready", true);
}

/// Environment variables the AppImage runtime (AppRun + linuxdeploy hooks) sets for
/// the GUI process. They must not reach uv, Python, or the tools Python spawns
/// (ffmpeg, pw-record): the bundled GTK-era libs shadow system ones and break them.
const RUNTIME_ENV_TO_SCRUB: &[&str] = &[
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "PYTHONHOME",
    "PYTHONPATH",
    "GTK_PATH",
    "GTK_DATA_PREFIX",
    "GTK_EXE_PREFIX",
    "GTK_IM_MODULE_FILE",
    "GDK_PIXBUF_MODULE_FILE",
    "GDK_PIXBUF_MODULEDIR",
    "GIO_MODULE_DIR",
    "GSETTINGS_SCHEMA_DIR",
    "GST_PLUGIN_SYSTEM_PATH",
    "GST_PLUGIN_SYSTEM_PATH_1_0",
    "GST_PLUGIN_SCANNER",
    "PERLLIB",
    "QT_PLUGIN_PATH",
    "XDG_DATA_DIRS",
];

fn scrub_runtime_env(cmd: &mut StdCommand) {
    for key in RUNTIME_ENV_TO_SCRUB {
        cmd.env_remove(key);
    }
    // AppRun keeps the pre-launch value here; hand it back to children.
    if let Ok(orig) = std::env::var("APPIMAGE_ORIGINAL_LD_LIBRARY_PATH") {
        if !orig.is_empty() {
            cmd.env("LD_LIBRARY_PATH", orig);
        }
    }
    if let Ok(orig) = std::env::var("APPIMAGE_ORIGINAL_XDG_DATA_DIRS") {
        if !orig.is_empty() {
            cmd.env("XDG_DATA_DIRS", orig);
        }
    }
}

fn on_path(binary: &str) -> bool {
    std::env::var_os("PATH")
        .map(|paths| std::env::split_paths(&paths).any(|dir| dir.join(binary).is_file()))
        .unwrap_or(false)
}

/// Dev: run the backend from the source tree with hot reload.
fn dev_command() -> StdCommand {
    let backend_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("CARGO_MANIFEST_DIR has no parent")
        .join("backend");
    info!("DEV: spawning backend from {:?}", backend_dir);
    let mut cmd = StdCommand::new("uv");
    cmd.args([
        "run",
        "uvicorn",
        "main:app",
        "--host",
        "127.0.0.1",
        "--port",
        &BACKEND_PORT.to_string(),
        "--reload",
    ])
    .current_dir(&backend_dir);
    cmd
}

/// Release: locations of the bundled backend, the uv sidecar, and per-user dirs.
#[derive(Clone)]
struct ReleaseLayout {
    backend_dir: PathBuf,
    uv: PathBuf,
    venv: PathBuf,
    data_dir: PathBuf,
    /// Where uv keeps the managed Python the venv runs on (per app, like the venv).
    python_dir: PathBuf,
    /// The offline AppImage's bundled uv cache and Python (build-offline-appimage.sh).
    offline: Option<PathBuf>,
}

impl ReleaseLayout {
    fn resolve(app: &AppHandle) -> Result<Self, String> {
        let resource_dir = app
            .path()
            .resource_dir()
            .map_err(|e| format!("resource dir: {e}"))?;
        let backend_dir = resource_dir.join("backend");
        if !backend_dir.join("uv.lock").is_file() {
            return Err(format!("bundled backend not found at {:?}", backend_dir));
        }
        let exe = std::env::current_exe().map_err(|e| format!("current_exe: {e}"))?;
        let uv = exe
            .parent()
            .ok_or("executable has no parent dir")?
            .join("mnemosyne-uv");
        if !uv.is_file() {
            return Err(format!("uv sidecar not found at {:?}", uv));
        }
        let local = app
            .path()
            .app_local_data_dir()
            .map_err(|e| format!("app local data dir: {e}"))?;
        let venv = local.join("venv");
        let data_dir = local.join("data");
        fs::create_dir_all(&data_dir).map_err(|e| format!("create {:?}: {e}", data_dir))?;
        let offline = Some(resource_dir.join("offline")).filter(|d| d.join("uv-cache").is_dir());
        let python_dir = local.join("python");
        Ok(Self { backend_dir, uv, venv, data_dir, python_dir, offline })
    }

    fn python(&self) -> PathBuf {
        self.venv.join("bin").join("python")
    }

    /// Written after the base + onnx sync for this uv.lock.
    fn stamp(&self) -> PathBuf {
        self.venv.join(".installed-uv.lock")
    }

    /// Written after the GPU extra is synced for this uv.lock.
    fn gpu_stamp(&self) -> PathBuf {
        self.venv.join(".installed-gpu-uv.lock")
    }

    fn lock(&self) -> Vec<u8> {
        fs::read(self.backend_dir.join("uv.lock")).unwrap_or_default()
    }

    fn is_installed(&self) -> bool {
        self.python().is_file() && fs::read(self.stamp()).map(|b| b == self.lock()).unwrap_or(false)
    }

    fn gpu_installed(&self) -> bool {
        self.python().is_file()
            && fs::read(self.gpu_stamp()).map(|b| b == self.lock()).unwrap_or(false)
    }
}

/// `uv sync` the backend into the per-user venv, passing each progress line to `on_line`.
/// `gpu` adds the GPU extra (torch, WhisperX, NeMo). `inexact` keeps packages the selected extras
/// do not need (so a base sync after an upgrade does not uninstall torch just to reinstall
/// it in the GPU phase). Returns whether any package was installed or removed.
fn uv_sync(
    layout: &ReleaseLayout,
    gpu: bool,
    inexact: bool,
    offline_cache: Option<&Path>,
    mut on_line: impl FnMut(&str),
) -> Result<bool, String> {
    let mut cmd = StdCommand::new(&layout.uv);
    scrub_runtime_env(&mut cmd);
    cmd.args(["sync", "--frozen", "--no-dev", "--extra", "onnx"]);
    if gpu {
        cmd.args(["--extra", "gpu"]);
    }
    if inexact {
        cmd.arg("--inexact");
    }
    cmd.arg("--project")
        .arg(&layout.backend_dir)
        .env("UV_PROJECT_ENVIRONMENT", &layout.venv)
        .env("UV_NO_PROGRESS", "1")
        // Fedora's uv package ships /etc/uv/uv.toml with python-downloads = "manual"
        // and python-preference = "system", which stop uv fetching the pinned Python.
        // Environment variables take precedence over config files.
        .env("UV_PYTHON_DOWNLOADS", "automatic")
        .env("UV_PYTHON_PREFERENCE", "managed")
        .env("UV_PYTHON_INSTALL_DIR", &layout.python_dir)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped());

    if let Some(cache) = offline_cache {
        cmd.env("UV_CACHE_DIR", cache)
            .env("UV_OFFLINE", "1")
            .env("UV_PYTHON_DOWNLOADS", "never");
    }

    let mut child = cmd.spawn().map_err(|e| format!("failed to run uv: {e}"))?;
    let stderr = child.stderr.take().ok_or("no stderr from uv")?;
    let mut last_lines: Vec<String> = Vec::new();
    let mut changed = false;
    for line in BufReader::new(stderr).lines().map_while(Result::ok) {
        let line = line.trim().to_string();
        if line.is_empty() {
            continue;
        }
        changed |= line.starts_with("+ ") || line.starts_with("- ");
        last_lines.push(line.clone());
        if last_lines.len() > 20 {
            last_lines.remove(0);
        }
        on_line(&line);
    }
    let status = child.wait().map_err(|e| format!("uv wait: {e}"))?;
    if !status.success() {
        return Err(format!(
            "uv sync failed ({status}):\n{}",
            last_lines.join("\n")
        ));
    }
    Ok(changed)
}

/// Phase 1 (blocking, before the backend starts): Python, the API and the CPU engines.
fn install_base(app: &AppHandle, layout: &ReleaseLayout) -> Result<(), String> {
    emit_status(
        app,
        "installing",
        "Installing Python runtime and dependencies. First run only.",
    );
    match &layout.offline {
        Some(offline) => install_offline(app, layout, offline)?,
        None => {
            uv_sync(layout, false, true, None, |line| emit_status(app, "installing", line))?;
        }
    }
    fs::write(layout.stamp(), layout.lock()).map_err(|e| e.to_string())?;
    Ok(())
}

/// The offline AppImage: install from its bundled uv cache and Python. They are copied out
/// first: the AppImage is read-only and mounted at a new path each launch, while uv writes to
/// its cache and the venv must keep pointing at its Python. The cache copy is removed after
/// (the venv's files are hardlinks into it, so they stay).
fn install_offline(app: &AppHandle, layout: &ReleaseLayout, offline: &Path) -> Result<(), String> {
    emit_status(app, "installing", "Installing from the bundled packages (no download needed).");
    let cache = layout.venv.with_file_name("uv-offline-cache");
    let copy = |from: PathBuf, to: &Path| -> Result<(), String> {
        fs::create_dir_all(to).map_err(|e| format!("create {to:?}: {e}"))?;
        let status = StdCommand::new("cp")
            .arg("-a")
            .arg(from.join("."))
            .arg(to)
            .status()
            .map_err(|e| format!("cp: {e}"))?;
        if status.success() { Ok(()) } else { Err(format!("copying {from:?} failed")) }
    };
    copy(offline.join("python"), &layout.python_dir)?;
    copy(offline.join("uv-cache"), &cache)?;
    let result =
        uv_sync(layout, false, true, Some(&cache), |line| emit_status(app, "installing", line));
    let _ = fs::remove_dir_all(&cache);
    result.map(|_| ())
}

#[derive(Clone, serde::Serialize)]
struct GpuInstall {
    state: String, // installing | done | error
    message: String,
    restart: bool, // the backend must restart to use it
}

fn emit_gpu(app: &AppHandle, state: &str, message: impl Into<String>, restart: bool) {
    let message = message.into();
    info!("[gpu {}] {}", state, message);
    let _ = app.emit(
        "gpu-install",
        GpuInstall { state: state.to_string(), message, restart },
    );
}

/// Phase 2 (in the background, while the backend already runs on CPU engines): the GPU
/// extra. The UI restarts the backend once nothing is recording or running.
fn install_gpu(app: &AppHandle, layout: &ReleaseLayout) {
    emit_gpu(app, "installing", "Installing GPU support (NVIDIA)…", false);
    match uv_sync(layout, true, false, None, |line| emit_gpu(app, "installing", line, false)) {
        Ok(changed) => {
            if let Err(e) = fs::write(layout.gpu_stamp(), layout.lock()) {
                warn!("could not write the GPU stamp: {e}");
            }
            emit_gpu(app, "done", "GPU support installed", changed);
        }
        Err(e) => emit_gpu(app, "error", e, false),
    }
}

fn release_command(layout: &ReleaseLayout) -> StdCommand {
    info!(
        "RELEASE: spawning {:?} main.py in {:?}",
        layout.python(),
        layout.backend_dir
    );
    let mut cmd = StdCommand::new(layout.python());
    scrub_runtime_env(&mut cmd);
    cmd.args(["main.py", "--host", "127.0.0.1", "--port", &BACKEND_PORT.to_string()])
        .current_dir(&layout.backend_dir)
        .env("MNEMOSYNE_DATA_DIR", &layout.data_dir)
        // The backend ends when this app does (after saving a recording it was making).
        .env("MNEMOSYNE_APP_PID", std::process::id().to_string())
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env("PYTHONUNBUFFERED", "1");
    cmd
}

/// Prepare (install if needed) and spawn the backend. Runs on a worker thread.
fn start_backend(app: AppHandle) {
    match existing_backend() {
        Existing::Free => {}
        Existing::Other => {
            emit_status(
                &app,
                "error",
                format!("Port {BACKEND_PORT} is in use by another program"),
            );
            return;
        }
        Existing::Backend { version, pid } if version == app.package_info().version.to_string() => {
            adopt_backend(&app, pid);
            return;
        }
        Existing::Backend { version, pid } => {
            info!("A backend of version {version} (pid {pid:?}) is still running; stopping it");
            emit_status(&app, "starting", "Stopping the previous version's backend...");
            if !pid.map(stop_backend_pid).unwrap_or(false) {
                emit_status(
                    &app,
                    "error",
                    format!(
                        "A Mnemosyne backend of version {version} is still running on port \
                         {BACKEND_PORT} and could not be stopped"
                    ),
                );
                return;
            }
        }
    }

    let mut gpu_layout: Option<ReleaseLayout> = None;
    let mut cmd = if cfg!(debug_assertions) {
        dev_command()
    } else {
        let layout = match ReleaseLayout::resolve(&app) {
            Ok(l) => l,
            Err(e) => {
                emit_status(&app, "error", e);
                return;
            }
        };
        if !layout.is_installed() {
            if let Err(e) = install_base(&app, &layout) {
                emit_status(&app, "error", e);
                return;
            }
        }
        if gpu_available() && !layout.gpu_installed() {
            gpu_layout = Some(layout.clone());
        }
        release_command(&layout)
    };

    emit_status(&app, "starting", "Starting backend...");
    cmd.stdin(Stdio::null())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit());

    let child = match spawn_in_process_group(&mut cmd) {
        Ok(c) => c,
        Err(e) => {
            emit_status(&app, "error", format!("Failed to spawn backend: {e}"));
            return;
        }
    };
    info!("Backend spawned with PID: {}", child.id());
    *app.state::<BackendState>().child.lock().unwrap() = Some(child);

    let app2 = app.clone();
    tauri::async_runtime::spawn(async move {
        // First start after install may need to import torch; be generous.
        let healthy = wait_for_backend(120).await;
        if healthy {
            emit_status(&app2, "ready", format!("Backend ready on port {BACKEND_PORT}"));
        } else {
            emit_status(&app2, "error", "Backend did not become healthy within 120s");
        }
        let _ = app2.emit("backend-ready", healthy);
        if healthy {
            if let Some(layout) = gpu_layout {
                let app3 = app2.clone();
                let _ = std::thread::Builder::new()
                    .name("gpu-install".into())
                    .spawn(move || install_gpu(&app3, &layout));
            }
        }
    });
}

/// Stop the backend and start it again (after the GPU extra was installed).
#[tauri::command]
fn restart_backend(app: AppHandle) {
    info!("Restarting the backend");
    if let Some(mut child) = app.state::<BackendState>().child.lock().unwrap().take() {
        kill_process_tree(&mut child);
    }
    let adopted = app.state::<BackendState>().adopted.lock().unwrap().take();
    let handle = app.clone();
    let _ = std::thread::Builder::new()
        .name("backend-supervisor".into())
        .spawn(move || {
            if let Some(pid) = adopted {
                stop_backend_pid(pid); // or start_backend would take it over again
            }
            start_backend(handle)
        });
}

// ---- tray, single instance, remote control -----------------------------------

/// Recording actions the UI performs when asked by the tray or a second launch
/// (`mnemosyne --toggle|--start|--stop`, meant to be bound to a desktop shortcut
/// because Wayland does not let apps grab global keys).
const ACTION_EVENT: &str = "tray-action";

struct TrayState {
    toggle: MenuItem<Wry>,
}

/// An action requested on the command line of the *first* launch; the UI asks for
/// it once it is ready, since no listener exists yet at that point.
struct LaunchAction(Mutex<Option<String>>);

fn action_from_args<I: IntoIterator<Item = S>, S: AsRef<str>>(args: I) -> Option<&'static str> {
    for a in args {
        match a.as_ref() {
            "--toggle" => return Some("toggle-record"),
            "--start" => return Some("start-record"),
            "--stop" => return Some("stop-record"),
            _ => {}
        }
    }
    None
}

fn show_main_window(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
    }
}

#[tauri::command]
fn set_recording_state(app: AppHandle, recording: bool) -> Result<(), String> {
    if let Some(state) = app.try_state::<TrayState>() {
        state
            .toggle
            .set_text(if recording { "Stop recording" } else { "Start recording" })
            .map_err(|e| e.to_string())?;
    }
    if let Some(tray) = app.tray_by_id("main") {
        let _ = tray.set_tooltip(Some(if recording {
            "Mnemosyne: recording"
        } else {
            "Mnemosyne"
        }));
    }
    Ok(())
}

#[tauri::command]
fn take_launch_action(state: tauri::State<'_, LaunchAction>) -> Option<String> {
    state.0.lock().unwrap().take()
}

#[tauri::command]
fn show_window(app: AppHandle) {
    show_main_window(&app);
}

/// Whether this install can update itself. Flatpaks update through Flatpak; the
/// binary inside comes from the deb, so the updater would otherwise try dpkg.
#[tauri::command]
fn can_self_update() -> bool {
    !Path::new("/.flatpak-info").exists()
}

/// Restart after an update is installed. Goes through the normal exit path, so the
/// backend is killed (RunEvent::Exit) and the single-instance lock is released first.
#[tauri::command]
fn restart_app(app: AppHandle) {
    info!("Restarting to apply update");
    app.request_restart();
}

/// Release smoke test (CI sets MNEMOSYNE_SMOKE=1; see scripts/smoke-appimage.sh).
fn smoke_mode() -> bool {
    std::env::var_os("MNEMOSYNE_SMOKE").is_some()
}

#[tauri::command]
fn is_smoke_test() -> bool {
    smoke_mode()
}

/// The page reports the smoke test's result (src/lib/app/smoke.ts); print it and exit.
#[tauri::command]
fn smoke_result(app: AppHandle, ok: bool, detail: String) {
    if !smoke_mode() {
        return;
    }
    println!("SMOKE {}: {detail}", if ok { "OK" } else { "FAIL" });
    app.exit(if ok { 0 } else { 1 });
}

/// If WebKit's web process dies (a broken media stack did this in the AppImage), reload the
/// page instead of leaving a white window. At most three reloads a minute, so a page that
/// crashes on load cannot loop forever.
#[cfg(target_os = "linux")]
fn reload_after_webview_crash(app: &AppHandle) {
    use std::cell::RefCell;
    use std::time::{Duration, Instant};
    use webkit2gtk::WebViewExt;

    let Some(window) = app.get_webview_window("main") else {
        return;
    };
    let app = app.clone();
    let _ = window.with_webview(move |webview| {
        let recent: RefCell<Vec<Instant>> = RefCell::new(Vec::new());
        webview.inner().connect_web_process_terminated(move |view, reason| {
            if smoke_mode() {
                println!("SMOKE FAIL: WebKit web process terminated ({reason:?})");
                app.exit(3);
                return;
            }
            let now = Instant::now();
            let mut recent = recent.borrow_mut();
            recent.retain(|t| now.duration_since(*t) < Duration::from_secs(60));
            if recent.len() >= 3 {
                error!("WebKit web process terminated again ({reason:?}); not reloading");
                return;
            }
            recent.push(now);
            warn!("WebKit web process terminated ({reason:?}); reloading the window");
            view.reload();
        });
    });
}

#[cfg(not(target_os = "linux"))]
fn reload_after_webview_crash(_app: &AppHandle) {}

/// Whether a shared library can be loaded (dlopen, closed right away).
fn lib_loadable(name: &str) -> bool {
    let c = std::ffi::CString::new(name).unwrap();
    // SAFETY: dlopen with a valid C string; the handle is closed right away.
    unsafe {
        let h = libc::dlopen(c.as_ptr(), libc::RTLD_LAZY);
        if h.is_null() {
            false
        } else {
            libc::dlclose(h);
            true
        }
    }
}

/// Whether the library the Linux tray loads at runtime can be found.
fn appindicator_available() -> bool {
    ["libayatana-appindicator3.so.1", "libappindicator3.so.1"]
        .iter()
        .any(|name| lib_loadable(name))
}

/// An NVIDIA driver is usable: nvidia-smi on PATH, or the CUDA driver library loadable
/// (the Flatpak's NVIDIA GL extension ships libcuda but not nvidia-smi).
fn gpu_available() -> bool {
    on_path("nvidia-smi") || lib_loadable("libcuda.so.1")
}

fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let toggle = MenuItem::with_id(app, "toggle", "Start recording", true, None::<&str>)?;
    let show = MenuItem::with_id(app, "show", "Show Mnemosyne", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let menu = Menu::with_items(
        app,
        &[&toggle, &show, &PredefinedMenuItem::separator(app)?, &quit],
    )?;
    let mut builder = TrayIconBuilder::with_id("main")
        .tooltip("Mnemosyne")
        .menu(&menu)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "toggle" => {
                let _ = app.emit(ACTION_EVENT, "toggle-record");
            }
            "show" => show_main_window(app),
            "quit" => app.exit(0),
            _ => {}
        });
    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }
    builder.build(app)?;
    app.manage(TrayState { toggle });
    Ok(())
}

fn log_dir_hint(path: &Path) -> String {
    format!("{}", path.display())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        // Must be the first plugin: a second launch hands its args to us and exits.
        .plugin(tauri_plugin_single_instance::init(|app, argv, _cwd| {
            match action_from_args(argv.iter()) {
                Some(action) => {
                    info!("Remote action from second launch: {action}");
                    let _ = app.emit(ACTION_EVENT, action);
                }
                None => show_main_window(app),
            }
        }))
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_notification::init())
        .manage(BackendState { child: Mutex::new(None), adopted: Mutex::new(None) })
        .manage(LaunchAction(Mutex::new(
            action_from_args(std::env::args().skip(1)).map(str::to_string),
        )))
        .invoke_handler(tauri::generate_handler![
            set_recording_state,
            take_launch_action,
            show_window,
            restart_app,
            can_self_update,
            restart_backend,
            is_smoke_test,
            smoke_result
        ])
        .setup(|app| {
            app.handle().plugin(
                tauri_plugin_log::Builder::default()
                    .level(if cfg!(debug_assertions) {
                        log::LevelFilter::Info
                    } else {
                        log::LevelFilter::Info
                    })
                    .build(),
            )?;
            if let Ok(dir) = app.path().app_log_dir() {
                info!("Logs: {}", log_dir_hint(&dir));
            }

            reload_after_webview_crash(app.handle());

            // A missing tray host (e.g. GNOME without the AppIndicator extension) must
            // not stop the app. Without the appindicator library the tray crate panics,
            // so check for it first.
            if !appindicator_available() {
                warn!("System tray unavailable: no libayatana-appindicator3 or libappindicator3");
            } else if let Err(e) = build_tray(app.handle()) {
                warn!("System tray unavailable: {e}");
            }

            let handle = app.handle().clone();
            std::thread::Builder::new()
                .name("backend-supervisor".into())
                .spawn(move || start_backend(handle))
                .map_err(|e| {
                    error!("failed to start supervisor thread: {e}");
                    e
                })?;
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|app_handle, event| {
        if let tauri::RunEvent::Exit = event {
            info!("App exiting, shutting down backend...");
            let state = app_handle.state::<BackendState>();
            let child = state.child.lock().unwrap().take();
            let adopted = state.adopted.lock().unwrap().take();
            if let Some(mut child) = child {
                kill_process_tree(&mut child);
                info!("Backend process tree killed.");
            } else if let Some(pid) = adopted {
                stop_backend_pid(pid);
                info!("Backend {pid} (taken over at start) stopped.");
            } else {
                warn!("No backend child to kill (install may still be running).");
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::action_from_args;

    #[test]
    fn parses_remote_actions() {
        assert_eq!(action_from_args(["mnemosyne", "--toggle"]), Some("toggle-record"));
        assert_eq!(action_from_args(["--start"]), Some("start-record"));
        assert_eq!(action_from_args(["x", "--stop", "--toggle"]), Some("stop-record"));
        assert_eq!(action_from_args(["mnemosyne"]), None);
        assert_eq!(action_from_args(Vec::<String>::new()), None);
    }
}
