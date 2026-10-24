"""Contacts: other people's keys and the cards that say how to reach their computers
(docs/plans/2026-10-23-phase-8-tasks.md, C1, C2, C4).

A card is `{person, name, machines: [{name, ticket}], at, sig}`, signed by the person key
(services/person.py). Two people who pair share one random 32-byte `secret`; the token X
presents to Y is HMAC-SHA256(secret, "mnemosyne/share/to:" + Y's key), so every machine of a
person derives the same tokens and writes one CONTACT device (services/pairing.py) per endpoint
on the contact's card, holding the hash of the token the contact presents. Contacts are global
to the library (the `contacts` table, not meeting data) and travel in library sync, which is
why a removed one keeps its row (`removed_at`) and `merge_rows` exists. Not for team servers.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from .. import access
from ..models.contact import Card, CardMachine, ContactInfo
from .link import LinkError, endpoint_id_from_ticket
from .pairing import CONTACT
from .person import PersonKeys

if TYPE_CHECKING:
    from ..api.context import AppContext

logger = logging.getLogger(__name__)

_HEX64 = re.compile(r"[0-9a-f]{64}")
TOKEN_CONTEXT = b"mnemosyne/share/to:"


class ContactsRefused(Exception):
    """Contacts are not available here; the message says why, for the person."""


class ContactsUnavailable(ContactsRefused):
    """Something needed is not running yet (remote access)."""


def _check(app: AppContext) -> None:
    if app.settings.team_mode:
        raise ContactsRefused("Contacts are for personal computers, not a team server")


def require_available(app: AppContext) -> None:
    """Raise ContactsRefused on a team server (for routes that only list)."""
    _check(app)


def check_can_pair(app: AppContext) -> None:
    """Pairing needs an API token, as sync's does: without one auth is off, and the backend
    could not tell a contact's tunnelled requests from this computer's own."""
    _check(app)
    if not app.settings.api_token:
        raise ContactsUnavailable(
            "Set an API token first: without one, anyone who reaches the backend can use it"
        )


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ---- cards ----------------------------------------------------------------------------------


def _unsigned(card: Card) -> dict:
    return card.model_dump(exclude={"sig"})


def _my_name(app: AppContext) -> str:
    """What this person is called to others: the name their own lines carry in transcripts,
    unless it is still the default "Me" (then the caller falls back to the computer's name)."""
    name = (app.settings.local_speaker_name or "").strip()
    return "" if name.lower() == "me" else name


def display_name(app: AppContext) -> str:
    """What others see this person called (the card's name)."""
    return _my_name(app) or app.sync_peers.self_info().name or "Me"


def build_card(app: AppContext) -> Card:
    """This person's card: this computer and every library-sync peer, signed."""
    _check(app)
    status = app.link.status
    if not status.running or not status.ticket:
        raise ContactsUnavailable("Turn on remote access first")
    me = app.sync_peers.self_info()
    machines = [CardMachine(name=me.name, ticket=status.ticket)]
    seen = {status.ticket}
    for peer in app.sync_peers.peers():
        if peer.ticket and peer.ticket not in seen:
            seen.add(peer.ticket)
            machines.append(CardMachine(name=peer.name, ticket=peer.ticket))
    name = display_name(app)
    card = Card(person=app.person.public_hex, name=name, machines=machines, at=now_iso(), sig="")
    sig = app.person.sign(PersonKeys.canonical(_unsigned(card)))
    return card.model_copy(update={"sig": sig})


def _as_card(card: Card | dict) -> Card | None:
    if isinstance(card, Card):
        return card
    try:
        return Card.model_validate(card)
    except Exception:
        return None


def verify_card(card: Card | dict, expected_person: str | None = None) -> bool:
    """Whether the card is well formed and signed by its own `person` key (and, when given, by
    `expected_person`)."""
    parsed = _as_card(card)
    if parsed is None or not _HEX64.fullmatch(parsed.person):
        return False
    if expected_person is not None and parsed.person != expected_person:
        return False
    return PersonKeys.verify(parsed.person, parsed.sig, PersonKeys.canonical(_unsigned(parsed)))


def endpoints(card: Card | dict) -> list[str]:
    """The endpoint ids of the card's machines (tickets that do not parse are skipped)."""
    parsed = _as_card(card)
    found: list[str] = []
    for machine in parsed.machines if parsed else []:
        try:
            endpoint = endpoint_id_from_ticket(machine.ticket)
        except ValueError:
            continue
        if endpoint not in found:
            found.append(endpoint)
    return found


# ---- tokens ---------------------------------------------------------------------------------


def new_secret() -> str:
    return secrets.token_hex(32)


def _token(secret: str, to_pub: str) -> str:
    return hmac.new(
        bytes.fromhex(secret), TOKEN_CONTEXT + to_pub.encode(), hashlib.sha256
    ).hexdigest()


def tokens(secret: str, me_pub: str, them_pub: str) -> tuple[str, str]:
    """(the token I present to them, the token they present to me). Bearer strings like any
    device token: the auth middleware compares their SHA-256 with the stored hash."""
    return _token(secret, them_pub), _token(secret, me_pub)


# ---- rows -----------------------------------------------------------------------------------


def upsert_contact(app: AppContext, card: Card | dict, secret: str) -> dict:
    """Store (or refresh) a contact from a signed card and write its CONTACT devices. Refuses a
    card that does not verify, one for our own key, or a malformed secret; an older card than
    the stored one changes nothing. Pairing a removed contact again restores it."""
    _check(app)
    parsed = _as_card(card)
    if parsed is None or not verify_card(parsed):
        raise ContactsRefused("That contact card is not valid")
    if parsed.person == app.person.public_hex:
        raise ContactsRefused("That card is your own: pair with someone else")
    try:
        bytes.fromhex(secret)
        if len(secret) != 64:
            raise ValueError
    except ValueError as e:
        raise ContactsRefused("The shared secret is not valid") from e
    held = app.repo.contact(parsed.person)
    now = now_iso()
    if held is None:
        row = {
            "person": parsed.person,
            "name": parsed.name,
            "my_name": "",
            "card": parsed.model_dump(),
            "secret": secret,
            "machines": {},
            "cursors": {},
            "created_at": now,
            "updated_at": now,
            "removed_at": "",
        }
    else:
        if parsed.at < held["card"].get("at", "") and not held["removed_at"]:
            return held  # an older card than the one we have
        row = {
            **held,
            "name": parsed.name,
            "card": parsed.model_dump(),
            "secret": secret,
            "updated_at": now,
            "removed_at": "",
        }
        if held["removed_at"]:
            row["created_at"] = now
    app.repo.put_contact(row)
    ensure_devices(app, row)
    return row


def ensure_devices(app: AppContext, row: dict) -> None:
    """One CONTACT device per endpoint on the contact's card; none for a removed contact."""
    _check(app)
    if row.get("removed_at"):
        app.pairing.revoke_contact(row["person"])
        return
    _, theirs = tokens(row["secret"], app.person.public_hex, row["person"])
    digest = hashlib.sha256(theirs.encode()).hexdigest()
    app.pairing.set_contact_devices(
        row["person"], endpoints(row["card"]), digest, my_or_card_name(row)
    )


def my_or_card_name(row: dict) -> str:
    return (row.get("my_name") or row.get("name") or "").strip()


async def drop_contact_access(app: AppContext, row: dict) -> None:
    """Close everything a contact had on this computer: its CONTACT devices are revoked and
    each machine dialed for it is hung up. The row itself is left to the caller (`forget_contact`
    stamps it removed; library sync takes the stamp from the other computer)."""
    app.pairing.revoke_contact(row["person"])
    for endpoint, state in (row.get("machines") or {}).items():
        if isinstance(state, dict) and state.get("listen"):
            try:
                await app.link.request("hangup", peer=endpoint)
            except LinkError as e:  # not running: nothing is dialed
                logger.info("Hang up on contact %s: %s", row["name"], e)
    app.repo.set_contact_runtime(row["person"], machines={})
    forget_their_meetings(app, row, f"you removed {my_or_card_name(row) or 'this contact'}")


def forget_their_meetings(app: AppContext, row: dict, why: str) -> int:
    """The recipient side of a removal: delete at once every meeting this contact shared with
    us (through the normal delete path, as this computer's own work; no tombstone is made for
    the copies) and drop what was waiting in the outbox for them (their meetings are gone, and
    they would not take it). What we sent them earlier stays with them. Never raises."""
    deleted = 0
    try:
        with access.internally():
            for sid in app.repo.shared_from_ids(row["person"]):
                if app.sessions.delete_session(sid):
                    deleted += 1
                    logger.info("Shared meeting %s removed: %s", sid, why)
        app.repo.outbox_drop_person(row["person"])
    except Exception:  # noqa: BLE001 - the contact is removed all the same
        logger.warning("Could not delete the meetings of %s", row.get("name"), exc_info=True)
    return deleted


async def revoke_shares_with(app: AppContext, person: str) -> int:
    """The owner side of a removal: revoke every live share this person has, so each becomes a
    tombstone, then (once, never failing the removal) ask the person's computers to pull them
    now: after the removal their token stops working and they could never fetch them. Without
    that, they learn of it from the 401s (share_pull.GONE_AFTER). Returns the shares revoked."""
    revoked = 0
    for share in app.repo.shares_for_person(person):
        if app.repo.revoke_peer_share(share["session_id"], person):
            revoked += 1
    if revoked and app.share_announce is not None:
        await app.share_announce.notify_removal(person)
    return revoked


async def forget_contact(app: AppContext, person: str) -> bool:
    """Remove a contact: their shares are revoked and they are asked to pull that, the row stays
    (removed_at) so the removal can travel, its devices are revoked, any machine dialed for it
    is hung up and the meetings they shared with us are deleted. False when there is no such
    contact."""
    _check(app)
    row = app.repo.contact(person)
    if row is None:
        return False
    if not row["removed_at"]:
        try:
            await revoke_shares_with(app, person)
        except Exception:  # noqa: BLE001 - never fails the removal
            logger.warning("Could not revoke the shares with %s", row.get("name"), exc_info=True)
        app.repo.remove_contact(person, now_iso())
    await drop_contact_access(app, row)
    return True


async def rename_contact(app: AppContext, person: str, name: str) -> dict | None:
    """Call a contact what you like (`my_name`; empty goes back to the name on their card). The
    change travels in library sync like any contact change. None when there is no such contact."""
    _check(app)
    row = app.repo.contact(person)
    if row is None or row["removed_at"]:
        return None
    row = {**row, "my_name": name.strip()[:80], "updated_at": now_iso()}
    app.repo.put_contact(row)
    ensure_devices(app, row)
    return row


# ---- pairing --------------------------------------------------------------------------------


def split_invite(invite: str) -> tuple[str, str]:
    ticket, _, code = invite.strip().rpartition("#")
    if not ticket or not code:
        raise ContactsRefused("That is not a contact invite (expected <ticket>#<code>)")
    return ticket, code


async def pair_with_invite(app: AppContext, invite: str) -> dict:
    """This computer is B: redeem the contact invite A's person showed. B picks the shared
    secret and sends its signed card with it through the sidecar; A checks the card against the
    endpoint that is calling, stores it and answers with its own card and the token B will
    present to A. Both sides then hold a contact row and a CONTACT device per endpoint of the
    other's card, and each dials the other's machines. Nothing is stored here unless the whole
    reply checks out; a failure after that removes the contact again. Returns the contact row."""
    check_can_pair(app)
    ticket, _ = split_invite(invite)
    try:
        invite_endpoint = endpoint_id_from_ticket(ticket)
    except ValueError as e:
        raise ContactsRefused("That is not a contact invite (expected <ticket>#<code>)") from e
    if invite_endpoint == app.link.status.endpoint_id:
        raise ContactsRefused("That invite is from this computer: show it on the other one")
    mine = build_card(app)  # ContactsUnavailable while remote access is off
    secret = new_secret()
    try:
        reply = await app.link.request(
            "pair",
            invite=invite.strip(),
            name=mine.name,
            card={"card": mine.model_dump(), "secret": secret},
        )
    except LinkError as e:
        raise ContactsRefused(f"Pairing failed: {e}") from e
    their = _as_card(reply.get("card") or {})
    if their is None or not verify_card(their):
        raise ContactsRefused("The other computer sent no valid contact card: is it up to date?")
    if their.person == app.person.public_hex:
        raise ContactsRefused("That invite is yours: pair with someone else")
    if invite_endpoint not in endpoints(their):
        raise ContactsRefused(
            "That contact card does not list the computer you pasted the invite of"
        )
    presented, _ = tokens(secret, app.person.public_hex, their.person)
    if not hmac.compare_digest(str(reply.get("token") or ""), presented):
        raise ContactsRefused("The other computer did not accept the shared secret")
    row = upsert_contact(app, their, secret)
    try:
        await dial_all(app, row)
    except Exception:
        await forget_contact(app, row["person"])
        raise
    logger.info("Paired with contact %s", my_or_card_name(row))
    return app.repo.contact(row["person"]) or row


async def dial_all(app: AppContext, row: dict) -> dict:
    """Have the sidecar serve each of the contact's machines on a loopback address and keep it
    as `machines[endpoint_id] = {"listen": ...}` (replacing what was kept: listeners are new
    after a restart). A machine that cannot be dialed is logged and left out. Returns the
    machines dialed."""
    _check(app)
    if row.get("removed_at"):
        return {}
    own = app.link.status.endpoint_id
    dialed: dict[str, dict] = {}
    for machine in row["card"].get("machines", []):
        try:
            endpoint = endpoint_id_from_ticket(machine["ticket"])
        except (ValueError, KeyError):
            continue
        if endpoint == own or endpoint in dialed:
            continue
        try:
            reply = await app.link.request("dial", ticket=machine["ticket"])
        except LinkError as e:
            logger.warning("Could not dial %s's %s yet: %s", row["name"], machine.get("name"), e)
            continue
        if reply.get("listen"):
            dialed[endpoint] = {"listen": str(reply["listen"])}
    app.repo.set_contact_runtime(row["person"], machines=dialed)
    return dialed


async def dial_contacts(app: AppContext) -> None:
    """Dial every live contact's machines again (the sidecar just started). Never raises."""
    try:
        _check(app)
        rows = app.repo.contacts()
    except Exception:  # noqa: BLE001 - a team server, or a locked database
        return
    for row in rows:
        try:
            await dial_all(app, row)
        except Exception as e:  # noqa: BLE001 - one contact must not stop the others
            logger.warning("Could not dial contact %s: %s", row.get("name"), e)


# ---- who is who -----------------------------------------------------------------------------


def pid_of(person: str) -> str:
    return "p:" + person[:16]


def principal_of(row: dict) -> access.Principal:
    """The contact as a team-mode member, so the hub applies the usual rules to what they send."""
    return access.Principal(
        user_id=pid_of(row["person"]), name=my_or_card_name(row), role=access.MEMBER
    )


def my_principal(app: AppContext) -> access.Principal:
    _check(app)
    name = _my_name(app) or app.sync_peers.self_info().name
    return access.Principal(user_id=app.person.pid, name=name, role=access.MEMBER)


def by_endpoint(app: AppContext, endpoint_id: str) -> dict | None:
    """The live contact a CONTACT device of this endpoint belongs to."""
    _check(app)
    for device in app.pairing.devices():
        if device.kind == CONTACT and device.endpoint_id == endpoint_id and device.person:
            row = app.repo.contact(device.person)
            if row is not None and not row["removed_at"]:
                return row
    return None


def by_pid(app: AppContext, pid: str) -> dict | None:
    """The live contact whose `p:<16 hex>` id this is (what a CONTACT request runs as)."""
    _check(app)
    for row in app.repo.contacts():
        if pid_of(row["person"]) == pid:
            return row
    return None


def info(app: AppContext, row: dict) -> ContactInfo:
    _check(app)
    person = row["person"]
    return ContactInfo(
        person=person,
        pid=pid_of(person),
        name=my_or_card_name(row),
        card_name=row["card"].get("name", row["name"]),
        fingerprint=_fingerprint(person),
        machines=[m.get("name", "") for m in row["card"].get("machines", [])],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _fingerprint(person: str) -> str:
    """The same words PersonKeys.fingerprint gives for this key."""
    import base64

    digest = hashlib.sha256(bytes.fromhex(person)).digest()
    text = base64.b32encode(digest).decode().rstrip("=")[:32]
    return " ".join(text[i : i + 4] for i in range(0, 32, 4))


# ---- library sync ---------------------------------------------------------------------------


def merge_rows(ours: dict | None, theirs: dict) -> dict | None:
    """The contact row two computers agree on (pure). The newer `updated_at` wins; at the same
    time a removed row wins; the card and secret travel with the winner. `my_name` comes from the
    newer row too. Runtime state (machines, cursors) is each computer's own and stays ours."""
    if ours is None:
        return {**theirs, "machines": {}, "cursors": {}}
    if ours["person"] != theirs["person"]:
        return None
    a, b = ours["updated_at"], theirs["updated_at"]
    if a == b:
        winner = theirs if theirs.get("removed_at") and not ours.get("removed_at") else ours
    else:
        winner = theirs if b > a else ours
    return {
        **winner,
        "machines": ours.get("machines") or {},
        "cursors": ours.get("cursors") or {},
        "created_at": min(ours["created_at"], theirs["created_at"]),
    }


def export_rows(app: AppContext) -> list[dict]:
    """Every contact row, removed ones too, for another computer of this person (library sync,
    GET /api/sync/contacts). Complete: card and secret included, because those computers are one
    person's. Only the runtime state (machines, cursors) is left out: it is each computer's own."""
    return [
        {k: v for k, v in row.items() if k not in ("machines", "cursors")}
        for row in app.repo.contacts(include_removed=True)
    ]


def _travelling(row: dict | None) -> dict | None:
    return (
        None if row is None else {k: v for k, v in row.items() if k not in ("machines", "cursors")}
    )


async def apply_peer_rows(app: AppContext, rows: list[dict]) -> list[dict]:
    """Merge the contact rows of another computer of this person into ours (`merge_rows`). A row
    that changed ours is stored; a live one gets its CONTACT devices (`ensure_devices`), one that
    became removed is closed the way `forget_contact` closes it, without stamping it again.
    Malformed rows are skipped, our own key never becomes a contact. Returns the live rows that
    changed, for the caller to dial. Never raises for one bad row."""
    _check(app)
    changed: list[dict] = []
    for theirs in rows:
        try:
            if not isinstance(theirs, dict) or not _HEX64.fullmatch(str(theirs.get("person", ""))):
                continue
            if theirs["person"] == app.person.public_hex:
                continue
            for key in ("name", "card", "secret", "created_at", "updated_at"):
                if key not in theirs:
                    raise KeyError(key)
            ours = app.repo.contact(theirs["person"])
            merged = merge_rows(ours, theirs)
            if merged is None or _travelling(merged) == _travelling(ours):
                continue
            app.repo.put_contact(merged)
            if merged.get("removed_at"):
                if ours is not None:
                    await drop_contact_access(app, ours)
                else:
                    app.pairing.revoke_contact(merged["person"])
            else:
                ensure_devices(app, merged)
                changed.append(merged)
        except Exception as e:  # noqa: BLE001 - one bad row must not stop the others
            logger.warning("Could not take in a contact from a synced computer: %s", e)
    return changed


async def dial_in_background(app: AppContext, rows: list[dict]) -> None:
    """Dial the machines of contacts library sync just brought; never raises."""
    for row in rows:
        try:
            await dial_all(app, row)
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not dial contact %s: %s", row.get("name"), e)


# ---- the person key and library sync ---------------------------------------------------------


def has_people_data(app: AppContext) -> bool:
    """Whether this library has anything tied to the current person key that another computer
    would lose by taking another key: live contacts. A later task adds shares to peers."""
    try:
        return bool(app.repo.contacts())
    except Exception:  # noqa: BLE001 - e.g. a locked database: say "no data", the caller asks again
        return False


def adoption(mine: str, mine_has: bool, theirs: str, theirs_has: bool) -> str | None:
    """Who adopts whose person key when two computers pair for library sync, from the point of
    view of the inviter (A = "mine", the one that showed the code) and the redeemer (B = "yours").
    "same": the keys are equal. "mine": B adopts A's key (A has people data, or neither has: the
    inviter's wins). "yours": A adopts B's (only B has people data). None: both have people data
    and different keys, so they are different people."""
    if mine == theirs:
        return "same"
    if mine_has and theirs_has:
        return None
    return "yours" if theirs_has else "mine"


def on_key_changed(app: AppContext) -> None:
    """This computer took another person key (library sync pairing). Cards are built on demand and
    contact tokens derive from both keys; the side that changes has no people data (see
    `adoption`), so nothing else needs redoing."""
    logger.info("This computer now uses the person key %s", app.person.pid)
