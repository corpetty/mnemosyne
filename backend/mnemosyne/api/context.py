"""Application context: every service the routes need, built once per app."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Request, WebSocket

from .. import access
from ..audio.capture import RecordingSession
from ..audio.echo_cancel import EchoCancelManager
from ..config import Settings
from ..events import EventBus
from ..jobs import JobManager
from ..search.index import VectorIndex
from ..services.awake import KeepAwake
from ..services.calendar_service import CalendarService, calendar_source
from ..services.link import LinkService, RoomsLink, parse_relays
from ..services.meeting_types import available_types
from ..services.model_service import ModelService
from ..services.pairing import PairingService
from ..services.session_service import SessionService
from ..services.speaker_service import SpeakerService
from ..services.storage_service import StorageService
from ..services.summarization_service import SummarizationService
from ..services.sync_peers import SyncPeerStore, dial_all
from ..services.team_host import TeamHost
from ..services.users import UserService
from ..storage.sqlite import SessionRepository, import_json_sessions

logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    settings: Settings
    repo: SessionRepository
    sessions: SessionService
    models: ModelService
    summarizer: SummarizationService
    speakers: SpeakerService
    storage: StorageService
    calendar: CalendarService
    bus: EventBus
    jobs: JobManager
    index: VectorIndex
    pairing: PairingService
    users: UserService
    link: LinkService
    rooms_link: RoomsLink
    sync_peers: SyncPeerStore
    active_recordings: dict[str, RecordingSession] = field(default_factory=dict)
    startup_problems: list[str] = field(default_factory=list)  # shown by the app
    starting: set[str] = field(default_factory=set)  # sessions whose recording is starting
    echo: EchoCancelManager = field(default_factory=EchoCancelManager)
    _retention_task: asyncio.Task | None = None
    _digest_task: asyncio.Task | None = None
    _apps_task: asyncio.Task | None = None
    _backup_task: asyncio.Task | None = None
    _idle_task: asyncio.Task | None = None
    _awake_task: asyncio.Task | None = None
    _vault_task: asyncio.Task | None = None
    # Ticks read back from the Obsidian vault (services/vault_sync.py): seconds between passes
    # (0: none; tests drive sync_vault directly) and each note's mtime at the last pass.
    vault_sync_interval: float = 300.0
    vault_seen: dict = field(default_factory=dict)
    _intervals: tuple[float, float] = (6 * 3600, 900)
    capture_apps_now: list = field(default_factory=list)  # last poll, for /api/audio/apps
    live: dict = field(default_factory=dict)  # session id -> running LiveTranscriber
    # session id -> (time.monotonic() last used, {label: voice embedding}): streamed speakers'
    # voices, so the next part of a meeting gives the same person the same live label.
    live_voices: dict = field(default_factory=dict)
    copilot_notes: dict = field(default_factory=dict)  # session id -> CopilotNotes
    # Live shared notes (services/shared_notes.py): the open meetings' rooms and fold locks.
    notes_hub: Any = None
    # Guest notes (services/guest_notes.py): the bridges of live rooms and a lock per meeting.
    guest_bridges: Any = None
    # A team server's per-person calendars (calendar_for), by ICS link.
    person_calendars: dict = field(default_factory=dict)
    team_host: TeamHost = field(default_factory=lambda: TeamHost())
    awake: KeepAwake = field(default_factory=lambda: KeepAwake())  # services/awake.py
    app: Any = None  # the FastAPI app, for the team listener (set by api/app.py)
    recovered: list = field(default_factory=list)  # RecoveredRecording, since this start
    # Encryption at rest: where the master key lives, the key once known, and whether the
    # meetings are encrypted but the key is missing (then only the recovery code gets in).
    keystore: Any = None
    master_key: bytes | None = None
    locked: bool = False
    level_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    http_transport: object | None = None  # tests inject an httpx transport for integrations
    app_watch: Any = None  # api/app_watch.py: the desktop app this backend belongs to
    sync: Any = None  # services/sync_service.py SyncService: library sync with the peers
    share_announce: Any = (
        None  # services/share_announce.py: pokes contacts a shared meeting changed
    )
    share_pull: Any = None  # services/share_pull.py SharePuller: meetings contacts share with us
    share_push: Any = None  # services/share_push.py SharePusher: what I did on them, sent back
    person: Any = None  # services/person.py PersonKeys: this person's signing key

    def __post_init__(self) -> None:
        self.summarizer.name_source = self.known_names
        self.sessions.type_source = lambda: available_types(self.settings)
        self.sessions.is_recording = lambda sid: sid in self.active_recordings
        self.repo.on_new_session = self._new_session
        self.sessions.before_delete = self._forget_publications

    def _forget_publications(self, session_id: str):
        from ..services import guest_notes
        from ..services.publishing import forget_session

        afters = [
            after
            for after in (
                forget_session(self, session_id),
                guest_notes.forget_session(self, session_id),
            )
            if after is not None
        ]
        if not afters:
            return None

        def after() -> None:
            for step in afters:
                step()

        return after

    def calendar_for(self, user_id: str) -> CalendarService:
        """Whose calendar: on a team server, a person's own ICS feed (services/prefs.py),
        else the server's."""
        if self.settings.team_mode and user_id:
            from ..services.prefs import get

            url = (get(self, user_id).calendar_ics_url or "").strip()
            if url:
                if url not in self.person_calendars:
                    self.person_calendars[url] = CalendarService(url)
                return self.person_calendars[url]
        return self.calendar

    def _new_session(self, session_id: str, owner_id: str) -> None:
        """A meeting was just created: share it when its owner always shares. Never raises."""
        from ..services.prefs import share_if_wanted

        try:
            share_if_wanted(self, session_id, owner_id)
        except Exception:
            logger.warning("Could not apply sharing preferences to %s", session_id, exc_info=True)

    def known_names(self) -> list[str]:
        """People's names, for redaction before cloud calls."""
        from ..services.people import list_people

        st = self.settings
        return [
            p.name for p in list_people(self.repo, (st.local_speaker_name, st.remote_speaker_name))
        ]

    @property
    def file_key(self) -> bytes | None:
        """Key for encrypted audio files (None when encryption is off or locked)."""
        from ..storage.crypto import derive

        return derive(self.master_key, "files") if self.master_key else None

    @classmethod
    def build(cls, settings: Settings, keystore: Any = None) -> AppContext:
        from ..storage.crypto import SystemKeyStore, derive, key_check

        settings.data_dir.mkdir(parents=True, exist_ok=True)
        from ..services.backup import apply_path_rebase, apply_pending_restore

        apply_pending_restore(settings)  # before the database (and its key check) is opened
        bus = EventBus()
        if keystore is None:
            from ..demo import enabled as demo_mode
            from ..storage.crypto import CredentialKeyStore, FileKeyStore

            if demo_mode():
                keystore = FileKeyStore(settings.data_dir / "demo-key")
            elif os.environ.get("MNEMOSYNE_KEY_FILE"):  # a server: systemd credential
                keystore = CredentialKeyStore(Path(os.environ["MNEMOSYNE_KEY_FILE"]))
            else:
                keystore = SystemKeyStore(settings.data_dir)
        master, locked = None, False
        if settings.encrypt_at_rest:
            master = keystore.get()
            if master is None or key_check(master) != settings.encryption_check:
                logger.warning("Meetings are encrypted and the key is not available: locked")
                master, locked = None, True
        if locked:  # an empty stand-in until the recovery code arrives (see unlock)
            repo = SessionRepository(Path(":memory:"))
        else:
            repo = SessionRepository(settings.db_path, derive(master, "db") if master else None)
            apply_path_rebase(repo, settings)
            import_json_sessions(repo, settings.sessions_dir)
        ctx = cls(
            keystore=keystore,
            master_key=master,
            locked=locked,
            settings=settings,
            repo=repo,
            sessions=SessionService(repo, settings.recordings_dir, bus),
            models=ModelService(settings),
            summarizer=SummarizationService(settings),
            speakers=SpeakerService(repo, settings.speaker_match_threshold),
            storage=StorageService(repo, settings.data_dir, settings.recordings_dir),
            calendar=CalendarService(calendar_source(settings)),
            bus=bus,
            index=VectorIndex(repo, settings),
            pairing=PairingService(settings.data_dir / "paired_devices.json"),
            users=UserService(settings.data_dir / "users.json"),
            link=LinkService(settings.data_dir, int(os.environ.get("MNEMOSYNE_BIND_PORT", "8008"))),
            rooms_link=_rooms_link(settings),
            sync_peers=SyncPeerStore(settings.data_dir / "sync_peers.json"),
            jobs=JobManager(
                bus,
                concurrency={
                    "transcribe": 1,
                    "summarize": 2,
                    "ask": 2,
                    "digest": 1,
                    "followup": 2,
                    "thread": 2,
                },
            ),
        )
        from ..services.person import PersonKeys

        ctx.person = PersonKeys(
            settings.data_dir, lambda: ctx.master_key, lambda: ctx.settings.encrypt_at_rest
        )
        if not locked:  # while locked, unlock loads it
            try:
                ctx.person.ensure()
            except Exception:
                logger.warning("Could not load the person key", exc_info=True)
        from ..services.sync_service import SyncService

        ctx.sync = SyncService(ctx)  # also names this machine in version vectors
        from ..services.share_announce import ShareAnnouncer

        ctx.share_announce = ShareAnnouncer(ctx)
        from ..services.share_pull import SharePuller

        ctx.share_pull = SharePuller(ctx)
        from ..services.share_push import SharePusher

        ctx.share_push = SharePusher(ctx)

        # The sidecar's listeners are new each start: dial the sync peers and the contacts'
        # machines again, then pull.
        async def redial() -> None:
            from ..services.contacts import dial_contacts

            await dial_all(ctx.link, ctx.sync_peers)
            await dial_contacts(ctx)  # never raises
            ctx.sync.wake()
            ctx.share_pull.wake()

        ctx.link.on_ready.append(redial)
        return ctx

    async def apply_settings(self, settings: Settings) -> None:
        """Swap in new settings and rebuild anything that depends on them."""
        from ..services import supervision

        before = self.settings
        self.settings = settings
        if supervision.enabled(settings) and (
            not supervision.enabled(before) or settings.review_phrases != before.review_phrases
        ):
            self.submit_supervision_scan()
        self.summarizer = SummarizationService(settings)
        self.summarizer.name_source = self.known_names
        self.speakers.threshold = settings.speaker_match_threshold
        if (settings.semantic_search, settings.embedding_model) != (
            self.index.enabled,
            self.index.model,
        ):
            await self.index.stop()
            self.index = VectorIndex(self.repo, settings)
            self.index.start(self.bus)
        self.index.include_shared(settings.shared_in_search)  # a rebuilt index has it already
        if calendar_source(settings) != self.calendar.source:
            self.calendar = CalendarService(calendar_source(settings))
        await self.models.apply_settings(settings)
        await self.apply_remote_access()

    def submit_supervision_scan(self):
        """Every meeting against the current review phrases, as a `supervision_scan` job."""
        import asyncio

        from ..services import supervision

        async def run(job) -> dict:
            job.update("Looking for flagged phrases", progress=0.0)
            return await asyncio.to_thread(
                supervision.scan_all, self, lambda f: job.update(progress=f)
            )

        return self.jobs.submit("supervision_scan", run)

    async def apply_remote_access(self) -> None:
        relays = parse_relays(self.settings.remote_relays)
        if self.link.active and (not self.settings.remote_access or relays != self.link.relays):
            await self.link.stop()
        self.link.relays = relays
        if self.settings.remote_access and not self.link.active:
            self.link.start()

    async def ensure_rooms(self) -> RoomsLink:
        """Start the guest rooms sidecar if it is not running and wait for its ready line.
        Idempotent. Raises LinkError (services/link.py) when it cannot start. A running sidecar
        keeps the relays it started with."""
        link = self.rooms_link
        if not link.active or link._task.done():
            link.relays = parse_relays(self.settings.remote_relays)
            link.start()
        await link.wait_ready()
        return link

    def busy_sessions(self) -> set[str]:
        """Sessions whose audio must not be touched right now."""
        busy = set(self.active_recordings)
        busy |= {j.session_id for j in self.jobs.list(active_only=True) if j.session_id}
        return busy

    def run_retention(self):
        days = self.settings.audio_retention_days
        if days <= 0:
            return None
        from ..services.records import protected_ids

        keep = self.busy_sessions() | protected_ids(self)  # never records or held meetings
        result = self.storage.cleanup(days, busy=keep)
        if result.sessions:
            logger.info(
                "Retention: removed audio from %d session(s), %d bytes",
                len(result.sessions),
                result.freed_bytes,
            )
            for u in result.sessions:
                self.bus.publish(
                    {"type": "session", "session_id": u.session_id, "status": "audio_deleted"}
                )
        return result

    async def _retention_loop(self, interval_seconds: float) -> None:
        while True:
            try:
                self.run_retention()
            except Exception:
                logger.exception("Audio retention pass failed")
            try:
                from ..services.publishing import sweep_expired

                sweep_expired(self)
            except Exception:
                logger.exception("Removing expired publications failed")
            await asyncio.sleep(interval_seconds)

    def maybe_schedule_digest(self, now: datetime | None = None):
        """Queue this week's digest if the schedule says it is due and none exists yet. On a
        team server, each person's own (their schedule, their meetings and those shared with
        them, saved as theirs); returns the jobs queued."""
        if not self.settings.team_mode:
            return self._schedule_digest(self.settings, now)
        jobs = []
        for user in self.users.list():
            if user.disabled:
                continue
            reset = access.current.set(access.Principal(user.id, user.name, user.role))
            try:
                from ..services.prefs import effective

                if job := self._schedule_digest(effective(self, user.id), now):
                    jobs.append(job)
            finally:
                access.current.reset(reset)
        return jobs

    def _schedule_digest(self, st: Settings, now: datetime | None):
        """As whoever is current (access.py): the jobs it queues and the digests it checks."""
        from ..services.digest_service import due_week, range_label
        from ..services.pipeline import make_digest

        week = due_week(now or datetime.now(), st.digest_weekday, st.digest_hour)
        if week is None or self.repo.has_digest(range_label(*week)):
            return None
        me = access.user_id()
        if any(j.kind == "digest" and j.owner_id == me for j in self.jobs.list(active_only=True)):
            return None
        if not any(s.summary.strip() for s in self.repo.sessions_between(*week)):
            return None
        logger.info("Scheduled digest for %s %s", range_label(*week), me)
        return self.jobs.submit("digest", make_digest(self, *week))

    def poll_capture_apps(self, dump: list | None = None) -> None:
        """Compare the apps capturing audio with the last poll and publish changes."""
        from ..audio.streams import capture_apps

        ignore = {x.strip() for x in self.settings.auto_record_ignore_apps.split(",") if x.strip()}
        now = capture_apps(dump, ignore)
        before = {a.app for a in self.capture_apps_now}
        after = {a.app for a in now}
        self.capture_apps_now = now
        for app in sorted(after - before):
            self.bus.publish({"type": "meeting_app", "status": "started", "app": app})
        for app in sorted(before - after):
            self.bus.publish({"type": "meeting_app", "status": "stopped", "app": app})

    async def _apps_loop(self, interval_seconds: float) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            if self.settings.auto_record == "off":
                self.capture_apps_now = []
                continue
            try:
                dump = await asyncio.to_thread(_pw_dump)
                self.poll_capture_apps(dump)
            except Exception as e:
                logger.debug("Capture app poll failed: %s", e)

    async def _digest_loop(self, interval_seconds: float) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                self.maybe_schedule_digest()
            except Exception:
                logger.exception("Digest schedule check failed")

    async def _idle_loop(self, interval_seconds: float) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await self.models.unload_if_idle(busy=self.busy())
                from ..services.local_llm import manager

                idle = self.settings.unload_models_after_minutes * 60
                if not self.jobs.list(active_only=True):
                    manager(self.settings.models_dir).stop_if_idle(idle)
            except Exception:
                logger.exception("Unloading idle models failed")

    async def _awake_loop(self, interval_seconds: float) -> None:
        from ..services.awake import reason

        while True:
            try:
                self.awake.want(reason(self))
            except Exception:
                logger.exception("Keeping the computer awake failed")
            await asyncio.sleep(interval_seconds)

    def busy(self) -> bool:
        """Recording, or any job running: models may be in use."""
        return bool(self.active_recordings) or bool(self.jobs.list(active_only=True))

    async def _backup_loop(self, interval_seconds: float) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                self.maybe_back_up()
            except Exception:
                logger.exception("Backup schedule check failed")

    def maybe_back_up(self):
        """Start an automatic backup when one is due, not while recording or backing up."""
        from ..services.backup import due

        if self.active_recordings or any(
            j.kind == "backup" for j in self.jobs.list(active_only=True)
        ):
            return None
        if not due(self.settings):
            return None
        return self.jobs.submit("backup", backup_runner(self, prune_after=True))

    async def _try(self, what: str, step) -> None:
        """One startup step: a failure is logged and reported (/api/system), never fatal. The
        app must come up (to record, to show what went wrong) even if a side service cannot."""
        try:
            result = step()
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            logger.exception("Startup: %s failed", what)
            self.startup_problems.append(f"{what} failed at startup: {e}")

    async def startup(
        self, retention_interval: float = 6 * 3600, digest_interval: float = 900
    ) -> None:
        from ..config import STARTUP_PROBLEMS
        from ..storage.crypto import clean_scratch

        self.startup_problems[:0] = STARTUP_PROBLEMS
        self._intervals = (retention_interval, digest_interval)
        await self._try("Cleaning temporary copies", clean_scratch)
        # Also while locked: a paired computer can unlock.
        await self._try("Remote access", self.apply_remote_access)
        if self.settings.share_on_network and self.app is not None:
            await self._try("Sharing on the network", lambda: self.team_host.start(self.app, self))
        if self.locked:
            logger.warning("Waiting for the recovery code before starting")
            return
        await self._start_services()

    async def unlock(self, master: bytes) -> None:
        """The recovery code arrived: keep the key, open the real database, start up."""
        from ..services.backup import apply_path_rebase
        from ..storage.crypto import derive

        self.repo.reopen(self.settings.db_path, derive(master, "db"))
        apply_path_rebase(self.repo, self.settings)  # a restore from another machine
        self.keystore.set(master)
        self.master_key, self.locked = master, False
        try:
            self.person.ensure()
        except Exception:
            logger.warning("Could not load the person key", exc_info=True)
        await self._start_services()

    async def _start_services(self) -> None:
        from ..services.recovery import recover_interrupted

        retention_interval, digest_interval = self._intervals
        # A transcription the last backend never finished (killed, crashed): no job runs it
        # now, so the meeting goes back to what it was, transcript or not.
        from ..models.session import SessionStatus

        def reset_transcribing():
            for s in self.sessions.list_sessions():
                if s.status == SessionStatus.TRANSCRIBING and not s.origin:
                    done = SessionStatus.COMPLETED if s.has_transcript else SessionStatus.CREATED
                    self.sessions.set_status(s.id, done)

        await self._try("Resetting unfinished transcriptions", reset_transcribing)
        # First, so retention and the index never see a half-finished recording.
        await self._try("Recovering interrupted recordings", lambda: recover_interrupted(self))
        if self.settings.echo_cancel:
            await self._try("Echo cancellation", self._start_echo)
        self._retention_task = asyncio.create_task(self._retention_loop(retention_interval))
        self._digest_task = asyncio.create_task(self._digest_loop(digest_interval))
        self.index.start(self.bus)
        self._apps_task = asyncio.create_task(self._apps_loop(5.0))
        self._backup_task = asyncio.create_task(self._backup_loop(3600.0))
        self._idle_task = asyncio.create_task(self._idle_loop(60.0))
        self._awake_task = asyncio.create_task(self._awake_loop(5.0))
        if self.vault_sync_interval > 0:
            from ..services.vault_sync import sync_loop

            self._vault_task = asyncio.create_task(sync_loop(self, self.vault_sync_interval))
        # Linked tasks that disagree (a tick merged in elsewhere) settle on their head.
        await self._try("Task links", self._reconcile_tasks)
        # Library sync: claim meetings from before it, then pull from the peers and watch.
        await self._try("Library sync", self.sync.start)
        await self._try("Share announcer", self.share_announce.start)
        await self._try("Share pull", self.share_pull.start)
        await self._try("Share push", self.share_push.start)
        # Live guest notes rooms: a bridge for each, joined with the last seq it applied.
        from ..services import guest_notes

        await self._try("Guest notes", lambda: guest_notes.resume(self))

    async def _reconcile_tasks(self) -> None:
        from ..services import task_links, task_migration

        try:
            # Once per data dir: link what old summaries left unlinked (it reconciles itself).
            done = await asyncio.to_thread(task_migration.migrate, self)
            if not done.get("skipped") and not done.get("failed"):
                return
            n = await asyncio.to_thread(task_links.reconcile, self)
            if n:
                logger.info("Task links: %d meeting(s) brought in line", n)
        except Exception:
            logger.warning("Task links: reconcile failed", exc_info=True)

    def echo_denoise(self):
        """The noise suppressor the echo canceller should run with: None when it is off, or
        its plugin is not downloaded (turning it on downloads it) or cannot run here."""
        from ..audio import noise_suppression as ns

        if not self.settings.noise_suppression or not ns.supported()[0]:
            return None
        plugin = ns.plugin_path(self.settings.models_dir)
        return ns.Denoise(plugin, self.settings.noise_suppression_db) if plugin else None

    def recording_targets(self) -> dict[str, str]:
        """Nodes our recordings take in place of the chosen device: the noise-suppressed mic is
        recorded from before DeepFilterNet unless `noise_suppression_record` is on."""
        from ..audio import noise_suppression as ns
        from ..audio.echo_cancel import SOURCE_NODE

        running = self.echo.active and self.echo.denoise is not None and not self.echo.denoise_error
        if not running or self.settings.noise_suppression_record:
            return {}
        return {SOURCE_NODE: ns.RAW_SOURCE_NODE}

    async def _start_echo(self) -> None:
        denoise = self.echo_denoise()
        try:
            status = await self.echo.start(self.settings.echo_cancel_mic or None, denoise)
        except ValueError as e:  # a bad saved mic: fall back to the default source
            logger.warning("%s", e)
            status = await self.echo.start(None, denoise)
        if not status.active:
            logger.warning("Echo cancellation not started: %s", status.reason)

    async def shutdown(self) -> None:
        from ..services.local_llm import manager

        await self.team_host.stop()
        self.awake.release()

        manager(self.settings.models_dir).stop()  # the built-in model's server, if it runs
        # Recordings still running end here, their files closed properly; the next start
        # recovers them into their meetings (services/recovery.py). Left running, pw-record
        # would outlive the backend and record on with nobody to save it.
        import time

        from ..audio.capture import stop_capture
        from ..services import history

        for session_id, recording in list(self.active_recordings.items()):
            try:
                await stop_capture(recording)
            except Exception:
                logger.exception("Could not stop a recording at shutdown")
            history.log(
                self,
                session_id,
                "recording_stopped",
                recording.part,
                reason="shutdown",
                seconds=round(time.time() - recording.started_at, 1),
            )
        await self.sync.stop()
        if self.share_announce is not None:
            await self.share_announce.stop()
        if self.share_push is not None:
            await self.share_push.stop()
        if self.share_pull is not None:
            await self.share_pull.stop()
        await self.link.stop()
        await self.rooms_link.stop()
        await self.index.stop()
        for task in (
            self._retention_task,
            self._digest_task,
            self._apps_task,
            self._backup_task,
            self._idle_task,
            self._awake_task,
            self._vault_task,
        ):
            if task is not None:
                task.cancel()
        await self.echo.stop()
        await self.jobs.shutdown()
        await self.models.unload()
        from ..services import guest_notes, shared_notes

        await guest_notes.shutdown(self)  # before the notes hub: its bridges detach from it
        await shared_notes.shutdown(self)
        self.repo.close()


def backup_runner(app: AppContext, prune_after: bool = False):
    """Job runner: write a backup (services/backup.py), then drop the oldest beyond the limit."""
    from ..services.backup import create_backup, prune

    async def run(ctx) -> dict:
        ctx.update("Backing up", progress=0.0)
        loop = asyncio.get_running_loop()
        last = [-1.0]

        def progress(frac: float) -> None:  # from the backup thread; whole percents only
            if frac - last[0] >= 0.01:
                last[0] = frac
                loop.call_soon_threadsafe(ctx.update, None, round(min(frac, 0.99), 2))

        info = await asyncio.to_thread(create_backup, app, progress)
        removed = prune(app.settings) if prune_after else []
        return {**info.model_dump(mode="json"), "removed": removed}

    return run


def _pw_dump() -> list:
    import json
    import subprocess

    out = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5, check=True)
    return json.loads(out.stdout)


def _rooms_link(settings: Settings) -> RoomsLink:
    """The guest rooms sidecar (not started): it serves the same guest page and viewer builds
    the backend serves on /n/ and /p/."""
    from .routes.publishing import GUEST_DIR, VIEWER_DIR  # routes import this module

    return RoomsLink(settings.data_dir, GUEST_DIR, VIEWER_DIR)


async def ensure_rooms(ctx: AppContext) -> RoomsLink:
    """Start the guest rooms sidecar on demand (AppContext.ensure_rooms)."""
    return await ctx.ensure_rooms()


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def get_ws_ctx(ws: WebSocket) -> AppContext:
    return ws.app.state.ctx
