"""A source that stops being captured mid-recording is noticed (audio/health.py), and the
recording can go on as the next part of the meeting (POST /api/audio/restart)."""

import time

from mnemosyne.audio.health import OK, STALLED, STOPPED, CaptureHealth


def test_data_not_level_is_what_counts():
    now = [0.0]
    h = CaptureHealth({1: "Mic", 2: "Speakers"}, stall_after=10, clock=lambda: now[0])
    now[0] = 9
    assert h.update(1, 0, exited=False) is None  # quiet for 9 s: fine
    now[0] = 10
    change = h.update(1, 0, exited=False)
    assert change.state == STALLED and change.message == "No audio from Mic for 10 s"
    assert h.update(1, 0, exited=False) is None  # said once
    assert h.problems() == {1: STALLED}
    change = h.update(1, 480, exited=False)  # digital silence is still data
    assert change.state == OK and change.message == "Mic is recording again"
    change = h.update(2, 480, exited=True)
    assert change.state == STOPPED and change.message == "Speakers stopped recording"
    now[0] = 100
    assert h.update(2, 0, exited=True) is None  # stopped stays stopped
    assert h.problems() == {2: STOPPED}


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def test_a_stopped_recorder_is_reported_and_the_recording_restarts(client, ctx, fake_pipewire):
    sid = client.post("/api/audio/start", json={"device_ids": [1, 2]}).json()["session_id"]
    ctx.copilot_notes[sid] = "notes so far"
    first = ctx.active_recordings[sid]
    assert first.node_names == {1: "mic", 2: "spk"} and first.labels[2] == "Speakers"

    first.processes[1].process.returncode = 1  # the Speakers recorder died
    _wait(lambda: client.get("/api/audio/active").json()[0]["problems"] == {"2": "stopped"})

    res = client.post(f"/api/audio/restart/{sid}")
    assert res.status_code == 200, res.text
    assert res.json()["session_id"] == sid
    second = ctx.active_recordings[sid]
    assert second is not first and second.part == 1
    assert [p.device_id for p in second.processes] == [1, 2]
    (active,) = client.get("/api/audio/active").json()
    assert active["problems"] == {} and active["part"] == 1
    session = client.get(f"/api/sessions/{sid}").json()
    assert session["audio_file"] and {r["part"] for r in session["recordings"]} == {0}
    assert ctx.copilot_notes.get(sid) == "notes so far"  # the same meeting carries on


def test_restart_needs_a_recording(client):
    assert client.post("/api/audio/restart/nope").status_code == 404
