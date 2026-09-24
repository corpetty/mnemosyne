"""Which other apps are capturing audio right now (a meeting app, or a browser in a call).

PipeWire lists every recording stream as a `Stream/Input/Audio` node with the app's name and
binary. Our own recordings are named "Mnemosyne" (see capture.build_record_command)."""

from __future__ import annotations

import json
import subprocess

from ..models.base import ApiModel

# Friendly names for common meeting apps and browsers, by binary or application.name.
KNOWN = {
    "zoom": "Zoom",
    "zoom voiceengine": "Zoom",
    "teams": "Teams",
    "teams-for-linux": "Teams",
    "slack": "Slack",
    "discord": "Discord",
    "signal-desktop": "Signal",
    "skypeforlinux": "Skype",
    "firefox": "Firefox",
    "chrome": "Chrome",
    "google chrome": "Chrome",
    "chromium": "Chromium",
    "chromium-browser": "Chromium",
    "brave": "Brave",
    "vivaldi": "Vivaldi",
    "webrtc voiceengine": "Browser call",
    "element": "Element",
    "jitsi meet": "Jitsi",
}

OURS = {"mnemosyne"}


class CaptureApp(ApiModel):
    app: str  # display name
    binary: str
    node_id: int


def _label(name: str, binary: str) -> str:
    for key in (binary.lower(), name.lower()):
        if key in KNOWN:
            return KNOWN[key]
    return name or binary


def capture_apps(dump: list | None = None, ignore: set[str] = frozenset()) -> list[CaptureApp]:
    """Other apps with an open recording stream, one entry per app."""
    if dump is None:
        out = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5, check=True)
        dump = json.loads(out.stdout)
    ignored = {i.casefold() for i in ignore} | OURS
    apps: dict[str, CaptureApp] = {}
    for obj in dump:
        if not str(obj.get("type", "")).endswith("Node"):
            continue
        props = (obj.get("info") or {}).get("props") or {}
        if props.get("media.class") != "Stream/Input/Audio":
            continue
        name = str(props.get("application.name") or "")
        binary = str(props.get("application.process.binary") or "")
        if {name.casefold(), binary.casefold()} & ignored or not (name or binary):
            continue
        label = _label(name, binary)
        if label.casefold() in ignored:
            continue
        apps.setdefault(label, CaptureApp(app=label, binary=binary, node_id=int(obj["id"])))
    return list(apps.values())
