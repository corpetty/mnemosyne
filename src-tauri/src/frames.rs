//! The window can stop showing new frames for good under XWayland on NVIDIA (the AppImage runs
//! there: its GTK hook sets GDK_BACKEND=x11). Seen 2026-10-05 with WebKit compositing already
//! off: the page kept running and took clicks (the main loop and the web process woke hundreds
//! of times a second), the backend answered, yet nothing new reached the screen; reloading the
//! page, minimizing and restoring, and resizing did not help. GTK 3 waits for the compositor's
//! _NET_WM_FRAME_DRAWN after each frame before it draws the next (frame sync); one that never
//! comes freezes the window's frame clock while everything else goes on.
//!
//! - The app now runs natively on Wayland when it can (main.rs); this is for X11 sessions and
//!   `MNEMOSYNE_X11=1`.
//! - Frame sync is turned off for the main window under X11, and again whenever it gets a new
//!   X window (`MNEMOSYNE_FRAME_SYNC=1` leaves it on, to compare).
//! - "Redraw window" (tray) and `mnemosyne --redraw` give the window a new X window, and with it
//!   a new frame clock: a way out without a restart if it happens anyway.

use gtk::glib::translate::ToGlibPtr;
use gtk::prelude::*;
use log::{info, warn};
use tauri::{AppHandle, Manager};

extern "C" {
    // GTK 3 public API (gdk/x11/gdkx11window.h): "can be used to disable frame
    // synchronization for a window". No Rust binding for gdkx11 in our dependency tree.
    fn gdk_x11_window_set_frame_sync_enabled(
        window: *mut gtk::gdk::ffi::GdkWindow,
        frame_sync_enabled: gtk::glib::ffi::gboolean,
    );
}

fn frame_sync_off(window: &gtk::ApplicationWindow) {
    let Some(gdk_window) = window.window() else {
        return; // not realized yet: the realize handler does it
    };
    if gdk_window.display().type_().name() != "GdkX11Display" {
        return; // native Wayland: no such handshake
    }
    unsafe { gdk_x11_window_set_frame_sync_enabled(gdk_window.to_glib_none().0, 0) };
    info!("X11 frame sync off for the window");
}

/// Turn frame sync off for the main window, now and after any re-realize. Main thread.
pub fn setup(app: &AppHandle) {
    if std::env::var_os("MNEMOSYNE_FRAME_SYNC").is_some_and(|v| v == "1") {
        info!("X11 frame sync left on (MNEMOSYNE_FRAME_SYNC=1)");
        return;
    }
    let Some(window) = app.get_webview_window("main") else {
        return;
    };
    match window.gtk_window() {
        Ok(gtk_window) => {
            frame_sync_off(&gtk_window);
            gtk_window.connect_realize(frame_sync_off);
        }
        Err(e) => warn!("No GTK window to turn frame sync off for: {e}"),
    }
}

/// Give the main window a new X window (hide, unrealize, show): a frozen frame clock goes with
/// the old one. The page and its state stay as they are.
pub fn redraw(app: &AppHandle) {
    let handle = app.clone();
    let _ = app.run_on_main_thread(move || {
        let Some(window) = handle.get_webview_window("main") else {
            return;
        };
        let Ok(gtk_window) = window.gtk_window() else {
            return;
        };
        warn!("Redrawing the window on a new X window");
        // A new X window starts at 2x2 somewhere off screen: put it back where it was.
        let (width, height) = gtk_window.size();
        let (x, y) = gtk_window.position();
        let maximized = gtk_window.is_maximized();
        gtk_window.hide();
        gtk_window.unrealize();
        gtk_window.resize(width.max(400), height.max(300));
        gtk_window.move_(x, y);
        gtk_window.show();
        if maximized {
            gtk_window.maximize();
        }
        gtk_window.present();
    });
}
