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
struct ReleaseLayout {
    backend_dir: PathBuf,
    uv: PathBuf,
    venv: PathBuf,
    data_dir: PathBuf,
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
        Ok(Self { backend_dir, uv, venv, data_dir })
    }

    fn python(&self) -> PathBuf {
        self.venv.join("bin").join("python")
    }

    fn stamp(&self) -> PathBuf {
        self.venv.join(".installed-uv.lock")
    }

    fn is_installed(&self) -> bool {
        let lock = fs::read(self.backend_dir.join("uv.lock")).unwrap_or_default();
        self.python().is_file() && fs::read(self.stamp()).map(|b| b == lock).unwrap_or(false)
    }
}

/// `uv sync` the backend into the per-user venv, streaming progress lines.
fn install_backend(app: &AppHandle, layout: &ReleaseLayout) -> Result<(), String> {
    let gpu = on_path("nvidia-smi");
    emit_status(
        app,
        "installing",
        if gpu {
            "Installing Python runtime and ML dependencies (NVIDIA GPU detected). First run only."
        } else {
            "Installing Python runtime and dependencies (CPU engines only). First run only."
        },
    );

    let mut cmd = StdCommand::new(&layout.uv);
    scrub_runtime_env(&mut cmd);
    cmd.args(["sync", "--frozen", "--no-dev", "--extra", "onnx"]);
    if gpu {
        cmd.args(["--extra", "gpu"]);
    }
    cmd.arg("--project")
        .arg(&layout.backend_dir)
        .env("UV_PROJECT_ENVIRONMENT", &layout.venv)
        .env("UV_NO_PROGRESS", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped());

    let mut child = cmd.spawn().map_err(|e| format!("failed to run uv: {e}"))?;
    let stderr = child.stderr.take().ok_or("no stderr from uv")?;
    let mut last_lines: Vec<String> = Vec::new();
    for line in BufReader::new(stderr).lines().map_while(Result::ok) {
        let line = line.trim().to_string();
        if line.is_empty() {
            continue;
        }
        last_lines.push(line.clone());
        if last_lines.len() > 20 {
            last_lines.remove(0);
        }
        emit_status(app, "installing", line);
    }
    let status = child.wait().map_err(|e| format!("uv wait: {e}"))?;
    if !status.success() {
        return Err(format!(
            "uv sync failed ({status}):\n{}",
            last_lines.join("\n")
        ));
    }
    let lock = fs::read(layout.backend_dir.join("uv.lock")).map_err(|e| e.to_string())?;
    fs::write(layout.stamp(), lock).map_err(|e| e.to_string())?;
    Ok(())
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
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env("PYTHONUNBUFFERED", "1");
    cmd
}

/// Prepare (install if needed) and spawn the backend. Runs on a worker thread.
fn start_backend(app: AppHandle) {
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
            if let Err(e) = install_backend(&app, &layout) {
                emit_status(&app, "error", e);
                return;
            }
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

/// Restart after an update is installed. Goes through the normal exit path, so the
/// backend is killed (RunEvent::Exit) and the single-instance lock is released first.
#[tauri::command]
fn restart_app(app: AppHandle) {
    info!("Restarting to apply update");
    app.request_restart();
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
        .manage(BackendState { child: Mutex::new(None) })
        .manage(LaunchAction(Mutex::new(
            action_from_args(std::env::args().skip(1)).map(str::to_string),
        )))
        .invoke_handler(tauri::generate_handler![
            set_recording_state,
            take_launch_action,
            show_window,
            restart_app
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

            // A missing tray host (e.g. GNOME without the AppIndicator extension) must
            // not stop the app.
            if let Err(e) = build_tray(app.handle()) {
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
            let child = app_handle.state::<BackendState>().child.lock().unwrap().take();
            if let Some(mut child) = child {
                kill_process_tree(&mut child);
                info!("Backend process tree killed.");
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
