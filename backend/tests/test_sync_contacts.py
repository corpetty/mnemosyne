"""The person key and the contacts between one person's computers (docs/plans/2026-10-23-phase-8-
tasks.md, C4 and C5): sync pairing makes both computers use one key (services/sync_peers.py
`adopt_person_key`, the sync kind of routes/pairing.py, `GET|PUT /api/sync/person-key`), and every
sync round merges the peer's contacts (`GET /api/sync/contacts`, services/sync_service.py
`_contacts`, services/contacts.py `apply_peer_rows`).

Two apps in this process. The sidecar between them is stood in by a `link.request` that carries B's
`pair` to A's POST /api/pairing/redeem as the real sidecar does (endpoint id, code, `peer` and
`card` passed through, the reply's `sync` and token handed back); sync rounds and the key calls go
through `httpx.ASGITransport` with the token each side holds for the other."""

import base64
import copy

import httpx
import pytest

from mnemosyne.api.app import create_app
from mnemosyne.config import Settings
from mnemosyne.models.contact import Card
from mnemosyne.models.session import Session, SessionStatus
from mnemosyne.models.sync_session import SyncSession
from mnemosyne.services import contacts
from mnemosyne.services.link import LinkError, endpoint_id_from_ticket
from mnemosyne.services.pairing import CONTACT, DESKTOP
from mnemosyne.services.person import PersonKeys
from mnemosyne.storage.crypto import MemoryKeyStore

pytestmark = pytest.mark.anyio

EP = {"A": "a1" * 32, "B": "b2" * 32, "X1": "c1" * 32, "X2": "c2" * 32, "Y": "d4" * 32}


def ticket(endpoint_id: str) -> str:
    raw = b"\x00" + bytes.fromhex(endpoint_id) + b"\x00"
    return "endpoint" + base64.b32encode(raw).decode().lower().rstrip("=")


def card_of(keys: PersonKeys, name="Ada", machines=(("Desk", "X1"), ("Laptop", "X2"))):
    body = {
        "person": keys.public_hex,
        "name": name,
        "machines": [{"name": n, "ticket": ticket(EP[e])} for n, e in machines],
        "at": "2026-10-23T10:00:00+00:00",
    }
    return Card(**body, sig=keys.sign(PersonKeys.canonical(body)))


class Node:
    """One computer: an app with remote access 'running' on a stand-in sidecar."""

    def __init__(self, tmp_path, name: str, world: dict):
        self.name = name
        self.world = world
        self.endpoint = EP[name]
        settings = Settings(
            data_dir=tmp_path / name / "data",
            backup_dir=str(tmp_path / name / "backups"),
            api_token=f"admin-{name}",
            remote_access=True,
        )
        self.app = create_app(settings, keystore=MemoryKeyStore())
        self.ctx = self.app.state.ctx
        self.ctx.sync.sync_interval = None  # tests run the rounds
        self.ctx.sync.poke_delay = 0.0
        self.ctx.sync_peers.set_name(f"{name} desk")
        self.ctx.link.status.running = True
        self.ctx.link.status.endpoint_id = self.endpoint
        self.ctx.link.status.ticket = ticket(self.endpoint)
        self.ops: list[tuple[str, dict]] = []
        self.listens: dict[str, str] = {}
        self.tamper_out = None  # (args) -> None: change what leaves with `pair`
        self.tamper_in = None  # (reply) -> None: change what comes back
        self.redeemed: list[tuple[int, dict]] = []  # what A answered to this node's pair
        self.ctx.link.request = self.request
        self.ctx.sync.client_for = self.client_for
        self.fail_key_calls = False
        self.hide_contacts = False  # answer 404 to /api/sync/contacts, like an older peer
        world[self.endpoint] = self

    # The sidecar.
    async def request(self, op: str, **args) -> dict:
        self.ops.append((op, copy.deepcopy(args)))
        if op == "dial":
            endpoint = endpoint_id_from_ticket(args["ticket"])
            listen = self.listens.setdefault(endpoint, f"127.0.0.1:{41000 + len(self.listens)}")
            return {"ok": True, "peer": endpoint, "listen": listen}
        if op == "hangup":
            return {"ok": True, "peer": args["peer"], "closed": True}
        assert op == "pair", op
        if self.tamper_out:
            self.tamper_out(args)
        invite_ticket, _, code = args["invite"].rpartition("#")
        home = self.world[endpoint_id_from_ticket(invite_ticket)]
        body = {"code": code, "name": args["name"], "endpoint_id": self.endpoint}
        for key in ("peer", "card"):
            if key in args:
                body[key] = args[key]
        async with home.client(None) as c:
            r = await c.post("/api/pairing/redeem", json=body)
        self.redeemed.append((r.status_code, r.json()))
        if r.status_code != 200:
            raise LinkError(str(r.json().get("detail") or "pairing refused"))
        got = r.json()
        reply = {
            "ok": True,
            "token": got["token"],
            "sync": got["sync"],
            "ticket": invite_ticket,
            "peer": home.endpoint,
        }
        if self.tamper_in:
            self.tamper_in(reply)
        return reply

    def client_for(self, peer) -> httpx.AsyncClient:
        """Sync rounds and the key calls: the peer's app, with the token this node holds."""
        other = self.world[peer.endpoint_id]
        node = self

        class Transport(httpx.AsyncBaseTransport):
            inner = httpx.ASGITransport(app=other.app)

            async def handle_async_request(self, request):
                if node.fail_key_calls and request.url.path.endswith("/person-key"):
                    raise httpx.ConnectError("tunnel closed", request=request)
                if node.hide_contacts and request.url.path == "/api/sync/contacts":
                    return httpx.Response(404, json={"detail": "Not Found"})
                return await self.inner.handle_async_request(request)

        return httpx.AsyncClient(
            transport=Transport(),
            base_url="http://peer",
            headers={"Authorization": f"Bearer {peer.token}"},
        )

    def client(self, token: str | None = "admin") -> httpx.AsyncClient:
        headers = {}
        if token == "admin":
            headers["Authorization"] = f"Bearer {self.ctx.settings.api_token}"
        elif token:
            headers["Authorization"] = f"Bearer {token}"
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test", headers=headers
        )

    @property
    def sync(self):
        return self.ctx.sync

    @property
    def person(self) -> str:
        return self.ctx.person.public_hex

    @property
    def id(self) -> str:
        return self.ctx.sync_peers.self_info().id

    def peer(self, other: "Node"):
        return self.ctx.sync_peers.get(other.id)

    def ops_of(self, op: str) -> list[dict]:
        return [a for o, a in self.ops if o == op]

    def add_contact(self, keys: PersonKeys, secret="5" * 64, **card):
        return contacts.upsert_contact(self.ctx, card_of(keys, **card), secret)


@pytest.fixture
async def nodes(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMOSYNE_CONFIG_FILE", str(tmp_path / "config.toml"))
    world: dict = {}
    a, b = Node(tmp_path, "A", world), Node(tmp_path, "B", world)
    for n in (a, b):
        n.sync.start()
    yield a, b
    for n in (a, b):
        await n.sync.stop()
        await n.ctx.jobs.shutdown()
        n.ctx.repo.close()


@pytest.fixture
def ada(tmp_path):
    return PersonKeys(tmp_path / "ada")


@pytest.fixture
def grace(tmp_path):
    return PersonKeys(tmp_path / "grace")


async def invite(a: Node) -> str:
    async with a.client() as c:
        r = await c.post("/api/pairing/codes", json={"kind": "sync"})
    assert r.status_code == 200, r.text
    return r.json()["invite"]


async def pair(b: Node, a: Node, code: str | None = None):
    """B redeems a sync invite A shows."""
    async with b.client() as c:
        return await c.post(
            "/api/sync/pair", json={"invite": code or await invite(a), "name": f"{b.name} laptop"}
        )


async def pull(x: Node, y: Node):
    state = await x.ctx.sync.sync_round(x.peer(y))
    await x.ctx.sync.drain()
    return state


def nothing_paired(*nodes: Node):
    for n in nodes:
        assert n.ctx.sync_peers.peers() == []
        assert [d for d in n.ctx.pairing.devices() if d.kind == DESKTOP] == []


# ---- the person key at pairing ---------------------------------------------------------------


async def test_two_fresh_computers_end_up_with_the_inviters_key(nodes):
    a, b = nodes
    assert a.person != b.person
    first, before = a.person, b.person
    r = await pair(b, a)
    assert r.status_code == 200, r.text
    assert r.json()["person_error"] is None
    assert a.person == b.person == first  # B adopted A's
    assert a.ctx.person.fingerprint == b.ctx.person.fingerprint
    assert a.ctx.person.export_private() == b.ctx.person.export_private()

    # What B sent, and what A answered.
    [(_, sent)] = [o for o in b.ops if o[0] == "pair"]
    assert sent["card"] == {"person": {"pub": before, "has_people_data": False}}
    (status, body), *_ = b.redeemed
    assert status == 200 and body["sync"]["person"] == {"pub": first, "adopt": "mine"}
    # The settings screen reads both machines as one person.
    async with b.client() as c:
        me = (await c.get("/api/sync/peers")).json()["self"]
    assert me["person"] == {"pid": b.ctx.person.pid, "fingerprint": b.ctx.person.fingerprint}


async def test_the_side_with_contacts_keeps_its_key_when_A_has_them(nodes, ada):
    a, b = nodes
    a.add_contact(ada)
    first = a.person
    r = await pair(b, a)
    assert r.status_code == 200, r.text
    assert a.person == b.person == first
    (_, body), *_ = b.redeemed
    assert body["sync"]["person"]["adopt"] == "mine"
    # A's contact keeps working: its devices are untouched by the key.
    assert len(a.ctx.pairing.contacts_for(ada.public_hex)) == 2


async def test_A_adopts_Bs_key_when_only_B_has_contacts(nodes, ada):
    a, b = nodes
    b.add_contact(ada)
    key_b = b.person
    assert a.person != key_b
    r = await pair(b, a)
    assert r.status_code == 200, r.text
    assert r.json()["person_error"] is None
    assert a.person == b.person == key_b
    (_, body), *_ = b.redeemed
    assert body["sync"]["person"]["adopt"] == "yours"
    # B put its key to A over the tunnel (the PUT is not a sidecar op: nothing else to see).
    assert len(b.ctx.pairing.contacts_for(ada.public_hex)) == 2
    assert b.ctx.repo.contact(ada.public_hex)["removed_at"] == ""


async def test_different_people_are_not_paired(nodes, ada, grace):
    a, b = nodes
    a.add_contact(ada)
    b.add_contact(grace)
    keys = (a.person, b.person)
    code = await invite(a)
    r = await pair(b, a, code)
    assert r.status_code == 400
    assert "belong to different people" in r.json()["detail"]
    ((status, body),) = b.redeemed
    assert status == 409 and "remove the people one of them shares with first" in body["detail"]
    # Nothing recorded on either side, and the keys are as they were.
    nothing_paired(a, b)
    assert (a.person, b.person) == keys
    assert a.ctx.sync_peers.find_by_endpoint(EP["B"]) is None
    # The code was not used up: pairing nothing costs nothing.
    assert a.ctx.pairing.code_kind(code.rpartition("#")[2]) == "sync"
    # B sent no DESKTOP device away for good: its trust in A was revoked.
    assert b.ctx.pairing.desktops_for(EP["A"]) == []


async def test_computers_from_before_the_person_key_pair_as_they_did(nodes):
    a, b = nodes
    keys = (a.person, b.person)

    # An older B sends no card at all: A offers nothing.
    b.tamper_out = lambda args: args.pop("card")
    r = await pair(b, a)
    assert r.status_code == 200, r.text
    (_, body), *_ = b.redeemed
    assert body["sync"]["person"] is None
    assert (a.person, b.person) == keys and r.json()["person_error"] is None

    # An older A answers without an offer: B changes nothing and asks for nothing.
    for n in (a, b):
        await n.ctx.sync.stop()
    b.redeemed.clear()
    b.tamper_out = None
    b.tamper_in = lambda reply: reply["sync"].pop("person", None)
    r = await pair(b, a)
    assert r.status_code == 200, r.text
    assert (a.person, b.person) == keys and r.json()["person_error"] is None


async def test_a_failure_sharing_the_key_leaves_the_pairing_and_says_so(nodes):
    a, b = nodes
    b.fail_key_calls = True
    r = await pair(b, a)
    assert r.status_code == 200, r.text
    assert "person key was not shared" in r.json()["person_error"]
    assert a.peer(b) is not None and b.peer(a) is not None  # still paired
    assert a.person != b.person
    # The next pairing does it.
    b.fail_key_calls = False
    r = await pair(b, a)
    assert r.status_code == 200 and r.json()["person_error"] is None
    assert a.person == b.person


# ---- the routes ------------------------------------------------------------------------------


async def test_only_a_synced_computer_gets_the_key_and_the_contacts(nodes, ada):
    a, b = nodes
    a.add_contact(ada)
    await pair(b, a)
    token_b_holds = b.peer(a).token
    paths = ("/api/sync/person-key", "/api/sync/contacts")

    # A synced computer (B, with the token A gave it) may read both, and put the key A has.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a.app),
        base_url="http://peer",
        headers={"Authorization": f"Bearer {token_b_holds}"},
    ) as c:
        key = await c.get("/api/sync/person-key")
        assert key.status_code == 200 and key.json() == {"private": a.ctx.person.export_private()}
        listed = await c.get("/api/sync/contacts")
        assert listed.status_code == 200
        body = listed.json()
        assert body["person"] == a.person
        [row] = body["contacts"]
        assert row["person"] == ada.public_hex and row["secret"] == "5" * 64
        assert row["card"]["person"] == ada.public_hex and row["removed_at"] == ""
        assert "machines" not in row and "cursors" not in row
        same = await c.put("/api/sync/person-key", json={"private": key.json()["private"]})
        assert same.status_code == 200 and same.json()["pid"] == a.ctx.person.pid
        # A different key while A shares with people: refused, nothing changes.
        other = PersonKeys(a.ctx.settings.data_dir / "other")
        other.ensure()
        refused = await c.put("/api/sync/person-key", json={"private": other.export_private()})
        assert refused.status_code == 409
        assert (await c.put("/api/sync/person-key", json={"private": "nope"})).status_code == 400
    assert a.ctx.person.public_hex == a.person

    # The admin token is the owner's, not a sync peer: refused. So is a DESKTOP device that is
    # not a sync peer.
    _, loose = a.ctx.pairing.add_desktop("Not synced", "e5" * 32)
    for token in ("admin", loose):
        async with a.client(token) as c:
            for path in paths:
                assert (await c.get(path)).status_code == 403, (token, path)
            put = await c.put("/api/sync/person-key", json={"private": key.json()["private"]})
            assert put.status_code == 403


async def test_a_phone_and_a_contact_cannot_reach_them(nodes, ada):
    a, b = nodes
    await pair(b, a)
    # A paired phone.
    code, _ = a.ctx.pairing.new_code("phone")
    _, phone = a.ctx.pairing.redeem(code, "Phone", None)
    # Ada, a contact of A's: the token she presents to A.
    row = a.add_contact(ada)
    _, ada_token = contacts.tokens(row["secret"], a.person, ada.public_hex)
    device = a.ctx.pairing.verify(ada_token)
    assert device is not None and device.kind == CONTACT
    key = {"private": a.ctx.person.export_private()}
    for token in (phone, ada_token, "not-a-token"):
        async with a.client(token) as c:
            for path in ("/api/sync/person-key", "/api/sync/contacts"):
                assert (await c.get(path)).status_code in (401, 403), (token, path)
            assert (await c.put("/api/sync/person-key", json=key)).status_code in (401, 403)
    # No token at all.
    async with a.client(None) as c:
        for path in ("/api/sync/person-key", "/api/sync/contacts"):
            assert (await c.get(path)).status_code == 401


async def test_a_team_server_answers_409(nodes):
    a, b = nodes
    await pair(b, a)
    a.ctx.settings.team_mode = True
    peer_token = b.peer(a).token
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a.app),
        base_url="http://peer",
        headers={"Authorization": f"Bearer {peer_token}"},
    ) as c:
        for path in ("/api/sync/person-key", "/api/sync/contacts"):
            assert (await c.get(path)).status_code == 409


# ---- contacts in a round ---------------------------------------------------------------------


async def test_contacts_travel_between_a_persons_computers(nodes, ada):
    a, b = nodes
    await pair(b, a)
    assert a.person == b.person
    assert "contacts" not in SyncSession.model_fields  # never inside a meeting

    # A added Ada; B has her after a round, with devices for both her machines.
    a.add_contact(ada)
    assert b.ctx.repo.contact(ada.public_hex) is None
    await pull(b, a)
    row = b.ctx.repo.contact(ada.public_hex)
    assert row is not None and row["name"] == "Ada" and row["secret"] == "5" * 64
    assert row["card"]["person"] == ada.public_hex
    assert {d.endpoint_id for d in b.ctx.pairing.contacts_for(ada.public_hex)} == {
        EP["X1"],
        EP["X2"],
    }
    # Her token opens B as her, and it is the token A derived too.
    _, token = contacts.tokens("5" * 64, b.person, ada.public_hex)
    assert b.ctx.pairing.verify(token).person == ada.public_hex
    # B dialed her machines in the background.
    assert {a["ticket"] for a in b.ops_of("dial")} >= {ticket(EP["X1"]), ticket(EP["X2"])}
    assert set(b.ctx.repo.contact(ada.public_hex)["machines"]) == {EP["X1"], EP["X2"]}

    # The same round again changes nothing and dials nothing new.
    dials = len(b.ops_of("dial"))
    await pull(b, a)
    assert len(b.ops_of("dial")) == dials

    # A renames her for itself: B calls her that too, devices follow.
    await contacts.rename_contact(a.ctx, ada.public_hex, "Ada L")
    await pull(b, a)
    row = b.ctx.repo.contact(ada.public_hex)
    assert row["my_name"] == "Ada L" and contacts.my_or_card_name(row) == "Ada L"
    assert {d.name for d in b.ctx.pairing.contacts_for(ada.public_hex)} == {"Ada L"}
    assert set(row["machines"]) == {EP["X1"], EP["X2"]}  # runtime state stayed

    # B removes her: A drops her, revokes her devices and hangs up on her machines.
    await contacts.dial_all(a.ctx, a.ctx.repo.contact(ada.public_hex))
    assert set(a.ctx.repo.contact(ada.public_hex)["machines"]) == {EP["X1"], EP["X2"]}
    assert await contacts.forget_contact(b.ctx, ada.public_hex)
    removed_at = b.ctx.repo.contact(ada.public_hex)["removed_at"]
    assert removed_at
    await pull(a, b)
    here = a.ctx.repo.contact(ada.public_hex)
    assert here["removed_at"] == removed_at  # the stamp travelled, A did not make its own
    assert a.ctx.pairing.contacts_for(ada.public_hex) == []
    assert {x["peer"] for x in a.ops_of("hangup")} == {EP["X1"], EP["X2"]}
    assert here["machines"] == {}
    assert a.ctx.repo.contacts() == [] and len(a.ctx.repo.contacts(include_removed=True)) == 1
    _, token = contacts.tokens("5" * 64, a.person, ada.public_hex)
    assert a.ctx.pairing.verify(token) is None

    # A pulling again, and B pulling the removal back, change nothing.
    await pull(a, b)
    await pull(b, a)
    assert b.ctx.repo.contact(ada.public_hex)["removed_at"] == removed_at


async def test_a_removal_that_arrives_first_is_remembered(nodes, ada):
    """B never had Ada: a removed row still arrives (so it cannot come back from an older copy)."""
    a, b = nodes
    await pair(b, a)
    a.add_contact(ada)
    await contacts.forget_contact(a.ctx, ada.public_hex)
    await pull(b, a)
    row = b.ctx.repo.contact(ada.public_hex)
    assert row is not None and row["removed_at"]
    assert b.ctx.pairing.contacts_for(ada.public_hex) == [] and b.ctx.repo.contacts() == []
    assert ticket(EP["X1"]) not in [x["ticket"] for x in b.ops_of("dial")]


async def test_an_older_peer_without_contacts_is_skipped(nodes, ada):
    a, b = nodes
    await pair(b, a)
    a.add_contact(ada)
    b.hide_contacts = True  # A answers 404 to /api/sync/contacts, like an older peer
    state = await pull(b, a)
    assert state.state == "done" and state.error is None
    assert b.ctx.repo.contacts(include_removed=True) == []
    b.hide_contacts = False
    await pull(b, a)
    assert b.ctx.repo.contact(ada.public_hex) is not None


# ---- computers paired before the person key was shared ---------------------------------------


async def paired_with_own_keys(a: Node, b: Node) -> None:
    """Paired as before the key existed: each computer made its own key and kept it."""
    assert (await pair(b, a)).status_code == 200
    b.ctx.person.rotate()
    assert a.person != b.person


@pytest.mark.parametrize("first", ["a", "b"])
async def test_neither_having_contacts_the_lower_key_wins_whoever_asks_first(nodes, first):
    a, b = nodes
    await paired_with_own_keys(a, b)
    lower = min(a.person, b.person)
    rounds = [(a, b), (b, a)] if first == "a" else [(b, a), (a, b)]
    for x, y in rounds:
        assert (await pull(x, y)).state == "done"
    assert a.person == b.person == lower
    # And it stays put.
    for x, y in rounds:
        await pull(x, y)
    assert a.person == b.person == lower
    assert a.sync.state(b.id).warning is None and b.sync.state(a.id).warning is None


@pytest.mark.parametrize("holder", ["a", "b"])
async def test_the_side_without_contacts_adopts_and_gets_them_in_the_same_round(nodes, ada, holder):
    a, b = nodes
    await paired_with_own_keys(a, b)
    has, lacks = (a, b) if holder == "a" else (b, a)
    has.add_contact(ada)
    key = has.person

    # The holder asking first changes nothing (and gets nothing: the other has none).
    await pull(has, lacks)
    assert has.person == key and lacks.person != key

    await pull(lacks, has)
    assert lacks.person == has.person == key
    row = lacks.ctx.repo.contact(ada.public_hex)
    assert row is not None and row["secret"] == "5" * 64
    assert {d.endpoint_id for d in lacks.ctx.pairing.contacts_for(ada.public_hex)} == {
        EP["X1"],
        EP["X2"],
    }
    assert has.ctx.repo.contact(ada.public_hex)["removed_at"] == ""


async def test_two_people_with_contacts_keep_their_keys_and_a_warning(nodes, ada, grace):
    a, b = nodes
    await paired_with_own_keys(a, b)
    a.add_contact(ada)
    b.add_contact(grace)
    keys = (a.person, b.person)
    session = a.ctx.repo.save(Session(name="Planning", status=SessionStatus.COMPLETED))

    for _ in range(2):  # sticky: still there the second time
        state = await pull(b, a)
        assert state.state == "done"  # not an error: meetings sync
        assert state.warning == "A desk belongs to another person: contacts are not shared"
    assert (a.person, b.person) == keys
    assert b.ctx.repo.contact(ada.public_hex) is None
    assert b.ctx.repo.get(session.id) is not None  # the meeting arrived anyway

    await pull(a, b)
    assert a.ctx.repo.contact(grace.public_hex) is None and (a.person, b.person) == keys
    assert (
        a.sync.state(b.id).warning == "B laptop belongs to another person: contacts are not shared"
    )
    async with b.client() as c:
        [peer] = (await c.get("/api/sync/status")).json()["peers"]
    assert peer["warning"] == "A desk belongs to another person: contacts are not shared"
    assert peer["state"] == "done"
