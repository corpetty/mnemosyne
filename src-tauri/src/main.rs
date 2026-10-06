// Prevents additional console window on Windows in release, DO NOT REMOVE!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    // WebKitGTK's DMA-BUF renderer fails on NVIDIA + Wayland ("Failed to create GBM
    // buffer") and the window stays white. Must be set before WebKit initialises.
    // Users can still override by exporting the variable themselves.
    if std::env::var_os("WEBKIT_DISABLE_DMABUF_RENDERER").is_none() {
        std::env::set_var("WEBKIT_DISABLE_DMABUF_RENDERER", "1");
    }
    // On NVIDIA, WebKit's GPU compositing can stop presenting frames for good after the
    // window changes workspace or monitor (seen under XWayland, which the AppImage uses):
    // the page keeps running but the window never repaints, even when resized, until a
    // restart. Compositing on the CPU avoids that path and is ample for this UI.
    if std::env::var_os("WEBKIT_DISABLE_COMPOSITING_MODE").is_none()
        && std::path::Path::new("/proc/driver/nvidia/version").exists()
    {
        std::env::set_var("WEBKIT_DISABLE_COMPOSITING_MODE", "1");
    }
    // Native Wayland in a Wayland session. The AppImage's GTK hook sets GDK_BACKEND=x11 (after
    // tauri#8541, a crash on Wayland back then), and under XWayland on NVIDIA the window kept
    // freezing (frames.rs); the AppImage on Wayland worked (2026-10-06). The deb, rpm and
    // Flatpak already ran on Wayland: only the AppImage changes. MNEMOSYNE_X11=1 keeps XWayland.
    if std::env::var_os("WAYLAND_DISPLAY").is_some()
        && !std::env::var_os("MNEMOSYNE_X11").is_some_and(|v| v == "1")
    {
        std::env::set_var("GDK_BACKEND", "wayland");
    }
    mnemosyne_lib::run();
}
