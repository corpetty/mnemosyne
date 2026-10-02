//! Preferences that belong to this window rather than to a backend (which may be remote):
//! whether closing the window only hides it to the tray. Kept in `<app config>/shell.json`.

use std::fs;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

use log::warn;
use tauri::{AppHandle, Manager};

#[derive(Clone, Default, serde::Serialize, serde::Deserialize)]
#[serde(default)]
pub struct ShellPrefs {
    pub close_to_tray: bool,
}

pub struct ShellPrefsState {
    prefs: Mutex<ShellPrefs>,
    path: Option<PathBuf>,
    /// The tray was built (GNOME without the AppIndicator extension has none): hiding the
    /// window with no tray to bring it back would strand it.
    pub tray: AtomicBool,
}

impl ShellPrefsState {
    pub fn load(app: &AppHandle) -> Self {
        let path = app.path().app_config_dir().ok().map(|d| d.join("shell.json"));
        let prefs = path
            .as_ref()
            .and_then(|p| fs::read_to_string(p).ok())
            .and_then(|s| serde_json::from_str(&s).ok())
            .unwrap_or_default();
        Self { prefs: Mutex::new(prefs), path, tray: AtomicBool::new(false) }
    }

    /// Close hides the window instead: asked for, and there is a tray to come back from.
    pub fn close_hides(&self) -> bool {
        self.prefs.lock().unwrap().close_to_tray && self.tray.load(Ordering::Relaxed)
    }

    fn save(&self, prefs: &ShellPrefs) {
        let Some(path) = &self.path else { return };
        if let Some(dir) = path.parent() {
            let _ = fs::create_dir_all(dir);
        }
        match serde_json::to_string_pretty(prefs) {
            Ok(json) => {
                if let Err(e) = fs::write(path, json) {
                    warn!("Could not save {path:?}: {e}");
                }
            }
            Err(e) => warn!("Could not save the shell preferences: {e}"),
        }
    }
}

#[derive(serde::Serialize)]
pub struct ShellPrefsView {
    close_to_tray: bool,
    /// Whether the system tray works here (else the option cannot apply).
    tray: bool,
}

#[tauri::command]
pub fn shell_prefs(state: tauri::State<'_, ShellPrefsState>) -> ShellPrefsView {
    ShellPrefsView {
        close_to_tray: state.prefs.lock().unwrap().close_to_tray,
        tray: state.tray.load(Ordering::Relaxed),
    }
}

#[tauri::command]
pub fn set_close_to_tray(state: tauri::State<'_, ShellPrefsState>, enabled: bool) {
    let prefs = {
        let mut prefs = state.prefs.lock().unwrap();
        prefs.close_to_tray = enabled;
        prefs.clone()
    };
    state.save(&prefs);
}
