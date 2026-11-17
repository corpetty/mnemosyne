"""Which other apps are capturing audio right now (a meeting app, or a browser in a call).

Linux: PipeWire lists every recording stream as a `Stream/Input/Audio` node with the app's name
and binary. Our own recordings are named "Mnemosyne" (see capture.build_record_command).

macOS: the capture sidecar's `apps` (audio/coreaudio.py `capture_processes`) lists Core Audio's
process objects with input running, as `{pid, bundle_id, name}`; `node_id` is then the pid.
Apps are named by bundle id (`MAC_BUNDLES`). Browsers capture from helper processes
(`com.google.Chrome.helper`), named after their app by bundle-id prefix. WebKit's helpers
(`com.apple.WebKit.GPU`, ...) are shared by Safari and every app that embeds a web view, so they
are a "Browser call", not "Safari". Our own recorders are the sidecar (`mnemosyne-capture`)."""

from __future__ import annotations

import json
import subprocess
import sys

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

# macOS, by bundle id (casefolded). A helper whose bundle id starts with one of these and a
# dot (`com.google.chrome.helper.renderer`) gets the same name.
MAC_BUNDLES = {
    "us.zoom.xos": "Zoom",
    "com.microsoft.teams2": "Teams",
    "com.microsoft.teams": "Teams",
    "com.tinyspeck.slackmacgap": "Slack",
    "com.hnc.discord": "Discord",
    "com.apple.facetime": "FaceTime",
    # FaceTime's (and iPhone calls') conferencing daemon (guess: may be where the audio runs).
    "com.apple.avconferenced": "FaceTime",
    "cisco-systems.spark": "Webex",
    "com.cisco.webexmeetingsapp": "Webex",
    "com.webex.meetingmanager": "Webex",
    "ru.keepcoder.telegram": "Telegram",
    "org.telegram.desktop": "Telegram",
    "net.whatsapp.whatsapp": "WhatsApp",
    "desktop.whatsapp": "WhatsApp",
    "org.whispersystems.signal-desktop": "Signal",
    "com.skype.skype": "Skype",
    "im.riot.app": "Element",
    "com.google.chrome": "Chrome",
    "org.chromium.chromium": "Chromium",
    "com.apple.safari": "Safari",
    "org.mozilla.firefox": "Firefox",
    "org.mozilla.firefoxdeveloperedition": "Firefox",
    "org.mozilla.nightly": "Firefox",
    "org.mozilla.plugincontainer": "Firefox",  # Firefox's child processes
    "company.thebrowser.browser": "Arc",
    "com.brave.browser": "Brave",
    "com.microsoft.edgemac": "Edge",
    "com.vivaldi.vivaldi": "Vivaldi",
    "com.operasoftware.opera": "Opera",
}
WEBKIT_PREFIX = "com.apple.webkit."  # Safari's and every web view's helper processes

OURS = {"mnemosyne", "mnemosyne-capture", "com.corpetty.mnemosyne"}


class CaptureApp(ApiModel):
    app: str  # display name
    binary: str
    node_id: int


def _label(name: str, binary: str) -> str:
    for key in (binary.lower(), name.lower()):
        if key in KNOWN:
            return KNOWN[key]
    return name or binary


def _mac_label(bundle_id: str, name: str) -> str | None:
    """The app's name, or None for one of Apple's system services (Siri, dictation, …), which
    open the microphone without a meeting: only the Apple apps named above count."""
    bid = bundle_id.casefold()
    if bid in MAC_BUNDLES:
        return MAC_BUNDLES[bid]
    prefixes = [key for key in MAC_BUNDLES if bid.startswith(key + ".")]
    if prefixes:
        return MAC_BUNDLES[max(prefixes, key=len)]
    if bid.startswith(WEBKIT_PREFIX):
        return "Browser call"
    if bid.startswith("com.apple."):
        return None
    return _label(name, name) if name else bundle_id


def _from_processes(processes: list, ignored: set[str]) -> list[CaptureApp]:
    apps: dict[str, CaptureApp] = {}
    for proc in processes:
        if not isinstance(proc, dict):
            continue
        try:
            pid = int(proc["pid"])
        except (KeyError, TypeError, ValueError):
            continue
        bundle_id = str(proc.get("bundle_id") or "")
        name = str(proc.get("name") or "")
        if not (bundle_id or name):
            continue
        bid = bundle_id.casefold()
        if {bid, name.casefold()} & ignored or any(bid.startswith(o + ".") for o in OURS):
            continue
        label = _mac_label(bundle_id, name)
        if label is None or label.casefold() in ignored:
            continue
        apps.setdefault(label, CaptureApp(app=label, binary=name or bundle_id, node_id=pid))
    return list(apps.values())


def capture_apps(
    dump: list | None = None,
    ignore: set[str] = frozenset(),
    *,
    processes: list | None = None,
) -> list[CaptureApp]:
    """Other apps with an open recording stream, one entry per app.

    `processes` is the capture sidecar's `apps` output (macOS); `dump` is pw-dump's (Linux).
    Given neither, they are read here: from the sidecar on darwin, else from pw-dump."""
    ignored = {i.casefold() for i in ignore} | OURS
    if processes is None and dump is None and sys.platform == "darwin":
        from .coreaudio import capture_processes

        processes = capture_processes()
    if processes is not None:
        return _from_processes(processes, ignored)
    if dump is None:
        out = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5, check=True)
        dump = json.loads(out.stdout)
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
