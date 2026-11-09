"""Live publications (services/live_publications.py): the watcher that keeps a followed link
up to date (task 5 of docs/plans/2026-11-05-live-publications.md) and the bridge that pushes
versions to the readers through a room on the publish host (task 6).

The watcher is driven by hand: a fake clock and `tick()`, never its loop (conftest sets its
interval to 0), and the publish host is test_publishing.py's FakeHost behind an httpx
MockTransport. The bridge (off in every other test, conftest) runs for real against
tests/fake_guest_room.py's FakeRoomHost (the room routes and its WebSocket) beside that FakeHost
(`live` fixture); readers are GuestClients holding the link's key."""

import asyncio
import base64
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from mnemosyne.models.publication import PublicationSelection, QuoteSpan
from mnemosyne.models.session import ActionItem
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services import live_publications as lp
from mnemosyne.services import notes_media, publishing
from mnemosyne.services import pub_live_format as plf
from mnemosyne.services import publication_format as pf
from mnemosyne.services.live_publications import Watcher
from tests.fake_guest_room import T_NOTES, T_SNAPSHOT, FakeRoomHost, GuestClient
from tests.test_notes_media import jpeg, sha, value, webp
from tests.test_publishing import FakeHost, _Job, _key_and_id, _meeting, _picture_meeting

pytestmark = pytest.mark.anyio


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def host(ctx, settings):
    h = FakeHost()
    ctx.http_transport = httpx.MockTransport(h.handler)
    settings.publish_host = "https://pub.example.org"
    return h


@pytest.fixture
def seals(monkeypatch):
    from mnemosyne.services import records

    calls = []
    monkeypatch.setattr(records, "seal_later", lambda app, sid, reason: calls.append((sid, reason)))
    return calls


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def watcher(ctx, clock):
    w: Watcher = ctx.live_watcher  # the one status() asks
    w.clock = clock
    assert w.interval == 0  # conftest: no loop in tests
    return w


async def _first(ctx, sid, **sel) -> dict:
    """Publish by hand (the person's act), with `follow` on unless said otherwise."""
    selection = PublicationSelection(**{"where": "host", "follow": True, **sel})
    return (await publishing.publish(ctx, sid, selection, _Job()))["publication"]


async def _at(watcher, clock, t: float) -> list:
    """Tick at `t` seconds after the start; returns the jobs it submitted, finished."""
    clock.t = 1000.0 + t
    jobs = await watcher.tick()
    for job in jobs:
        await watcher.ctx.jobs.wait(job.id)
    return [watcher.ctx.jobs.get(j.id) for j in jobs]


def _auto_versions(ctx, sid) -> list[int]:
    return [
        e["detail"]["version"]
        for e in ctx.repo.events(sid)
        if e["kind"] == publishing.EVENT_AUTO_UPDATED
    ]


def _opened(host, pub) -> pf.Manifest:
    pub_id, key = _key_and_id(pub["url"])
    return pf.open_manifest(pub_id, host.version, host.manifest, key)


async def test_a_summary_edit_yields_one_automatic_version_after_the_quiet_time(
    ctx, host, seals, watcher, clock
):
    s = _meeting(ctx)
    pub = await _first(ctx, s.id)
    watcher.start()
    assert await _at(watcher, clock, 0) == []
    ctx.repo.update_fields(s.id, summary="Ana Lopez and Bo decided to ship Monday.")
    assert await _at(watcher, clock, 1) == []  # noticed: quiet from here
    assert await _at(watcher, clock, 6) == []
    (job,) = await _at(watcher, clock, 11)
    assert job.kind == "publish" and job.status == "completed", job.error
    assert job.session_id == s.id
    assert host.version == 2 and _auto_versions(ctx, s.id) == [2]
    assert "Monday" in _opened(host, pub).summary
    row = ctx.repo.live_publication(s.id)
    assert not publishing.is_behind(ctx, row, ctx.repo.get(s.id))
    assert seals == [(s.id, publishing.SEAL_PUBLISHED)]  # an automatic version seals nothing
    # Its own row write moved the change log: looked at, nothing to publish.
    assert await _at(watcher, clock, 20) == []
    assert await _at(watcher, clock, 60) == []
    assert host.version == 2 and watcher.reason(s.id) == ""


async def test_three_edits_in_eight_seconds_yield_one_version(ctx, host, seals, watcher, clock):
    s = _meeting(ctx)
    pub = await _first(ctx, s.id)
    watcher.start()
    for t, day in ((0, "Monday"), (4, "Tuesday"), (8, "Wednesday")):
        ctx.repo.update_fields(s.id, summary=f"Ana Lopez and Bo decided to ship {day}.")
        assert await _at(watcher, clock, t) == []
    assert await _at(watcher, clock, 12) == []  # 4 s after the last edit
    (job,) = await _at(watcher, clock, 18)
    assert job.status == "completed", job.error
    assert host.version == 2 and _auto_versions(ctx, s.id) == [2]
    assert "Wednesday" in _opened(host, pub).summary
    # A change right after the version waits for the gap (30 s since the last one).
    ctx.repo.update_fields(s.id, summary="Ana Lopez and Bo decided to ship Thursday.")
    assert await _at(watcher, clock, 19) == []
    assert await _at(watcher, clock, 40) == []  # quiet, but only 22 s since the last version
    (job,) = await _at(watcher, clock, 49)
    assert job.status == "completed" and host.version == 3


async def test_steady_changes_still_reach_readers_after_a_minute(ctx, host, seals, watcher, clock):
    """People typing together never leave 10 s of quiet: the first unpublished change waits
    at most MAX_WAIT (60 s), then GAP and the quiet rule apply again."""
    s = _meeting(ctx)
    pub = await _first(ctx, s.id)
    watcher.start()
    published = []
    for t in range(0, 91, 3):  # a change every 3 s for 90 s
        ctx.repo.update_fields(s.id, summary=f"Ana Lopez and Bo decided to ship at {t}.")
        for job in await _at(watcher, clock, t):
            assert job.status == "completed", job.error
            published.append(t)
    assert published == [60]  # once, at the 60 s mark
    assert "ship at 60." in _opened(host, pub).summary
    for t in range(93, 100, 3):
        assert await _at(watcher, clock, t) == []
    (job,) = await _at(watcher, clock, 100)  # quiet 10 s after the last change (at 90)
    assert job.status == "completed" and host.version == 3
    assert "ship at 90." in _opened(host, pub).summary
    assert _auto_versions(ctx, s.id) == [2, 3]


async def test_an_identical_rewrite_yields_none(ctx, host, seals, watcher, clock):
    s = _meeting(ctx)
    await _first(ctx, s.id)
    watcher.start()
    seq = ctx.repo.current_sync_seq()
    ctx.repo.update_fields(s.id, summary=s.summary)  # the same text: still a write
    ctx.repo.update_fields(s.id, notes=s.notes)
    assert ctx.repo.current_sync_seq() > seq
    for t in (0, 11, 60, 120):
        assert await _at(watcher, clock, t) == []
    assert host.version == 1 and _auto_versions(ctx, s.id) == []


async def test_a_publication_that_is_not_followed_is_left_alone(ctx, host, seals, watcher, clock):
    s = _meeting(ctx)
    await _first(ctx, s.id, follow=False, comments=True)
    watcher.start()
    ctx.repo.update_fields(s.id, summary="Ana Lopez and Bo decided to ship Monday.")
    for t in (0, 11, 60):
        assert await _at(watcher, clock, t) == []
    assert host.version == 1


class Hosts:
    """One FakeHost per publication id (a FakeHost keeps one publisher token)."""

    def __init__(self):
        self.by_id: dict[str, FakeHost] = {}

    def handler(self, request):
        pub_id = request.url.path.strip("/").split("/")[1]
        return self.by_id.setdefault(pub_id, FakeHost()).handler(request)


async def test_a_foreign_meeting_and_a_shared_one_yield_none(ctx, settings, seals, watcher, clock):
    hosts = Hosts()
    ctx.http_transport = httpx.MockTransport(hosts.handler)
    settings.publish_host = "https://pub.example.org"
    recorded_elsewhere = _meeting(ctx)
    await _first(ctx, recorded_elsewhere.id)
    theirs = _meeting(ctx)
    await _first(ctx, theirs.id)
    with ctx.repo._lock, ctx.repo._conn:
        ctx.repo._conn.execute(
            "UPDATE sessions SET origin='peer-machine' WHERE id=?", (recorded_elsewhere.id,)
        )
        ctx.repo._conn.execute("UPDATE sessions SET shared_from='ab' WHERE id=?", (theirs.id,))
    watcher.start()  # both followed rows are looked at once at start
    from mnemosyne import access

    with access.internally():  # as a pull from the sharer writes it
        for sid in (recorded_elsewhere.id, theirs.id):
            ctx.repo.update_fields(sid, summary="Ana Lopez and Bo decided to ship Monday.")
    for t in (0, 11, 60, 200):
        assert await _at(watcher, clock, t) == []
    assert [h.version for h in hosts.by_id.values()] == [1, 1]  # neither got a second version
    for sid in (recorded_elsewhere.id, theirs.id):
        assert _auto_versions(ctx, sid) == []
        assert publishing.is_behind(ctx, ctx.repo.live_publication(sid), ctx.repo.get(sid))


async def test_no_version_while_a_publish_job_runs(ctx, host, seals, watcher, clock):
    import asyncio

    s = _meeting(ctx)
    await _first(ctx, s.id)
    watcher.start()
    ctx.repo.update_fields(s.id, summary="Ana Lopez and Bo decided to ship Monday.")
    await _at(watcher, clock, 0)
    gate = asyncio.Event()

    async def busy(job):
        await gate.wait()
        return {}

    running = ctx.jobs.submit("publish", busy, session_id=s.id)  # a manual Update, say
    await asyncio.sleep(0)
    assert await _at(watcher, clock, 11) == []
    gate.set()
    await ctx.jobs.wait(running.id)
    (job,) = await _at(watcher, clock, 12)  # still dirty: looked at once the job is over
    assert job.status == "completed" and host.version == 2


class FailingHost(FakeHost):
    """A host that refuses manifests while `full` (507: not retried by `_host`)."""

    def __init__(self):
        super().__init__()
        self.full = False

    def handler(self, request):
        if self.full and request.method == "PUT":
            self.calls.append((request.method, request.url.path, dict(request.headers)))
            return httpx.Response(507, json={"error": "full"})
        return super().handler(request)


async def test_a_failing_host_backs_off_and_recovers(ctx, settings, seals, watcher, clock):
    host = FailingHost()
    ctx.http_transport = httpx.MockTransport(host.handler)
    settings.publish_host = "https://pub.example.org"
    s = _meeting(ctx)
    await _first(ctx, s.id)
    watcher.start()
    host.full = True
    ctx.repo.update_fields(s.id, summary="Ana Lopez and Bo decided to ship Monday.")
    await _at(watcher, clock, 0)
    (job,) = await _at(watcher, clock, 10)
    assert job.status == "failed" and job.error == "The publish host is full"
    failure = watcher.failure(s.id)
    assert failure.count == 1 and failure.retry_at == clock.t + 60
    reason = "The last automatic update failed: The publish host is full"
    assert watcher.reason(s.id) == reason
    # What the owner's UI shows: behind, with the reason (in amber).
    model = publishing.to_model(ctx, ctx.repo.live_publication(s.id), ctx.repo.get(s.id))
    assert model.behind and model.live_reason == reason and model.live == "offline"

    # Nothing until the backoff passes, even with new changes (which do not reset it).
    ctx.repo.update_fields(s.id, summary="Ana Lopez and Bo decided to ship Tuesday.")
    for t in (30, 45, 69):
        assert await _at(watcher, clock, t) == []
    (job,) = await _at(watcher, clock, 71)  # 1 minute after the failure: again, and again full
    assert job.status == "failed"
    assert watcher.failure(s.id).count == 2 and watcher.failure(s.id).retry_at == clock.t + 120
    # No new change needed for the retry: the failure keeps the meeting on the list.
    assert await _at(watcher, clock, 190) == []
    host.full = False
    (job,) = await _at(watcher, clock, 192)  # 2 minutes after the second failure
    assert job.status == "completed", job.error
    assert host.version == 2 and _auto_versions(ctx, s.id) == [2]
    assert watcher.failure(s.id) is None and watcher.reason(s.id) == ""
    model = publishing.to_model(ctx, ctx.repo.live_publication(s.id), ctx.repo.get(s.id))
    assert not model.behind and model.live_reason == ""
    assert "Tuesday" in _opened(host, {"url": model.url}).summary
    assert seals == [(s.id, publishing.SEAL_PUBLISHED)]


async def test_the_backoff_grows_to_ten_minutes(ctx, settings, seals, watcher, clock):
    host = FailingHost()
    ctx.http_transport = httpx.MockTransport(host.handler)
    settings.publish_host = "https://pub.example.org"
    s = _meeting(ctx)
    await _first(ctx, s.id)
    watcher.start()
    host.full = True
    ctx.repo.update_fields(s.id, summary="Ana Lopez and Bo decided to ship Monday.")
    await _at(watcher, clock, 0)
    t, waits = 10.0, []
    for _ in range(5):
        (job,) = await _at(watcher, clock, t)
        assert job.status == "failed"
        retry = watcher.failure(s.id).retry_at
        waits.append(retry - clock.t)
        assert await _at(watcher, clock, retry - 1000.0 - 1) == []
        t = retry - 1000.0
    assert waits == [60, 120, 300, 600, 600]


async def test_a_manual_publish_clears_a_failure(ctx, settings, seals, watcher, clock):
    host = FailingHost()
    ctx.http_transport = httpx.MockTransport(host.handler)
    settings.publish_host = "https://pub.example.org"
    s = _meeting(ctx)
    await _first(ctx, s.id)
    watcher.start()
    host.full = True
    ctx.repo.update_fields(s.id, summary="Ana Lopez and Bo decided to ship Monday.")
    await _at(watcher, clock, 0)
    (job,) = await _at(watcher, clock, 10)
    assert job.status == "failed" and watcher.reason(s.id)
    host.full = False
    await _first(ctx, s.id)  # the person presses Update
    assert await _at(watcher, clock, 15) == []  # noticed the row write
    assert await _at(watcher, clock, 71) == []  # not behind: nothing to do, failure gone
    assert watcher.failure(s.id) is None and watcher.reason(s.id) == ""


async def test_a_turned_off_follow_or_an_unpublish_stops_it(ctx, host, seals, watcher, clock):
    s = _meeting(ctx)
    await _first(ctx, s.id)
    watcher.start()
    await _first(ctx, s.id, follow=False)  # Update with the switch off
    ctx.repo.update_fields(s.id, summary="Ana Lopez and Bo decided to ship Monday.")
    for t in (0, 11, 60):
        assert await _at(watcher, clock, t) == []
    assert host.version == 2 and _auto_versions(ctx, s.id) == []
    await _first(ctx, s.id)  # on again (version 3), then unpublished
    await publishing.unpublish(ctx, s.id)
    ctx.repo.update_fields(s.id, summary="Ana Lopez and Bo decided to ship Tuesday.")
    for t in (100, 111, 200):
        assert await _at(watcher, clock, t) == []
    assert _auto_versions(ctx, s.id) == []


async def test_it_runs_as_the_owner_on_a_team_server(ctx, host, seals, watcher, clock):
    from mnemosyne import access

    ctx.users.add("Ada Admin", "ada@example.org", access.ADMIN)
    ann = ctx.users.add("Ann Owner", "ann@example.org", access.MEMBER)
    s = _meeting(ctx, owner_id=ann.id)
    who = access.Principal(ann.id, ann.name, ann.role)
    token = access.current.set(who)
    try:
        await _first(ctx, s.id)
    finally:
        access.current.reset(token)
    ctx.settings.team_mode = True
    ctx.settings.allow_publishing = True
    watcher.start()
    ctx.repo.update_fields(s.id, summary="Ana Lopez and Bo decided to ship Monday.")
    await _at(watcher, clock, 0)
    (job,) = await _at(watcher, clock, 11)
    assert job.status == "completed", job.error
    assert job.owner_id == ann.id
    assert ctx.repo.live_publication(s.id)["published_by"] == "Ann Owner"
    # Disabled: nobody to publish as.
    ctx.users.update(ann.id, disabled=True)
    ctx.repo.update_fields(s.id, summary="Ana Lopez and Bo decided to ship Tuesday.")
    for t in (100, 111, 200):
        assert await _at(watcher, clock, t) == []
    assert host.version == 2


async def test_a_change_while_the_backend_was_down_is_caught_up_at_start(
    ctx, host, seals, watcher, clock
):
    s = _meeting(ctx)
    await _first(ctx, s.id)
    ctx.repo.update_fields(s.id, summary="Ana Lopez and Bo decided to ship Monday.")
    watcher.start()  # the change is older than the counter it starts from
    assert await _at(watcher, clock, 0) == []
    (job,) = await _at(watcher, clock, 11)
    assert job.status == "completed" and host.version == 2


# ---- the leak test ("Rules: sync, records, team, failure and offline") ----


async def test_an_automatic_version_never_widens_the_selection(
    ctx, live, seals, watcher, clock, caplog
):
    caplog.set_level("DEBUG")
    host = live.pub
    pics = (webp(640, 480), jpeg(800, 600), webp(320, 240))
    s, (a, b, c) = _picture_meeting(ctx, *pics, captions=("Whiteboard", "Sketch", "Napkin"))
    # Decisions unticked, two of the three images, two attendees listed, one quote.
    pub = await _first(
        ctx,
        s.id,
        notes=True,
        decisions=False,
        images=[a, b],
        attendees=["Ana Lopez", "Bo Chen"],
        quotes=[QuoteSpan(start=0.0, end=6.0)],
    )
    stored = ctx.repo.live_publication(s.id)["selection"]
    assert stored["images"] == [a, b] and stored["follow"]
    first = _opened(host, pub)
    assert first.decisions == [] and [i.id for i in first.images] == [a, b]
    bridge = await _bridge(ctx, s.id)
    reader = await _reader(live, ctx, s.id)
    watcher.start()

    # The meeting grows: a transcript line, an image in the notes, a new attendee, a
    # re-summarize with more decisions and an action item naming her, an e-mail in the notes.
    s = ctx.repo.get(s.id)
    ctx.repo.replace_segments(
        s.id,
        [
            *s.transcript,
            TranscriptSegment(
                text="Dee Ray's number is 123456789012.", speaker="Dee Ray", start=13, end=16
            ),
        ],
    )
    late = jpeg(300, 200)
    info = notes_media.store(ctx, s.id, sha(late), late)
    notes = (
        s.notes
        + f"![Late picture](mnemosyne:image/{value(late, info.ext, info.width, info.height)})\n"
        + "Dee Ray can be reached at dee.ray@example.org about the deck.\n"
    )
    ctx.repo.update_fields(s.id, notes=notes)
    ctx.repo.update_fields(s.id, attendees=[*s.attendees, "Dee Ray"])
    data = s.summary_data.model_copy(
        update={
            "decisions": ["Ship on Friday (Ana)", "Dee Ray owns the launch deck"],
            "decision_ids": ["dec1", "dec2"],
            "decision_at": [0.0, 13.0],
            "action_items": [
                ActionItem(id="act1", text="Write the notes", owner="Bo Chen"),
                ActionItem(id="act2", text="Send Dee Ray the deck", owner="Dee Ray"),
            ],
        }
    )
    ctx.repo.update_fields(
        s.id,
        summary="Ana Lopez, Bo and Dee Ray decided to ship Friday; mail dee.ray@example.org.",
        summary_data=data,
    )
    await _at(watcher, clock, 0)
    (job,) = await _at(watcher, clock, 11)
    assert job.status == "completed", job.error
    assert _auto_versions(ctx, s.id) == [2]

    m = _opened(host, pub)
    body = json.loads(pf.manifest_bytes(m))
    assert "segments" not in body and "transcript" not in body
    raw = json.dumps(body)
    assert "Dee Ray" not in raw and "Dee" not in raw  # the new person is "Person N" everywhere
    assert "dee.ray@example.org" not in raw and "123456789012" not in raw
    assert "[EMAIL]" in m.notes and "[EMAIL]" in m.summary
    assert m.decisions == []  # re-summarized decisions stay out
    assert m.attendees == ["Ana Lopez", "Bo Chen"]
    assert [i.id for i in m.images] == [a, b]  # the same two: not the third, not the late one
    assert sha(late) not in raw and c not in raw
    assert "[image: Late picture]" in m.notes and "[image: Napkin]" in m.notes
    owners = [i.owner for i in m.action_items]
    assert owners[0] == "Bo Chen" and owners[1].startswith("Person ")
    assert "Person " in m.summary
    assert len(m.quotes) == 1 and "number" not in m.quotes[0].text  # only the quote's lines
    # The selection did not move, and the manifest is exactly what the STORED selection gives.
    row = ctx.repo.live_publication(s.id)
    assert row["selection"] == stored
    expected, _ = publishing.build_manifest(
        ctx,
        ctx.repo.get(s.id),
        PublicationSelection.model_validate(stored),
        m.id,
        m.version,
        published_at=datetime.fromisoformat(m.published_at.replace("Z", "+00:00")),
        expires_at=datetime.fromisoformat(m.expires_at.replace("Z", "+00:00")),
        image_refs={i.id: i for i in m.images},
        live=m.live,
    )
    assert pf.manifest_bytes(expected) == pf.manifest_bytes(m)

    # Every frame the bridge put on the wire (what the reader received, snapshot included, and
    # every snapshot it posted) is VERSION, THREADS or META with exactly their fields, signed
    # by the key the manifest names.
    sign_pub = bytes.fromhex(m.live.sign)
    await _until(lambda: 2 in _versions(reader, live, row, sign_pub), "the VERSION")
    room = bridge.room
    wire = [env for _, env in reader.binary(T_SNAPSHOT) + reader.binary(T_NOTES)]
    wire += [body for mth, path, _, body in live.rooms.calls if path.endswith("/snapshot")]
    kinds = set()
    for env in wire:
        kind, body = plf.open_signed(row["key"], room, env, sign_pub)
        assert set(body) == set(lp.FRAMES[kind].model_fields), (kind, body)
        kinds.add(kind)
        if kind is plf.Kind.VERSION and body["version"] == m.version:
            assert body["sha256"] == hashlib.sha256(host.manifest).hexdigest()
            assert body["published_at"] == m.published_at and body["expires_at"] == m.expires_at
        if kind is plf.Kind.THREADS:
            assert body["threads"] == []  # no reader has commented here
    assert kinds == {plf.Kind.VERSION, plf.Kind.THREADS, plf.Kind.META}

    # (c) The owner keys are in no history line, records export or API answer, the link key in
    # no history or record, and none of them in the log.
    from mnemosyne.services import records

    session = ctx.repo.get(s.id)
    history = json.dumps(ctx.repo.events(s.id), default=str)
    record = json.dumps(records.publications_record(ctx, session), default=str)
    api = publishing.to_model(ctx, row, session).model_dump_json()
    for secret in (row["sign_key"], row["box_key"]):
        for text in (history, record, api, caplog.text):
            assert secret.hex() not in text and pf.b64url_encode(secret) not in text
    for text in (history, record, caplog.text):
        assert row["key"].hex() not in text and pf.encode_key(row["key"]) not in text
    assert "sign_key" not in api and "box_key" not in api
    await reader.close()


# ---- the bridge (task 6) ----


class LiveHosts:
    """The publication's host as one origin: FakeHost for `/pub/`, a FakeRoomHost for `/live/`
    (its HTTP routes here, its WebSocket at its own address, which is `publish_host`). `log`
    lists every request as (method, path), in order; `pointer_404` plays a host that answers
    404 on the pointer route."""

    def __init__(self, pub: FakeHost, rooms: FakeRoomHost):
        self.pub, self.rooms = pub, rooms
        self.log: list[tuple[str, str]] = []
        self.pointer_404 = False

    async def handler(self, request):
        path = request.url.path
        self.log.append((request.method, path))
        if path.startswith("/live/"):
            return await self.rooms.handler(request)
        if self.pointer_404 and path.endswith("/room"):
            return httpx.Response(404, json={"error": "not_found"})
        return self.pub.handler(request)


@pytest.fixture
async def live(ctx, settings, monkeypatch):
    monkeypatch.setattr(lp, "TICK", 1000.0)  # housekeeping only when a test calls tick()
    monkeypatch.setattr(lp, "ROOM_BACKOFF", (0.01,))
    rooms = await FakeRoomHost().start()
    hosts = LiveHosts(FakeHost(), rooms)
    ctx.http_transport = httpx.MockTransport(hosts.handler)
    settings.publish_host = rooms.url
    ctx.live_bridges.enabled = True
    yield hosts
    await ctx.live_bridges.shutdown()
    await rooms.close()


async def _until(cond, what: str, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := cond():
            return value
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


async def _bridge(ctx, sid) -> lp.PubBridge:
    """The meeting's publication bridge, once it is in its room."""
    pub_id = ctx.repo.live_publication(sid)["id"]

    def joined():
        b = ctx.live_bridges.get(pub_id)
        return b if b is not None and b.connected.is_set() else None

    return await _until(joined, "the bridge to join its room")


async def _reader(live, ctx, sid) -> GuestClient:
    """A reader's page in the room the host names, with the token from the link's key."""
    row = ctx.repo.live_publication(sid)
    return await GuestClient.join(live.rooms, live.pub.room, plf.room_token(row["key"]))


def _opened_frames(reader, row, room, sign_pub) -> list[tuple[plf.Kind, dict]]:
    """What a reader received, opened and verified (snapshot first)."""
    out = []
    for _, env in reader.binary(T_SNAPSHOT) + reader.binary(T_NOTES):
        out.append(plf.open_signed(row["key"], room, env, sign_pub))
    return out


def _versions(reader, live, row, sign_pub) -> list[int]:
    return [
        body["version"]
        for kind, body in _opened_frames(reader, row, live.pub.room, sign_pub)
        if kind is plf.Kind.VERSION
    ]


def _sign_pub(row) -> bytes:
    return plf.sign_public(row["sign_key"])


async def test_a_publish_sends_version_with_the_manifest_sha(ctx, live, seals):
    s = _meeting(ctx)
    pub = await _first(ctx, s.id)
    bridge = await _bridge(ctx, s.id)
    row = ctx.repo.live_publication(s.id)
    room = live.pub.room  # the host names the room the bridge made
    assert room and bridge.room == room and live.rooms.room()[0] == room
    # The pointer before the room: an older host never gets one.
    puts = [p for mth, p in live.log if mth == "PUT" and ("/room" in p or "/live/" in p)]
    assert puts[:2] == [f"/pub/{row['id']}/room", f"/live/{room}"]
    reader = await _reader(live, ctx, s.id)
    await _until(lambda: bridge.readers == 1, "the reader to count")
    model = publishing.to_model(ctx, row, ctx.repo.get(s.id))
    assert (model.live, model.readers, model.live_reason) == ("live", 1, "")

    # The room's snapshot is a signed THREADS (empty until task 11), posted before the join.
    (snapshot,) = [b for _, p, _, b in live.rooms.calls if p == f"/live/{room}/snapshot"]
    kind, body = plf.open_signed(row["key"], room, snapshot, _sign_pub(row))
    assert (kind, body) == (
        plf.Kind.THREADS,
        {"threads": [], "pending": {}, "blocked": [], "at": body["at"]},
    )
    # The reader joined late: from the log, the join's META, then version 1 (queued before the
    # bridge was in the room; readers ignore a version they show already).
    frames = await _until(
        lambda: len(f := _opened_frames(reader, row, room, _sign_pub(row))) >= 2 and f,
        "META and VERSION",
    )
    assert frames[0] == (
        plf.Kind.META,
        {"follow": True, "comments": False, "notes": False, "at": frames[0][1]["at"]},
    )
    assert frames[1][0] is plf.Kind.VERSION and frames[1][1]["version"] == 1

    # An Update (comments turned on): META says so at once, then VERSION with the new sha.
    await _first(ctx, s.id, comments=True)
    await _until(lambda: 2 in _versions(reader, live, row, _sign_pub(row)), "the VERSION")
    m = _opened(live.pub, pub)
    got = [b for k, b in _opened_frames(reader, row, room, _sign_pub(row)) if k == plf.Kind.VERSION]
    assert got[-1] == {
        "version": 2,
        "sha256": hashlib.sha256(live.pub.manifest).hexdigest(),
        "published_at": m.published_at,
        "expires_at": m.expires_at,
    }
    metas = [b for k, b in _opened_frames(reader, row, room, _sign_pub(row)) if k == plf.Kind.META]
    assert metas[-1]["comments"] is True and metas[-1]["at"] > metas[0]["at"]
    assert bridge.version is None  # acknowledged by the room
    # A frame signed by anyone else does not open as the owner's.
    (_, env), *_ = reader.binary(T_NOTES)
    with pytest.raises(plf.PubLiveFormatError):
        plf.open_signed(row["key"], room, env, plf.sign_public(plf.new_private()))
    await reader.close()


async def test_a_4410_makes_a_new_room_and_moves_the_pointer(ctx, live, seals):
    s = _meeting(ctx)
    await _first(ctx, s.id)
    bridge = await _bridge(ctx, s.id)
    row = ctx.repo.live_publication(s.id)
    old = live.pub.room
    # The host deletes the room (expired, idled out, removed): the bridge hears 4410.
    gone = await live.rooms.handler(
        httpx.Request(
            "DELETE",
            f"{live.rooms.url}/live/{old}",
            headers={"authorization": f"Bearer {row['token']}"},
        )
    )
    assert gone.status_code == 204
    await _until(
        lambda: (
            live.pub.room not in (None, old)
            and bridge.connected.is_set()
            and bridge.room == live.pub.room
        ),
        "a new room",
    )
    new = live.pub.room
    assert live.rooms.room()[0] == new and live.rooms.rooms[old].deleted
    # The old id was tried first (410: refused for a while), then the new one was made.
    assert ("PUT", f"/live/{old}") in live.log and bridge.joins == 2
    assert lp._read_room(ctx, row["id"]) == (new, bridge.seq)
    reader = await _reader(live, ctx, s.id)
    await _first(ctx, s.id)  # versions reach the new room
    await _until(lambda: 2 in _versions(reader, live, row, _sign_pub(row)), "the VERSION")
    await reader.close()


async def test_a_stale_pointer_at_start_gets_a_new_room(ctx, live, seals):
    s = _meeting(ctx)
    await _first(ctx, s.id)
    first = await _bridge(ctx, s.id)
    old = first.room
    await ctx.live_bridges.shutdown()
    live.rooms.rooms[old].deleted = True  # gone while this computer was off
    assert await ctx.live_bridges.resume() == 1
    bridge = await _bridge(ctx, s.id)
    assert bridge.room != old and live.pub.room == bridge.room


@pytest.mark.parametrize("old_host", ["bad_sha256", "404"])
async def test_an_older_host_is_unsupported_and_gets_no_room(ctx, live, seals, old_host):
    if old_host == "404":
        live.pointer_404 = True
    else:
        live.pub.pointer = False  # takes "room" for a blob's sha
    s = _meeting(ctx)
    await _first(ctx, s.id)
    row = ctx.repo.live_publication(s.id)
    bridge = await _until(
        lambda: (b := ctx.live_bridges.get(row["id"])) and b.state == "unsupported" and b,
        "unsupported",
    )
    model = publishing.to_model(ctx, row, ctx.repo.get(s.id))
    assert (model.live, model.live_reason, model.readers) == ("unsupported", lp.UNSUPPORTED, 0)
    assert live.rooms.rooms == {} and not any(p.startswith("/live/") for _, p in live.log)
    assert live.pub.room is None and not bridge.connected.is_set()
    # Versions still upload; readers poll.
    await _first(ctx, s.id)
    assert live.pub.version == 2 and live.rooms.rooms == {}


async def test_unpublish_deletes_the_room_after_the_publication(ctx, live, seals):
    """The publication goes first: a reader hearing 4410 re-reads `/pub/<id>` and must find
    it gone ("No longer published"), not still there (it would fall back to polling)."""
    s = _meeting(ctx)
    await _first(ctx, s.id)
    bridge = await _bridge(ctx, s.id)
    row = ctx.repo.live_publication(s.id)
    room = bridge.room
    reader = await _reader(live, ctx, s.id)
    live.log.clear()
    await publishing.unpublish(ctx, s.id)
    deletes = [(mth, p) for mth, p in live.log if mth == "DELETE"]
    assert deletes == [("DELETE", f"/pub/{row['id']}"), ("DELETE", f"/live/{room}")]
    await _until(lambda: reader.closed_code == 4410, "the reader to hear it")
    assert live.pub.manifest is None  # what the reader then reads: gone
    assert ctx.live_bridges.get(row["id"]) is None and not bridge.running
    assert live.rooms.rooms[room].deleted
    assert not lp._room_file(ctx, row["id"]).exists()


async def test_retire_deletes_the_room_when_the_pointer_is_already_gone(ctx, live, seals):
    """With the publication (and so its room pointer) gone from the host, retiring still
    deletes the room it knows, and a pointer route answering 404 raises nothing."""
    s = _meeting(ctx)
    await _first(ctx, s.id)
    bridge = await _bridge(ctx, s.id)
    row = ctx.repo.live_publication(s.id)
    room = bridge.room
    await publishing._host_delete(ctx, row["host_url"], row["id"], row["token"])
    assert live.pub.manifest is None and live.pub.room is None
    live.log.clear()
    await lp.retire(ctx, row, clear_pointer=True)
    assert ("DELETE", f"/live/{room}") in live.log
    assert ("PUT", f"/pub/{row['id']}/room") in live.log  # 404 there: nothing raised
    assert live.rooms.rooms[room].deleted and ctx.live_bridges.get(row["id"]) is None
    # Without a bridge or the room file either, nothing to find and nothing raised.
    await lp.retire(ctx, row, clear_pointer=True)


async def test_the_room_goes_when_both_switches_turn_off(ctx, live, seals):
    s = _meeting(ctx)
    await _first(ctx, s.id, comments=True)
    bridge = await _bridge(ctx, s.id)
    room = bridge.room
    await _first(ctx, s.id, follow=False)  # an Update with both off: the link stays
    await ctx.live_bridges.settle()
    row = ctx.repo.live_publication(s.id)
    assert ctx.live_bridges.get(row["id"]) is None and live.rooms.rooms[room].deleted
    assert live.pub.room is None and live.pub.manifest is not None and live.pub.version == 2
    model = publishing.to_model(ctx, row, ctx.repo.get(s.id))
    assert model.live == "off"
    # On again: a fresh room.
    await _first(ctx, s.id)
    again = await _bridge(ctx, s.id)
    assert again.room not in ("", room) and live.pub.room == again.room


async def test_the_room_goes_with_the_meeting_and_at_expiry(ctx, live, seals):
    a = _meeting(ctx)
    await _first(ctx, a.id)
    room_a = (await _bridge(ctx, a.id)).room
    after = publishing.forget_session(ctx, a.id)  # what deleting the meeting runs
    after()
    await _until(lambda: live.rooms.rooms[room_a].deleted, "the meeting's room to go")
    await _until(lambda: live.pub.manifest is None, "the publication to go")

    live.pub = FakeHost()  # a second publication (a FakeHost keeps one token)
    b = _meeting(ctx)
    await _first(ctx, b.id)
    bridge = await _bridge(ctx, b.id)
    room_b = bridge.room
    row = ctx.repo.live_publication(b.id)
    row["expires_at"] = "2026-01-01T00:00:00+00:00"
    ctx.repo.put_publication(row)
    publishing.sweep_expired(ctx)
    await ctx.live_bridges.settle()
    assert live.rooms.rooms[room_b].deleted and ctx.live_bridges.get(row["id"]) is None


async def test_heartbeat_and_the_rooms_expiry(ctx, live, seals):
    s = _meeting(ctx)
    await _first(ctx, s.id, expires_days=3)
    bridge = await _bridge(ctx, s.id)
    row = ctx.repo.live_publication(s.id)
    room = bridge.room

    def room_expiries():
        return [
            h["x-room-expires"]
            for mth, p, h, _ in live.rooms.calls
            if (mth, p) == ("PUT", f"/live/{room}")
        ]

    # Never past the publication's own expiry (3 days, inside the room's 7).
    expires = datetime.fromisoformat(row["expires_at"]).astimezone(UTC)
    assert room_expiries() == [expires.strftime("%Y-%m-%dT%H:%M:%SZ")]
    reader = await _reader(live, ctx, s.id)

    def metas():
        return [
            b for k, b in _opened_frames(reader, row, room, _sign_pub(row)) if k is plf.Kind.META
        ]

    await _until(lambda: len(metas()) == 1, "the join's META")
    now = time.monotonic()
    await bridge.tick(now + 60)  # nothing due
    bridge._since_snapshot = False  # (the join's frames are pruned further down)
    await bridge.tick(now + lp.META_EVERY + 1)  # the heartbeat
    await _until(lambda: len(metas()) == 2, "the heartbeat")
    assert len(room_expiries()) == 1
    await bridge.tick(now + lp.EXTEND_EVERY + 1)  # the room's expiry moved; the log pruned
    assert len(room_expiries()) == 2 and room_expiries()[-1] == room_expiries()[0]

    def snapshots():
        return [p for _, p, _, _ in live.rooms.calls if p.endswith("/snapshot")]

    # That tick was also a heartbeat (12 h after the last), and as frames went out since the
    # last snapshot it pruned the log with one, which META follows (a reader joining past the
    # snapshot must still hear it).
    assert len(snapshots()) == 2
    await _until(lambda: len(metas()) == 4, "the heartbeat and META after the snapshot")
    await _until(lambda: not bridge.unacked, "the acks")
    assert live.rooms.rooms[room].snapshot_seq > 0
    # The META after a snapshot is no news: idle, no snapshot follows it.
    await bridge.tick(now + lp.EXTEND_EVERY + lp.SNAPSHOT_EVERY + 2)
    assert len(snapshots()) == 2 and len(metas()) == 4
    await reader.close()


async def test_resume_joins_the_same_room_with_the_seq_it_applied(ctx, live, seals):
    s = _meeting(ctx)
    await _first(ctx, s.id)
    first = await _bridge(ctx, s.id)
    await _until(lambda: first.seq >= 1, "the META's ack")
    room, seq = first.room, first.seq
    await ctx.live_bridges.shutdown()
    assert await ctx.live_bridges.resume() == 1
    bridge = await _bridge(ctx, s.id)
    assert bridge is not first and bridge.room == room
    assert live.rooms.joins[-1] == (room, seq)
    # A meeting recorded on the other computer is not bridged here.
    with ctx.repo._lock, ctx.repo._conn:
        ctx.repo._conn.execute("UPDATE sessions SET origin='peer-machine' WHERE id=?", (s.id,))
    await ctx.live_bridges.shutdown()
    assert await ctx.live_bridges.resume() == 0


async def test_an_update_from_the_other_computer_pushes_one_version(ctx, live, seals):
    """C12: the computer that does not bridge the publication sends its VERSION through a
    one-shot connection to the room the host names, and makes no room."""
    s = _meeting(ctx)
    await _first(ctx, s.id)
    bridge = await _bridge(ctx, s.id)
    row = ctx.repo.live_publication(s.id)
    room = bridge.room
    reader = await _reader(live, ctx, s.id)
    await ctx.live_bridges.shutdown()  # the origin's bridge is elsewhere now
    with ctx.repo._lock, ctx.repo._conn:
        ctx.repo._conn.execute("UPDATE sessions SET origin='peer-machine' WHERE id=?", (s.id,))
    joins = len(live.rooms.joins)
    await _first(ctx, s.id)  # a manual Update here
    await ctx.live_bridges.settle()
    await _until(lambda: 2 in _versions(reader, live, row, _sign_pub(row)), "the VERSION")
    assert ctx.live_bridges.get(row["id"]) is None  # no bridge here
    assert live.rooms.joins[joins:] == [(room, lp.NO_REPLAY)]  # one join, no replay
    assert live.pub.room == room and list(live.rooms.rooms) == [room]
    await reader.close()


async def test_no_room_no_push(ctx, live, seals):
    s = _meeting(ctx)
    with ctx.repo._lock, ctx.repo._conn:
        ctx.repo._conn.execute("UPDATE sessions SET origin='peer-machine' WHERE id=?", (s.id,))
    await _first(ctx, s.id)
    await ctx.live_bridges.settle()
    assert not any(p.startswith("/live/") for _, p in live.log)
    assert live.rooms.joins == [] and ctx.live_bridges.by_id == {}


async def test_readers_frames_go_to_the_hook_and_forged_owner_frames_nowhere(
    ctx, live, seals, monkeypatch
):
    seen = []

    async def hook(self, env, seq):
        seen.append((env, seq))

    monkeypatch.setattr(lp.PubBridge, "_on_reader_frame", hook)
    s = _meeting(ctx)
    await _first(ctx, s.id, comments=True)
    bridge = await _bridge(ctx, s.id)
    row = ctx.repo.live_publication(s.id)
    room = bridge.room
    reader = await _reader(live, ctx, s.id)
    reader_key = plf.new_private()
    op = {"op": "create", "id": "a" * 12, "text": "Why Friday?", "name": "Ana", "at": 1}
    comment = plf.box_op(row["key"], room, op, reader_key, plf.box_public(row["box_key"]))
    forged = plf.sign_frame(
        row["key"], room, plf.Kind.THREADS, {"threads": [], "at": "x"}, reader_key
    )
    await reader.send(bytes([T_NOTES]) + forged)
    await reader.send(bytes([T_NOTES]) + comment)
    await _until(lambda: len(seen) == 1 and bridge.dropped == 1, "the frames")
    assert seen[0][0] == comment
    assert bridge.seq == seen[0][1] == live.rooms.rooms[room].seq
    # Only the owner's box key opens it.
    assert plf.open_box(row["key"], room, comment, row["box_key"]).op == op
    await reader.close()


# ---- readers' comments (task 11) ----

ANCHOR = {
    "target": "",
    "target_id": None,
    "quote_id": None,
    "exact": None,
    "prefix": None,
    "suffix": None,
}


def _at_n(n: int) -> str:
    """The reader's clock: n seconds after 10:00, as `Date.toISOString()` writes it."""
    return f"2026-11-05T10:{n // 60:02d}:{n % 60:02d}.000Z"


def _create(cid: str, text: str, n: int = 0, name: str = "Ana", **anchor) -> dict:
    return {
        "op": "create",
        "id": cid,
        "parent_id": None,
        "text": text,
        "name": name,
        "at": _at_n(n),
        "anchor": {**ANCHOR, **anchor},
    }


def _reply(cid: str, parent: str, text: str, n: int = 0, name: str = "Ana") -> dict:
    op = _create(cid, text, n, name)
    return {**op, "op": "reply", "parent_id": parent, "anchor": None}


def _edit(cid: str, text: str, n: int, name: str = "Ana") -> dict:
    return {**_create(cid, text, n, name), "op": "edit", "anchor": None}


def _delete(cid: str, n: int, name: str = "Ana") -> dict:
    return {**_create(cid, "x", n, name), "op": "delete", "text": None, "anchor": None}


class Reader:
    """A reader's page: its own Ed25519 key, its room connection, what it sent."""

    def __init__(self, client: GuestClient, row: dict, room: str):
        self.client, self.row, self.room = client, row, room
        self.key = plf.new_private()
        self.rid = plf.rid_of(plf.sign_public(self.key))

    async def send(self, op: dict) -> bytes:
        env = plf.box_op(
            self.row["key"], self.room, op, self.key, plf.box_public(self.row["box_key"])
        )
        await self.client.send(bytes([T_NOTES]) + env)
        return env

    def frames(self) -> list[tuple[plf.Kind, dict]]:
        """Everything it received, opened and VERIFIED under the row's signing key (a frame
        that does not verify raises: every THREADS and META the bridge sent is signed)."""
        out = []
        for _, env in self.client.binary(T_SNAPSHOT) + self.client.binary(T_NOTES):
            kind, _ = plf.unseal(self.row["key"], self.room, env)
            if kind is plf.Kind.COMMENT:
                continue  # another reader's, relayed by the room
            out.append(plf.open_signed(self.row["key"], self.room, env, _sign_pub(self.row)))
        return out

    def threads(self) -> list[dict] | None:
        bodies = [b for k, b in self.frames() if k is plf.Kind.THREADS]
        return max(bodies, key=lambda b: b["at"])["threads"] if bodies else None

    def metas(self) -> list[dict]:
        return [b for k, b in self.frames() if k is plf.Kind.META]

    def threads_frame(self) -> dict | None:
        """The newest THREADS body, `pending` included."""
        bodies = [b for k, b in self.frames() if k is plf.Kind.THREADS]
        return max(bodies, key=lambda b: b["at"]) if bodies else None


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(lp, "THREADS_DEBOUNCE", 0.01)


async def _room_with_comments(ctx, live, **sel):
    s = _meeting(ctx)
    await _first(ctx, s.id, comments=True, **sel)
    bridge = await _bridge(ctx, s.id)
    row = ctx.repo.live_publication(s.id)
    client = await _reader(live, ctx, s.id)
    return s, bridge, row, Reader(client, row, bridge.room)


async def _taken(bridge, n: int) -> dict:
    await _until(lambda: sum(bridge.outcomes.values()) >= n, f"{n} reader ops")
    return bridge.outcomes


def _comments(ctx, sid):
    from mnemosyne.services.shared_notes import unrestricted

    with unrestricted():
        return ctx.repo.list_comments(sid)


async def test_a_readers_comment_on_a_decision(ctx, live, seals, quick):
    s, bridge, row, ana = await _room_with_comments(ctx, live)
    await ana.send(_create("a" * 12, "Why Friday?", target="decision", target_id="dec1"))
    assert (await _taken(bridge, 1)) == {"applied": 1}
    (c,) = _comments(ctx, s.id)
    assert (c.id, c.author_id, c.author) == ("a" * 12, f"reader:{ana.rid}", "Ana (reader)")
    assert (c.target, c.target_id, c.text) == ("decision", "dec1", "Why Friday?")
    # An ordinary comment: in the meeting's history, by the reader.
    (event,) = [e for e in ctx.repo.events(s.id) if e["kind"] == "commented"]
    assert event["detail"]["by"] == "Ana (reader)" and event["detail"]["target"] == "decision"
    # Back to the readers as THREADS (signed: Reader.frames verifies every frame).
    threads = await _until(ana.threads, "THREADS with the comment")
    (t,) = threads
    assert t["replies"] == [] and t["root"]["rid"] == ana.rid
    assert t["root"]["anchor"] == {
        "target": "decision",
        "target_id": "dec1",
        "quote_id": None,
        "quote": _opened(live.pub, {"url": row_url(ctx, s.id)}).decisions[0].text,
    }
    assert t["root"]["anchor"]["quote"] == "Ship on Friday (Person 1)"  # nobody listed
    assert set(t["root"]) == {
        "id",
        "parent_id",
        "author",
        "rid",
        "text",
        "anchor",
        "created_at",
        "updated_at",
        "resolved",
        "deleted",
        "detached",
        "changed",
    }
    assert t["root"]["author"] == "Ana (reader)" and t["root"]["parent_id"] is None
    assert t["root"]["created_at"].endswith("Z")
    # The same THREADS is the room's snapshot.
    snap = [b for _, p, _, b in live.rooms.calls if p == f"/live/{bridge.room}/snapshot"][-1]
    kind, body = plf.open_signed(row["key"], bridge.room, snap, _sign_pub(row))
    assert kind is plf.Kind.THREADS and body["threads"] == threads
    await ana.client.close()


async def test_a_comment_on_a_quote_is_on_its_transcript_lines(ctx, live, seals, quick):
    s, bridge, row, ana = await _room_with_comments(
        ctx, live, quotes=[QuoteSpan(start=0.0, end=6.0)]
    )
    (quote,) = _opened(live.pub, {"url": row_url(ctx, s.id)}).quotes
    await ana.send(_create("b" * 12, "Lovely line", target="quote", quote_id=quote.id))
    assert (await _taken(bridge, 1)) == {"applied": 1}
    (c,) = _comments(ctx, s.id)
    assert c.target == "segment" and c.anchor == {"part": 0, "start": 0.0, "end": 6.0}
    from mnemosyne.services import comments
    from mnemosyne.services.shared_notes import unrestricted

    with unrestricted():
        (t,) = comments.threads(ctx, s.id).threads
    assert t.root.segment_idx in (0, 1) and not t.root.detached
    (sent,) = await _until(ana.threads, "THREADS")
    assert sent["root"]["anchor"] == {
        "target": "segment",
        "target_id": None,
        "quote_id": quote.id,
        "quote": quote.text,
    }
    # A quote the publication does not show: nothing is made.
    await ana.send(_create("c" * 12, "Hm", 1, target="quote", quote_id="q00000000"))
    assert (await _taken(bridge, 2))["refused"] == 1 and len(_comments(ctx, s.id)) == 1
    await ana.client.close()


def row_url(ctx, sid) -> str:
    return publishing.to_model(ctx, ctx.repo.live_publication(sid), ctx.repo.get(sid)).url


async def test_summary_text_with_a_redacted_name_stays_on_the_meeting(ctx, live, seals, quick):
    # Cy Park is not listed: readers read "Person 1 was away."
    s, bridge, row, ana = await _room_with_comments(ctx, live, attendees=["Ana Lopez", "Bo Chen"])
    m = _opened(live.pub, {"url": row_url(ctx, s.id)})
    assert "Person 1 was away." in m.summary
    await ana.send(
        _create(
            "d" * 12,
            "Who was away?",
            target="body",
            exact="Person 1 was away.",
            prefix="decided to ship Friday. ",
            suffix="",
        )
    )
    await ana.send(_create("e" * 12, "Friday!", 1, target="body", exact="ship Friday"))
    assert (await _taken(bridge, 2)) == {"applied": 2}
    kept, found = sorted(_comments(ctx, s.id), key=lambda c: c.id)
    assert kept.target == "" and kept.text == "On: “Person 1 was away.”\n\nWho was away?"
    assert found.target == "body" and found.anchor["exact"] == "ship Friday"
    threads = await _until(lambda: (t := ana.threads()) and len(t) == 2 and t, "THREADS")
    by_id = {t["root"]["id"]: t["root"] for t in threads}
    assert by_id["d" * 12]["anchor"] == {**lp.NO_ANCHOR}
    assert by_id["e" * 12]["anchor"]["target"] == "body"
    assert by_id["e" * 12]["anchor"]["quote"] == "ship Friday"
    assert "Cy" not in json.dumps(threads)
    await ana.client.close()


def _forged_box(row, room, op: dict, signer: bytes, claimed_pub: bytes) -> bytes:
    """A COMMENT boxed for the owner whose signature is by `signer`, not by the key it
    carries (`claimed_pub`): what a reader impersonating another would send."""
    import os

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    op_bytes = pf.canonical_json(op)
    sig = plf.ed25519_sign(signer, plf.reader_message(room, claimed_pub, op_bytes))
    content = sig + claimed_pub + op_bytes
    eph = plf.new_private()
    eph_pub = plf.box_public(eph)
    key = plf.box_key(plf.x25519(eph, plf.box_public(row["box_key"])), room)
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, content, plf.box_aad(room, eph_pub))
    return plf.seal(row["key"], room, plf.Kind.COMMENT, eph_pub + nonce + ct)


async def test_ops_that_are_not_the_readers_own_are_refused(ctx, live, seals, quick):
    s, bridge, row, ana = await _room_with_comments(ctx, live)
    await ana.send(_create("a" * 12, "Mine", target=""))
    await _taken(bridge, 1)
    bo = Reader(await _reader(live, ctx, s.id), row, bridge.room)
    # Bo signs an edit with his key but carries Ana's public key: the box opens, the
    # signature does not match the key inside.
    forged = _forged_box(
        row, bridge.room, _edit("a" * 12, "Not mine", 5), bo.key, plf.sign_public(ana.key)
    )
    await bo.client.send(bytes([T_NOTES]) + forged)
    # Signed as himself, his rid is not the comment's author.
    await bo.send(_edit("a" * 12, "Not mine", 6, name="Bo"))
    await bo.send(_delete("a" * 12, 7, name="Bo"))
    outcomes = await _taken(bridge, 4)
    assert outcomes == {"applied": 1, "unopened": 1, "not_yours": 2}
    (c,) = _comments(ctx, s.id)
    assert c.text == "Mine" and not c.deleted
    # Ana herself may.
    await ana.send(_edit("a" * 12, "Mine, edited", 8))
    await _taken(bridge, 5)
    assert _comments(ctx, s.id)[0].text == "Mine, edited"
    await ana.client.close()
    await bo.client.close()


async def test_a_replayed_box_is_a_no_op(ctx, live, seals, quick):
    from mnemosyne.services import comments
    from mnemosyne.services.shared_notes import unrestricted

    s, bridge, row, ana = await _room_with_comments(ctx, live)
    create = await ana.send(_create("a" * 12, "First", target=""))
    old_edit = await ana.send(_edit("a" * 12, "Second", 5))
    await ana.send(_edit("a" * 12, "Third", 9))
    await _taken(bridge, 3)
    # Anyone in the room can copy a frame and send it again: the same create, an older edit.
    for env in (create, old_edit):
        await ana.client.send(bytes([T_NOTES]) + env)
    outcomes = await _taken(bridge, 5)
    assert outcomes == {"applied": 3, "replay": 2}
    (c,) = _comments(ctx, s.id)
    assert c.text == "Third"
    # The owner deletes it; the create sent again does not bring it back, after a restart too.
    with unrestricted():
        comments.delete(ctx, s.id, "a" * 12)
    assert _comments(ctx, s.id) == []
    await ctx.live_bridges.shutdown()
    await ctx.live_bridges.resume()
    bridge = await _bridge(ctx, s.id)
    await ana.client.send(bytes([T_NOTES]) + create)
    await _taken(bridge, 1)
    assert bridge.outcomes == {"replay": 1} and _comments(ctx, s.id) == []
    await ana.client.close()


async def test_the_21st_op_in_ten_minutes_is_dropped(ctx, live, seals, quick):
    s, bridge, row, ana = await _room_with_comments(ctx, live)
    clock = Clock()
    bridge.clock = clock
    for i in range(21):
        await ana.send(_create(f"{i:012x}", f"Comment {i}", i))
    outcomes = await _taken(bridge, 21)
    assert outcomes == {"applied": 20, "rate": 1}
    assert len(_comments(ctx, s.id)) == 20
    clock.t += lp.RATE_WINDOW  # ten minutes on: again
    await ana.send(_create(f"{99:012x}", "Later", 99))
    await _taken(bridge, 22)
    assert bridge.outcomes["applied"] == 21
    await ana.client.close()


async def test_caps_on_text_and_name(ctx, live, seals, quick):
    s, bridge, row, ana = await _room_with_comments(ctx, live)
    await ana.send(_create("a" * 12, "x" * (lp.MAX_TEXT + 1)))
    await ana.send(_create("b" * 12, "   "))
    await ana.send(_create("c" * 12, "y" * lp.MAX_TEXT, 1, name="N" * 200))
    outcomes = await _taken(bridge, 3)
    assert outcomes == {"bad": 2, "applied": 1}
    (c,) = _comments(ctx, s.id)
    assert c.author == "N" * lp.MAX_NAME + " (reader)" and len(c.text) == lp.MAX_TEXT
    await ana.client.close()


async def test_comments_off_drops_the_op_and_meta_says_so(ctx, live, seals, quick):
    s = _meeting(ctx)
    await _first(ctx, s.id, comments=False)  # follow only
    bridge = await _bridge(ctx, s.id)
    row = ctx.repo.live_publication(s.id)
    ana = Reader(await _reader(live, ctx, s.id), row, bridge.room)
    await _until(lambda: len(ana.metas()) == 1, "the join's META")
    bridge._meta_at = float("-inf")  # (the join's META is more than META_NUDGE ago)
    await ana.send(_create("a" * 12, "Hello?"))
    assert (await _taken(bridge, 1)) == {"off": 1}
    assert _comments(ctx, s.id) == []
    metas = await _until(lambda: len(m := ana.metas()) == 2 and m, "META again")
    assert metas[-1]["comments"] is False
    await ana.client.close()


async def test_owner_replies_resolves_and_deletes_reach_readers(ctx, live, settings, seals, quick):
    from mnemosyne.models.comment import CommentCreate
    from mnemosyne.services import comments
    from mnemosyne.services.shared_notes import unrestricted

    s, bridge, row, ana = await _room_with_comments(ctx, live, attendees=["Ana Lopez", "Bo Chen"])
    await ana.send(_create("a" * 12, "Why Friday?", target="decision", target_id="dec1"))
    await ana.send(_create("b" * 12, "And the notes?", 1, target="item", target_id="act1"))
    await _taken(bridge, 2)
    settings.local_speaker_name = "Me"  # the desktop's default name: "Owner" to readers
    with unrestricted():
        comments.create(
            ctx,
            s.id,
            CommentCreate(parent_id="a" * 12, text="Cy Park asked; card 4111111111111111"),
        )
        comments.create(ctx, s.id, CommentCreate(text="Our own note on Cy Park"))
    t = await _until(
        lambda: (ts := ana.threads()) and any(x["replies"] for x in ts) and ts, "the reply"
    )
    assert len(t) == 2  # the owner's own thread stays in the app
    (reply,) = [x for x in t if x["root"]["id"] == "a" * 12][0]["replies"]
    assert reply["author"] == "Owner" and reply["rid"] is None
    assert "Cy" not in reply["text"] and "Person 1 asked" in reply["text"]
    assert "4111111111111111" not in reply["text"]
    # The reader replies in the thread; the owner resolves it and deletes the other thread.
    await ana.send(_reply("c" * 12, "a" * 12, "Thanks", 2))
    await _taken(bridge, 3)
    with unrestricted():
        comments.resolve(ctx, s.id, "a" * 12)
        comments.delete(ctx, s.id, "b" * 12)

    def settled():
        ts = ana.threads() or []
        roots = {x["root"]["id"]: x for x in ts}
        if "b" * 12 not in roots or not roots["b" * 12]["root"]["deleted"]:
            return None
        return roots if roots["a" * 12]["root"]["resolved"] else None

    roots = await _until(settled, "resolved and removed")
    assert [r["id"] for r in roots["a" * 12]["replies"]] == [reply["id"], "c" * 12]
    removed = roots["b" * 12]["root"]
    assert removed["text"] == "" and removed["deleted"] and removed["anchor"]["quote"] is None
    assert removed["author"] == "Ana (reader)" and roots["b" * 12]["replies"] == []
    # A reader may not reply to the owner's own thread (it is not theirs to see).
    with unrestricted():
        own = [c for c in _comments(ctx, s.id) if not c.author_id.startswith("reader:")]
    root_id = [c.id for c in own if not c.parent_id][0]
    await ana.send(_reply("d" * 12, root_id, "Peek", 3))
    assert (await _taken(bridge, 4))["refused"] == 1
    await ana.client.close()


async def test_threads_for_readers(ctx, settings):
    from mnemosyne.models.comment import CommentCreate
    from mnemosyne.services import comments
    from mnemosyne.services.shared_notes import unrestricted

    s = _meeting(ctx)
    ana = ("reader:" + "1" * 16, "Ana (reader)")
    sel = PublicationSelection(
        where="host",
        attendees=["Ana Lopez", "Bo Chen"],
        questions=False,
        names="as_is",
        quotes=[QuoteSpan(start=0.0, end=6.0)],
    )
    with unrestricted():

        def make(cid, actor=None, **kw):
            return comments.create(ctx, s.id, CommentCreate(**kw), comment_id=cid, actor=actor)

        make("a" * 12, ana, target="decision", target_id="dec1", text="Is Cy Park OK?")
        settings.local_speaker_name = "Cy Park"
        make("a1" + "0" * 10, parent_id="a" * 12, text="Cy Park said yes")
        make("b" * 12, text="The owner's own thread")  # never out
        make("c" * 12, ana, target="question", target_id="que1", text="Unticked part")
        make("d" * 12, ana, target="segment", idx=3, text="A line no quote covers")
        make("e" * 12, ana, target="segment", idx=0, idx_to=1, text="In the quote")
        ctx.repo.add_comment(  # (comments.create needs the sidecar for the notes)
            s.id,
            id="f" * 12,
            text="On the notes",
            author_id=ana[0],
            author=ana[1],
            target="notes",
            anchor={"mark": "comment:" + "f" * 12},
            quote="scratch",
        )
    threads = lp.threads_for_readers(ctx, s.id, sel)
    assert [t["root"]["id"] for t in threads] == ["a" * 12, "e" * 12]
    first = threads[0]
    assert first["root"]["text"] == "Is Cy Park OK?"  # a reader's words as they wrote them
    (reply,) = first["replies"]
    assert reply["author"] == "Person 1" and reply["text"] == "Person 1 said yes"
    assert reply["anchor"] == lp.NO_ANCHOR and reply["rid"] is None
    assert first["root"]["rid"] == "1" * 16
    content, specs = publishing.build_content(ctx, ctx.repo.get(s.id), sel)
    assert threads[1]["root"]["anchor"]["quote_id"] == specs[0]["id"]
    assert threads[1]["root"]["anchor"]["quote"] == content["quotes"][0]["text"]
    # Decisions unticked: that thread goes too.
    sel2 = sel.model_copy(update={"decisions": False})
    assert [t["root"]["id"] for t in lp.threads_for_readers(ctx, s.id, sel2)] == ["e" * 12]


async def test_leak_a_comment_frames_open_only_for_the_owner(ctx, live, seals, monkeypatch):
    """Leak test (a): every reader holds the frames key and the room replays its log to every
    joiner, yet two readers' COMMENT frames open for the bridge (the box key) and for neither
    reader; and the second reader's edit or delete under the first's comment id is refused."""
    monkeypatch.setattr(lp, "THREADS_DEBOUNCE", 1000.0)  # no snapshot: the log is kept whole
    s, bridge, row, ana = await _room_with_comments(ctx, live)
    bo = Reader(await _reader(live, ctx, s.id), row, bridge.room)
    sent = [
        await ana.send(_create("a" * 12, "Ana's secret-ish remark", target="")),
        await bo.send(_create("b" * 12, "Bo's remark", target="")),
    ]
    await _taken(bridge, 2)
    late = await GuestClient.join(live.rooms, bridge.room, plf.room_token(row["key"]), since=0)
    logged = await _until(
        lambda: (
            len(c := [e for _, e in late.binary(T_NOTES) if e[0] == plf.Kind.COMMENT]) == 2 and c
        ),
        "the log's COMMENT frames",
    )
    assert logged == sent
    for env in logged:
        assert plf.open_box(row["key"], bridge.room, env, row["box_key"]).op["text"]
        for reader in (ana, bo):
            with pytest.raises(plf.PubLiveFormatError):
                plf.open_box(row["key"], bridge.room, env, reader.key)
            with pytest.raises(plf.PubLiveFormatError):
                plf.open_signed(row["key"], bridge.room, env, plf.sign_public(reader.key))
        _, plain = plf.unseal(row["key"], bridge.room, env)  # what the frames key shows
        assert b"remark" not in plain
    await bo.send(_edit("a" * 12, "Bo was here", 5, name="Bo"))
    await bo.send(_delete("a" * 12, 6, name="Bo"))
    assert (await _taken(bridge, 4))["not_yours"] == 2
    texts = sorted(c.text for c in _comments(ctx, s.id))
    assert texts == ["Ana's secret-ish remark", "Bo's remark"]
    for c in (ana.client, bo.client, late):
        await c.close()


async def test_an_owner_reply_names_people_as_the_manifest_does(ctx):
    """One Redaction for the manifest and for the threads: someone left off the attendee list
    is the same "Person N" in an owner's reply as everywhere in the publication."""
    from mnemosyne.models.comment import CommentCreate
    from mnemosyne.services import comments
    from mnemosyne.services.shared_notes import unrestricted

    s = _meeting(ctx)
    # Only Ana listed: Bo Chen is "Person 1", Cy Park "Person 2" in the manifest.
    sel = PublicationSelection(where="host", attendees=["Ana Lopez"])
    content, _ = publishing.build_content(ctx, ctx.repo.get(s.id), sel)
    assert "Person 2 was away." in content["summary"]
    assert content["questions"][0]["text"] == "Does Person 2 know?"
    reader = ("reader:" + "2" * 16, "Ana (reader)")
    with unrestricted():
        comments.create(ctx, s.id, CommentCreate(text="Who?"), comment_id="a" * 12, actor=reader)
        comments.create(
            ctx, s.id, CommentCreate(parent_id="a" * 12, text="Cy Park, and Bo Chen too")
        )
    (thread,) = lp.threads_for_readers(ctx, s.id, sel)
    assert thread["replies"][0]["text"] == "Person 2, and Person 1 too"


# ---- approving readers' comments before other readers see them (task 13b) ---------------------

ANA = ("reader:" + "1" * 16, "Ana (reader)")
BO = ("reader:" + "2" * 16, "Bo (reader)")


def _moderated(ctx, **sel) -> tuple[str, PublicationSelection]:
    s = _meeting(ctx)
    return s.id, PublicationSelection(where="host", comments=True, moderate=True, **sel)


def _make(ctx, sid, cid, actor=None, **kw):
    from mnemosyne.models.comment import CommentCreate
    from mnemosyne.services import comments
    from mnemosyne.services.shared_notes import unrestricted

    with unrestricted():
        return comments.create(ctx, sid, CommentCreate(**kw), comment_id=cid, actor=actor)


def _ids(threads) -> list[str]:
    return [t["root"]["id"] for t in threads]


async def test_moderate_leaves_out_unapproved_readers_comments_and_lists_their_ids(ctx):
    from mnemosyne.services import comments
    from mnemosyne.services.shared_notes import unrestricted

    sid, sel = _moderated(ctx)
    _make(ctx, sid, "a" * 12, ANA, text="Waiting root")
    _make(ctx, sid, "b" * 12, ANA, text="Approved root")
    _make(ctx, sid, "c" * 12, BO, text="Reply of Bo's", parent_id="b" * 12)  # unapproved reply
    _make(ctx, sid, "d" * 12, ANA, text="Hidden root")
    _make(ctx, sid, "e" * 12, ANA, text="Approved reply", parent_id="b" * 12)
    _make(ctx, sid, "f" * 12, parent_id="b" * 12, text="The owner needs no approval")
    _make(ctx, sid, "9" * 12, ANA, text="Deleted while waiting")
    _make(ctx, sid, "8" * 12, BO, text="Unapproved reply to a waiting root", parent_id="a" * 12)
    with unrestricted():
        comments.approve(ctx, sid, "b" * 12)
        comments.approve(ctx, sid, "e" * 12)
        comments.approve(ctx, sid, "d" * 12)
        comments.hide(ctx, sid, "d" * 12)
        comments.delete(ctx, sid, "9" * 12)
    threads = lp.threads_for_readers(ctx, sid, sel)
    assert _ids(threads) == ["b" * 12]  # the waiting root takes its thread, replies included
    assert [r["id"] for r in threads[0]["replies"]] == ["e" * 12, "f" * 12]
    # What is waiting, per writer: ids only, a deleted one is not waiting, a hidden one is not.
    waiting = lp.pending_for_readers(ctx, sid, sel)
    assert waiting == {"1" * 16: ["a" * 12], "2" * 16: ["8" * 12, "c" * 12]}
    blob = json.dumps(threads) + json.dumps(waiting)
    for text in ("Waiting root", "Reply of Bo", "Hidden root", "Unapproved reply", "Deleted"):
        assert text not in blob
    # Approving brings a thread (and the reply that was waiting under it) in.
    with unrestricted():
        comments.approve(ctx, sid, "a" * 12)
        comments.approve(ctx, sid, "c" * 12)
    threads = lp.threads_for_readers(ctx, sid, sel)
    assert _ids(threads) == ["a" * 12, "b" * 12]
    assert [r["id"] for r in threads[1]["replies"]] == ["c" * 12, "e" * 12, "f" * 12]
    assert lp.pending_for_readers(ctx, sid, sel) == {"2" * 16: ["8" * 12]}


async def test_without_moderate_everything_shows_but_what_the_owner_hid(ctx):
    from mnemosyne.services import comments
    from mnemosyne.services.shared_notes import unrestricted

    sid, sel = _moderated(ctx)
    _make(ctx, sid, "a" * 12, ANA, text="One")
    _make(ctx, sid, "b" * 12, ANA, text="Two")
    _make(ctx, sid, "c" * 12, BO, text="A reply", parent_id="b" * 12)
    _make(ctx, sid, "d" * 12, BO, text="A hidden reply", parent_id="b" * 12)
    with unrestricted():
        comments.hide(ctx, sid, "a" * 12)
        comments.hide(ctx, sid, "d" * 12)
    for off in (
        sel.model_copy(update={"moderate": False}),
        # moderate only counts while comments are on
        sel.model_copy(update={"comments": False}),
    ):
        threads = lp.threads_for_readers(ctx, sid, off)
        assert _ids(threads) == ["b" * 12]  # Hide is the owner's kill switch, moderate or not
        assert [r["id"] for r in threads[0]["replies"]] == ["c" * 12]
        assert lp.pending_for_readers(ctx, sid, off) == {}
    with unrestricted():
        comments.approve(ctx, sid, "a" * 12)  # showing a hidden thread again
    assert _ids(lp.threads_for_readers(ctx, sid, sel.model_copy(update={"moderate": False}))) == [
        "a" * 12,
        "b" * 12,
    ]


async def test_comments_from_before_moderation_count_as_approved(ctx):
    from mnemosyne.services import comments
    from mnemosyne.services.shared_notes import unrestricted

    sid, sel = _moderated(ctx)
    _make(ctx, sid, "a" * 12, ANA, text="Old")
    _make(ctx, sid, "b" * 12, BO, text="Edited later")
    # The switch went on a minute from now: both were there before, everyone saw them.
    soon = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    on = sel.model_copy(update={"moderated_since": soon})
    assert _ids(lp.threads_for_readers(ctx, sid, on)) == ["a" * 12, "b" * 12]
    assert lp.pending_for_readers(ctx, sid, on) == {}
    # An hour ago: both came after it and wait.
    ago = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    off = sel.model_copy(update={"moderated_since": ago})
    assert lp.threads_for_readers(ctx, sid, off) == []
    assert set(lp.pending_for_readers(ctx, sid, off)) == {"1" * 16, "2" * 16}
    # No date at all (a selection written by hand): nothing is grandfathered.
    assert lp.threads_for_readers(ctx, sid, sel) == []
    # An edit after the switch is a new comment: back to waiting, unless approved again.
    with unrestricted():
        ctx.repo.add_comment(
            sid,
            id="c" * 12,
            text="Written before",
            author_id=ANA[0],
            author=ANA[1],
            created_at="2020-01-01T10:00:00",
            updated_at="2020-01-01T10:00:00",
        )
    assert "c" * 12 in _ids(lp.threads_for_readers(ctx, sid, off))
    with unrestricted():
        comments.edit(ctx, sid, "c" * 12, "Changed after", actor=ANA)
    assert "c" * 12 not in _ids(lp.threads_for_readers(ctx, sid, off))
    assert "c" * 12 in lp.pending_for_readers(ctx, sid, off)["1" * 16]


async def test_publishing_stamps_when_moderation_went_on(ctx, host, seals):
    s = _meeting(ctx)

    async def publish(**sel) -> PublicationSelection:
        pub = await _first(ctx, s.id, follow=False, comments=True, **sel)
        return PublicationSelection.model_validate(pub["selection"])

    off = await publish(moderated_since="2000-01-01T00:00:00Z")  # not the caller's to say
    assert not off.moderate and off.moderated_since is None
    first = await publish(moderate=True)
    assert first.moderated_since and first.moderated_since > "2020"
    again = await publish(moderate=True, moderated_since="2000-01-01T00:00:00Z")
    assert again.moderated_since == first.moderated_since  # kept while it stays on
    gone = await publish(moderate=False)
    assert gone.moderated_since is None
    await asyncio.sleep(1.1)  # (second resolution)
    back = await publish(moderate=True)
    assert back.moderated_since and back.moderated_since > first.moderated_since


async def test_pending_ids_reach_every_reader_without_text_and_follow_the_owner(
    ctx, live, seals, quick
):
    from mnemosyne.services import comments
    from mnemosyne.services.shared_notes import unrestricted

    s, bridge, row, ana = await _room_with_comments(ctx, live, moderate=True)
    bo = Reader(await _reader(live, ctx, s.id), row, bridge.room)
    await ana.send(_create("a" * 12, "Secret-ish remark from Ana"))
    assert (await _taken(bridge, 1)) == {"applied": 1}
    # Both readers hear: nothing to read, and Ana's id, with no words, under her rid.
    for reader in (ana, bo):
        frame = await _until(
            lambda r=reader: (f := r.threads_frame()) and f["pending"] and f, "the pending id"
        )
        assert frame["threads"] == [] and frame["pending"] == {ana.rid: ["a" * 12]}
        assert "remark" not in json.dumps(frame)
    # The owner approves: the thread appears for both, nothing is waiting.
    with unrestricted():
        comments.approve(ctx, s.id, "a" * 12)
    for reader in (ana, bo):
        frame = await _until(
            lambda r=reader: (f := r.threads_frame()) and f["threads"] and f, "the approved thread"
        )
        assert frame["pending"] == {} and frame["threads"][0]["root"]["text"].startswith("Secret")
    # Hide takes it away again (and says nothing is waiting: it was answered).
    with unrestricted():
        comments.hide(ctx, s.id, "a" * 12)
    frame = await _until(
        lambda: (f := bo.threads_frame()) and not f["threads"] and f, "the hidden thread gone"
    )
    assert frame["pending"] == {}
    # Ana edits an approved comment of hers: it waits again.
    with unrestricted():
        comments.approve(ctx, s.id, "a" * 12)
    await ana.send(_edit("a" * 12, "Quite different now", 5))
    await _taken(bridge, 2)
    frame = await _until(
        lambda: (f := bo.threads_frame()) and f["pending"] and f, "the edit to wait again"
    )
    assert frame["threads"] == [] and frame["pending"] == {ana.rid: ["a" * 12]}
    assert ctx.repo.get_comment("a" * 12).approved_at is None
    # The snapshot is the same frame.
    snap = [b for _, p, _, b in live.rooms.calls if p == f"/live/{bridge.room}/snapshot"][-1]
    kind, body = plf.open_signed(row["key"], bridge.room, snap, _sign_pub(row))
    assert kind is plf.Kind.THREADS and body["pending"] == {ana.rid: ["a" * 12]}
    for c in (ana.client, bo.client):
        await c.close()


async def test_a_reader_cannot_reply_in_a_thread_it_is_not_shown(ctx, live, seals, quick):
    s, bridge, row, ana = await _room_with_comments(ctx, live, moderate=True)
    await ana.send(_create("a" * 12, "Waiting"))
    await _taken(bridge, 1)
    await ana.send(_reply("b" * 12, "a" * 12, "Replying to myself", 1))
    assert (await _taken(bridge, 2))["refused"] == 1  # readers reply to what they are shown
    assert [c.id for c in _comments(ctx, s.id)] == ["a" * 12]
    await ana.client.close()


async def test_turning_moderation_off_sends_the_waiting_threads(ctx, live, seals, quick):
    s, bridge, row, ana = await _room_with_comments(ctx, live, moderate=True)
    await ana.send(_create("a" * 12, "Was waiting"))
    await _taken(bridge, 1)
    await _until(lambda: (f := ana.threads_frame()) and f["pending"], "pending")
    await _first(ctx, s.id, comments=True, moderate=False)  # an Update with the switch off
    frame = await _until(
        lambda: (f := ana.threads_frame()) and f["threads"] and f, "THREADS after the Update"
    )
    assert frame["pending"] == {} and frame["threads"][0]["root"]["id"] == "a" * 12
    # ... and on again: the comment, written before, stays visible; a new one waits.
    await asyncio.sleep(1.1)
    await _first(ctx, s.id, comments=True, moderate=True)
    await ana.send(_create("b" * 12, "New and waiting", 1))
    await _taken(bridge, 2)
    frame = await _until(
        lambda: (f := ana.threads_frame()) and f["pending"] and f, "the new one to wait"
    )
    assert frame["pending"] == {ana.rid: ["b" * 12]}
    assert [t["root"]["id"] for t in frame["threads"]] == ["a" * 12]
    await ana.client.close()


# ---- blocking a reader (M4, task 3) ----


async def test_a_blocked_reader_is_told_and_nothing_of_theirs_is_written(ctx, live, seals, quick):
    from mnemosyne.services import publishing

    s, bridge, row, ana = await _room_with_comments(ctx, live)
    bo = Reader(await _reader(live, ctx, s.id), row, bridge.room)
    await ana.send(_create("a" * 12, "Ana, before"))
    await _taken(bridge, 1)
    publishing.block_reader(ctx, s.id, ana.rid)  # block alone: what she wrote stays
    for reader in (ana, bo):  # everyone hears the rid, and only her page acts on it
        frame = await _until(
            lambda r=reader: (f := r.threads_frame()) and f["blocked"] and f, "THREADS blocked"
        )
        assert frame["blocked"] == [ana.rid]
        assert [t["root"]["id"] for t in frame["threads"]] == ["a" * 12]
    # Her create, edit and delete are all dropped; nothing changes.
    await ana.send(_create("b" * 12, "Ana, after", 1))
    await ana.send(_edit("a" * 12, "Ana, rewritten", 2))
    await ana.send(_delete("a" * 12, 3))
    await ana.send(_reply("c" * 12, "a" * 12, "Ana, replying", 4))
    assert (await _taken(bridge, 5)) == {"applied": 1, "blocked": 4}
    (c,) = _comments(ctx, s.id)
    assert c.id == "a" * 12 and c.text == "Ana, before" and not c.deleted
    # Bo is not affected.
    await bo.send(_create("d" * 12, "Bo", 5, name="Bo"))
    await _taken(bridge, 6)
    assert {c.id for c in _comments(ctx, s.id)} == {"a" * 12, "d" * 12}
    # A repeated block changes nothing; the row carries it, newest first.
    publishing.block_reader(ctx, s.id, ana.rid)
    assert list(ctx.repo.live_publication(s.id)["blocked_readers"]) == [ana.rid]
    for c in (ana.client, bo.client):
        await c.close()


async def test_block_and_hide_hides_each_comment_and_unblock_keeps_them_hidden(
    ctx, live, seals, quick
):
    from mnemosyne.services import comments, publishing
    from mnemosyne.services.shared_notes import unrestricted

    s, bridge, row, ana = await _room_with_comments(ctx, live)
    bo = Reader(await _reader(live, ctx, s.id), row, bridge.room)
    await ana.send(_create("a" * 12, "Ana one"))
    await ana.send(_create("b" * 12, "Ana two", 1))
    await bo.send(_create("c" * 12, "Bo one", 2, name="Bo"))
    await _taken(bridge, 3)
    with unrestricted():
        comments.hide(ctx, s.id, "b" * 12)  # hidden already: untouched by the block
    hidden_before = ctx.repo.get_comment("b" * 12).hidden_at
    publishing.block_reader(ctx, s.id, ana.rid, hide=True)
    by_id = {c.id: c for c in _comments(ctx, s.id)}
    assert by_id["a" * 12].hidden_at is not None and by_id["b" * 12].hidden_at == hidden_before
    assert by_id["c" * 12].hidden_at is None  # Bo's stays
    frame = await _until(
        lambda: (f := bo.threads_frame()) and f["blocked"] and f, "THREADS after the block"
    )
    assert [t["root"]["id"] for t in frame["threads"]] == ["c" * 12]
    assert frame["blocked"] == [ana.rid]
    events = ctx.repo.events(s.id)
    blocked = [e["detail"] for e in events if e["kind"] == "reader_blocked"]
    assert len(blocked) == 1 and blocked[0]["rid"] == ana.rid and blocked[0]["name"] == "Ana"
    assert "Ana one" not in json.dumps(blocked)  # the block line never carries a comment's text
    assert sum(e["kind"] == "comment_hidden" for e in events) == 2  # b was hidden before
    # Unblock: her ops land again, what was hidden stays hidden, THREADS stops naming her.
    publishing.unblock_reader(ctx, s.id, ana.rid)
    frame = await _until(
        lambda: (f := bo.threads_frame()) and not f["blocked"] and f, "THREADS after the unblock"
    )
    assert [t["root"]["id"] for t in frame["threads"]] == ["c" * 12]
    await ana.send(_create("d" * 12, "Ana, back", 3))
    await _taken(bridge, 4)
    assert bridge.outcomes == {"applied": 4}
    assert ctx.repo.get_comment("a" * 12).hidden_at is not None
    assert ctx.repo.get_comment("d" * 12).hidden_at is None
    for c in (ana.client, bo.client):
        await c.close()


async def test_the_61st_op_in_ten_minutes_across_readers_is_dropped(ctx, live, seals, quick):
    s, bridge, row, ana = await _room_with_comments(ctx, live)
    clock = Clock()
    bridge.clock = clock
    readers = [ana] + [Reader(await _reader(live, ctx, s.id), row, bridge.room) for _ in range(3)]
    n = 0
    for reader in readers[:3]:  # 3 readers x RATE_OPS = RATE_TOTAL, each inside its own cap
        for _ in range(lp.RATE_OPS):
            await reader.send(_create(f"{n:012x}", f"Comment {n}", n))
            n += 1
    await _taken(bridge, lp.RATE_TOTAL)
    assert bridge.outcomes == {"applied": lp.RATE_TOTAL}
    await readers[3].send(_create(f"{n:012x}", "One too many", n))  # a fourth reader's first
    await _taken(bridge, lp.RATE_TOTAL + 1)
    assert bridge.outcomes == {"applied": lp.RATE_TOTAL, "rate": 1}
    assert len(_comments(ctx, s.id)) == lp.RATE_TOTAL
    clock.t += lp.RATE_WINDOW  # ten minutes on: again
    await readers[3].send(_create(f"{n + 1:012x}", "Later", n + 1))
    await _taken(bridge, lp.RATE_TOTAL + 2)
    assert bridge.outcomes["applied"] == lp.RATE_TOTAL + 1
    for r in readers:
        await r.client.close()


# ---- readers' comments on the published notes (M4, task 2d) ----

NOTES = "Plans\nAna Lopez will send the budget to Cy Park.\nDone.\n"
PASSAGE = "send the budget to Person 1"  # what readers read: Cy Park is not listed


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    from mnemosyne.services import shared_notes
    from tests import fake_link

    monkeypatch.setattr(shared_notes, "FOLD_DELAY", 1000.0)  # folds only when a test asks
    monkeypatch.setattr(shared_notes, "FOLD_EVERY", 1000.0)
    return fake_link.install(tmp_path, monkeypatch)


async def _notes_room(ctx, live, **sel):
    """A meeting with notes naming someone the selection leaves out, published with comments
    and the notes, a reader in its room."""
    s = _meeting(ctx)
    ctx.repo.update_fields(s.id, notes=NOTES)
    sel = {"comments": True, "notes": True, "attendees": ["Ana Lopez", "Bo Chen"], **sel}
    await _first(ctx, s.id, **sel)
    bridge = await _bridge(ctx, s.id)
    row = ctx.repo.live_publication(s.id)
    reader = Reader(await _reader(live, ctx, s.id), row, bridge.room)
    return s, bridge, row, reader


async def _meta(reader) -> dict:
    return (await _until(reader.metas, "META"))[-1]


def _on_notes(cid: str, exact: str, n: int = 0, text: str = "Who is that?") -> dict:
    return _create(cid, text, n, target="notes", exact=exact, prefix="Ana Lopez will ", suffix=".")


def _marks_requests(sidecar) -> list[dict]:
    return [r for r in sidecar.doc_requests if r["op"] == "mark_comment_at"]


async def test_a_readers_notes_comment_marks_the_real_text(ctx, live, seals, quick, sidecar):
    from mnemosyne.services import guest_notes, shared_notes
    from tests.fake_link import doc_frame

    s, bridge, row, ana = await _notes_room(ctx, live)
    m = _opened(live.pub, {"url": row_url(ctx, s.id)})
    assert PASSAGE in m.notes and "Cy" not in m.notes
    assert (await _meta(ana))["notes"] is True
    cid = "a" * 12
    env = await ana.send(_on_notes(cid, PASSAGE))
    assert (await _taken(bridge, 1)) == {"applied": 1}
    (c,) = _comments(ctx, s.id)
    assert (c.target, c.author_id, c.author) == ("notes", f"reader:{ana.rid}", "Ana (reader)")
    assert c.anchor == {"mark": f"comment:{cid}"} and c.quote == PASSAGE
    # The mark is on the real words, in the document, written as the reader.
    plain, ranges = await _until(
        lambda: (got := ctx.repo.notes_marks(s.id)) and got[1].get(cid) and got, "the mark"
    )
    assert plain[slice(*ranges[cid])] == "send the budget to Cy Park"
    assert ctx.repo.notes_ops(s.id)[-1]["author_id"] == f"reader:{ana.rid}"
    # THREADS: on the notes, the marked text as readers read it.
    threads = await _until(
        lambda: (t := ana.threads()) and not t[0]["root"]["detached"] and t, "THREADS, marked"
    )
    (t,) = threads
    assert t["root"]["anchor"] == {
        "target": "notes",
        "target_id": None,
        "quote_id": None,
        "quote": PASSAGE,
    }
    assert t["root"]["rid"] == ana.rid and "Cy" not in json.dumps(threads)
    # The same frame again (anyone in the room can copy it): no second comment, no second mark.
    await ana.client.send(bytes([T_NOTES]) + env)
    assert (await _taken(bridge, 2)) == {"applied": 1, "replay": 1}
    assert len(_marks_requests(sidecar)) == 1 and len(_comments(ctx, s.id)) == 1
    # C4: a reader's notes thread is an ordinary notes comment: the meeting's guests see it.
    (g,) = guest_notes.threads_for_guests(ctx, s.id)
    assert (g["root"]["id"], g["root"]["author"]) == (cid, "Ana (reader)")
    assert g["root"]["mark"] == f"comment:{cid}"

    # The quote readers get is what the mark covers now, not what the reader sent: the owner
    # writes Bo Chen (listed: as is) over Cy Park (the toy's ranges stay where they were).
    at = plain.index("Cy Park")
    edit = doc_frame("o1", ["del", at, 7], ["ins", at, "Bo Chen"])
    ctx.repo.append_notes_ops(s.id, [base64.b64decode(edit)], "", "Owner", True)
    shared_notes.fold_blocking(ctx, s.id)
    sel = PublicationSelection.model_validate(ctx.repo.live_publication(s.id)["selection"])
    (t,) = lp.threads_for_readers(ctx, s.id, sel)
    assert t["root"]["anchor"]["quote"] == "send the budget to Bo Chen"
    assert t["root"]["changed"] is True and t["root"]["detached"] is False
    await ana.client.close()


async def test_a_notes_passage_that_is_not_there_stays_detached(ctx, live, seals, quick, sidecar):
    s, bridge, row, ana = await _notes_room(ctx, live)
    await ana.send(_on_notes("b" * 12, "words nobody wrote", text="Hm?"))
    assert (await _taken(bridge, 1)) == {"applied": 1}
    (c,) = _comments(ctx, s.id)
    assert c.target == "notes" and c.quote == "words nobody wrote"
    assert _marks_requests(sidecar) == []
    assert "b" * 12 not in ((ctx.repo.notes_marks(s.id) or ("", {}))[1])
    (t,) = await _until(ana.threads, "THREADS")
    assert t["root"]["detached"] is True
    assert t["root"]["anchor"] == {**lp.NO_ANCHOR, "target": "notes", "quote": "words nobody wrote"}
    await ana.client.close()


async def test_a_notes_passage_the_sidecar_cannot_mark_is_shown_by_its_quote(
    ctx, live, seals, quick, monkeypatch
):
    """The sidecar answered the probe, and is gone when the mark is made: the comment exists,
    detached, and readers still see it on the notes by its quote."""
    from mnemosyne.services import shared_notes

    monkeypatch.setattr(shared_notes, "available", lambda: True)
    s, bridge, row, ana = await _notes_room(ctx, live)
    assert bridge.notes_ok and (await _meta(ana))["notes"] is True
    await ana.send(_on_notes("c" * 12, PASSAGE))
    assert (await _taken(bridge, 1)) == {"applied": 1}
    (c,) = _comments(ctx, s.id)
    assert (c.target, c.quote) == ("notes", PASSAGE)
    assert ctx.repo.notes_marks(s.id) is None and ctx.repo.notes_ops(s.id) == []
    (t,) = await _until(ana.threads, "THREADS")
    assert t["root"]["detached"] is True
    assert t["root"]["anchor"] == {**lp.NO_ANCHOR, "target": "notes", "quote": PASSAGE}
    await ana.client.close()


async def test_without_a_sidecar_a_notes_comment_stays_on_the_meeting(ctx, live, seals, quick):
    """No sidecar here: META says the notes take no comments; an op sent anyway (an older
    page) keeps the thread on the meeting with the passage in its text (C5), never dropped."""
    s, bridge, row, ana = await _notes_room(ctx, live)
    assert not bridge.notes_ok
    meta = await _meta(ana)
    assert (meta["comments"], meta["notes"]) == (True, False)
    await ana.send(_on_notes("d" * 12, PASSAGE, text="Who?"))
    assert (await _taken(bridge, 1)) == {"applied": 1}
    (c,) = _comments(ctx, s.id)
    assert c.target == "" and c.text == f"On: “{PASSAGE}”\n\nWho?"
    (t,) = await _until(ana.threads, "THREADS")
    assert t["root"]["anchor"] == lp.NO_ANCHOR
    await ana.client.close()


async def test_notes_left_out_take_no_comments_and_show_none(ctx, live, seals, quick, sidecar):
    s, bridge, row, ana = await _notes_room(ctx, live, notes=False)
    assert bridge.notes_ok and (await _meta(ana))["notes"] is False
    await ana.send(_on_notes("e" * 12, PASSAGE))
    assert (await _taken(bridge, 1)) == {"refused": 1}
    assert _comments(ctx, s.id) == [] and _marks_requests(sidecar) == []
    # A notes thread a reader made while the notes were published is not shown now.
    ctx.repo.add_comment(
        s.id,
        id="f" * 12,
        text="On the notes",
        author_id=f"reader:{ana.rid}",
        author="Ana (reader)",
        target="notes",
        anchor={"mark": "comment:" + "f" * 12},
        quote=PASSAGE,
    )
    sel = PublicationSelection.model_validate(ctx.repo.live_publication(s.id)["selection"])
    assert lp.threads_for_readers(ctx, s.id, sel) == []
    shown = lp.threads_for_readers(ctx, s.id, sel.model_copy(update={"notes": True}))
    assert [t["root"]["anchor"]["target"] for t in shown] == ["notes"]
    await ana.client.close()


async def test_meta_says_notes_only_when_they_can_be_commented_on(ctx, live, seals, quick, sidecar):
    s, bridge, row, ana = await _notes_room(ctx, live)
    meta = await _meta(ana)
    assert set(meta) == {"comments", "follow", "notes", "at"}
    assert (meta["comments"], meta["notes"]) == (True, True)
    # Comments off: no notes either.
    await _first(ctx, s.id, comments=False, notes=True, attendees=["Ana Lopez", "Bo Chen"])
    meta = await _until(lambda: (m := ana.metas()[-1])["comments"] is False and m, "META off")
    assert meta["notes"] is False
    # On again, the notes published but empty now.
    ctx.repo.update_fields(s.id, notes="")
    await _first(ctx, s.id, comments=True, notes=True, attendees=["Ana Lopez", "Bo Chen"])
    meta = await _until(lambda: (m := ana.metas()[-1])["comments"] is True and m, "META on")
    assert meta["notes"] is False
    assert bridge._meta_state()[2] is False
    # Notes again: the next look (a tick) says so.
    ctx.repo.update_fields(s.id, notes=NOTES)
    await bridge.tick()
    meta = await _until(lambda: (m := ana.metas()[-1])["notes"] is True and m, "META notes")
    assert meta["comments"] is True
    await ana.client.close()
