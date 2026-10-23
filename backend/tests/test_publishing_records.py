"""Publishing and the team server's rules and the meeting's record (phase 7 P8): readers see that
a meeting is published and not the link, admins publish any meeting, each publish / update /
unpublish is sealed and logged (also under a legal hold), and the records export lists the
publications without their keys."""

import io
import json
import time
import zipfile

from mnemosyne import access
from mnemosyne.services import publication_format as pf
from mnemosyne.services import records, sharing
from tests.test_publishing import (  # noqa: F401
    _meeting,
    _publish,
    fake_cut,
    team,
)


def _wait_for_seals(ctx, sid, count, timeout=10):
    """Publishing seals in the background (records.seal_later): wait for `count` seals."""
    deadline = time.monotonic() + timeout
    while len(ctx.repo.seals(sid)) < count:
        assert time.monotonic() < deadline, ctx.repo.seals(sid)
        time.sleep(0.05)
    return ctx.repo.seals(sid)


# ---- the team server -------------------------------------------------------------------------


def test_readers_see_that_it_is_published_but_not_the_link(team):  # noqa: F811
    client, ctx, people = team
    ann, ann_auth = people["Ann"]
    bea = ctx.users.add("Bea", "bea@team.test", "member")
    code, _ = ctx.users.invite(bea.id)
    bea_auth = {"Authorization": f"Bearer {ctx.users.redeem(code, 'test')[1]}"}
    ctx.settings.allow_publishing = True
    reset = access.current.set(access.Principal(ann.id, ann.name, ann.role))
    try:
        s = _meeting(ctx)
    finally:
        access.current.reset(reset)

    # Not shared with Bea yet: she does not know the meeting.
    assert client.get(f"/api/sessions/{s.id}/publication", headers=bea_auth).status_code == 404
    pub = _publish(client, s.id, ann_auth)
    sharing.set_shares(ctx, s.id, {bea.id}, ann.name)

    for who in (bea_auth, people["Rex"][1]):  # a member it is shared with, a reviewer
        seen = client.get(f"/api/sessions/{s.id}/publication", headers=who).json()
        assert seen["id"] == pub["id"] and seen["version"] == 1 and seen["url"] is None
        assert seen["served_by"] is None
        listed = client.get("/api/publications", headers=who).json()
        assert [i["publication"]["id"] for i in listed] == [pub["id"]]
        assert [i["publication"]["url"] for i in listed] == [None]
        assert (
            client.put(f"/api/sessions/{s.id}/publication", json={}, headers=who).status_code == 403
        )
        assert client.delete(f"/api/sessions/{s.id}/publication", headers=who).status_code == 403
    # The owner and an admin see the link.
    for who in (ann_auth, people["Admin"][1]):
        assert (
            client.get(f"/api/sessions/{s.id}/publication", headers=who).json()["url"] == pub["url"]
        )


def test_an_admin_publishes_and_unpublishes_any_meeting(team, fake_cut):  # noqa: F811
    client, ctx, people = team
    ann, ann_auth = people["Ann"]
    admin_auth = people["Admin"][1]
    reset = access.current.set(access.Principal(ann.id, ann.name, ann.role))
    try:
        s = _meeting(ctx)
    finally:
        access.current.reset(reset)
    ctx.settings.allow_publishing = False
    # Off: nobody, the admin included, until the admin turns it on.
    r = client.put(f"/api/sessions/{s.id}/publication", json={}, headers=admin_auth)
    assert r.status_code == 403

    ctx.settings.allow_publishing = True
    pub = _publish(client, s.id, admin_auth)
    assert pub["published_by"] == "Admin" and pub["url"]
    # The owner sees it, and can update it; another member cannot touch it.
    assert (
        client.get(f"/api/sessions/{s.id}/publication", headers=ann_auth).json()["id"] == pub["id"]
    )
    assert _publish(client, s.id, ann_auth)["version"] == 2
    bo = ctx.users.add("Bo", "bo@team.test", "member")
    code, _ = ctx.users.invite(bo.id)
    bo_auth = {"Authorization": f"Bearer {ctx.users.redeem(code, 'test')[1]}"}
    assert client.delete(f"/api/sessions/{s.id}/publication", headers=bo_auth).status_code in (
        403,
        404,
    )
    assert client.delete(f"/api/sessions/{s.id}/publication", headers=admin_auth).status_code == 200
    by = [(e["kind"], e["detail"].get("by")) for e in ctx.repo.events(s.id)]
    assert ("published", "Admin") in by and ("publication_updated", "Ann") in by
    assert ("unpublished", "Admin") in by


# ---- the record ------------------------------------------------------------------------------


def test_each_change_is_sealed_with_a_readable_reason_also_under_a_legal_hold(client, ctx):
    s = _meeting(ctx)
    ctx.repo.set_legal_hold(s.id, "case 42")
    assert ctx.repo.get(s.id).legal_hold == "case 42"
    pub = _publish(client, s.id)
    seals = _wait_for_seals(ctx, s.id, 1)
    _publish(client, s.id, summary=False)
    seals = _wait_for_seals(ctx, s.id, 2)
    assert client.delete(f"/api/sessions/{s.id}/publication").status_code == 200
    seals = _wait_for_seals(ctx, s.id, 3)
    # The content never changed, and still each event is a link in the chain.
    assert [x["reason"] for x in seals] == ["published", "publication updated", "unpublished"]
    assert len({x["content_hash"] for x in seals}) == 1
    check = records.verify(ctx, s.id)
    assert check.ok and check.seals == 3
    kinds = [e["kind"] for e in ctx.repo.events(s.id)]
    assert (
        kinds.count("published") == 1 and "publication_updated" in kinds and "unpublished" in kinds
    )
    # The hold still stops deletion; publishing is not one.
    assert pub["version"] == 1
    assert client.delete(f"/api/sessions/{s.id}").status_code in (403, 409)
    assert ctx.repo.get(s.id) is not None


def test_the_export_lists_publications_never_their_keys(client, ctx, tmp_path):
    s = _meeting(ctx)
    other = _meeting(ctx)  # never published
    pub = _publish(client, s.id, attendees=["Ana Lopez"], expires_days=30)
    _publish(client, s.id, attendees=["Ana Lopez", "Bo Chen"], expires_days=30)
    assert client.delete(f"/api/sessions/{s.id}/publication").status_code == 200
    again = _publish(client, s.id)  # a second publication, live
    row = ctx.repo.live_publication(s.id)
    dead = ctx.repo.get_publication(pub["id"])
    assert dead["unpublished_at"] and again["id"] != pub["id"]

    dest = tmp_path / "export.zip"
    records.export_zip(ctx, records.ExportRequest(session_ids=[s.id, other.id]), dest)
    z = zipfile.ZipFile(io.BytesIO(dest.read_bytes()))
    files = [n for n in z.namelist() if n.endswith("publications.json")]
    assert len(files) == 1 and f"({s.id})" in files[0]  # only the meeting that has any
    listing = json.loads(z.read(files[0]))
    by_id = {p["id"]: p for p in listing}
    assert set(by_id) == {pub["id"], again["id"]}

    first = by_id[pub["id"]]
    assert first["where"] == "local" and first["host"] == "" and first["current_version"] == 2
    assert [(v["version"], v["by"]) for v in first["versions"]] == [(1, ""), (2, "")]
    assert all(v["at"] and "summary" in v["parts"] for v in first["versions"])
    assert first["selection"]["attendees"] == ["Ana Lopez", "Bo Chen"]
    assert first["expires_at"] and first["unpublished_at"]
    live = by_id[again["id"]]
    assert live["unpublished_at"] is None and live["versions"][0]["version"] == 1
    assert live["unpublished_by"] == ""

    # No key or token anywhere in the export (the SHA256SUMS cover it, so the check is whole).
    everything = b"".join(z.read(n) for n in z.namelist())
    for r in (dead, row):
        assert pf.encode_key(r["key"]).encode() not in everything
        assert r["key"].hex().encode() not in everything
        assert r["token"].encode() not in everything
    assert b"publications.json" in z.read("SHA256SUMS")
    assert "publications.json" in z.read("README.txt").decode()
    assert not any(n.endswith("publications.json") and f"({other.id})" in n for n in z.namelist())


def test_publishing_still_works_without_a_record_to_seal(client, ctx):
    """A meeting with neither transcript nor summary has nothing to seal: no seal, no error."""
    from mnemosyne.models.session import Session

    s = ctx.repo.save(Session(name="Empty"))
    _publish(client, s.id)
    time.sleep(0.2)
    assert ctx.repo.seals(s.id) == []
