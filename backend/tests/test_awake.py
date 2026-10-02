"""Keeping the computer awake while something records here, or all the time it is shared when
asked (services/awake.py)."""

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
