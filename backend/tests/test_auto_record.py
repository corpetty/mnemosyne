"""Auto-record: spotting meeting apps that capture audio."""

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
