"""The backend ends with the desktop app, except to save a recording (api/app_watch.py), and a
second backend never touches the first one's recording (cli.bind, services/recovery.py)."""

import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from mnemosyne.api.app_watch import AppWatch
from mnemosyne.services.recovery import _recorders_writing, recorder_owner, stop_orphan_recorders


def _app():
    return subprocess.Popen(["sleep", "300"])


def _gone(proc):
    proc.kill()
    proc.wait()


@pytest.fixture
def watched(ctx):
    """An AppWatch on a stand-in app process, with a hand-driven clock."""
    app = _app()
    clock = [0.0]
    events = SimpleNamespace(exits=0, notes=[])

    def exit_():
        events.exits += 1

    watch = AppWatch(
        ctx,
        app.pid,
        grace=60,
        exit=exit_,
        notify=lambda summary, body: events.notes.append(summary),
        clock=lambda: clock[0],
    )
    ctx.app_watch = watch
    yield SimpleNamespace(watch=watch, app=app, clock=clock, events=events)
    _gone(app)


def test_an_idle_backend_ends_with_the_app(client, watched):
    client.portal.call(watched.watch.check)
    assert watched.events.exits == 0
    _gone(watched.app)
    client.portal.call(watched.watch.check)
    assert watched.events.exits == 1 and watched.watch.done


def test_jobs_are_finished_before_ending(client, ctx, watched, monkeypatch):
    running = [SimpleNamespace(kind="transcribe", is_terminal=False)]
    monkeypatch.setattr(ctx.jobs, "list", lambda **_: running)
    _gone(watched.app)
    client.portal.call(watched.watch.check)
    assert watched.events.exits == 0
    running.clear()
    client.portal.call(watched.watch.check)
    assert watched.events.exits == 1


def test_a_recording_waits_for_the_app_then_is_saved(client, ctx, watched, fake_pipewire):
    w = watched
    sid = client.post("/api/audio/start", json={"device_ids": [1]}).json()["session_id"]
    _gone(w.app)
    client.portal.call(w.watch.check)
    # Still recording, and the user is told.
    assert w.events.notes == ["Mnemosyne closed while recording"] and w.events.exits == 0
    assert sid in ctx.active_recordings

    # The app is relaunched and takes the backend over: the recording goes on.
    again = _app()
    try:
        assert client.post("/api/system/attach", json={"pid": again.pid}).json() == {
            "watching": True
        }
        w.clock[0] = 1000
        client.portal.call(w.watch.check)
        assert w.watch.orphaned_since is None and sid in ctx.active_recordings
    finally:
        _gone(again)

    # Gone again, and nobody comes back within the grace period: saved like Stop, then exit.
    client.portal.call(w.watch.check)
    w.clock[0] = 1059
    client.portal.call(w.watch.check)
    assert sid in ctx.active_recordings and w.events.exits == 0
    w.clock[0] = 1061
    client.portal.call(w.watch.check)
    assert sid not in ctx.active_recordings
    session = client.get(f"/api/sessions/{sid}").json()
    assert session["status"] == "created" and session["audio_file"]
    assert w.events.notes[-1] == "Mnemosyne saved the recording"
    assert w.events.exits == 1


def test_attach_without_a_watch_leaves_the_backend_independent(client):
    """Server mode, or a backend started by hand: an app does not get to end it."""
    assert client.post("/api/system/attach", json={"pid": 1}).json() == {"watching": False}


def test_the_app_attaches_without_the_api_token(settings, keystore):
    """The shell does not know `api_token` (pairing, remote access): without this, a relaunched
    app got a 401, the backend thought nobody came back and stopped the recording."""
    from fastapi.testclient import TestClient

    from mnemosyne.api.app import create_app

    settings.api_token = "s3cret"
    app = create_app(settings, keystore=keystore)
    old, again = _app(), _app()
    try:
        body = {"pid": again.pid}
        with TestClient(app, base_url="http://127.0.0.1:8008", client=("127.0.0.1", 50000)) as c:
            watch = app.state.ctx.app_watch = AppWatch(app.state.ctx, old.pid, exit=lambda: None)
            assert c.post("/api/system/attach", json=body).json() == {"watching": True}
            assert watch.pid == again.pid
            # The rest of the API still wants the token, and a proxy on this machine is not the app.
            assert c.get("/api/sessions").status_code == 401
            proxied = {"X-Forwarded-For": "100.64.0.7"}
            assert c.post("/api/system/attach", json=body, headers=proxied).status_code == 401
            # Someone else's process (init) does not get to end this backend.
            assert c.post("/api/system/attach", json={"pid": 1}).json() == {"watching": False}
            elsewhere = TestClient(app)  # not from 127.0.0.1 (no `with`: the app is running)
            assert elsewhere.post("/api/system/attach", json=body).status_code == 401
    finally:
        _gone(old)
        _gone(again)


def test_a_reconnecting_ui_finds_the_recording(client, fake_pipewire):
    assert client.get("/api/audio/active").json() == []
    before = time.time()
    sid = client.post("/api/audio/start", json={"device_ids": [1, 2]}).json()["session_id"]
    (active,) = client.get("/api/audio/active").json()
    assert active["session_id"] == sid and active["device_ids"] == [1, 2]
    assert active["part"] == 0 and before - 1 <= active["started_at"] <= time.time()
    health = client.get("/health").json()
    assert health["recording"] is True and isinstance(health["pid"], int)
    client.post(f"/api/audio/stop/{sid}", json={"transcribe": False})
    assert client.get("/api/audio/active").json() == []


def test_a_second_backend_does_not_start_on_a_taken_port():
    from mnemosyne.cli import bind

    first = bind("127.0.0.1", 0)
    try:
        with pytest.raises(SystemExit) as e:
            bind("127.0.0.1", first.getsockname()[1])
        assert e.value.code == 3
    finally:
        first.close()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_recovery_leaves_a_live_backends_recorders_alone(tmp_path):
    """A recorder whose backend lives is not interrupted; once its backend is gone, it is."""
    recorder = tmp_path / "pw-record"  # a stand-in: Python under pw-record's name
    recorder.symlink_to(sys.executable)
    wav = tmp_path / "rec_device_1.wav"
    code = "import time; time.sleep(60)"
    backend = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"import subprocess, time; subprocess.Popen([{str(recorder)!r}, '-c', {code!r}, "
            f"{str(wav)!r}]); time.sleep(60)",
        ]
    )
    try:
        deadline = time.monotonic() + 10
        while not _recorders_writing({str(wav)}):
            assert time.monotonic() < deadline, "the stand-in recorder did not start"
            time.sleep(0.05)
        assert recorder_owner([wav]) == backend.pid
    finally:
        _gone(backend)
    assert recorder_owner([wav]) is None  # orphaned now
    assert stop_orphan_recorders([wav]) == 1


def test_a_shared_computer_keeps_serving_after_the_app_quits(client, ctx, watched, monkeypatch):
    from mnemosyne.api import app_watch

    monkeypatch.setattr(app_watch, "outlives_app", lambda c: True)
    _gone(watched.app)
    client.portal.call(watched.watch.check)
    client.portal.call(watched.watch.check)
    assert watched.events.exits == 0 and not watched.watch.done
    assert watched.events.notes == ["Mnemosyne is still shared with your team"]  # once
    # The app comes back and quits again: told again.
    again = _app()
    client.post("/api/system/attach", json={"pid": again.pid})
    client.portal.call(watched.watch.check)
    _gone(again)
    client.portal.call(watched.watch.check)
    assert watched.events.notes.count("Mnemosyne is still shared with your team") == 2
    # Sharing stopped (from the app, or settings changed): ends with the app as before.
    monkeypatch.setattr(app_watch, "outlives_app", lambda c: False)
    client.portal.call(watched.watch.check)
    assert watched.events.exits == 1


def test_a_teammates_browser_recording_goes_on_while_serving(client, ctx, watched, monkeypatch):
    from mnemosyne.api import app_watch
    from mnemosyne.audio.capture import BrowserProcess, RecordingProcess, RecordingSession

    monkeypatch.setattr(app_watch, "outlives_app", lambda c: True)
    rec = RecordingSession(session_id="s1", output_dir=ctx.settings.data_dir, is_recording=True)
    rec.processes.append(
        RecordingProcess(
            device_id=-1, output_path=ctx.settings.data_dir / "x", process=BrowserProcess()
        )
    )
    ctx.active_recordings["s1"] = rec
    try:
        _gone(watched.app)
        watched.clock[0] = 10_000
        client.portal.call(watched.watch.check)
        client.portal.call(watched.watch.check)
        assert "s1" in ctx.active_recordings and watched.events.exits == 0
        assert "Mnemosyne closed while recording" not in watched.events.notes
    finally:
        ctx.active_recordings.pop("s1", None)
