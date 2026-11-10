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


def _system_silence_health(seconds_per_read=30.0, reads=4, **kw):
    from mnemosyne.audio.health import SILENT, SILENT_HINT

    h = CaptureHealth({1: "Mic", 2: "System audio"})
    changes = [h.observe_silence(2, seconds_per_read, True, **kw) for _ in range(reads)]
    return h, changes, SILENT, SILENT_HINT


def test_two_minutes_of_exact_zeros_hint_at_the_permission_and_any_sample_clears_it():
    h, changes, silent, hint = _system_silence_health()
    assert changes[:3] == [None, None, None]  # 90 s: not yet
    assert changes[3].state == silent and changes[3].message == hint
    assert (
        hint == "System audio is silent: check Privacy & Security → Screen & System Audio Recording"
    )
    assert h.problems() == {2: silent}
    assert h.observe_silence(2, 30, True) is None  # said once
    change = h.observe_silence(2, 0.25, False)  # one non-zero sample in the chunk
    assert change.state == OK and h.problems() == {}
    assert h.observe_silence(2, 100, True) is None  # the run starts over


def test_a_stopped_source_keeps_its_stronger_state():
    h = CaptureHealth({2: "System audio"}, clock=lambda: 0.0)
    h.update(2, 0, exited=True)
    assert h.observe_silence(2, 500, True) is None and h.problems() == {2: STOPPED}


def _wav(path, seconds, nonzero_at=None, rate=16000):
    import wave

    import numpy as np

    pcm = np.zeros(int(seconds * rate), dtype=np.int16)
    if nonzero_at is not None:
        pcm[nonzero_at] = 1
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def _run_stream(tmp_path, monkeypatch, platform, source, wav_kw, ctx):
    """Run _stream_levels over one WAV and return the capture_health events it published."""
    import asyncio
    from types import SimpleNamespace

    from mnemosyne.api.routes import audio as routes
    from mnemosyne.audio.capture import RecordingProcess, RecordingSession

    wav = tmp_path / "s.wav"
    _wav(wav, **wav_kw)
    proc = RecordingProcess(
        device_id=2,
        process=SimpleNamespace(returncode=None),
        output_path=wav,
        source=source,
    )
    rec = RecordingSession("sid", tmp_path, [proc], is_recording=True)
    monkeypatch.setattr(routes.sys, "platform", platform)
    events = []
    monkeypatch.setattr(ctx.bus, "publish", events.append)

    async def go():
        task = asyncio.create_task(routes._stream_levels(ctx, "sid", rec, interval=0.01))
        await asyncio.sleep(0.2)
        task.cancel()
        await task

    asyncio.run(go())
    return [e for e in events if e["type"] == "capture_health"]


def test_stream_hints_only_for_system_audio_on_a_mac(tmp_path, monkeypatch, ctx):
    from mnemosyne.audio.health import SILENT_HINT

    zeros = {"seconds": 130}
    (event,) = _run_stream(tmp_path, monkeypatch, "darwin", "system", zeros, ctx)
    assert (
        event["state"] == "silent" and event["message"] == SILENT_HINT and event["device_id"] == 2
    )
    assert _run_stream(tmp_path, monkeypatch, "linux", "system", zeros, ctx) == []
    assert _run_stream(tmp_path, monkeypatch, "darwin", "mic", zeros, ctx) == []
    quiet = {"seconds": 130, "nonzero_at": 5}
    assert _run_stream(tmp_path, monkeypatch, "darwin", "system", quiet, ctx) == []
