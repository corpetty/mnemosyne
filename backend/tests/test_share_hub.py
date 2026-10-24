"""The hub side of sharing (api/routes/share.py) and the owner's share management
(api/routes/peer_shares.py): a contact's token reads only what is shared with it."""

import base64
from datetime import datetime

import pytest

from mnemosyne.models.contact import Card
from mnemosyne.models.session import Session
from mnemosyne.models.shared_meeting import SharedMeeting, ShareParts
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services import contacts, shares
from mnemosyne.services.person import PersonKeys

EP_ADA, EP_BOB, EP_ADA2 = "a1" * 32, "b2" * 32, "c3" * 32


def ticket(endpoint_id: str) -> str:
    raw = b"\x00" + bytes.fromhex(endpoint_id) + b"\x00"
    return "endpoint" + base64.b32encode(raw).decode().lower().rstrip("=")


def card_of(keys: PersonKeys, name, endpoints, at="2026-10-23T10:00:00+00:00") -> Card:
    body = {
        "person": keys.public_hex,
        "name": name,
        "machines": [{"name": f"M{i}", "ticket": ticket(e)} for i, e in enumerate(endpoints)],
        "at": at,
    }
    return Card(**body, sig=keys.sign(PersonKeys.canonical(body)))


class Contact:
    def __init__(self, ctx, tmp_path, name, endpoint):
        self.keys = PersonKeys(tmp_path / name)
        self.person = self.keys.public_hex
        self.secret = contacts.new_secret()
        self.row = contacts.upsert_contact(ctx, card_of(self.keys, name, [endpoint]), self.secret)
        # The token this contact presents to us.
        _, token = contacts.tokens(self.secret, ctx.person.public_hex, self.person)
        self.headers = {"Authorization": f"Bearer {token}"}


ADMIN = {"Authorization": "Bearer s3cret"}


@pytest.fixture
def hub(ctx, client, tmp_path):
    ctx.settings.api_token = "s3cret"
    ctx.settings.local_speaker_name = "Corey"
    ctx.ada = Contact(ctx, tmp_path, "Ada", EP_ADA)
    ctx.bob = Contact(ctx, tmp_path, "Bob", EP_BOB)
    return ctx


def meeting(ctx, name="Release sync") -> str:
    s = Session(
        name=name,
        created_at=datetime(2026, 10, 20, 10, 0),
        summary="We ship on Friday.",
        notes="Scratch.",
        transcript=[TranscriptSegment(text="We ship on Friday.", speaker="Ana", start=0, end=4)],
    )
    return ctx.repo.save(s).id


def share(ctx, sid, who, **parts):
    ctx.repo.put_peer_share(sid, who.person, parts, by="Corey")


def pull(client, who, **params):
    return client.get("/api/share/changes", params=params, headers=who.headers)


# ---- /api/share/hello and auth ---------------------------------------------------------------


def test_hello_names_this_person(hub, client):
    r = client.get("/api/share/hello", headers=hub.ada.headers)
    assert r.status_code == 200
    assert r.json() == {
        "protocol": 1,
        "app_version": client.app.version,
        "person": hub.person.public_hex,
        "name": "Corey",
    }


def test_only_a_contact_may_use_the_hub(hub, client):
    for path in ("/api/share/hello", "/api/share/changes"):
        assert client.get(path, headers=ADMIN).status_code == 403  # the admin token is no contact
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post("/api/share/poke", headers=ADMIN).status_code == 403


def test_the_hub_is_closed_in_team_mode(hub, client):
    hub.settings.team_mode = True
    for r in (
        client.get("/api/share/hello", headers=ADMIN),
        client.get("/api/share/changes", headers=ADMIN),
        client.post("/api/share/poke", headers=ADMIN),
        client.post("/api/share/card", json={}, headers=ADMIN),
    ):
        assert r.status_code == 409
    # And a contact's own token no longer opens anything.
    assert client.get("/api/share/hello", headers=hub.ada.headers).status_code == 401


def test_poke_is_accepted(hub, client):
    r = client.post("/api/share/poke", headers=hub.ada.headers)
    assert r.status_code == 204 and r.content == b""


# ---- /api/share/changes ----------------------------------------------------------------------


def test_a_contact_gets_exactly_its_shared_meetings(hub, client):
    mine, theirs, private = meeting(hub, "For Ada"), meeting(hub, "For Bob"), meeting(hub, "Mine")
    share(hub, mine, hub.ada)
    share(hub, theirs, hub.bob)
    r = pull(client, hub.ada)
    assert r.status_code == 200
    body = r.json()
    assert [s["id"] for s in body["sessions"]] == [mine]
    assert body["more"] is False and body["tombstones"] == []
    assert private not in r.text and theirs not in r.text
    sm = SharedMeeting.model_validate(body["sessions"][0])
    assert sm.owner.person == hub.person.public_hex and sm.owner.name == "Corey"
    assert shares.verify_shared_meeting(sm, hub.person.public_hex)
    assert [s["id"] for s in pull(client, hub.bob).json()["sessions"]] == [theirs]


def test_the_transcript_travels_only_when_ticked(hub, client):
    a, b = meeting(hub, "A"), meeting(hub, "B")
    share(hub, a, hub.ada)  # defaults: no transcript
    share(hub, b, hub.ada, transcript=True)
    docs = {s["id"]: s for s in pull(client, hub.ada).json()["sessions"]}
    assert docs[a]["transcript"] == [] and "ship on Friday" not in str(docs[a]["transcript"])
    assert [seg["text"] for seg in docs[b]["transcript"]] == ["We ship on Friday."]


def test_changes_page_by_the_cursor(hub, client):
    ids = [meeting(hub, f"M{i}") for i in range(3)]
    for sid in ids:
        share(hub, sid, hub.ada)
    first = pull(client, hub.ada, limit=1).json()
    assert [s["id"] for s in first["sessions"]] == [ids[0]] and first["more"] is True
    second = pull(client, hub.ada, limit=1, since=first["next_since"]).json()
    assert [s["id"] for s in second["sessions"]] == [ids[1]] and second["more"] is True
    third = pull(client, hub.ada, limit=5, since=second["next_since"]).json()
    assert [s["id"] for s in third["sessions"]] == [ids[2]] and third["more"] is False
    done = pull(client, hub.ada, since=third["next_since"]).json()
    assert done["sessions"] == [] and done["next_since"] == third["next_since"]
    # A change to a shared meeting puts it in the feed again.
    share(hub, ids[0], hub.ada, transcript=True)
    again = pull(client, hub.ada, since=third["next_since"]).json()
    assert [s["id"] for s in again["sessions"]] == [ids[0]]
    assert again["sessions"][0]["parts"]["transcript"] is True


def test_the_limit_is_bounded(hub, client):
    assert pull(client, hub.ada, limit=0).status_code == 422
    assert pull(client, hub.ada, limit=101).status_code == 422
    assert pull(client, hub.ada, limit=100).status_code == 200


def test_revoking_and_deleting_come_as_tombstones(hub, client):
    kept, revoked, deleted = (meeting(hub, n) for n in ("kept", "revoked", "deleted"))
    for sid in (kept, revoked, deleted):
        share(hub, sid, hub.ada)
    first = pull(client, hub.ada).json()
    assert len(first["sessions"]) == 3 and first["tombstones"] == []

    assert hub.repo.revoke_peer_share(revoked, hub.ada.person)
    hub.repo.delete(deleted)
    body = pull(client, hub.ada, since=first["next_since"]).json()
    stones = {t["session_id"]: t["reason"] for t in body["tombstones"]}
    assert stones == {revoked: "unshared", deleted: "deleted"}
    assert all(s["id"] == kept for s in body["sessions"])
    assert revoked not in [s["id"] for s in body["sessions"]]
    assert body["tombstones_next"] == max(t["at"] for t in body["tombstones"])
    # Asked from there, nothing is left to forget, and the marker stays.
    after = pull(client, hub.ada, tombstones_since=body["tombstones_next"]).json()
    assert after["tombstones"] == [] and after["tombstones_next"] == body["tombstones_next"]
    # Another contact hears of nothing it never had.
    assert pull(client, hub.bob).json()["tombstones"] == []


def test_a_share_withdrawn_between_listing_and_building_is_skipped(hub, client, monkeypatch):
    a, b = meeting(hub, "A"), meeting(hub, "B")
    share(hub, a, hub.ada)
    share(hub, b, hub.ada)
    real = shares.build_shared_meeting

    def vanishing(app, session_id, person):
        if session_id == a:
            raise shares.ShareNotFound(session_id)
        return real(app, session_id, person)

    monkeypatch.setattr("mnemosyne.api.routes.share.build_shared_meeting", vanishing)
    body = pull(client, hub.ada).json()
    assert [s["id"] for s in body["sessions"]] == [b]


def test_a_removed_contact_is_refused(hub, client):
    import asyncio

    asyncio.run(contacts.forget_contact(hub, hub.ada.person))
    assert client.get("/api/share/hello", headers=hub.ada.headers).status_code == 401


# ---- /api/share/card -------------------------------------------------------------------------


def post_card(client, who, card):
    return client.post("/api/share/card", json=card.model_dump(), headers=who.headers)


def test_a_newer_valid_card_replaces_the_stored_one(hub, client):
    newer = card_of(hub.ada.keys, "Ada L", [EP_ADA, EP_ADA2], at="2026-10-24T10:00:00+00:00")
    assert post_card(client, hub.ada, newer).status_code == 204
    row = hub.repo.contact(hub.ada.person)
    assert row["card"] == newer.model_dump() and row["name"] == "Ada L"
    assert row["secret"] == hub.ada.secret
    assert sorted(d.endpoint_id for d in hub.pairing.contacts_for(hub.ada.person)) == [
        EP_ADA,
        EP_ADA2,
    ]
    # The same token still opens the hub, from the new machine too.
    assert client.get("/api/share/hello", headers=hub.ada.headers).status_code == 200


def test_an_older_or_equal_card_is_refused(hub, client):
    held = hub.repo.contact(hub.ada.person)["card"]
    older = card_of(hub.ada.keys, "Old", [EP_ADA2], at="2026-10-22T10:00:00+00:00")
    assert post_card(client, hub.ada, older).status_code == 400
    same_time = card_of(hub.ada.keys, "Same", [EP_ADA2], at=held["at"])
    assert post_card(client, hub.ada, same_time).status_code == 400
    assert hub.repo.contact(hub.ada.person)["card"] == held


def test_a_foreign_or_forged_card_is_refused(hub, client):
    held = hub.repo.contact(hub.ada.person)["card"]
    at = "2026-10-24T10:00:00+00:00"
    # Bob's own valid card, sent with Ada's token.
    assert post_card(client, hub.ada, card_of(hub.bob.keys, "Bob", [EP_BOB], at)).status_code == 400
    # Ada's card with a machine added after it was signed.
    forged = card_of(hub.ada.keys, "Ada", [EP_ADA], at).model_dump()
    forged["machines"].append({"name": "Evil", "ticket": ticket(EP_ADA2)})
    r = client.post("/api/share/card", json=forged, headers=hub.ada.headers)
    assert r.status_code == 400
    # A card for this person's own key.
    mine = card_of(hub.person, "Corey", [EP_ADA2], at)
    assert post_card(client, hub.ada, mine).status_code == 400
    assert hub.repo.contact(hub.ada.person)["card"] == held
    assert client.post("/api/share/card", json=held, headers=ADMIN).status_code == 403


# ---- peer-shares (the owner) -----------------------------------------------------------------


def url(sid, person=""):
    return f"/api/sessions/{sid}/peer-shares" + (f"/{person}" if person else "")


def kinds(client, sid):
    return [e["kind"] for e in client.get(f"/api/sessions/{sid}/history").json()["events"]]


def test_the_owner_shares_changes_and_stops(hub, client):
    sid = meeting(hub)
    assert client.get(url(sid), headers=ADMIN).json() == []

    r = client.put(url(sid, hub.ada.person), json={"transcript": True}, headers=ADMIN)
    assert r.status_code == 200
    info = r.json()
    assert info["person"] == hub.ada.person and info["name"] == "Ada"
    assert info["parts"]["transcript"] is True and info["parts"]["summary"] is True
    assert pull(client, hub.ada).json()["sessions"][0]["parts"]["transcript"] is True

    # Changing what they have.
    r = client.put(url(sid, hub.ada.person), json={"transcript": False}, headers=ADMIN)
    assert r.json()["parts"]["transcript"] is False and r.json()["at"] == info["at"]
    listed = client.get(url(sid), headers=ADMIN).json()
    assert [(s["person"], s["parts"]["transcript"]) for s in listed] == [(hub.ada.person, False)]

    assert client.delete(url(sid, hub.ada.person), headers=ADMIN).status_code == 204
    assert client.get(url(sid), headers=ADMIN).json() == []
    assert client.delete(url(sid, hub.ada.person), headers=ADMIN).status_code == 404
    stones = pull(client, hub.ada).json()["tombstones"]
    assert [(t["session_id"], t["reason"]) for t in stones] == [(sid, "unshared")]

    ev = client.get(f"/api/sessions/{sid}/history", headers=ADMIN).json()["events"]
    logged = [e for e in ev if e["kind"] in ("shared_with", "share_changed", "unshared")]
    assert [e["kind"] for e in logged] == ["shared_with", "share_changed", "unshared"]
    assert logged[0]["detail"]["name"] == "Ada" and "transcript" in logged[0]["detail"]["parts"]
    assert "transcript" not in logged[1]["detail"]["parts"]

    # Sharing again after stopping is a new share.
    client.put(url(sid, hub.ada.person), json=ShareParts().model_dump(), headers=ADMIN)
    ev = client.get(f"/api/sessions/{sid}/history", headers=ADMIN).json()["events"]
    assert [e["kind"] for e in ev if e["kind"].startswith("share")][-1] == "shared_with"


def test_unknown_missing_or_removed_targets_are_404(hub, client):
    sid = meeting(hub)
    assert client.put(url(sid, "e" * 64), json={}, headers=ADMIN).status_code == 404
    assert client.put(url("nope", hub.ada.person), json={}, headers=ADMIN).status_code == 404
    assert client.get(url("nope"), headers=ADMIN).status_code == 404
    assert client.delete(url(sid, hub.bob.person), headers=ADMIN).status_code == 404
    import asyncio

    asyncio.run(contacts.forget_contact(hub, hub.bob.person))
    assert client.put(url(sid, hub.bob.person), json={}, headers=ADMIN).status_code == 404


def test_a_contact_cannot_manage_shares(hub, client):
    sid = meeting(hub)
    share(hub, sid, hub.ada)
    for call in (
        lambda: client.get(url(sid), headers=hub.ada.headers),
        lambda: client.put(url(sid, hub.bob.person), json={}, headers=hub.ada.headers),
        lambda: client.delete(url(sid, hub.ada.person), headers=hub.ada.headers),
    ):
        assert call().status_code in (401, 403)
    assert [s["person"] for s in hub.repo.peer_shares(sid)] == [hub.ada.person]
    assert client.get(url(sid)).status_code == 401


def test_a_meeting_shared_with_us_cannot_be_shared_onward(hub, client):
    sid = meeting(hub)
    hub.repo._conn.execute("UPDATE sessions SET shared_from=? WHERE id=?", (hub.ada.person, sid))
    hub.repo._conn.commit()
    assert client.put(url(sid, hub.bob.person), json={}, headers=ADMIN).status_code == 403
    assert client.delete(url(sid, hub.bob.person), headers=ADMIN).status_code == 403
    r = client.get(url(sid), headers=ADMIN)
    assert r.status_code == 403 and "Shared by Ada" in r.json()["detail"]
    assert hub.repo.peer_shares(sid) == []


def test_a_reader_of_someone_elses_meeting_cannot_share_it(hub, client):
    from mnemosyne import access

    sid = meeting(hub)
    hub.repo._conn.execute("UPDATE sessions SET owner_id='u-owner' WHERE id=?", (sid,))
    hub.repo._conn.commit()
    token = access.current.set(access.Principal("u-other", "Other", access.REVIEWER))
    try:
        with pytest.raises(access.Forbidden):
            from mnemosyne.api.routes import peer_shares

            peer_shares._owner_check(hub, sid)
    finally:
        access.current.reset(token)


def test_peer_shares_are_closed_in_team_mode(hub, client):
    sid = meeting(hub)
    hub.settings.team_mode = True
    assert client.get(url(sid), headers=ADMIN).status_code == 409
    assert client.put(url(sid, hub.ada.person), json={}, headers=ADMIN).status_code == 409
    assert client.delete(url(sid, hub.ada.person), headers=ADMIN).status_code == 409
