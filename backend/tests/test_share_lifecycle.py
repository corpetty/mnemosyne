# ruff: noqa: F811  (the fixtures imported from test_share_pull are used by name)
"""Ending a share, end to end (task 17): the owner O unshares, deletes, or removes a contact; a
recipient removes O. Three apps in this process (O and the recipients Ann and Ben), talking
through `httpx.ASGITransport` as in test_share_pull.py."""

import pytest

from mnemosyne.models.session import Session
from mnemosyne.services import contacts, share_pull, share_push
from tests.test_share_pull import (  # noqa: F401 - fixtures and helpers
    ENDPOINTS,
    Node,
    befriend,
    meeting,
    share,
    to_app,
)

pytestmark = pytest.mark.anyio

ENDPOINTS.setdefault("Ann", "c3" * 32)
ENDPOINTS.setdefault("Ben", "d4" * 32)


@pytest.fixture
async def trio(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMOSYNE_CONFIG_FILE", str(tmp_path / "config.toml"))
    owner, ann, ben = Node(tmp_path, "Owner"), Node(tmp_path, "Ann"), Node(tmp_path, "Ben")
    for r in (ann, ben):
        befriend(owner, r)
        # The owner has dialed the recipient's machine (to poke it).
        owner.repo.set_contact_runtime(r.person, machines={r.endpoint: {"listen": "their-machine"}})
    yield owner, ann, ben
    for n in (owner, ann, ben):
        await n.puller.stop()
        await n.ctx.jobs.shutdown()
        n.repo.close()


def wire_pokes(owner: Node, *recipients: Node) -> None:
    """The owner's pokes reach the recipients' apps (in tests the announcer has no network)."""
    by_person = {r.person: r for r in recipients}
    real = {r.person: to_app(r.app) for r in recipients}

    def client_for(listen: str, token: str):
        # the announcer knows the machine, not the person: tell them apart by the token
        for person, row in ((p, owner.repo.contact(p)) for p in by_person):
            mine, _ = contacts.tokens(row["secret"], owner.person, person)
            if mine == token:
                return real[person](listen, token)
        raise AssertionError("poke with an unknown token")

    owner.ctx.share_announce.client_for = client_for


async def pull(owner: Node, recipient: Node):
    return await recipient.puller.pull(owner.person)


async def test_unshare_removes_only_that_recipients_copy(trio):
    owner, ann, ben = trio
    sid = meeting(owner)
    share(owner, ann, sid)
    share(owner, ben, sid)
    await pull(owner, ann)
    await pull(owner, ben)
    assert owner.repo.revoke_peer_share(sid, ann.person)
    st = await pull(owner, ann)
    assert st.deleted == 1 and ann.repo.get(sid) is None
    await pull(owner, ben)
    assert ben.repo.get(sid) is not None


async def test_deleting_the_meeting_removes_every_copy(trio):
    owner, ann, ben = trio
    sid = meeting(owner)
    share(owner, ann, sid)
    share(owner, ben, sid)
    await pull(owner, ann)
    await pull(owner, ben)
    assert owner.ctx.sessions.delete_session(sid)
    for r in (ann, ben):
        st = await pull(owner, r)
        assert st.deleted == 1 and r.repo.get(sid) is None


async def test_removing_the_owner_deletes_copies_and_the_outbox_at_once(trio):
    owner, ann, _ = trio
    sid, other = meeting(owner), meeting(owner, "Other")
    mine = ann.repo.save(Session(name="Mine")).id
    share(owner, ann, sid)
    share(owner, ann, other)
    await pull(owner, ann)
    # A comment sent and accepted earlier, one still waiting.
    async with ann.admin() as c:
        r1 = await c.post(f"/api/sessions/{sid}/comments", json={"text": "Sent"})
    await share_push.push(ann.ctx, ann.repo.contact(owner.person))
    async with ann.admin() as c:
        await c.post(f"/api/sessions/{sid}/comments", json={"text": "Waiting"})
    assert ann.repo.outbox_pending(owner.person)
    assert [c.text for c in owner.repo.list_comments(sid)] == ["Sent"] and r1.status_code == 200

    assert await contacts.forget_contact(ann.ctx, owner.person)
    assert ann.repo.get(sid) is None and ann.repo.get(other) is None
    assert ann.repo.get(mine) is not None  # only what they shared
    assert ann.repo.outbox_pending(owner.person) == []
    assert [c.text for c in owner.repo.list_comments(sid)] == ["Sent"]


async def test_removing_a_contact_tells_it_first_and_revokes_the_shares(trio):
    owner, ann, ben = trio
    sid = meeting(owner)
    share(owner, ann, sid)
    share(owner, ben, sid)
    await pull(owner, ann)
    await pull(owner, ben)
    wire_pokes(owner, ann, ben)
    assert await contacts.forget_contact(owner.ctx, ann.person)
    # Ann's token no longer works, yet she has already pulled the tombstone.
    assert owner.repo.shares_for_person(ann.person) == []
    assert ann.repo.get(sid) is None
    assert ben.repo.get(sid) is not None and owner.repo.shares_for_person(ben.person)
    st = await pull(owner, ann)
    assert st.state == "error"  # she can no longer pull: the 401 rule takes over


async def test_a_removal_that_could_not_poke_ends_with_gone_after_three_401s(trio):
    owner, ann, ben = trio
    sid = meeting(owner)
    share(owner, ann, sid)
    await pull(owner, ann)
    assert await contacts.forget_contact(owner.ctx, ann.person)  # no poke wired: it fails quietly
    assert owner.repo.shares_for_person(ann.person) == []
    assert ann.repo.get(sid) is not None  # not told
    for n in range(1, share_pull.GONE_AFTER):
        st = await pull(owner, ann)
        assert st.state == "error" and ann.repo.get(sid) is not None, n
    st = await pull(owner, ann)
    assert st.state == "gone" and st.error == "Owner no longer shares with you"
    assert ann.repo.get(sid) is None
    row = ann.repo.contact(owner.person)
    assert row is not None and not row["removed_at"]  # the row stays
    assert ann.puller.info(owner.person).state == "gone"


async def test_a_good_round_resets_the_count(trio):
    owner, ann, _ = trio
    sid = meeting(owner)
    share(owner, ann, sid)
    await pull(owner, ann)
    real = ann.puller.client_for
    ann.puller.client_for = lambda listen, token: real(listen, "wrong-token")
    for _ in range(share_pull.GONE_AFTER - 1):
        assert (await pull(owner, ann)).state == "error"
    ann.puller.client_for = real
    assert (await pull(owner, ann)).state == "ok"
    ann.puller.client_for = lambda listen, token: real(listen, "wrong-token")
    for _ in range(share_pull.GONE_AFTER - 1):
        assert (await pull(owner, ann)).state == "error"
    assert ann.repo.get(sid) is not None


async def test_pairing_again_after_removal_pulls_normally(trio):
    owner, ann, _ = trio
    old = meeting(owner, "Old")
    share(owner, ann, old)
    await pull(owner, ann)
    await contacts.forget_contact(owner.ctx, ann.person)
    for _ in range(share_pull.GONE_AFTER):
        await pull(owner, ann)
    assert (await pull(owner, ann)).state == "gone"
    # Pair again (as pairing does): a new secret, both rows restored.
    secret = contacts.new_secret()
    contacts.upsert_contact(owner.ctx, ann.card(), secret)
    contacts.upsert_contact(ann.ctx, owner.card(), secret)
    ann.repo.set_contact_runtime(
        owner.person, machines={owner.endpoint: {"listen": "owner-machine"}}
    )
    fresh = meeting(owner, "Fresh")
    share(owner, ann, fresh)
    st = await pull(owner, ann)
    assert st.state == "ok" and st.received == 1
    assert ann.repo.get(fresh) is not None
