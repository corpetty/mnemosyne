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
    // Native Wayland in a Wayland session, with a fallback to X11 if it fails (display.rs).
    #[cfg(target_os = "linux")]
    mnemosyne_lib::display::choose();
    mnemosyne_lib::run();
}
