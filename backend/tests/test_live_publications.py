"""Live publications (services/live_publications.py): the watcher that keeps a followed link
up to date (task 5 of docs/plans/2026-11-05-live-publications.md) and the bridge that pushes
versions to the readers through a room on the publish host (task 6).

The watcher is driven by hand: a fake clock and `tick()`, never its loop (conftest sets its
interval to 0), and the publish host is test_publishing.py's FakeHost behind an httpx
MockTransport. The bridge (off in every other test, conftest) runs for real against
tests/fake_guest_room.py's FakeRoomHost (the room routes and its WebSocket) beside that FakeHost
(`live` fixture); readers are GuestClients holding the link's key."""

import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime

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
            assert body["threads"] == []  # task 11 fills them
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
    assert (kind, body) == (plf.Kind.THREADS, {"threads": [], "at": body["at"]})
    # The reader joined late: from the log, the join's META, then version 1 (queued before the
    # bridge was in the room; readers ignore a version they show already).
    frames = await _until(
        lambda: len(f := _opened_frames(reader, row, room, _sign_pub(row))) >= 2 and f,
        "META and VERSION",
    )
    assert frames[0] == (
        plf.Kind.META,
        {"follow": True, "comments": False, "at": frames[0][1]["at"]},
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


async def test_unpublish_deletes_the_room_before_the_publication(ctx, live, seals):
    s = _meeting(ctx)
    await _first(ctx, s.id)
    bridge = await _bridge(ctx, s.id)
    row = ctx.repo.live_publication(s.id)
    room = bridge.room
    reader = await _reader(live, ctx, s.id)
    live.log.clear()
    await publishing.unpublish(ctx, s.id)
    deletes = [(mth, p) for mth, p in live.log if mth == "DELETE"]
    assert deletes == [("DELETE", f"/live/{room}"), ("DELETE", f"/pub/{row['id']}")]
    await _until(lambda: reader.closed_code == 4410, "the reader to hear it")
    assert ctx.live_bridges.get(row["id"]) is None and not bridge.running
    assert live.rooms.rooms[room].deleted
    assert not lp._room_file(ctx, row["id"]).exists()


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
