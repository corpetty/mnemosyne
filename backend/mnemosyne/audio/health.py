"""Is every source of a recording still being captured?

Two ways a capture fails while the recording goes on: its recorder exits (pw-record crashed or
was killed, its device disappeared), or it stops writing (the node it was linked to went away,
e.g. the echo canceller). Silence is not a failure: EasyEffects' noise reduction writes exact
digital silence when nobody speaks, and silence still grows the file. So the signal is data,
not level.

`CaptureHealth.update` is fed each source's new samples and whether its recorder has exited,
and returns what changed, for `capture_health` events.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

STALL_SECONDS = 10.0
# macOS hands a denied system-audio tap exact zeros, which look like silence. After this much
# audio of nothing else, say where the permission is.
SILENT_SECONDS = 120.0
SILENT_HINT = "System audio is silent: check Privacy & Security → Screen & System Audio Recording"

STOPPED = "stopped"  # the recorder exited
STALLED = "stalled"  # no new audio for STALL_SECONDS
SILENT = (
    "silent"  # macOS system audio: only exact digital zeros (a denied tap), see observe_silence
)
OK = "ok"


@dataclass
class Change:
    device_id: int
    state: str  # STOPPED, STALLED, or OK (captured again after a stall)
    message: str


class CaptureHealth:
    def __init__(
        self,
        names: dict[int, str],
        stall_after: float = STALL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.names = names
        self.stall_after = stall_after
        self._clock = clock
        start = clock()
        self.last_data = dict.fromkeys(names, start)
        self.state = dict.fromkeys(names, OK)
        self._zero_seconds: dict[int, float] = {}

    def problems(self) -> dict[int, str]:
        return {d: s for d, s in self.state.items() if s != OK}

    def update(self, device_id: int, new_samples: int, exited: bool) -> Change | None:
        now = self._clock()
        name = self.names.get(device_id, str(device_id))
        before = self.state.get(device_id, OK)
        if new_samples > 0:
            self.last_data[device_id] = now
        if before == STOPPED:
            return None  # a recorder does not come back by itself
        if exited:
            state, message = STOPPED, f"{name} stopped recording"
        elif now - self.last_data.get(device_id, now) >= self.stall_after:
            seconds = round(now - self.last_data[device_id])
            state, message = STALLED, f"No audio from {name} for {seconds} s"
        elif before == SILENT:
            return None  # only observe_silence clears this one (samples that are not zeros)
        else:
            state, message = OK, f"{name} is recording again"
        if state == before:
            return None
        self.state[device_id] = state
        return Change(device_id, state, message)

    def observe_silence(
        self, device_id: int, seconds: float, all_zero: bool, limit: float = SILENT_SECONDS
    ) -> Change | None:
        """macOS system audio only: `seconds` of new audio, all of it exact zeros or not. A run of
        zeros that reaches `limit` is SILENT; any other sample clears it. Callers do not ask for
        this on Linux, where exact zeros are normal (RNNoise, an idle sink)."""
        if seconds <= 0:
            return None
        before = self.state.get(device_id, OK)
        if before not in (OK, SILENT):
            return None  # stopped or stalled say more
        if all_zero:
            self._zero_seconds[device_id] = self._zero_seconds.get(device_id, 0.0) + seconds
        else:
            self._zero_seconds[device_id] = 0.0
        if self._zero_seconds[device_id] >= limit:
            state, message = SILENT, SILENT_HINT
        else:
            state, message = OK, f"{self.names.get(device_id, device_id)} is recording audio again"
        if state == before:
            return None
        self.state[device_id] = state
        return Change(device_id, state, message)
