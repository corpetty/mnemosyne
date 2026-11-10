"""Pairing two people's computers (routes/contacts.py, the contact kind of routes/pairing.py,
services/contacts.py `pair_with_invite` / `dial_all` / `dial_contacts`): two apps in this
process, with the sidecar between them stood in by a `link.request` that carries B's `pair` to
A's POST /api/pairing/redeem exactly as the real sidecar does (the calling endpoint id, the
invite's code, the card passed through, and the reply's token and card handed back)."""

import base64
import copy

import httpx
import pytest

from mnemosyne.api.app import create_app
from mnemosyne.config import Settings
from mnemosyne.services import contacts
from mnemosyne.services.link import LinkError, endpoint_id_from_ticket
from mnemosyne.services.pairing import CONTACT, InvalidPairingCode
from mnemosyne.services.person import PersonKeys
from mnemosyne.services.sync_peers import SyncPeer, now_iso
from mnemosyne.storage.crypto import MemoryKeyStore

pytestmark = pytest.mark.anyio

EP = {"A": "a1" * 32, "A2": "a2" * 32, "B": "b2" * 32, "X": "c3" * 32}


def ticket(endpoint_id: str) -> str:
    raw = b"\x00" + bytes.fromhex(endpoint_id) + b"\x00"
    return "endpoint" + base64.b32encode(raw).decode().lower().rstrip("=")


async def _stand_in_tunnel(_app) -> int:
    return 18009


class Node:
    """One person's computer: an app with remote access 'running' on a stand-in sidecar."""

    def __init__(self, tmp_path, name: str, world: dict):
        self.name = name
        self.world = world
        self.endpoint = EP[name]
        settings = Settings(
            data_dir=tmp_path / name / "data",
            backup_dir=str(tmp_path / name / "backups"),
            api_token=f"admin-{name}",
            local_speaker_name=f"Person {name}",
        )
        self.app = create_app(settings, keystore=MemoryKeyStore())
        self.ctx = self.app.state.ctx
        self.ctx.sync_peers.set_name(f"{name} desk")
        self.ctx.settings.remote_access = True  # as the sidecar stand-in below implies
        self.ctx.link.status.running = True
        self.ctx.link.status.endpoint_id = self.endpoint
        self.ctx.link.status.ticket = ticket(self.endpoint)
        self.ctx.tunnel.ensure = _stand_in_tunnel  # no real tunnel listener for a stand-in
        self.ops: list[tuple[str, dict]] = []
        self.unreachable: set[str] = set()  # endpoints dial refuses
        self.tamper_out = None  # (card_args) -> None: change what leaves with `pair`
        self.tamper_in = None  # (reply) -> None: change what comes back
        self.listens: dict[str, str] = {}
        self.ctx.link.request = self.request
        world[self.endpoint] = self

    # The sidecar.
    async def request(self, op: str, **args) -> dict:
        self.ops.append((op, copy.deepcopy(args)))
        if op == "dial":
            endpoint = endpoint_id_from_ticket(args["ticket"])
            if endpoint in self.unreachable:
                raise LinkError("Could not reach that computer")
            listen = self.listens.setdefault(endpoint, f"127.0.0.1:{41000 + len(self.listens)}")
            return {"ok": True, "peer": endpoint, "listen": listen}
        if op == "hangup":
            return {"ok": True, "peer": args["peer"], "closed": True}
        assert op == "pair", op
        if self.tamper_out:
            self.tamper_out(args["card"])
        invite_ticket, _, code = args["invite"].rpartition("#")
        home = self.world[endpoint_id_from_ticket(invite_ticket)]
        body = {
            "code": code,
            "name": args["name"],
            "endpoint_id": self.endpoint,
            "card": args["card"],
        }
        async with home.client(None) as c:
            r = await c.post("/api/pairing/redeem", json=body)
        if r.status_code != 200:
            raise LinkError(str(r.json().get("detail") or "pairing refused"))
        got = r.json()
        reply = {
            "ok": True,
            "token": got["token"],
            "card": got["card"],
            "sync": got["sync"],
            "ticket": invite_ticket,
            "peer": home.endpoint,
        }
        if self.tamper_in:
            self.tamper_in(reply)
        return reply

    def client(self, token: str | None = "admin") -> httpx.AsyncClient:
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {self.ctx.settings.api_token}"
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test", headers=headers
        )

    @property
    def person(self) -> str:
        return self.ctx.person.public_hex

    def op_tickets(self, op: str) -> list[str]:
        return [a["ticket"] for o, a in self.ops if o == op]

    async def code(self) -> str:
        async with self.client() as c:
            r = await c.post("/api/contacts/codes")
        assert r.status_code == 200, r.text
        assert r.json()["kind"] == "contact"
        return r.json()["invite"]


@pytest.fixture
async def nodes(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMOSYNE_CONFIG_FILE", str(tmp_path / "config.toml"))
    world: dict = {}
    a, b = Node(tmp_path, "A", world), Node(tmp_path, "B", world)
    yield a, b
    for n in (a, b):
        await n.ctx.jobs.shutdown()
        n.ctx.repo.close()


async def pair(b: Node, invite: str):
    async with b.client() as c:
        return await c.post("/api/contacts/pair", json={"invite": invite})


def nothing_recorded(*nodes: Node):
    for n in nodes:
        assert n.ctx.repo.contacts(include_removed=True) == []
        assert n.ctx.pairing.devices() == []


# ---- the happy path -------------------------------------------------------------------------


async def test_pairing_leaves_both_sides_with_each_other(nodes):
    a, b = nodes
    # A has a second computer in its library sync: its card lists both machines.
    a.ctx.sync_peers.add(SyncPeer("p2", "A laptop", EP["A2"], ticket(EP["A2"]), "tok", now_iso()))
    invite = await a.code()
    assert invite.startswith(ticket(EP["A"]) + "#")

    r = await pair(b, invite)
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["person"] == a.person and got["pid"] == "p:" + a.person[:16]
    assert got["name"] == got["card_name"] == "Person A"
    assert got["machines"] == ["A desk", "A laptop"]
    assert got["fingerprint"] == a.ctx.person.fingerprint

    row_b, row_a = b.ctx.repo.contact(a.person), a.ctx.repo.contact(b.person)
    assert row_b is not None and row_a is not None
    assert row_a["secret"] == row_b["secret"] and len(row_b["secret"]) == 64
    assert row_a["name"] == "Person B" and row_b["name"] == "Person A"
    assert contacts.verify_card(row_a["card"], expected_person=b.person)
    assert contacts.verify_card(row_b["card"], expected_person=a.person)

    # CONTACT devices, one per endpoint of the other's card, and no random-token device.
    assert {d.endpoint_id for d in b.ctx.pairing.contacts_for(a.person)} == {EP["A"], EP["A2"]}
    assert {d.endpoint_id for d in a.ctx.pairing.contacts_for(b.person)} == {EP["B"]}
    assert all(d.kind == CONTACT for d in a.ctx.pairing.devices() + b.ctx.pairing.devices())

    # The token B presents to A opens A as B's contact, and the other way round.
    to_a, from_a = contacts.tokens(row_b["secret"], b.person, a.person)
    device = a.ctx.pairing.verify(to_a)
    assert device is not None and device.kind == CONTACT and device.person == b.person
    assert device.endpoint_id == EP["B"]
    device = b.ctx.pairing.verify(from_a)
    assert device is not None and device.kind == CONTACT and device.person == a.person
    assert a.ctx.pairing.verify(from_a) is None and b.ctx.pairing.verify(to_a) is None

    # Each dialed the other's machines and kept the loopback addresses.
    assert set(b.op_tickets("dial")) == {ticket(EP["A"]), ticket(EP["A2"])}
    assert a.op_tickets("dial") == [ticket(EP["B"])]
    assert set(row_b["machines"]) == {EP["A"], EP["A2"]}
    assert b.ctx.repo.contact(a.person)["machines"][EP["A"]]["listen"].startswith("127.0.0.1:")
    assert a.ctx.repo.contact(b.person)["machines"][EP["B"]]["listen"].startswith("127.0.0.1:")

    # What B sent through the sidecar: its card and the secret; the code is used up.
    [(_, sent)] = [o for o in b.ops if o[0] == "pair"]
    assert sent["card"]["secret"] == row_b["secret"] and sent["invite"] == invite
    assert (await pair(b, invite)).status_code in (400, 409)
    assert a.ctx.pairing.code_kind(invite.rpartition("#")[2]) is None


async def test_the_contact_code_is_not_a_device_code(nodes):
    a, b = nodes
    code = (await a.code()).rpartition("#")[2]
    async with a.client(None) as c:  # no token needed to redeem, but a contact code needs a card
        r = await c.post("/api/pairing/redeem", json={"code": code, "endpoint_id": EP["B"]})
    assert r.status_code == 400 and "card" in r.json()["detail"]
    assert a.ctx.pairing.code_kind(code) == "contact"  # not used up
    nothing_recorded(a)
    with pytest.raises(InvalidPairingCode, match="contact"):
        a.ctx.pairing.redeem(code, "x", EP["B"])


async def test_pairing_again_replaces_the_secret_and_restores_a_removed_contact(nodes):
    a, b = nodes
    await pair(b, await a.code())
    first = b.ctx.repo.contact(a.person)["secret"]
    async with b.client() as c:
        assert (await c.delete(f"/api/contacts/{a.person}")).status_code == 200
    assert b.ctx.pairing.contacts_for(a.person) == []
    assert (await pair(b, await a.code())).status_code == 200
    row = b.ctx.repo.contact(a.person)
    assert row["removed_at"] == "" and row["secret"] != first
    assert len(b.ctx.pairing.contacts_for(a.person)) == 1
    # A holds one row for B, with the new secret.
    assert a.ctx.repo.contact(b.person)["secret"] == row["secret"]
    assert len(a.ctx.repo.contacts(include_removed=True)) == 1


# ---- refusals leave nothing behind ----------------------------------------------------------


async def test_a_tampered_card_is_refused_and_nothing_is_recorded(nodes):
    a, b = nodes
    invite = await a.code()
    b.tamper_out = lambda card: card["card"].update(name="Mallory")
    r = await pair(b, invite)
    assert r.status_code == 400 and "not valid" in r.json()["detail"]
    nothing_recorded(a, b)
    assert a.ctx.pairing.code_kind(invite.rpartition("#")[2]) == "contact"  # still usable
    # Without the tampering the same code works.
    b.tamper_out = None
    assert (await pair(b, invite)).status_code == 200


async def test_a_card_that_does_not_list_the_calling_computer_is_refused(nodes, tmp_path):
    a, b = nodes
    invite = await a.code()
    stranger = PersonKeys(tmp_path / "stranger")
    body = {
        "person": stranger.public_hex,
        "name": "Stranger",
        "machines": [{"name": "Desk", "ticket": ticket(EP["X"])}],
        "at": "2026-10-23T10:00:00+00:00",
    }
    signed = {**body, "sig": stranger.sign(PersonKeys.canonical(body))}

    def swap(card):
        card["card"] = signed

    b.tamper_out = swap
    r = await pair(b, invite)
    assert r.status_code == 400 and "does not list the computer" in r.json()["detail"]
    nothing_recorded(a, b)


async def test_a_card_for_our_own_key_is_refused(nodes):
    a, b = nodes
    code = (await a.code()).rpartition("#")[2]
    own = contacts.build_card(a.ctx).model_dump()
    async with a.client(None) as c:
        r = await c.post(
            "/api/pairing/redeem",
            json={
                "code": code,
                "endpoint_id": EP["A"],
                "card": {"card": own, "secret": contacts.new_secret()},
            },
        )
    assert r.status_code == 400 and "your own" in r.json()["detail"]
    nothing_recorded(a)


async def test_a_bad_secret_is_refused(nodes):
    a, b = nodes
    invite = await a.code()
    b.tamper_out = lambda card: card.update(secret="not-hex")
    r = await pair(b, invite)
    assert r.status_code == 400 and "secret" in r.json()["detail"]
    nothing_recorded(a, b)


async def test_a_wrong_reply_leaves_nothing_on_this_side(nodes, tmp_path):
    a, b = nodes
    for tamper, text in (
        (lambda reply: reply.update(token="0" * 64), "shared secret"),
        (lambda reply: reply["card"].update(name="Mallory"), "no valid contact card"),
        (lambda reply: reply.update(card=None), "no valid contact card"),
    ):
        b.tamper_in = tamper
        r = await pair(b, await a.code())
        assert r.status_code == 400 and text in r.json()["detail"], r.text
        assert b.ctx.repo.contacts(include_removed=True) == []
        assert b.ctx.pairing.devices() == []
    # A card of someone else, validly signed, that does not list the invite's computer.
    other = PersonKeys(tmp_path / "other")
    body = {
        "person": other.public_hex,
        "name": "Other",
        "machines": [{"name": "Desk", "ticket": ticket(EP["X"])}],
        "at": "2026-10-23T10:00:00+00:00",
    }
    b.tamper_in = lambda reply: reply.update(
        card={**body, "sig": other.sign(PersonKeys.canonical(body))}
    )
    r = await pair(b, await a.code())
    assert r.status_code == 400 and "does not list" in r.json()["detail"]
    assert b.ctx.repo.contacts(include_removed=True) == []
    assert b.ctx.pairing.devices() == []


async def test_a_dial_failure_does_not_undo_the_pairing(nodes):
    a, b = nodes
    b.unreachable = {EP["A"]}
    r = await pair(b, await a.code())
    assert r.status_code == 200
    assert b.ctx.repo.contact(a.person)["machines"] == {}  # kept, dialed later at startup
    assert len(b.ctx.pairing.contacts_for(a.person)) == 1


async def test_invites_that_are_not_contact_invites(nodes):
    a, b = nodes
    for invite in ("nonsense", f"{ticket(EP['A'])}#", "endpointzzz#code"):
        r = await pair(b, invite)
        assert r.status_code == 400 and "contact invite" in r.json()["detail"], invite
    own = await b.code()
    r = await pair(b, own)
    assert r.status_code == 400 and "this computer" in r.json()["detail"]
    assert [o for o, _ in b.ops if o == "pair"] == []
    # A used-up or unknown code: the sidecar relays A's refusal.
    r = await pair(b, f"{ticket(EP['A'])}#nocode")
    assert r.status_code == 400 and "Pairing failed" in r.json()["detail"]
    nothing_recorded(a, b)


async def test_team_mode_refuses_everywhere(nodes):
    a, b = nodes
    invite = await a.code()
    a.ctx.settings.team_mode = True
    b.ctx.settings.team_mode = True
    async with a.client() as c:
        for r in (
            await c.post("/api/contacts/codes"),
            await c.get("/api/contacts"),
            await c.patch(f"/api/contacts/{b.person}", json={"name": "x"}),
            await c.delete(f"/api/contacts/{b.person}"),
        ):
            assert r.status_code == 409 and "team server" in r.json()["detail"]
    r = await pair(b, invite)
    assert r.status_code == 409 and "team server" in r.json()["detail"]
    # A code made earlier cannot be redeemed on a team server either.
    async with a.client(None) as c:
        r = await c.post(
            "/api/pairing/redeem",
            json={"code": invite.rpartition("#")[2], "endpoint_id": EP["B"], "card": {}},
        )
    assert r.status_code == 409 and "team server" in r.json()["detail"]
    a.ctx.settings.team_mode = b.ctx.settings.team_mode = False
    nothing_recorded(a, b)


async def test_remote_access_must_be_on(nodes):
    a, b = nodes
    invite = await a.code()
    b.ctx.link.status.running = False
    r = await pair(b, invite)
    # Its sidecar is gone and this test has none to start: pairing says why instead of asking
    # the person to turn anything on.
    assert r.status_code == 409 and "mnemosyne-link" in r.json()["detail"]
    a.ctx.link.status.running = False
    async with a.client() as c:
        r = await c.post("/api/contacts/codes")
    assert r.status_code == 409 and "mnemosyne-link" in r.json()["detail"]
    nothing_recorded(a, b)
    # A code redeemed while A's remote access is off is refused, not recorded.
    b.ctx.link.status.running = True
    r = await pair(b, invite)
    assert r.status_code == 400 and "remote access" in r.json()["detail"]
    nothing_recorded(a, b)


async def test_the_routes_need_the_admin_and_a_code_makes_the_api_token(nodes):
    a, _ = nodes
    async with a.client(None) as c:
        for r in (
            await c.get("/api/contacts"),
            await c.post("/api/contacts/codes"),
            await c.post("/api/contacts/pair", json={"invite": "x#y"}),
        ):
            assert r.status_code == 401
    a.ctx.settings.api_token = ""
    async with a.client(None) as c:
        r = await c.post("/api/contacts/codes")
    assert r.status_code == 200 and a.ctx.settings.api_token  # made, not asked for


async def test_pairing_makes_the_api_token_on_both_sides(nodes):
    a, b = nodes
    invite = await a.code()
    b.ctx.settings.api_token = ""
    async with b.client(None) as c:
        r = await c.post("/api/contacts/pair", json={"invite": invite})
    assert r.status_code == 200, r.text
    assert b.ctx.settings.api_token  # made here, as A's was when it showed the code


async def test_a_side_that_lost_its_token_after_showing_the_code_refuses_to_redeem(nodes):
    a, b = nodes
    invite = await a.code()
    b.ctx.settings.api_token = "admin-B"
    a.ctx.settings.api_token = ""
    r = await pair(b, invite)
    assert r.status_code == 400 and "API token" in r.json()["detail"]
    nothing_recorded(a, b)
    a.ctx.settings.api_token = "admin-A"
    assert a.ctx.pairing.code_kind(invite.rpartition("#")[2]) == "contact"
    assert (await pair(b, invite)).status_code == 200


# ---- the routes -----------------------------------------------------------------------------


async def test_listing_renaming_and_removing(nodes):
    a, b = nodes
    async with b.client() as c:
        empty = (await c.get("/api/contacts")).json()
    assert empty["contacts"] == []
    me = empty["me"]
    assert set(me) == {"person", "pid", "name", "fingerprint"}
    assert me["person"] == b.person and me["pid"] == b.ctx.person.pid
    assert me["name"] == "Person B" and me["fingerprint"] == b.ctx.person.fingerprint

    await pair(b, await a.code())
    async with b.client() as c:
        listed = (await c.get("/api/contacts")).json()
        [one] = listed["contacts"]
        assert set(one) == {
            "person",
            "pid",
            "name",
            "card_name",
            "fingerprint",
            "machines",
            "created_at",
            "updated_at",
            "pull",
        }
        assert one["person"] == a.person and "secret" not in str(listed)

        r = await c.patch(f"/api/contacts/{a.person}", json={"name": "  Ada  "})
        assert r.status_code == 200 and r.json()["name"] == "Ada"
        assert r.json()["card_name"] == "Person A"
        assert [d.name for d in b.ctx.pairing.contacts_for(a.person)] == ["Ada"]
        assert b.ctx.repo.contact(a.person)["my_name"] == "Ada"
        r = await c.patch(f"/api/contacts/{a.person}", json={"name": ""})
        assert r.json()["name"] == "Person A"
        assert [d.name for d in b.ctx.pairing.contacts_for(a.person)] == ["Person A"]
        assert (await c.patch("/api/contacts/" + "9" * 64, json={"name": "x"})).status_code == 404

        r = await c.delete(f"/api/contacts/{a.person}")
        assert r.status_code == 200
        assert (await c.get("/api/contacts")).json()["contacts"] == []
        assert (await c.delete(f"/api/contacts/{a.person}")).status_code == 404
        assert (await c.patch(f"/api/contacts/{a.person}", json={"name": "x"})).status_code == 404
    # The row stays (so the removal can travel), its devices are gone, its machines hung up.
    row = b.ctx.repo.contact(a.person)
    assert row["removed_at"] and row["machines"] == {}
    assert b.ctx.pairing.contacts_for(a.person) == []
    assert {a_["peer"] for o, a_ in b.ops if o == "hangup"} == {EP["A"]}
    # A's token no longer opens B.
    to_b = contacts.tokens(row["secret"], a.person, b.person)[0]
    assert b.ctx.pairing.verify(to_b) is None


async def test_the_pairing_code_response_shape(nodes):
    a, _ = nodes
    async with a.client() as c:
        r = await c.post("/api/contacts/codes")
        plain = await c.post("/api/pairing/codes", json={"kind": "contact"})
    body = r.json()
    assert set(body) == {"code", "kind", "expires_at", "urls", "invite"}
    assert body["urls"] == [] and body["invite"] == f"{ticket(EP['A'])}#{body['code']}"
    assert plain.status_code == 200 and plain.json()["kind"] == "contact"


async def test_a_contact_invite_takes_a_length(nodes):
    from datetime import UTC, datetime

    a, _ = nodes
    async with a.client() as c:
        day = await c.post("/api/contacts/codes", json={"minutes": 1440})
        bad = await c.post("/api/contacts/codes", json={"minutes": 2})
    left = datetime.fromisoformat(day.json()["expires_at"]) - datetime.now(UTC)
    assert 1439 < left.total_seconds() / 60 <= 1440
    assert bad.status_code == 422


# ---- dialing at startup ---------------------------------------------------------------------


async def test_contacts_are_dialed_again_when_the_sidecar_starts(nodes):
    a, b = nodes
    await pair(b, await a.code())
    b.ops.clear()
    b.listens.clear()
    b.ctx.repo.set_contact_runtime(a.person, machines={})
    for hook in b.ctx.link.on_ready:  # what LinkService runs once the sidecar is ready
        await hook()
    assert b.op_tickets("dial") == [ticket(EP["A"])]
    assert b.ctx.repo.contact(a.person)["machines"] == {EP["A"]: {"listen": "127.0.0.1:41000"}}


async def test_dial_contacts_skips_removed_ones_and_never_raises(nodes, caplog):
    a, b = nodes
    await pair(b, await a.code())
    b.ops.clear()

    b.unreachable = {EP["A"]}
    await contacts.dial_contacts(b.ctx)  # a machine that cannot be reached is skipped
    assert b.ctx.repo.contact(a.person)["machines"] == {}

    async def boom(op, **args):
        raise RuntimeError("sidecar exploded")

    b.ctx.link.request = boom
    await contacts.dial_contacts(b.ctx)  # nothing escapes
    assert "Could not dial contact" in caplog.text

    b.ctx.link.request = b.request
    b.unreachable = set()
    b.ops.clear()
    await contacts.forget_contact(b.ctx, a.person)
    b.ops.clear()
    await contacts.dial_contacts(b.ctx)
    assert b.ops == []

    b.ctx.settings.team_mode = True
    await contacts.dial_contacts(b.ctx)  # nothing to do on a team server
    assert b.ops == []


async def test_a_hosting_computer_pairs_contacts_but_a_dedicated_server_does_not(nodes):
    """A computer with a team owner is somebody's computer: its owner pairs contacts as on any
    other (docs/plans/2026-11-09-team-hosting.md 1(b)); a team member cannot."""
    a, b = nodes
    owner = a.ctx.users.add("Corey", "", "admin")
    member = a.ctx.users.add("Sam", "", "member")
    a.ctx.settings.team_mode = True
    a.ctx.settings.team_owner_id = owner.id
    a.ctx.follow_hosting()

    invite = await a.code()  # the API token is the owner's channel
    code, _ = a.ctx.users.invite(member.id)
    _, member_token = a.ctx.users.redeem(code, "browser")
    async with a.client(None) as c:
        for r in (
            await c.post(
                "/api/contacts/codes", headers={"Authorization": f"Bearer {member_token}"}
            ),
            await c.get("/api/contacts", headers={"Authorization": f"Bearer {member_token}"}),
        ):
            assert r.status_code == 403
    assert (await pair(b, invite)).status_code == 200
    assert [r["person"] for r in a.ctx.repo.contacts()] == [b.person]
    # The contact's token opens /api/share/ on the host, as that contact.
    to_a, _ = contacts.tokens(a.ctx.repo.contact(b.person)["secret"], b.person, a.person)
    async with a.client(None) as c:
        r = await c.get("/api/share/hello", headers={"Authorization": f"Bearer {to_a}"})
        assert r.status_code == 200 and r.json()["person"] == a.person
        # ... and a member's token never opens it.
        r = await c.get("/api/share/hello", headers={"Authorization": f"Bearer {member_token}"})
        assert r.status_code in (401, 403)
    # Without an owner it is a dedicated server again: nothing works.
    a.ctx.settings.team_owner_id = ""
    async with a.client() as c:
        assert (await c.post("/api/contacts/codes")).status_code == 409
