"""Recording start/stop with PipeWire and ffmpeg replaced by fakes."""

from pathlib import Path

import pytest
from src.mnemosyne.api.routes import audio as audio_routes
from src.mnemosyne.audio.capture import AudioDevice, RecordingProcess, RecordingSession

from tests.conftest import drain_until_job


class _FakeProc:
    returncode = 0


@pytest.fixture
def fake_pipewire(monkeypatch, tmp_path):
    devices = [
        AudioDevice(id=1, name="mic", description="Built-in Mic", media_class="Audio/Source"),
        AudioDevice(id=2, name="spk", description="Speakers", media_class="Audio/Sink"),
    ]

    async def start_recording(device_ids, output_dir, **_):
        output_dir.mkdir(parents=True, exist_ok=True)
        session = RecordingSession(session_id="rec00001", output_dir=output_dir)
        for d in device_ids:
            session.processes.append(
                RecordingProcess(
                    device_id=d, process=_FakeProc(), output_path=output_dir / f"dev{d}.wav"
                )
            )
        session.is_recording = True
        return session

    async def stop_recording(session):
        session.is_recording = False
        files = []
        for p in session.processes:
            path = p.output_path.with_suffix(".ogg")
            path.write_bytes(b"ogg")
            files.append(path)
        return files

    def mix_audio_files(inputs, output):
        output = Path(output).with_suffix(".ogg")
        output.write_bytes(b"mixed")
        return output

    monkeypatch.setattr(audio_routes, "list_devices", lambda: devices)
    monkeypatch.setattr(audio_routes, "start_recording", start_recording)
    monkeypatch.setattr(audio_routes, "stop_recording", stop_recording)
    monkeypatch.setattr(audio_routes, "mix_audio_files", mix_audio_files)
    return devices


def test_start_requires_devices(client, fake_pipewire):
    assert client.post("/api/audio/start", json={"device_ids": []}).status_code == 400


def test_start_stop_records_sources_and_queues_job(client, ctx, fake_pipewire, fake_engine):
    started = client.post("/api/audio/start", json={"device_ids": [1, 2]}).json()
    sid = started["session_id"]
    assert client.get(f"/api/sessions/{sid}").json()["status"] == "recording"
    assert client.get(f"/api/audio/status/{sid}").json()["device_count"] == 2
    assert (
        client.post("/api/audio/start", json={"device_ids": [1], "session_id": sid}).status_code
        == 409
    )

    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        stopped = client.post(f"/api/audio/stop/{sid}").json()
        assert stopped["job_id"]
        drain_until_job(ws, stopped["job_id"])

    session = stopped["session"]
    assert session["audio_file"].endswith("rec00001_mixed.ogg")
    assert [(r["source"], r["device_name"]) for r in session["recordings"]] == [
        ("mic", "Built-in Mic"),
        ("system", "Speakers"),
    ]
    # Per-source transcription: mic labelled as the local speaker, system diarized.
    assert fake_engine.transcribed_paths == [r["path"] for r in session["recordings"]]
    assert [(s.kind, s.speaker_label) for s in fake_engine.sources[0]] == [
        ("mic", "Me"),
        ("system", None),
    ]
    assert client.get(f"/api/sessions/{sid}").json()["status"] == "completed"
    assert client.get(f"/api/audio/status/{sid}").json()["exists"] is False


def test_stop_without_transcribe(client, ctx, fake_pipewire):
    sid = client.post("/api/audio/start", json={"device_ids": [1]}).json()["session_id"]
    stopped = client.post(f"/api/audio/stop/{sid}", json={"transcribe": False}).json()
    assert stopped["job_id"] is None
    assert stopped["session"]["status"] == "created"
    assert client.get("/api/jobs").json() == []


def test_auto_transcribe_setting_respected(client, ctx, fake_pipewire):
    ctx.settings.auto_transcribe = False
    sid = client.post("/api/audio/start", json={"device_ids": [1]}).json()["session_id"]
    assert client.post(f"/api/audio/stop/{sid}").json()["job_id"] is None


def test_stop_unknown_is_404(client, fake_pipewire):
    assert client.post("/api/audio/stop/nope").status_code == 404
