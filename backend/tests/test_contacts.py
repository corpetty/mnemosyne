"""Contacts: cards, tokens, rows and the CONTACT devices (services/contacts.py)."""

import base64
import hashlib
import json
import sqlite3

import pytest

from mnemosyne import access
from mnemosyne.models.contact import Card
from mnemosyne.services import contacts, pairing
from mnemosyne.services.person import PersonKeys
from mnemosyne.services.sync_peers import SyncPeer
from mnemosyne.storage.sqlite import SCHEMA_VERSION, SessionRepository

EP_A, EP_B, EP_C = "a1" * 32, "b2" * 32, "c3" * 32


def ticket(endpoint_id: str) -> str:
    raw = b"\x00" + bytes.fromhex(endpoint_id) + b"\x00"
    return "endpoint" + base64.b32encode(raw).decode().lower().rstrip("=")


@pytest.fixture
def me(ctx):
    ctx.link.status.running, ctx.link.status.ticket = True, ticket(EP_A)
    ctx.settings.local_speaker_name = "Corey"
    return ctx


@pytest.fixture
def ada(tmp_path):
    return PersonKeys(tmp_path / "ada")


def card_of(
    keys: PersonKeys, name="Ada", machines=((("Desk", EP_B),)), at="2026-10-23T10:00:00+00:00"
):
    body = {
        "person": keys.public_hex,
        "name": name,
        "machines": [{"name": n, "ticket": ticket(e)} for n, e in machines],
        "at": at,
    }
    return Card(**body, sig=keys.sign(PersonKeys.canonical(body)))


def sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---- cards ----------------------------------------------------------------------------------


def test_build_card_lists_this_computer_and_the_sync_peers(me):
    me.sync_peers.add(
        SyncPeer("p1", "Laptop", EP_C, ticket(EP_C), "tok", "2026-10-01T00:00:00+00:00")
    )
    card = contacts.build_card(me)
    assert card.person == me.person.public_hex and card.name == "Corey"
    assert [m.name for m in card.machines] == [me.sync_peers.self_info().name, "Laptop"]
    assert card.machines[0].ticket == ticket(EP_A)
    assert contacts.endpoints(card) == [EP_A, EP_C]
    assert contacts.verify_card(card) and contacts.verify_card(card.model_dump())
    assert contacts.verify_card(card, expected_person=me.person.public_hex)


def test_the_default_name_falls_back_to_the_computer_name(me):
    me.settings.local_speaker_name = "Me"
    assert contacts.build_card(me).name == me.sync_peers.self_info().name


def test_build_card_needs_remote_access(ctx):
    with pytest.raises(contacts.ContactsUnavailable, match="remote access"):
        contacts.build_card(ctx)


def test_tampering_breaks_the_signature(ada):
    card = card_of(ada, machines=(("Desk", EP_B), ("Phone", EP_C)))
    assert contacts.verify_card(card)
    d = card.model_dump()
    for change in (
        {"name": "Mallory"},
        {"machines": d["machines"][:1]},
        {"machines": [{"name": "Desk", "ticket": ticket(EP_A)}, d["machines"][1]]},
        {"at": "2026-10-24T10:00:00+00:00"},
        {"sig": "0" * 128},
        {"sig": "nothex"},
        {"person": "f" * 64},
        {"person": "short"},
    ):
        assert not contacts.verify_card({**d, **change}), change
    assert not contacts.verify_card({"person": ada.public_hex})
    assert not contacts.verify_card(card, expected_person="e" * 64)


def test_endpoints_skip_tickets_that_do_not_parse(ada):
    card = card_of(ada, machines=(("Desk", EP_B), ("Dup", EP_B), ("Odd", EP_C)))
    card.machines[2].ticket = "garbage"
    assert contacts.endpoints(card) == [EP_B]


# ---- tokens ---------------------------------------------------------------------------------


def test_tokens_are_symmetric_and_directional(ctx, ada):
    secret = contacts.new_secret()
    assert len(secret) == 64 and secret != contacts.new_secret()
    mine, theirs = ctx.person.public_hex, ada.public_hex
    to_them, from_them = contacts.tokens(secret, mine, theirs)
    # Seen from their side the two are swapped.
    assert contacts.tokens(secret, theirs, mine) == (from_them, to_them)
    assert to_them != from_them and len(to_them) == 64
    assert contacts.tokens(contacts.new_secret(), mine, theirs) != (to_them, from_them)


# ---- upsert / devices -----------------------------------------------------------------------


def test_upsert_makes_a_contact_device_per_endpoint(me, ada):
    card = card_of(ada, machines=(("Desk", EP_B), ("Phone", EP_C)))
    secret = contacts.new_secret()
    row = contacts.upsert_contact(me, card, secret)
    assert row["person"] == ada.public_hex and row["name"] == "Ada" and row["removed_at"] == ""
    assert me.repo.contact(ada.public_hex)["card"] == card.model_dump()

    _, presented = contacts.tokens(secret, me.person.public_hex, ada.public_hex)
    # What they present to us is what we hash; the token we present to them is not accepted.
    device = me.pairing.verify(presented)
    assert device.kind == pairing.CONTACT and device.person == ada.public_hex
    assert sorted(d.endpoint_id for d in me.pairing.contacts_for(ada.public_hex)) == [EP_B, EP_C]
    assert (
        me.pairing.verify(contacts.tokens(secret, me.person.public_hex, ada.public_hex)[0]) is None
    )
    assert contacts.by_endpoint(me, EP_C)["person"] == ada.public_hex
    assert contacts.by_endpoint(me, EP_A) is None


def test_a_card_with_a_machine_fewer_revokes_its_device(me, ada):
    secret = contacts.new_secret()
    contacts.upsert_contact(me, card_of(ada, machines=(("Desk", EP_B), ("Phone", EP_C))), secret)
    keep = {d.endpoint_id: d.id for d in me.pairing.contacts_for(ada.public_hex)}[EP_B]
    contacts.upsert_contact(
        me, card_of(ada, machines=(("Desk", EP_B),), at="2026-10-23T11:00:00+00:00"), secret
    )
    left = me.pairing.contacts_for(ada.public_hex)
    assert [(d.endpoint_id, d.id) for d in left] == [(EP_B, keep)]
    assert contacts.by_endpoint(me, EP_C) is None


def test_an_older_card_is_ignored(me, ada):
    secret = contacts.new_secret()
    contacts.upsert_contact(me, card_of(ada, name="Ada L", at="2026-10-23T12:00:00+00:00"), secret)
    stored = me.repo.contact(ada.public_hex)
    again = contacts.upsert_contact(
        me,
        card_of(ada, name="Old", machines=(("X", EP_C),), at="2026-10-23T09:00:00+00:00"),
        contacts.new_secret(),
    )
    assert again["name"] == "Ada L"
    assert me.repo.contact(ada.public_hex) == stored
    assert [d.endpoint_id for d in me.pairing.contacts_for(ada.public_hex)] == [EP_B]


def test_a_newer_card_updates_the_name_and_keeps_my_name(me, ada):
    secret = contacts.new_secret()
    row = contacts.upsert_contact(me, card_of(ada), secret)
    me.repo.put_contact({**row, "my_name": "Countess"})
    row = contacts.upsert_contact(
        me, card_of(ada, name="Ada Lovelace", at="2026-10-23T11:00:00+00:00"), secret
    )
    assert row["name"] == "Ada Lovelace" and row["my_name"] == "Countess"
    info = contacts.info(me, row)
    assert info.name == "Countess" and info.card_name == "Ada Lovelace"
    assert contacts.principal_of(row) == access.Principal(
        "p:" + ada.public_hex[:16], "Countess", access.MEMBER
    )


def test_refusals(me, ada, tmp_path):
    secret = contacts.new_secret()
    mine = card_of(me.person)
    with pytest.raises(contacts.ContactsRefused, match="own"):
        contacts.upsert_contact(me, mine, secret)
    forged = card_of(ada).model_dump() | {"name": "Mallory"}
    with pytest.raises(contacts.ContactsRefused, match="not valid"):
        contacts.upsert_contact(me, forged, secret)
    with pytest.raises(contacts.ContactsRefused, match="secret"):
        contacts.upsert_contact(me, card_of(ada), "short")
    # A card's person is its own row: another person's card never touches Ada's.
    bob = PersonKeys(tmp_path / "bob")
    contacts.upsert_contact(me, card_of(ada), secret)
    contacts.upsert_contact(me, card_of(bob, name="Bob", machines=(("B", EP_C),)), secret)
    assert {c["name"] for c in me.repo.contacts()} == {"Ada", "Bob"}
    assert [d.endpoint_id for d in me.pairing.contacts_for(ada.public_hex)] == [EP_B]


@pytest.mark.anyio
async def test_forget_revokes_devices_and_keeps_the_row(me, ada, monkeypatch):
    secret = contacts.new_secret()
    row = contacts.upsert_contact(me, card_of(ada), secret)
    me.repo.set_contact_runtime(ada.public_hex, machines={EP_B: {"listen": "127.0.0.1:41000"}})
    hung = []

    async def request(op, **args):
        hung.append((op, args))
        return {}

    monkeypatch.setattr(me.link, "request", request)
    assert await contacts.forget_contact(me, ada.public_hex)
    assert hung == [("hangup", {"peer": EP_B})]
    gone = me.repo.contact(ada.public_hex)
    assert gone["removed_at"] and gone["secret"] == secret and gone["machines"] == {}
    assert me.repo.contacts() == [] and len(me.repo.contacts(include_removed=True)) == 1
    assert me.pairing.contacts_for(ada.public_hex) == []
    _, presented = contacts.tokens(secret, me.person.public_hex, ada.public_hex)
    assert me.pairing.verify(presented) is None
    assert contacts.by_endpoint(me, EP_B) is None
    assert not await contacts.forget_contact(me, "9" * 64)
    assert row  # pairing again restores it
    restored = contacts.upsert_contact(me, card_of(ada, at="2026-10-23T13:00:00+00:00"), secret)
    assert restored["removed_at"] == "" and me.repo.contacts()[0]["person"] == ada.public_hex
    assert me.pairing.verify(presented).person == ada.public_hex


@pytest.mark.anyio
async def test_forget_without_a_link_still_works(me, ada):
    contacts.upsert_contact(me, card_of(ada), contacts.new_secret())
    me.repo.set_contact_runtime(ada.public_hex, machines={EP_B: {"listen": "127.0.0.1:41000"}})
    me.link.status.running = False  # link.request raises LinkError
    assert await contacts.forget_contact(me, ada.public_hex)
    assert me.repo.contact(ada.public_hex)["removed_at"]


def test_my_principal(me):
    p = contacts.my_principal(me)
    assert p == access.Principal(me.person.pid, "Corey", access.MEMBER)


# ---- merge ----------------------------------------------------------------------------------


def _row(updated="2026-10-23T10:00:00+00:00", removed="", my_name="", name="Ada", secret="s" * 64):
    return {
        "person": "a" * 64,
        "name": name,
        "my_name": my_name,
        "card": {"name": name, "machines": [], "at": updated},
        "secret": secret,
        "machines": {},
        "cursors": {},
        "created_at": "2026-10-01T00:00:00+00:00",
        "updated_at": updated,
        "removed_at": removed,
    }


def test_merge_rows():
    old, new = (
        _row(),
        _row(updated="2026-10-23T11:00:00+00:00", my_name="Countess", secret="t" * 64),
    )
    assert contacts.merge_rows(None, new)["secret"] == "t" * 64
    got = contacts.merge_rows(old, new)
    assert got["my_name"] == "Countess" and got["secret"] == "t" * 64
    assert contacts.merge_rows(new, old)["my_name"] == "Countess"  # newer wins either way
    # Same time: the removed one wins, whichever side it is on.
    gone = _row(removed="2026-10-23T10:00:00+00:00")
    assert contacts.merge_rows(old, gone)["removed_at"]
    assert contacts.merge_rows(gone, old)["removed_at"]
    # A newer row beats an older removal (paired again).
    assert contacts.merge_rows(gone, new)["removed_at"] == ""
    assert contacts.merge_rows(new, gone)["removed_at"] == ""
    # Runtime state stays ours; other people's rows never merge.
    ours = _row() | {"machines": {"e": {"listen": "x"}}, "cursors": {"e": 3}}
    merged = contacts.merge_rows(ours, new)
    assert merged["machines"] == {"e": {"listen": "x"}} and merged["cursors"] == {"e": 3}
    assert contacts.merge_rows(_row(), _row() | {"person": "b" * 64}) is None
    assert contacts.merge_rows(None, ours)["machines"] == {}


# ---- team mode ------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_everything_refuses_in_team_mode(me, ada):
    row = contacts.upsert_contact(me, card_of(ada), contacts.new_secret())
    me.settings.team_mode = True
    card = card_of(ada)
    for call in (
        lambda: contacts.build_card(me),
        lambda: contacts.upsert_contact(me, card, contacts.new_secret()),
        lambda: contacts.ensure_devices(me, row),
        lambda: contacts.my_principal(me),
        lambda: contacts.by_endpoint(me, EP_B),
        lambda: contacts.info(me, row),
    ):
        with pytest.raises(contacts.ContactsRefused):
            call()
    with pytest.raises(contacts.ContactsRefused):
        await contacts.forget_contact(me, ada.public_hex)


# ---- storage --------------------------------------------------------------------------------


def test_contacts_do_not_touch_meetings(tmp_path):
    repo = SessionRepository(tmp_path / "db.sqlite")
    before = repo.current_sync_seq()
    repo.put_contact(_row())
    repo.set_contact_runtime("a" * 64, machines={"e": {"listen": "x"}}, cursors={"e": 2})
    repo.remove_contact("a" * 64, "2026-10-24T00:00:00+00:00")
    assert repo.current_sync_seq() == before
    row = repo.contact("a" * 64)
    assert row["machines"] == {"e": {"listen": "x"}} and row["cursors"] == {"e": 2}
    assert row["removed_at"] == row["updated_at"] == "2026-10-24T00:00:00+00:00"
    assert repo.contacts() == [] and repo.contact("f" * 64) is None
    repo.close()


def test_a_v12_database_gains_the_contacts_table(tmp_path):
    path = tmp_path / "db.sqlite"
    repo = SessionRepository(path)
    repo.close()
    raw = sqlite3.connect(path)
    raw.execute("DROP TABLE contacts")
    raw.execute("UPDATE meta SET value='12' WHERE key='schema_version'")
    raw.commit()
    raw.close()
    repo = SessionRepository(path)
    repo.put_contact(_row())
    assert repo.contact("a" * 64)["name"] == "Ada"
    assert SCHEMA_VERSION == 14
    repo.close()


# ---- the device file ------------------------------------------------------------------------


def test_old_device_files_load_and_contacts_persist(tmp_path):
    path = tmp_path / "paired_devices.json"
    old = {
        "id": "d1",
        "name": "Phone",
        "token_hash": sha("tok"),
        "created_at": "2026-10-01T00:00:00+00:00",
        "last_seen_at": None,
        "kind": "phone",
        "endpoint_id": None,
    }
    path.write_text(json.dumps({"devices": [old]}))
    svc = pairing.PairingService(path)
    assert svc.verify("tok").person is None
    svc.set_contact_devices("p" * 64, [EP_B, EP_C], sha("shared"), "Ada")
    again = pairing.PairingService(path)
    assert again.verify("tok").kind == "phone"
    assert again.verify("shared").kind == pairing.CONTACT
    assert sorted(d.endpoint_id for d in again.contacts_for("p" * 64)) == [EP_B, EP_C]
    assert again.revoke_contact("p" * 64) == 2 and again.verify("shared") is None
    assert again.verify("tok") is not None
