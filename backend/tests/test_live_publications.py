"""Live publications (services/live_publications.py): the watcher that keeps a followed link
up to date (task 5 of docs/plans/2026-11-05-live-publications.md). The bridge's tests (task 6)
come later in this file.

The watcher is driven by hand: a fake clock and `tick()`, never its loop (conftest sets its
interval to 0), and the publish host is test_publishing.py's FakeHost behind an httpx
MockTransport."""

import json
from datetime import datetime

import httpx
import pytest

from mnemosyne.models.publication import PublicationSelection, QuoteSpan
from mnemosyne.models.session import ActionItem
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services import notes_media, publishing
from mnemosyne.services import publication_format as pf
from mnemosyne.services.live_publications import Watcher
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


async def test_an_automatic_version_never_widens_the_selection(ctx, host, seals, watcher, clock):
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
    # TODO(task 6, the bridge): every frame the bridge sent for this version is one of
    # VERSION/THREADS/META with exactly their fields (the outgoing-frame key-set assertion
    # against tests/fake_guest_room.py's FakeRoom), and round 1's (a)-(c) for frames and keys.
