use std::process::{Child, Command as StdCommand, Stdio};
use std::sync::Mutex;

use log::{error, info};
use tauri::{Emitter, Manager};

/// Holds the backend child process for lifecycle management.
struct BackendState {
    child: Mutex<Option<Child>>,
}

/// Kill a process and its entire process group (handles `uv run` -> `uvicorn` child).
fn kill_process_tree(child: &mut Child) {
    let pid = child.id() as i32;

    // Try killing the process group first (negative PID = process group)
    #[cfg(unix)]
    {
        unsafe {
            libc::kill(-pid, libc::SIGTERM);
        }
        // Give processes a moment to shut down gracefully
        std::thread::sleep(std::time::Duration::from_millis(500));
        // Force kill if still alive
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

/// Poll the backend port until it accepts TCP connections or timeout.
async fn wait_for_backend(timeout_secs: u64) -> bool {
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(timeout_secs);
    while std::time::Instant::now() < deadline {
        if tokio::net::TcpStream::connect("127.0.0.1:8008")
            .await
            .is_ok()
        {
            return true;
        }
        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    }
    false
}

/// Spawn a command in its own process group so we can kill the whole tree.
fn spawn_in_process_group(cmd: &mut StdCommand) -> std::io::Result<Child> {
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        unsafe {
            cmd.pre_exec(|| {
                // Create a new process group with this process as the leader
                libc::setsid();
                Ok(())
            });
        }
    }
    cmd.spawn()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(BackendState {
            child: Mutex::new(None),
        })
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }

            let app_handle = app.handle().clone();

            // Spawn the Python backend process
            let child = if cfg!(debug_assertions) {
                // DEV: use uv from the source backend/ directory
                let backend_dir = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
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
                    "8008",
                    "--reload",
                ])
                .current_dir(&backend_dir)
                .stdout(Stdio::piped())
                .stderr(Stdio::piped());

                spawn_in_process_group(&mut cmd)
                    .expect("Failed to spawn backend via uv. Is uv installed?")
            } else {
                // RELEASE: run PyInstaller binary from resources
                let resource_dir = app_handle
                    .path()
                    .resource_dir()
                    .expect("Failed to resolve resource dir");
                let backend_dir = resource_dir.join("backend");
                let backend_bin = backend_dir.join("mnemosyne-backend");

                info!("RELEASE: spawning backend from {:?}", backend_bin);

                let mut cmd = StdCommand::new(&backend_bin);
                cmd.args(["--host", "127.0.0.1", "--port", "8008"])
                    .current_dir(&backend_dir)
                    .stdout(Stdio::piped())
                    .stderr(Stdio::piped());

                spawn_in_process_group(&mut cmd).unwrap_or_else(|e| {
                    panic!("Failed to spawn backend from {:?}: {}", backend_bin, e)
                })
            };

            info!("Backend spawned with PID: {}", child.id());

            let state = app.state::<BackendState>();
            *state.child.lock().unwrap() = Some(child);

            // Spawn async health check task
            tauri::async_runtime::spawn(async move {
                info!("Waiting for backend to become healthy...");
                let healthy = wait_for_backend(30).await;
                if healthy {
                    info!("Backend is healthy on port 8008");
                } else {
                    error!("Backend did not become healthy within 30s");
                }
                let _ = app_handle.emit("backend-ready", healthy);
            });

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|app_handle, event| {
        if let tauri::RunEvent::Exit = event {
            info!("App exiting, shutting down backend...");
            let child = app_handle
                .state::<BackendState>()
                .child
                .lock()
                .unwrap()
                .take();
            if let Some(mut child) = child {
                kill_process_tree(&mut child);
                info!("Backend process tree killed.");
            }
        }
    });
}
