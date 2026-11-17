"""Auto-record: spotting meeting apps that capture audio."""

import asyncio
import json
import stat
import sys

import pytest

from mnemosyne.api import context
from mnemosyne.audio import coreaudio
from mnemosyne.audio.streams import capture_apps


def _node(i, cls, name=None, binary=None):
    props = {"media.class": cls}
    if name:
        props["application.name"] = name
    if binary:
        props["application.process.binary"] = binary
    return {"id": i, "type": "PipeWire:Interface:Node", "info": {"props": props}}


DUMP = [
    _node(1, "Audio/Source", "ALSA"),
    _node(2, "Stream/Input/Audio", "ZOOM VoiceEngine", "zoom"),
    _node(3, "Stream/Input/Audio", "ZOOM VoiceEngine", "zoom"),  # second zoom stream
    _node(4, "Stream/Input/Audio", "Firefox", "firefox"),
    _node(5, "Stream/Input/Audio", "Mnemosyne", "pw-record"),  # our own recorder
    _node(6, "Stream/Input/Audio", "easyeffects", "easyeffects"),
    _node(7, "Stream/Output/Audio", "Spotify", "spotify"),  # playback is not a meeting
    _node(8, "Stream/Input/Audio", "Some Recorder", "rec"),
    {"id": 9, "type": "PipeWire:Interface:Link", "info": {}},
]


def test_capture_apps_filters_and_labels():
    apps = capture_apps(DUMP, ignore={"easyeffects"})
    assert [(a.app, a.binary, a.node_id) for a in apps] == [
        ("Zoom", "zoom", 2),
        ("Firefox", "firefox", 4),
        ("Some Recorder", "rec", 8),
    ]
    assert [a.app for a in capture_apps(DUMP, ignore={"easyeffects", "some recorder"})] == [
        "Zoom",
        "Firefox",
    ]


def test_poll_publishes_changes(ctx):
    q = ctx.bus.subscribe()
    ctx.poll_capture_apps(DUMP)
    started = []
    while not q.empty():
        started.append(q.get_nowait())
    assert [(e["status"], e["app"]) for e in started] == [
        ("started", "Firefox"),
        ("started", "Some Recorder"),
        ("started", "Zoom"),
    ]
    ctx.poll_capture_apps([n for n in DUMP if n["id"] != 2 and n["id"] != 3])
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    assert [(e["type"], e["status"], e["app"]) for e in events] == [
        ("meeting_app", "stopped", "Zoom")
    ]


def test_apps_route(client, ctx):
    ctx.poll_capture_apps(DUMP)
    assert "Zoom" in [a["app"] for a in client.get("/api/audio/apps").json()]


def test_monitor_runs_after_startup_and_settings_do_not_add_loops(client, ctx):
    task = ctx._apps_task
    assert task is not None and not task.done()
    r = client.put("/api/settings", json={"auto_record": "ask"})
    assert r.status_code == 200, r.text
    assert ctx._apps_task is task  # saving settings must not start a second poller


# ---- macOS: the capture sidecar's `apps` (Core Audio process objects) ----------------------
# These run on every OS: `sys.platform` is monkeypatched and the sidecar is a stub.

PROCS = [
    {"pid": 501, "bundle_id": "us.zoom.xos", "name": "zoom.us"},
    {"pid": 502, "bundle_id": "com.google.Chrome.helper", "name": "Google Chrome Helper"},
    {"pid": 503, "bundle_id": "com.google.Chrome.helper.renderer", "name": "Google Chrome Helper"},
    {"pid": 504, "bundle_id": "com.apple.WebKit.GPU", "name": "com.apple.WebKit.GPU"},
    {"pid": 505, "bundle_id": "", "name": "mnemosyne-capture"},  # our recorder
    {"pid": 506, "bundle_id": "com.corpetty.mnemosyne", "name": "mnemosyne"},  # the app
    {"pid": 507, "bundle_id": "com.obsproject.obs-studio", "name": "OBS"},  # ignored by name
    {"pid": 508, "bundle_id": "com.microsoft.teams2", "name": "MSTeams"},
    {"pid": 509, "bundle_id": "", "name": "sox"},  # a command-line tool
    {"pid": 510, "bundle_id": "org.example.Recorder", "name": ""},
    {"pid": 511, "bundle_id": "", "name": ""},  # nothing to name it by
    {"bundle_id": "us.zoom.xos"},  # no pid
    "junk",
]


def test_mac_processes_are_named_by_bundle_id():
    apps = capture_apps(ignore={"obs"}, processes=PROCS)
    assert [(a.app, a.binary, a.node_id) for a in apps] == [
        ("Zoom", "zoom.us", 501),
        ("Chrome", "Google Chrome Helper", 502),  # helpers named after their app, once
        ("Browser call", "com.apple.WebKit.GPU", 504),  # WebKit: Safari or any web view
        ("Teams", "MSTeams", 508),
        ("sox", "sox", 509),
        ("org.example.Recorder", "org.example.Recorder", 510),
    ]
    # Ignoring works by label, bundle id or executable name.
    ignored = {"obs", "chrome", "us.zoom.xos", "msteams", "sox", "browser call"}
    assert [a.app for a in capture_apps(ignore=ignored, processes=PROCS)] == [
        "org.example.Recorder"
    ]


def test_mac_bundle_ids_match_case_insensitively_and_by_whole_prefix():
    procs = [
        {"pid": 1, "bundle_id": "COM.APPLE.SAFARI", "name": "Safari"},
        {"pid": 2, "bundle_id": "com.microsoft.teams2.helper", "name": "Teams Helper"},
        {"pid": 3, "bundle_id": "com.google.chromecast", "name": "cast"},  # not com.google.chrome.
        {"pid": 4, "bundle_id": "org.mozilla.plugincontainer", "name": "plugin-container"},
    ]
    assert [a.app for a in capture_apps(processes=procs)] == ["Safari", "Teams", "cast", "Firefox"]


def _mac(monkeypatch, tmp_path, apps_json: str, code: int = 0):
    """A pretend Mac whose sidecar answers `apps` with `apps_json`."""
    stub = tmp_path / "mnemosyne-capture"
    stub.write_text(
        f"""#!/bin/sh
[ "$1" = apps ] || {{ echo "unexpected $1" >&2; exit 2; }}
cat <<'JSON'
{apps_json}
JSON
exit {code}
"""
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("MNEMOSYNE_CAPTURE_BIN", str(stub))


def test_on_darwin_capture_apps_asks_the_sidecar(monkeypatch, tmp_path):
    _mac(monkeypatch, tmp_path, json.dumps(PROCS[:5]))
    assert [a.app for a in capture_apps()] == ["Zoom", "Chrome", "Browser call"]


def test_a_failing_sidecar_raises_so_the_poll_keeps_its_last_answer(monkeypatch, tmp_path):
    _mac(monkeypatch, tmp_path, '{"not": "a list"}')
    with pytest.raises(ValueError):
        coreaudio.capture_processes()
    _mac(monkeypatch, tmp_path, "[]", code=2)
    with pytest.raises(RuntimeError, match="exit 2"):
        coreaudio.capture_processes()
    monkeypatch.setenv("MNEMOSYNE_CAPTURE_BIN", str(tmp_path / "missing"))
    with pytest.raises(FileNotFoundError):
        capture_apps()


def test_the_poll_loop_reads_the_sidecar_on_darwin(monkeypatch, tmp_path, ctx):
    monkeypatch.setattr(sys, "platform", "darwin")
    calls = []

    def fake_processes(timeout: float = 5):
        calls.append(timeout)
        return PROCS[:2]

    def no_pw_dump():
        raise AssertionError("pw-dump on a Mac")

    monkeypatch.setattr(coreaudio, "capture_processes", fake_processes)
    monkeypatch.setattr(context, "_pw_dump", no_pw_dump)
    ctx.settings.auto_record = "ask"
    q = ctx.bus.subscribe()

    async def run():
        task = asyncio.create_task(ctx._apps_loop(0.01))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if ctx.capture_apps_now:
                break
        task.cancel()

    asyncio.run(run())
    assert calls
    assert [a.app for a in ctx.capture_apps_now] == ["Zoom", "Chrome"]
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    assert [(e["status"], e["app"]) for e in events] == [("started", "Chrome"), ("started", "Zoom")]


def test_apples_system_services_never_count_as_a_meeting():
    """Siri and dictation open the microphone without a meeting; FaceTime and Safari count."""
    procs = [
        {"pid": 601, "bundle_id": "com.apple.corespeechd", "name": "corespeechd"},
        {"pid": 602, "bundle_id": "com.apple.assistantd", "name": "assistantd"},
        {"pid": 603, "bundle_id": "com.apple.FaceTime", "name": "FaceTime"},
        {"pid": 604, "bundle_id": "com.apple.Safari", "name": "Safari"},
    ]
    assert [a.app for a in capture_apps(processes=procs)] == ["FaceTime", "Safari"]
