"""Library sync between two backends (services/sync_service.py, routes/sync.py): two apps in
this process, each paired with the other (a DESKTOP device and a sync peer record), each pulling
from the other through `httpx.ASGITransport`. No network, no iroh. Text merges run through the
stand-in sidecar (tests/fake_link.py), one scenario through the real mnemosyne-link if built."""

import json
from datetime import timedelta
from importlib.metadata import version as package_version

import httpx
import pytest

from mnemosyne.api.app import create_app
from mnemosyne.config import Settings
from mnemosyne.export.note_merge import MergeError, merge_many
from mnemosyne.models.session import (
    ActionItem,
    Session,
    SessionStatus,
    SummaryData,
    TranscriptSegment,
)
from mnemosyne.services import summary_doc
from mnemosyne.services.link import find_binary
from mnemosyne.services.sync_peers import SyncPeer, now_iso
from mnemosyne.storage.crypto import MemoryKeyStore
from tests import fake_link
from tests.fakes import FakeProvider

pytestmark = pytest.mark.anyio

ENDPOINTS = {"A": "a" * 64, "B": "b" * 64}


class Node:
    """One backend: its app, context and admin client."""

    def __init__(self, tmp_path, name: str):
        self.name = name
        settings = Settings(
            data_dir=tmp_path / name / "data",
            backup_dir=str(tmp_path / name / "backups"),
            api_token=f"admin-{name}",
        )
        self.app = create_app(settings, keystore=MemoryKeyStore())
        self.ctx = self.app.state.ctx
        self.ctx.sync.sync_interval = None  # tests run the rounds
        self.ctx.sync.poke_delay = 0.0
        self.ctx.sync_peers.set_name(name)

    @property
    def id(self) -> str:
        return self.ctx.sync_peers.self_info().id

    @property
    def sync(self):
        return self.ctx.sync

    @property
    def repo(self):
        return self.ctx.repo

    def client(self, token: str | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {token or self.ctx.settings.api_token}"},
        )

    def peer(self, other: "Node") -> SyncPeer:
        return self.ctx.sync_peers.get(other.id)

    def status(self, other: "Node"):
        return self.sync.state(other.id)


def to_app(app):
    """A `client_for` that reaches `app` in-process with the peer's token."""

    def client_for(peer: SyncPeer) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://peer",
            headers={"Authorization": f"Bearer {peer.token}"},
        )

    return client_for


def pair(x: Node, y: Node) -> None:
    """Pair x and y as pairing does: each trusts the other's endpoint (a DESKTOP device whose
    token the other holds) and records it as a sync peer."""
    _, token_for_x = y.ctx.pairing.add_desktop(x.name, ENDPOINTS[x.name])
    _, token_for_y = x.ctx.pairing.add_desktop(y.name, ENDPOINTS[y.name])
    for me, other, token in ((x, y, token_for_x), (y, x, token_for_y)):
        me.ctx.sync_peers.add(
            SyncPeer(
                id=other.id,
                name=other.name,
                endpoint_id=ENDPOINTS[other.name],
                ticket=f"ticket-{other.name}",
                token=token,
                created_at=now_iso(),
                listen="peer",
            )
        )
        me.sync.client_for = to_app(other.app)


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    return fake_link.install(tmp_path, monkeypatch)


@pytest.fixture
async def nodes(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMOSYNE_CONFIG_FILE", str(tmp_path / "config.toml"))
    a, b = Node(tmp_path, "A"), Node(tmp_path, "B")
    pair(a, b)
    for n in (a, b):
        n.sync.start()
    yield a, b
    for n in (a, b):
        await n.sync.stop()
        await n.ctx.jobs.shutdown()
        n.repo.close()


async def pull(x: Node, y: Node):
    """One round of x pulling from y."""
    return await x.sync.sync_round(x.peer(y))


def meeting(node: Node, name="Planning", **kw) -> str:
    fields = dict(
        name=name,
        status=SessionStatus.COMPLETED,
        audio_file="/rec/mixed.ogg",
        notes="Intro.\nPlan.\n",
        transcript=[TranscriptSegment(text="hello there", speaker="Ann", start=0.0, end=1.0)],
    )
    fields.update(kw)
    return node.repo.save(Session(**fields)).id


def vv(node: Node, sid: str) -> dict:
    return node.repo.sync_state(sid)[1]


# ---- a fresh copy ---------------------------------------------------------------------------


async def test_a_meeting_recorded_on_a_appears_on_b(nodes):
    a, b = nodes
    sid = meeting(a)
    events = b.ctx.bus.subscribe()
    st = await pull(b, a)
    assert (st.state, st.received, st.total, st.done) == ("done", 1, 1, 1)
    got = b.repo.get(sid)
    assert got.origin == a.id and got.audio_file is None
    assert got.status == SessionStatus.COMPLETED and got.notes == "Intro.\nPlan.\n"
    assert vv(b, sid) == vv(a, sid) == {a.id: 1}
    assert a.repo.get(sid).origin == ""
    peer = b.peer(a)
    assert peer.since == a.repo.current_sync_seq() and peer.last_sync_at
    # A remembers what it served B: the base of B's later edits.
    assert a.repo.served_snapshot(sid, b.id, 1) is not None
    seen = []
    while not events.empty():
        seen.append(events.get_nowait())
    assert {"type": "session", "session_id": sid, "status": "completed"} in seen
    syncs = [e for e in seen if e["type"] == "sync"]
    assert syncs[0]["state"] == "syncing" and syncs[-1] == {
        "type": "sync",
        "peer": a.id,
        "state": "done",
        "done": 1,
        "total": 1,
    }
    # Nothing new next time; A hears its own meeting back and ignores it.
    assert (await pull(b, a)).received == 0
    st = await pull(a, b)
    assert (st.state, st.received, st.updated, st.merged) == ("done", 0, 0, 0)
    assert vv(a, sid) == {a.id: 1} and a.repo.get(sid).audio_file == "/rec/mixed.ogg"
    # The status route says so too.
    async with b.client() as c:
        body = (await c.get("/api/sync/status")).json()
    (p,) = body["peers"]
    assert (p["state"], p["received"], p["clashes"], p["app_version"]) == (
        "done",
        0,
        [],
        package_version("mnemosyne-backend"),
    )


async def test_hello_and_changes_serve_documents_and_tombstones(nodes):
    a, b = nodes
    sid = meeting(a)
    gone = meeting(a, name="Old")
    async with a.client() as c:
        assert (await c.delete(f"/api/sessions/{gone}")).status_code == 200
        hello = (await c.get("/api/sync/hello")).json()
        assert hello["protocol"] == 1 and hello["id"] == a.id and hello["name"] == "A"
        assert hello["seq"] == a.repo.current_sync_seq() and hello["schema_version"] >= 12
        page = (await c.get("/api/sync/changes", params={"since": 0, "limit": 1})).json()
    assert [s["id"] for s in page["sessions"]] == [sid]
    assert page["sessions"][0]["origin"] == a.id
    assert page["more"] is False and page["total"] == 1
    assert [t["session_id"] for t in page["tombstones"]] == [gone]
    # The admin token is not a peer: nothing is remembered as served.
    assert a.repo.served_snapshot(sid, b.id, 1) is None


# ---- edits on both sides ------------------------------------------------------------------


def _edit_body(d: SummaryData, first_decision: str) -> dict:
    return {
        "summary": "We planned.\n",
        "topics": [],
        "decisions": [
            {"text": first_decision if i == 0 else t, "at": at, "id": did}
            for i, (t, at, did) in enumerate(
                zip(d.decisions, d.decision_at, d.decision_ids, strict=True)
            )
        ],
        "open_questions": [],
        "action_items": [x.model_dump(mode="json") for x in d.action_items],
        "chapters": [],
        "client_facts": [],
    }


async def test_edit_on_b_and_resummarize_on_a_converge(nodes, sidecar):
    a, b = nodes
    sid = meeting(a)
    data = SummaryData(
        title="Weekly",
        provider="fake",
        model="m",
        decisions=["Ship in October", "Skip the beta"],
        decision_at=[0.0, 30.0],
        action_items=[ActionItem(text="Send the budget report", owner="Bob")],
    )
    out, body, _ = summary_doc.apply_llm(a.ctx, sid, data, "We planned.\n")
    a.repo.update_fields(sid, summary=body, summary_data=out)
    await pull(b, a)

    # B edits the first decision by hand...
    d = b.repo.get(sid).summary_data
    async with b.client() as c:
        r = await c.put(f"/api/sessions/{sid}/summary", json=_edit_body(d, "Ship in late October"))
        assert r.status_code == 200, r.text
    # ...while A summarizes again: the same two decisions and a new one.
    provider = FakeProvider()
    provider.summary = provider.reply = json.dumps(
        {
            "summary": "We planned.",
            "title": "Weekly",
            "decisions": ["Ship in October", "Skip the beta", "Hire a writer"],
            "action_items": [{"text": "Send the budget report", "owner": "Bob"}],
        }
    )
    a.ctx.summarizer.providers = {"fake": provider}
    async with a.client() as c:
        r = await c.post(f"/api/sessions/{sid}/summarize", json={"provider": "fake"})
        assert r.status_code == 200, r.text
        job = await a.ctx.jobs.wait(r.json()["id"])
    assert job.status == "completed", job.error
    assert "Hire a writer" in a.repo.get(sid).summary_data.decisions

    st = await pull(a, b)  # concurrent: merged on A
    assert st.merged == 1
    st = await pull(b, a)  # A's merge is a fast-forward for B
    assert (st.updated, st.merged) == (1, 0)
    st = await pull(a, b)  # and B's copy of it is nothing new for A
    assert (st.updated, st.merged) == (0, 0)
    await pull(b, a)

    for n in (a, b):
        got = n.repo.get(sid).summary_data
        assert got.decisions.count("Ship in late October") == 1, n.name
        assert got.decisions.count("Hire a writer") == 1, n.name
        assert "Ship in October" not in got.decisions, n.name
        assert len(got.action_items) == 1
    assert a.repo.get(sid).summary_data.decisions == b.repo.get(sid).summary_data.decisions
    assert vv(a, sid) == vv(b, sid)
    (merged,) = [e for e in a.repo.events(sid) if e["kind"] == "sync_merged"]
    assert merged["detail"]["source"] == "Sync (B)"


async def test_ticks_on_both_sides_are_both_kept(nodes, sidecar):
    a, b = nodes
    data = SummaryData(
        action_items=[
            ActionItem(id="a1", text="Send the budget report"),
            ActionItem(id="a2", text="Book the venue"),
        ]
    )
    sid = meeting(a, summary="Body.\n", summary_data=data)
    await pull(b, a)
    async with a.client() as c:
        r = await c.patch(f"/api/sessions/{sid}/action-items/0", json={"done": True})
        assert r.status_code == 200, r.text
    async with b.client() as c:
        r = await c.patch(f"/api/sessions/{sid}/action-items/1", json={"done": True})
        assert r.status_code == 200, r.text
    for x, y in ((a, b), (b, a), (a, b), (b, a)):
        await pull(x, y)
    for n in (a, b):
        assert [i.done for i in n.repo.get(sid).summary_data.action_items] == [True, True]
    assert vv(a, sid) == vv(b, sid)


async def test_notes_edited_on_both_sides_merge_without_doubling(nodes, sidecar):
    a, b = nodes
    sid = meeting(a)
    await pull(b, a)
    a.repo.update_fields(sid, notes="Intro.\nA's line.\nPlan.\n")
    b.repo.update_fields(sid, notes="Intro.\nPlan.\nB's line.\n")
    for x, y in ((a, b), (b, a), (a, b), (b, a)):
        await pull(x, y)
    for n in (a, b):
        assert n.repo.get(sid).notes == "Intro.\nA's line.\nPlan.\nB's line.\n", n.name


# ---- deletes ------------------------------------------------------------------------------


async def test_a_delete_reaches_the_other_side_and_beats_an_edit(nodes):
    a, b = nodes
    sid = meeting(a)
    await pull(b, a)
    async with a.client() as c:
        assert (await c.delete(f"/api/sessions/{sid}")).status_code == 200
    b.repo.update_fields(sid, notes="Edited on B meanwhile.\n")
    st = await pull(a, b)  # B's edit arrives at A: deleted there, it stays deleted
    assert st.received == 0 and a.repo.get(sid) is None
    st = await pull(b, a)
    assert st.deleted == 1 and b.repo.get(sid) is None and b.repo.has_tombstone(sid)
    (t,) = b.repo.tombstones_since("")
    assert (t["device"], t["name"]) == (a.id, "Planning")
    for x, y in ((a, b), (b, a), (a, b)):
        st = await pull(x, y)
        assert (st.received, st.deleted) == (0, 0)
    assert a.repo.get(sid) is None and b.repo.get(sid) is None


async def test_deleting_a_copy_deletes_the_recording_on_its_origin(nodes):
    a, b = nodes
    sid = meeting(b)
    audio = b.ctx.settings.recordings_dir / sid
    audio.mkdir(parents=True)
    (audio / "mixed.ogg").write_bytes(b"ogg")
    await pull(a, b)
    assert a.repo.get(sid).origin == b.id
    async with a.client() as c:
        assert (await c.delete(f"/api/sessions/{sid}")).status_code == 200
    st = await pull(b, a)
    assert st.deleted == 1 and b.repo.get(sid) is None and not audio.exists()


async def test_a_busy_meeting_waits_for_its_delete(nodes):
    a, b = nodes
    sid = meeting(a)
    await pull(b, a)
    async with a.client() as c:
        assert (await c.delete(f"/api/sessions/{sid}")).status_code == 200
    b.ctx.active_recordings[sid] = object()  # recording into it on B
    st = await pull(b, a)
    assert st.deleted == 0 and b.repo.get(sid) is not None
    del b.ctx.active_recordings[sid]
    st = await pull(b, a)
    assert st.deleted == 1 and b.repo.get(sid) is None


# ---- refusals -----------------------------------------------------------------------------


async def test_an_id_clash_is_reported_and_skipped(nodes):
    a, b = nodes
    sid = meeting(a)
    other = Session(id=sid, name="Something else", created_at=a.repo.get(sid).created_at)
    other.created_at -= timedelta(hours=3)
    b.repo.save(other)
    st = await pull(b, a)
    assert st.state == "done" and st.clashes == {sid: "Planning"}
    assert b.repo.get(sid).name == "Something else" and b.repo.get(sid).origin == ""
    async with b.client() as c:
        (p,) = (await c.get("/api/sync/status")).json()["peers"]
    assert p["clashes"] == [{"session_id": sid, "name": "Planning"}]
    # Their meeting deleted there is not ours: ours stays.
    async with a.client() as c:
        assert (await c.delete(f"/api/sessions/{sid}")).status_code == 200
    st = await pull(b, a)
    assert st.deleted == 0 and b.repo.get(sid).name == "Something else"


async def test_a_locked_peer_is_skipped(nodes):
    a, b = nodes
    meeting(b)
    b.ctx.locked = True
    st = await pull(a, b)
    assert st.state == "locked" and "locked" in st.error
    assert a.ctx.sessions.list_sessions() == [] and a.peer(b).since == 0
    b.ctx.locked = False
    assert (await pull(a, b)).received == 1


class Flaky(httpx.AsyncBaseTransport):
    """Passes requests on; the n-th page of changes fails as if the connection dropped."""

    def __init__(self, app, fail_at: int | None):
        self.inner = httpx.ASGITransport(app=app)
        self.fail_at = fail_at
        self.asked: list[int] = []

    async def handle_async_request(self, request):
        if request.url.path == "/api/sync/changes":
            self.asked.append(int(request.url.params["since"]))
            if len(self.asked) == self.fail_at:
                raise httpx.ConnectError("dropped", request=request)
        return await self.inner.handle_async_request(request)


async def test_an_interrupted_first_sync_resumes_where_it_stopped(nodes):
    a, b = nodes
    ids = [meeting(a, name=f"M{i}") for i in range(5)]
    transport = Flaky(a.app, fail_at=2)
    b.sync.page_size = 2
    b.sync.client_for = lambda peer: httpx.AsyncClient(
        transport=transport,
        base_url="http://peer",
        headers={"Authorization": f"Bearer {peer.token}"},
    )
    st = await pull(b, a)
    assert st.state == "offline" and st.received == 2
    resume = b.peer(a).since
    assert resume > 0 and transport.asked == [0, resume]
    transport.fail_at, transport.asked = None, []
    st = await pull(b, a)
    assert st.state == "done" and st.received == 3 and transport.asked[0] == resume
    assert all(b.repo.get(i) is not None for i in ids)


def _hello(protocol: int):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/sync/hello":
            return httpx.Response(
                200,
                json={
                    "protocol": protocol,
                    "schema_version": 99,
                    "app_version": "9.9.9",
                    "id": "x",
                    "name": "A",
                    "seq": 0,
                },
            )
        raise AssertionError(f"asked {request.url.path} after the hello")

    return lambda peer: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://peer"
    )


async def test_another_protocol_is_skew(nodes):
    a, b = nodes
    b.sync.client_for = _hello(2)
    st = await pull(b, a)
    assert st.state == "skew" and "update this computer" in st.error and st.app_version == "9.9.9"
    b.sync.client_for = _hello(0)
    st = await pull(b, a)
    assert st.state == "skew" and st.error.startswith("Update Mnemosyne on A")


async def test_a_peer_that_cannot_be_reached_is_offline_with_backoff(nodes):
    a, b = nodes

    def down(request):
        raise httpx.ConnectError("refused", request=request)

    b.sync.client_for = lambda peer: httpx.AsyncClient(
        transport=httpx.MockTransport(down), base_url="http://peer"
    )
    st = await pull(b, a)
    assert st.state == "offline" and st.backoff == 60.0
    st = await pull(b, a)
    assert st.backoff == 120.0 and st.retry_at > 0
    # The scheduler leaves it alone until then; a poke from it does not wait.
    await b.sync.sync_all()
    assert st.backoff == 120.0
    assert b.sync.poke(a.id) and st.retry_at == 0.0


# ---- poke, now ------------------------------------------------------------------------------


async def test_a_poke_from_a_peer_pulls_from_it(nodes):
    a, b = nodes
    sid = meeting(b)
    async with a.client(b.peer(a).token) as c:  # B, with the token A gave it
        assert (await c.post("/api/sync/poke")).json() == {"scheduled": True}
    await a.sync.drain()
    assert a.repo.get(sid) is not None and a.repo.get(sid).origin == b.id
    async with a.client() as c:  # the admin token is not a peer
        assert (await c.post("/api/sync/poke")).json() == {"scheduled": False}


async def test_sync_now_pulls_from_every_peer(nodes):
    a, b = nodes
    sid = meeting(b)
    async with a.client() as c:
        assert (await c.post("/api/sync/now")).json() == {"started": [b.id]}
    await a.sync.drain()
    assert a.repo.get(sid) is not None


async def test_local_changes_poke_the_peers(nodes):
    a, b = nodes
    sid = meeting(a)
    await a.sync._announce()
    await b.sync.drain()
    assert b.repo.get(sid) is not None
    await a.sync._announce()  # nothing new: no poke
    assert not b.sync._poke_tasks


async def test_sync_routes_refuse_a_team_server(nodes):
    a, _ = nodes
    a.ctx.settings.team_mode = True
    a.ctx.settings.api_token = "admin-A"
    async with a.client() as c:
        assert (await c.get("/api/sync/hello")).status_code == 409


# ---- claiming meetings from before sync ---------------------------------------------------


async def test_legacy_version_vectors_are_claimed(nodes):
    a, _ = nodes
    one, two, three, four = (meeting(a, name=n) for n in "1234")
    a.repo.set_sync_state(one, {"": 3}, "")
    a.repo.set_sync_state(two, {}, "")
    a.repo.set_sync_state(three, {"": 2, a.id: 1}, "")
    a.repo.set_sync_state(four, {"elsewhere": 4}, "elsewhere")
    seq = a.repo.current_sync_seq()
    assert a.sync.claim() == 3
    assert vv(a, one) == {a.id: 3} and vv(a, two) == {a.id: 1}
    assert vv(a, three) == {a.id: 3} and vv(a, four) == {"elsewhere": 4}
    assert a.repo.current_sync_seq() == seq  # no new versions
    assert a.sync.claim() == 0
    assert a.repo.device_id == a.id


# ---- the real sidecar -----------------------------------------------------------------------


@pytest.fixture
def real_sidecar(monkeypatch):
    monkeypatch.delenv("MNEMOSYNE_LINK_BIN", raising=False)
    binary = find_binary()
    if binary is None:
        pytest.skip("no mnemosyne-link binary")
    try:
        result = merge_many(binary, [{"base": "a\n", "app": "a\n", "vault": "a b\n"}])
    except MergeError:
        pytest.skip("this mnemosyne-link has no batch `notes merge`")
    if isinstance(result[0], MergeError):
        pytest.skip("this mnemosyne-link has no `base` form")
    return binary


async def test_real_sidecar_notes_merge_without_doubling(nodes, real_sidecar):
    a, b = nodes
    sid = meeting(a)
    await pull(b, a)
    a.repo.update_fields(sid, notes="Intro.\nA's line.\nPlan.\n")
    b.repo.update_fields(sid, notes="Intro.\nPlan.\nB's line.\n")
    for x, y in ((a, b), (b, a), (a, b), (b, a)):
        await pull(x, y)
    for n in (a, b):
        assert n.repo.get(sid).notes == "Intro.\nA's line.\nPlan.\nB's line.\n", n.name
    assert vv(a, sid) == vv(b, sid)
