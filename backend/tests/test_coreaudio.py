"""Recording on a Mac through the capture sidecar (audio/coreaudio.py).

`sys.platform` is monkeypatched to "darwin" and MNEMOSYNE_CAPTURE_BIN points at a stub script
that prints a sample macOS device list. The recording tests wrap the REAL sidecar (capture/,
`--fake tone` through MNEMOSYNE_CAPTURE_FAKE; skipped when it is not built, like
test_capture_sidecar.py) so the backend's own recorder machinery runs end to end on any OS.
"""

import asyncio
import json
import shutil
import stat
import sys
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from mnemosyne.audio import capture, coreaudio, levels
from mnemosyne.audio.capture import AudioDevice
from mnemosyne.transcription.live import WavTail
from tests.test_capture_sidecar import BIN

SAMPLE_DEVICES = [
    {
        "id": 71,
        "name": "BuiltInMicrophoneDevice",
        "description": "MacBook Pro Microphone",
        "media_class": "Audio/Source",
        "is_monitor": False,
        "is_echo_cancelled": False,
        "default": True,
    },
    {
        "id": 83,
        "name": "BuiltInSpeakerDevice",
        "description": "MacBook Pro Speakers",
        "media_class": "Audio/Sink",
        "is_monitor": False,
        "is_echo_cancelled": False,
        "default": True,
    },
    {
        "id": 1,
        "name": "system",
        "description": "System audio (all apps)",
        "media_class": "Audio/Sink",
        "is_monitor": False,
        "is_echo_cancelled": False,
        "default": False,
    },
]


def _script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _stub(tmp_path, *, devices=SAMPLE_DEVICES, doctor=None, delegate: Path | None = None) -> Path:
    """A stand-in `mnemosyne-capture`: `devices` and `doctor` print canned JSON; everything else
    runs `delegate` (the real binary) when given, else fails."""
    doctor = {"version": "0", "real_capture": True} if doctor is None else doctor
    rest = f'exec "{delegate}" "$@"' if delegate else "echo stub cannot record >&2; exit 2"
    return _script(
        tmp_path / "mnemosyne-capture",
        f"""case "$1" in
  devices) cat <<'JSON'
{json.dumps(devices)}
JSON
  exit 0;;
  doctor) cat <<'JSON'
{json.dumps(doctor)}
JSON
  exit 0;;
esac
{rest}
""",
    )


@pytest.fixture
def mac(monkeypatch, tmp_path):
    """A pretend Mac with a stub sidecar; no recorder log until a test configures one."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("MNEMOSYNE_CAPTURE_FAKE", raising=False)
    monkeypatch.setenv("MNEMOSYNE_CAPTURE_BIN", str(_stub(tmp_path)))
    monkeypatch.setattr(coreaudio, "_data_dir", None)
    return tmp_path


MIC = AudioDevice(
    id=71, name="BuiltInMicrophoneDevice", description="Mic", media_class="Audio/Source"
)
SYSTEM = AudioDevice(id=1, name="system", description="System", media_class="Audio/Sink")


# ---- finding the sidecar and listing devices ----------------------------------------------


def test_sidecar_from_env_else_path(monkeypatch, tmp_path):
    monkeypatch.setenv("MNEMOSYNE_CAPTURE_BIN", str(tmp_path / "nope"))
    assert coreaudio.sidecar() is None  # set but missing: not found, no fall back to PATH
    stub = _stub(tmp_path)
    monkeypatch.setenv("MNEMOSYNE_CAPTURE_BIN", str(stub))
    assert coreaudio.sidecar() == str(stub)
    monkeypatch.delenv("MNEMOSYNE_CAPTURE_BIN")
    monkeypatch.setenv("PATH", str(tmp_path))
    assert coreaudio.sidecar() == str(stub)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert coreaudio.sidecar() is None


def test_list_devices_parses_the_sidecars_json(mac):
    devices = capture.list_devices()  # the darwin switch in capture.py
    assert [(d.id, d.name, d.description, d.media_class) for d in devices] == [
        (71, "BuiltInMicrophoneDevice", "MacBook Pro Microphone", "Audio/Source"),
        (83, "BuiltInSpeakerDevice", "MacBook Pro Speakers", "Audio/Sink"),
        (1, "system", "System audio (all apps)", "Audio/Sink"),
    ]
    assert devices[0].is_input and not devices[0].is_output
    assert devices[2].is_output and not devices[2].is_input


def test_list_devices_skips_odd_entries(monkeypatch, mac):
    odd = [*SAMPLE_DEVICES[:1], {"id": "x"}, "nope", {"name": "a"}]
    monkeypatch.setenv("MNEMOSYNE_CAPTURE_BIN", str(_stub(mac, devices=odd)))
    assert [d.id for d in coreaudio.list_devices()] == [71]


def test_list_devices_never_raises(monkeypatch, mac, caplog):
    monkeypatch.setenv("MNEMOSYNE_CAPTURE_BIN", str(mac / "nope"))  # no sidecar
    assert coreaudio.list_devices() == []
    _script(mac / "crash", "echo boom >&2; exit 1\n")
    monkeypatch.setenv("MNEMOSYNE_CAPTURE_BIN", str(mac / "crash"))
    assert coreaudio.list_devices() == []
    _script(mac / "junk", "echo not json\n")
    monkeypatch.setenv("MNEMOSYNE_CAPTURE_BIN", str(mac / "junk"))
    assert coreaudio.list_devices() == []
    _script(mac / "dict", "echo '{}'\n")
    monkeypatch.setenv("MNEMOSYNE_CAPTURE_BIN", str(mac / "dict"))
    assert coreaudio.list_devices() == []
    assert "Cannot list audio devices" in caplog.text


def test_list_devices_times_out_quietly(monkeypatch, mac):
    def slow(*a, **k):
        raise coreaudio.subprocess.TimeoutExpired("mnemosyne-capture", 10)

    monkeypatch.setattr(coreaudio.subprocess, "run", slow)
    assert coreaudio.list_devices() == []


def test_doctor_parses_or_gives_none(monkeypatch, mac):
    assert coreaudio.doctor() == {"version": "0", "real_capture": True}
    assert coreaudio.real_capture() is True
    monkeypatch.setenv("MNEMOSYNE_CAPTURE_BIN", str(_stub(mac, doctor={"real_capture": False})))
    assert coreaudio.real_capture() is False
    monkeypatch.setenv("MNEMOSYNE_CAPTURE_BIN", str(mac / "nope"))
    assert coreaudio.doctor() is None and coreaudio.real_capture() is False


# ---- the command ------------------------------------------------------------------------


def test_build_record_command_for_a_microphone(mac):
    stub = coreaudio.sidecar()
    cmd = capture.build_record_command(MIC, Path("/r/a.wav"), 48000, 1, "s16", node_name="pw-only")
    assert cmd == [
        stub,
        "record",
        "--target",
        "BuiltInMicrophoneDevice",
        "--rate",
        "48000",
        "--channels",
        "1",
        "/r/a.wav",
    ]
    assert "pw-record" not in cmd and not any(p.startswith(("-P", "--format")) for p in cmd)


def test_build_record_command_for_system_audio(mac):
    cmd = capture.build_record_command(SYSTEM, Path("/r/s.wav"), 16000, 1, "s16")
    assert cmd[1:5] == ["record", "--target", "system", "--rate"] and cmd[5] == "16000"


def test_the_recorder_logs_to_the_data_dirs_capture_log(mac):
    coreaudio.configure(mac / "data")
    log = mac / "data" / "logs" / "capture.log"
    cmd = capture.build_record_command(MIC, Path("/r/a.wav"), 48000, 1, "s16")
    assert cmd[cmd.index("--log") + 1] == str(log) and cmd[-1] == "/r/a.wav"
    assert log.parent.is_dir()
    no_log = coreaudio.build_record_command(MIC, Path("/t.wav"), 48000, 1, log=False)
    assert "--log" not in no_log


def test_a_big_capture_log_is_rotated(mac):
    coreaudio.configure(mac / "data")
    log = coreaudio.log_path(mac / "data")
    log.parent.mkdir(parents=True)
    log.write_bytes(b"x" * (coreaudio.LOG_ROTATE_BYTES + 1))
    coreaudio.build_record_command(MIC, Path("/r/a.wav"), 48000, 1)
    assert not log.exists() and log.with_name("capture.log.1").exists()


def test_the_test_only_fake_source_replaces_the_target(monkeypatch, mac):
    monkeypatch.setenv("MNEMOSYNE_CAPTURE_FAKE", "tone")
    cmd = capture.build_record_command(MIC, Path("/r/a.wav"), 48000, 1, "s16")
    assert cmd[1:4] == ["record", "--fake", "tone"] and "--target" not in cmd


def test_linux_is_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("MNEMOSYNE_CAPTURE_BIN", str(_stub(tmp_path)))
    cmd = capture.build_record_command(SYSTEM, Path("/x.wav"), 48000, 1, "s16", node_name="n")
    assert cmd[0] == "pw-record" and "stream.capture.sink=true" in cmd[cmd.index("-P") + 1]


# ---- levels and the self-test -----------------------------------------------------------


@pytest.mark.anyio
async def test_self_test_plays_and_records_nothing_on_a_mac(monkeypatch, mac):
    async def no_processes(*a, **k):
        raise AssertionError(f"nothing may run on a Mac self-test: {a}")

    monkeypatch.setattr(levels.asyncio, "create_subprocess_exec", no_processes)
    result = await levels.self_test(SYSTEM)
    assert result.passed is False and result.tone_detected is False
    assert result.linked_from == [] and result.level.rms_db == levels.FLOOR_DB
    assert "watch the System audio meter" in result.message
    assert "pw-play" not in result.message


def test_linked_sources_is_empty_on_a_mac(monkeypatch, mac):
    def no_pw_link(*a, **k):
        raise AssertionError("pw-link must not run on a Mac")

    monkeypatch.setattr(levels.subprocess, "run", no_pw_link)
    assert levels.linked_sources("anything") == []


needs_sidecar = pytest.mark.skipif(BIN is None, reason="the capture sidecar is not built")


@pytest.fixture
def fake_mac(monkeypatch, mac):
    """A pretend Mac whose sidecar is the real binary recording its fake tone."""
    assert BIN is not None
    monkeypatch.setenv("MNEMOSYNE_CAPTURE_BIN", str(_stub(mac, delegate=BIN)))
    monkeypatch.setenv("MNEMOSYNE_CAPTURE_FAKE", "tone")
    coreaudio.configure(mac / "data")
    return mac


@needs_sidecar
@pytest.mark.anyio
async def test_sample_level_goes_through_the_sidecar(fake_mac):
    level = await levels.sample_level(MIC, seconds=1.0)
    assert level.peak_db > -30  # the tone, not silence
    assert not coreaudio.log_path(fake_mac / "data").exists()  # a meter leaves no log


async def _wait_for_samples(path: Path, count: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() or path.stat().st_size < 44 + 2 * count:
        assert time.monotonic() < deadline, f"{path} never reached {count} samples"
        await asyncio.sleep(0.05)


@needs_sidecar
@pytest.mark.anyio
async def test_a_recording_through_the_backends_recorder_machinery(fake_mac):
    out = fake_mac / "rec"
    session = await capture.start_recording([71, 1], out)  # mic and system, listed by the stub
    try:
        assert session.is_recording and len(session.processes) == 2
        paths = [p.output_path for p in session.processes]
        assert [p.name for p in paths] == [
            f"{session.session_id}_device_71.wav",
            f"{session.session_id}_device_1.wav",
        ]
        for path in paths:
            await _wait_for_samples(path, 9600)
            assert path.read_bytes()[:44] == capture.wav_header(48000, 1)  # pw-record's header
            tail = WavTail(path)  # what the live transcript and the meters read
            samples = tail.read_new()
            assert tail.sample_rate == 48000 and len(samples) >= 9600
            assert int(abs(samples).max()) > 1000
        assert all(p.process.returncode is None for p in session.processes)  # still going
    finally:
        await capture.stop_capture(session)

    assert not session.is_recording
    for p in session.processes:
        assert p.process.returncode == 0  # SIGTERM is a clean stop for the sidecar
        with wave.open(str(p.output_path)) as w:  # final sizes written
            assert w.getframerate() == 48000 and w.getnchannels() == 1
            frames = w.getnframes()
            assert frames >= 9600
            assert frames == (p.output_path.stat().st_size - 44) // 2
        pcm = np.frombuffer(p.output_path.read_bytes()[44:], dtype=np.int16)
        assert levels.level_of(pcm).peak_db > -30
    log = coreaudio.log_path(fake_mac / "data").read_text()
    assert log.count(" started") >= 2 and " stopped" in log


@needs_sidecar
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
@pytest.mark.anyio
async def test_stop_recording_ends_with_opus_files(fake_mac):
    session = await capture.start_recording([71], fake_mac / "rec")
    await _wait_for_samples(session.processes[0].output_path, 9600)
    opus = await capture.stop_recording(session)
    assert len(opus) == 1 and opus[0] is not None
    assert opus[0].suffix == ".ogg" and opus[0].stat().st_size > 0
    assert not session.processes[0].output_path.exists()  # the WAV is replaced


@needs_sidecar
@pytest.mark.anyio
async def test_a_device_that_is_not_listed_is_skipped(fake_mac):
    session = await capture.start_recording([999], fake_mac / "rec")
    assert session.processes == []


@needs_sidecar
def test_the_real_binary_lists_no_devices_here_and_its_doctor_parses(monkeypatch, tmp_path):
    """Outside a Mac the real sidecar has no devices (so [] is the answer, not an error)."""
    monkeypatch.setenv("MNEMOSYNE_CAPTURE_BIN", str(BIN))
    if sys.platform != "darwin":
        assert coreaudio.list_devices() == []
    report = coreaudio.doctor()
    assert report is not None and "real_capture" in report and "version" in report
    assert json.dumps(report)  # plain JSON


# ---- the system info and the diagnostics ------------------------------------------------


def test_system_info_on_a_mac(client, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("MNEMOSYNE_CAPTURE_BIN", str(tmp_path / "missing"))
    info = client.get("/api/system").json()
    assert info["capture"] is False
    assert info["capture_problem"] == (
        "Recording on a Mac needs the capture helper, which this build does not include."
    )

    monkeypatch.setenv(
        "MNEMOSYNE_CAPTURE_BIN", str(_stub(tmp_path, doctor={"real_capture": False}))
    )
    assert client.get("/api/system").json()["capture"] is False  # only the test sources

    monkeypatch.setenv("MNEMOSYNE_CAPTURE_BIN", str(_stub(tmp_path, doctor={"real_capture": True})))
    info = client.get("/api/system").json()
    assert info["capture"] is True and info["capture_problem"] is None


def test_diagnostics_on_a_mac_name_the_helper_and_tail_its_log(client, ctx, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    stub = _stub(tmp_path, doctor={"real_capture": True, "version": "9.9"})
    monkeypatch.setenv("MNEMOSYNE_CAPTURE_BIN", str(stub))
    log = coreaudio.log_path(ctx.settings.data_dir)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("".join(f"2026-11-09T10:00:{i:02d}Z stats buffers={i}\n" for i in range(30)))
    text = client.get("/api/system/diagnostics").json()["text"]
    assert f"capture helper: {stub}" in text
    assert 'capture doctor: {"real_capture": true, "version": "9.9"}' in text
    assert "### Capture log (last 20 lines)" in text
    assert "stats buffers=29" in text and "stats buffers=9\n" not in text
    assert "pw-record" not in text

    monkeypatch.setenv("MNEMOSYNE_CAPTURE_BIN", str(tmp_path / "missing"))
    log.unlink()
    text = client.get("/api/system/diagnostics").json()["text"]
    assert "capture helper: not found" in text and "(no capture log yet)" in text


def test_diagnostics_on_linux_have_no_capture_log(client):
    text = client.get("/api/system/diagnostics").json()["text"]
    assert "Capture log" not in text and "capture helper" not in text
