"""pw-record command construction (regression: sinks must not fall back to the mic)."""

from pathlib import Path

from mnemosyne.audio.capture import AudioDevice, build_record_command

SINK = AudioDevice(
    id=69, name="alsa_output.usb.spdif", description="S/PDIF", media_class="Audio/Sink"
)
MIC = AudioDevice(id=76, name="alsa_input.usb.rode", description="RODE", media_class="Audio/Source")


def test_sink_captured_from_its_monitor_ports():
    cmd = build_record_command(SINK, Path("/x.wav"), 48000, 1, "s16")
    assert cmd[:4] == ["pw-record", "--rate=48000", "--channels=1", "--format=s16"]
    assert cmd[cmd.index("-P") + 1] == "{ application.name=Mnemosyne stream.capture.sink=true }"
    assert (
        cmd[cmd.index("--target") + 1] == "alsa_output.usb.spdif"
    )  # the sink, not "<sink>.monitor"
    assert not any(part.endswith(".monitor") for part in cmd)
    assert cmd[-1] == "/x.wav"


def test_source_targeted_by_node_name():
    cmd = build_record_command(MIC, Path("/m.wav"), 48000, 1, "s16")
    assert cmd[cmd.index("-P") + 1] == "{ application.name=Mnemosyne }"  # no capture.sink
    assert cmd[cmd.index("--target") + 1] == "alsa_input.usb.rode"
