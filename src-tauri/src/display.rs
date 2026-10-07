//! Which display backend GTK uses: native Wayland in a Wayland session, else X11.
//!
//! The AppImage's GTK hook sets GDK_BACKEND=x11 (after tauri#8541, a crash on Wayland back then),
//! and under XWayland on NVIDIA the window kept freezing (frames.rs); the AppImage on Wayland
//! worked here (2026-10-06), so `choose` sets GDK_BACKEND=wayland over the hook. The deb, rpm and
//! Flatpak already ran on Wayland: only the AppImage changes.
//!
//! In case Wayland still fails on some machine: a release build writes a marker
//! (`<config>/com.corpetty.mnemosyne/wayland-starting`) before starting on Wayland, and `started`
//! removes it once the window has been up for a while (and on exit). A marker found at the next
//! launch means that start died: `display-x11` is written next to it and every later launch uses
//! X11. `MNEMOSYNE_X11=1` forces X11; `MNEMOSYNE_WAYLAND=1` forces Wayland and forgets the
//! fallback.

use std::path::PathBuf;

const MARKER: &str = "wayland-starting";
const STICKY: &str = "display-x11";
/// Set by `choose` for the app: the marker to remove once up, and whether it fell back.
const MARKER_ENV: &str = "MNEMOSYNE_WAYLAND_MARKER";
const FELL_BACK_ENV: &str = "MNEMOSYNE_WAYLAND_FELL_BACK";

fn config_dir() -> Option<PathBuf> {
    let base = std::env::var_os("XDG_CONFIG_HOME")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".config")))?;
    Some(base.join("com.corpetty.mnemosyne"))
}

fn on(name: &str) -> bool {
    std::env::var_os(name).is_some_and(|v| v == "1")
}

/// Called first thing in main, before GTK starts.
pub fn choose() {
    if std::env::var_os("WAYLAND_DISPLAY").is_none() || on("MNEMOSYNE_X11") {
        return; // X11 (or what the environment says)
    }
    let forced = on("MNEMOSYNE_WAYLAND");
    // Development runs restart the app at will: no fallback there.
    let dir = config_dir().filter(|_| cfg!(not(debug_assertions)));
    let choice = match dir {
        Some(dir) => decide(&dir, forced),
        None => Choice { wayland: true, fell_back: false, marker: None },
    };
    if choice.fell_back {
        std::env::set_var(FELL_BACK_ENV, "1");
    }
    if let Some(marker) = choice.marker {
        std::env::set_var(MARKER_ENV, marker);
    }
    if choice.wayland {
        std::env::set_var("GDK_BACKEND", "wayland");
    }
}

#[derive(Debug, PartialEq)]
struct Choice {
    wayland: bool,
    fell_back: bool,      // the last start on Wayland died: from now on X11
    marker: Option<PathBuf>, // to remove once the window has stayed up
}

/// The fallback's bookkeeping in `dir` (see the module comment).
fn decide(dir: &std::path::Path, forced: bool) -> Choice {
    let (marker, sticky) = (dir.join(MARKER), dir.join(STICKY));
    let mut fell_back = false;
    if forced {
        let _ = std::fs::remove_file(&sticky);
    } else if marker.exists() {
        let _ = std::fs::write(
            &sticky,
            "The app did not come up on Wayland; it uses X11 from now on.\n\
             Start it once with MNEMOSYNE_WAYLAND=1 to try Wayland again.\n",
        );
        let _ = std::fs::remove_file(&marker);
        fell_back = true;
    }
    if !forced && sticky.exists() {
        return Choice { wayland: false, fell_back, marker: None };
    }
    let written = std::fs::create_dir_all(dir).is_ok() && std::fs::write(&marker, "").is_ok();
    Choice { wayland: true, fell_back, marker: written.then_some(marker) }
}

/// The window is up and has stayed up: Wayland works here. Also called on exit, so quitting
/// soon after a start does not count as a failed one.
pub fn started() {
    if let Some(marker) = std::env::var_os(MARKER_ENV) {
        let _ = std::fs::remove_file(marker);
    }
}

/// For the log, once logging is set up.
pub fn describe() -> String {
    let backend = std::env::var("GDK_BACKEND").unwrap_or_default();
    if std::env::var_os(FELL_BACK_ENV).is_some() {
        "The last start on Wayland did not come up: using X11 from now on \
         (MNEMOSYNE_WAYLAND=1 tries Wayland again)"
            .into()
    } else if backend == "wayland" {
        "Display: Wayland".into()
    } else {
        format!("Display: {}", if backend.is_empty() { "default" } else { &backend })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_start_that_never_came_up_falls_back_to_x11_for_good() {
        let dir = std::env::temp_dir().join(format!("mnemosyne-display-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);

        // First start: Wayland, with a marker until the window has stayed up.
        let first = decide(&dir, false);
        assert!(first.wayland && !first.fell_back);
        assert!(first.marker.as_ref().unwrap().exists());
        // It came up: the next start is Wayland again.
        std::fs::remove_file(first.marker.unwrap()).unwrap();
        assert!(decide(&dir, false).wayland);

        // That one died before the window was up (its marker is still there): X11 from now on.
        let fell = decide(&dir, false);
        assert_eq!((fell.wayland, fell.fell_back, fell.marker), (false, true, None));
        let later = decide(&dir, false);
        assert_eq!((later.wayland, later.fell_back), (false, false));

        // MNEMOSYNE_WAYLAND=1 tries Wayland again and forgets the fallback.
        let forced = decide(&dir, true);
        assert!(forced.wayland && forced.marker.is_some());
        std::fs::remove_file(forced.marker.unwrap()).unwrap();
        assert!(decide(&dir, false).wayland);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
