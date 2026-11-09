"""Keeping the computer awake while something records here, or all the time it is shared when
asked (services/awake.py)."""

import os
import sys
from types import SimpleNamespace

from mnemosyne.services import awake


class FakeProc:
    def __init__(self, reason):
        self.reason = reason
        self.stopped = False

    def poll(self):
        return 0 if self.stopped else None

    def terminate(self):
        self.stopped = True

    def wait(self, timeout=None):
        return 0


def _keeper():
    spawned = []

    def spawn(reason):
        spawned.append(FakeProc(reason))
        return spawned[-1]

    return awake.KeepAwake(spawn=spawn, available=True), spawned


def test_held_while_wanted_and_let_go_after():
    keeper, spawned = _keeper()
    keeper.want("Recording a meeting")
    keeper.want("Recording a meeting")  # still wanted: the same inhibitor
    assert keeper.holding and len(spawned) == 1 and spawned[0].reason == "Recording a meeting"
    keeper.want("Shared with a team")  # a new reason: held again with it
    assert spawned[0].stopped and spawned[1].reason == "Shared with a team"
    keeper.want("")
    assert not keeper.holding and spawned[1].stopped


def test_nothing_without_systemd_inhibit():
    keeper = awake.KeepAwake(spawn=lambda r: (_ for _ in ()).throw(AssertionError), available=False)
    keeper.want("Recording a meeting")
    assert not keeper.holding


def test_the_reason_follows_recordings_and_sharing(settings):
    recording = SimpleNamespace(is_recording=True)
    ctx = SimpleNamespace(
        active_recordings={},
        settings=settings,
        team_host=SimpleNamespace(running=True),
    )
    assert awake.reason(ctx) == ""
    ctx.active_recordings["s1"] = recording  # the desktop's or a teammate's browser recording
    assert awake.reason(ctx) == "Recording a meeting"
    ctx.active_recordings.clear()
    settings.share_on_network = True
    assert awake.reason(ctx) == ""  # shared, but only recordings keep it awake by default
    settings.keep_awake_while_sharing = True
    assert awake.reason(ctx) == "Shared with a team"


def test_guest_notes_rooms_on_this_computer_keep_it_awake(settings):
    def bridge(transport, ended_at=""):
        target = SimpleNamespace(kind="primary", transport=transport)
        return SimpleNamespace(
            running=True, target=target, closing=False, row={"ended_at": ended_at}
        )

    reg = SimpleNamespace(bridges={})
    ctx = SimpleNamespace(
        active_recordings={},
        settings=settings,
        team_host=SimpleNamespace(running=False),
        guest_bridges=reg,
    )
    assert awake.reason(ctx) == ""
    reg.bridges = {("s1", "primary"): bridge("host")}  # a publish host's room: nothing here
    assert awake.reason(ctx) == ""
    reg.bridges = {("s1", "primary"): bridge("iroh", ended_at="2026-10-25T10:00:00Z")}
    assert awake.reason(ctx) == ""
    reg.bridges = {("s1", "primary"): bridge("iroh")}
    assert awake.reason(ctx) == "Guest notes are open"
    reg.bridges = {("s1", "primary"): bridge("tunnel")}
    assert awake.reason(ctx) == "Guest notes are open"
    ctx.active_recordings["s2"] = SimpleNamespace(is_recording=True)
    assert awake.reason(ctx) == "Recording a meeting"  # a recording is the first reason


def test_linux_command_is_systemd_inhibit(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert awake._command("Recording a meeting") == [
        "systemd-inhibit",
        "--what=sleep",
        "--mode=block",
        "--who=Mnemosyne",
        "--why=Recording a meeting",
        "sleep",
        "infinity",
    ]


def test_darwin_command_is_caffeinate_for_this_backend(monkeypatch):
    # conftest stubs awake._spawn, so the argv is checked through _command, which _spawn uses
    monkeypatch.setattr(sys, "platform", "darwin")
    assert awake._command("Recording a meeting") == [
        "/usr/bin/caffeinate",
        "-i",
        "-w",
        str(os.getpid()),
    ]


def test_darwin_available_when_caffeinate_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    missing = tmp_path / "caffeinate"
    monkeypatch.setattr(awake, "CAFFEINATE", str(missing))
    assert awake.KeepAwake().available is False
    missing.write_text("")
    assert awake.KeepAwake().available is True


def test_darwin_release_stops_caffeinate(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    keeper, spawned = _keeper()
    keeper.want("Recording a meeting")
    assert keeper.holding and spawned[0].reason == "Recording a meeting"
    keeper.want("")
    assert not keeper.holding and spawned[0].stopped
