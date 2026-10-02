"""Keeping the computer awake: a logind sleep inhibitor held while something is recording here (the
desktop's own recording or a teammate's in their browser), and, when `keep_awake_while_sharing`
is on, while this computer is shared with a team (services/team_host.py).

It runs `systemd-inhibit --what=sleep --mode=block ... sleep infinity` and stops it to let go.
Best effort: without systemd-inhibit (the Flatpak) nothing is held, and `available` says so.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..api.context import AppContext

logger = logging.getLogger(__name__)


def _spawn(reason: str) -> subprocess.Popen:
    return subprocess.Popen(
        [
            "systemd-inhibit",
            "--what=sleep",
            "--mode=block",
            "--who=Mnemosyne",
            f"--why={reason}",
            "sleep",
            "infinity",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # not stopped with the backend's process group by accident
    )


def reason(ctx: AppContext) -> str:
    """Why the computer must stay awake now ("" when it need not)."""
    if any(r.is_recording for r in ctx.active_recordings.values()):
        return "Recording a meeting"
    st = ctx.settings
    if st.share_on_network and st.keep_awake_while_sharing and ctx.team_host.running:
        return "Shared with a team"
    return ""


class KeepAwake:
    def __init__(
        self,
        spawn: Callable[[str], subprocess.Popen] | None = None,
        available: bool | None = None,
    ):
        self._own_spawn = spawn  # tests; else the module's _spawn, looked up when used
        if available is None:
            available = shutil.which("systemd-inhibit") is not None
        self.available = available
        self._proc: subprocess.Popen | None = None
        self.reason = ""

    @property
    def holding(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def want(self, why: str) -> None:
        """Hold the inhibitor for `why`, or let go when it is ""."""
        if why == self.reason and (self.holding or not why):
            return
        self.release()
        if not why or not self.available:
            return
        try:
            self._proc = (self._own_spawn or _spawn)(why)
            self.reason = why
            logger.info("Keeping the computer awake: %s", why)
        except OSError:
            logger.warning("Could not keep the computer awake", exc_info=True)
            self.available = False

    def release(self) -> None:
        proc, self._proc = self._proc, None
        if self.reason:
            logger.info("No longer keeping the computer awake")
        self.reason = ""
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
