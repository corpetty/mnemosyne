#!/usr/bin/env python3
"""Check that an extracted AppImage can play audio with its own GStreamer.

WebKitGTK plays <audio> through GStreamer. 0.7.1 bundled the GStreamer core but no plugins, and
the host's plugins do not load into it, so WebKit's web process died on the first <audio>
element. Loads the bundled libgstreamer with the environment the AppImage's gstreamer hook sets
and looks up every element audio playback needs.

Usage: check-appimage-gstreamer.py <squashfs-root>
"""

import ctypes
import os
import subprocess
import sys
from pathlib import Path

ELEMENTS = [
    "autoaudiosink",
    "pulsesink",
    "playbin",
    "uridecodebin",
    "souphttpsrc",
    "wavparse",
    "oggdemux",
    "opusdec",
    "audioconvert",
    "audioresample",
]


def main() -> int:
    root = Path(sys.argv[1]).resolve()
    hook = root / "apprun-hooks" / "linuxdeploy-plugin-gstreamer.sh"
    plugins = root / "usr" / "lib" / "gstreamer-1.0"
    problems = []
    if not hook.exists():
        problems.append(f"missing {hook.relative_to(root)}")
    count = len(list(plugins.glob("libgst*.so"))) if plugins.is_dir() else 0
    if os.environ.get("_GST_CHILD") != "1":
        print(f"{count} GStreamer plugins bundled")
    if count == 0:
        problems.append("no GStreamer plugins bundled (bundleMediaFramework off?)")
    if problems:
        print("\n".join(problems))
        return 1

    if os.environ.get("_GST_CHILD") != "1":
        # Re-run in a clean environment with the hook's variables, like the AppImage does.
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": os.environ.get("HOME", "/tmp"),
            "APPDIR": str(root),
            "LD_LIBRARY_PATH": str(root / "usr" / "lib"),
            "GST_REGISTRY_REUSE_PLUGIN_SCANNER": "no",
            "GST_REGISTRY_1_0": str(root.parent / "gst-registry.bin"),
            "GST_PLUGIN_SYSTEM_PATH_1_0": str(plugins),
            "GST_PLUGIN_PATH_1_0": str(plugins),
            "GST_PLUGIN_SCANNER_1_0": str(
                root / "usr/lib/gstreamer1.0/gstreamer-1.0/gst-plugin-scanner"
            ),
            "_GST_CHILD": "1",
        }
        return subprocess.run([sys.executable, __file__, str(root)], env=env).returncode

    gst = ctypes.CDLL(str(root / "usr" / "lib" / "libgstreamer-1.0.so.0"))
    gst.gst_init(None, None)
    gst.gst_element_factory_find.restype = ctypes.c_void_p
    missing = [e for e in ELEMENTS if not gst.gst_element_factory_find(e.encode())]
    for e in ELEMENTS:
        print(f"  {e}: {'MISSING' if e in missing else 'ok'}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
