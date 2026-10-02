"""The desktop app's backend outlives the app only to save a recording.

The Tauri shell starts the backend in a session of its own (so it can stop the whole process
tree), which also means an app that crashes or is force-quit leaves the backend running, still
recording and still holding the port. The shell passes its pid in MNEMOSYNE_APP_PID and we
watch it:

- The app is gone and nothing is going on: shut down.
- The app is gone while jobs run (a transcription, a summary): finish them, then shut down.
- The app is gone during a recording: keep recording for ORPHAN_GRACE, so that a relaunched app
  picks the recording up (it attaches with POST /api/system/attach and shows it), and say so in
  a desktop notification: nothing records unseen. When nobody comes back, stop and save the
  recording the way Stop does, then shut down.
- The computer is shared with a team and `keep_sharing_after_quit` is on (`outlives_app`): keep
  serving them, say so in a notification, and never shut down. Teammates' browser recordings go
  on; the desktop's own recording is still saved after ORPHAN_GRACE.

Without MNEMOSYNE_APP_PID (server mode, `uvicorn main:app`, tests) nothing is watched.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .context import AppContext

logger = logging.getLogger(__name__)


def outlives_app(ctx: AppContext) -> bool:
    """Whether this backend keeps serving a team when the desktop app quits (team_host.py)."""
    st = ctx.settings
    return st.share_on_network and st.keep_sharing_after_quit and ctx.team_host.running


ORPHAN_GRACE = 15 * 60.0  # seconds a recording goes on without the app
# Jobs that belong to a recording and end with it; anything else is work to finish first.
RECORDING_JOBS = ("live", "copilot")


def start_time(pid: int) -> int | None:
    """When a process started (clock ticks since boot), to tell it from a later process that
    reuses its pid. None when there is no such process."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # The command name (field 2) may contain spaces and parentheses; the rest follows the last ")".
    fields = stat.rsplit(")", 1)[1].split()
    try:
        return int(fields[19])  # field 22 of the whole line
    except (IndexError, ValueError):
        return None


def notify(summary: str, body: str) -> None:
    """A desktop notification (org.freedesktop.Notifications). Best effort."""
    try:
        from jeepney import DBusAddress, new_method_call
        from jeepney.io.blocking import open_dbus_connection

        addr = DBusAddress(
            "/org/freedesktop/Notifications",
            bus_name="org.freedesktop.Notifications",
            interface="org.freedesktop.Notifications",
        )
        msg = new_method_call(
            addr,
            "Notify",
            "susssasa{sv}i",
            ("Mnemosyne", 0, "com.corpetty.mnemosyne", summary, body, [], {}, -1),
        )
        conn = open_dbus_connection(bus="SESSION")
        try:
            conn.send_and_get_reply(msg, timeout=5)
        finally:
            conn.close()
    except Exception:
        logger.debug("Desktop notification failed", exc_info=True)


def _exit() -> None:
    """Shut down the way Ctrl+C does: uvicorn stops serving and runs the app's shutdown."""
    os.kill(os.getpid(), signal.SIGTERM)


class AppWatch:
    def __init__(
        self,
        ctx: AppContext,
        pid: int,
        grace: float = ORPHAN_GRACE,
        interval: float = 2.0,
        exit: Callable[[], None] = _exit,
        notify: Callable[[str, str], None] = notify,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.ctx = ctx
        self.grace = grace
        self.interval = interval
        self._exit = exit
        self._notify = notify
        self._clock = clock
        self.pid, self._started = pid, start_time(pid)
        self.orphaned_since: float | None = None
        self.told_serving = False  # the "still shared" notification was shown
        self.done = False

    @classmethod
    def from_env(cls, ctx: AppContext) -> AppWatch | None:
        raw = os.environ.get("MNEMOSYNE_APP_PID", "")
        if not raw.isdigit():
            return None
        watch = cls(ctx, int(raw))
        if watch._started is None:  # the app is already gone, or not ours to see
            logger.warning("App pid %s not found; not watching it", raw)
            return None
        logger.info("Watching the app (pid %s)", raw)
        return watch

    def attach(self, pid: int) -> bool:
        """A (re)started app takes over this backend. False when there is no such process."""
        started = start_time(pid)
        if started is None:
            return False
        if self.orphaned_since is not None:
            logger.info("The app is back (pid %d)", pid)
        self.pid, self._started = pid, started
        self.orphaned_since = None
        self.told_serving = False
        return True

    def app_alive(self) -> bool:
        return self._started is not None and start_time(self.pid) == self._started

    async def run(self) -> None:
        while not self.done:
            await asyncio.sleep(self.interval)
            try:
                await self.check()
            except Exception:
                logger.exception("App watch failed")

    async def check(self) -> None:
        if self.done:
            return
        if self.app_alive():
            if self.orphaned_since is not None:
                logger.info("The app is back")
            self.orphaned_since = None
            self.told_serving = False
            return
        from ..audio.capture import is_browser

        serving = outlives_app(self.ctx)
        recording = [
            s
            for s, r in self.ctx.active_recordings.items()
            if r.is_recording and not (serving and is_browser(r))  # a teammate's goes on
        ]
        now = self._clock()
        if recording:
            if self.orphaned_since is None:
                self.orphaned_since = now
                minutes = round(self.grace / 60)
                logger.warning(
                    "The app is gone while recording; recording on for %d min so it can pick "
                    "the recording up",
                    minutes,
                )
                await asyncio.to_thread(
                    self._notify,
                    "Mnemosyne closed while recording",
                    f"Still recording. Open Mnemosyne within {minutes} minutes to carry on; "
                    "otherwise the recording is stopped and saved.",
                )
                return
            if now - self.orphaned_since < self.grace:
                return
            logger.warning("Nobody came back for the recording; stopping and saving it")
            await self._save(recording)
            await asyncio.to_thread(
                self._notify,
                "Mnemosyne saved the recording",
                "It was stopped because the app was closed. Open Mnemosyne to transcribe it.",
            )
            self.orphaned_since = None
        if serving:
            if not self.told_serving:
                self.told_serving = True
                logger.info("The app is gone; still sharing this computer with the team")
                await asyncio.to_thread(
                    self._notify,
                    "Mnemosyne is still shared with your team",
                    "It keeps serving them after quitting. Open Mnemosyne to stop sharing.",
                )
            return
        busy = [
            j
            for j in self.ctx.jobs.list(active_only=True)
            if j.kind not in RECORDING_JOBS and not j.is_terminal
        ]
        if busy:
            if self.orphaned_since is None:
                self.orphaned_since = now
                logger.info("The app is gone; finishing %d job(s) first", len(busy))
            return
        logger.info("The app is gone; shutting down")
        self.done = True
        self._exit()

    async def _save(self, session_ids: list[str]) -> None:
        from .routes.audio import stop_active

        for session_id in session_ids:
            job, _ = await stop_active(self.ctx, session_id, transcribe=False, reason="app_gone")
            await self.ctx.jobs.wait(job.id)


def watch_app(ctx: AppContext) -> tuple[AppWatch, asyncio.Task] | None:
    """Watch the app named in the environment, if any."""
    watch = AppWatch.from_env(ctx)
    if watch is None:
        return None
    ctx.app_watch = watch
    return watch, asyncio.create_task(watch.run())
