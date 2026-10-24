"""Telling contacts a shared meeting changed (services/share_announce.py)."""

import httpx
import pytest

from mnemosyne.models.session import Session, SessionStatus
from mnemosyne.services import contacts
from mnemosyne.services.share_announce import ShareAnnouncer
from tests.test_contacts import EP_B, ada, card_of, me  # noqa: F401

pytestmark = pytest.mark.anyio

LISTEN = "127.0.0.1:41000"


def meeting(ctx, name="Planning") -> str:
    return ctx.repo.save(Session(name=name, status=SessionStatus.COMPLETED)).id


@pytest.fixture
def setup(me, ada):  # noqa: F811
    row = contacts.upsert_contact(me, card_of(ada), contacts.new_secret())
    me.repo.set_contact_runtime(row["person"], machines={EP_B: {"listen": LISTEN}})
    ann = ShareAnnouncer(me)
    ann.watch_interval = 0
    ann.debounce = 0
    ann.start()
    ann.calls = []

    def factory(listen, token):
        def handler(request: httpx.Request) -> httpx.Response:
            ann.calls.append((listen, request.url.path, request.headers["authorization"]))
            return httpx.Response(204)

        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url=f"http://{listen}",
            headers={"Authorization": f"Bearer {token}"},
        )

    ann.client_for = factory
    return me, ann, row


async def test_sharing_pokes_the_contact_with_my_token(setup):
    ctx, ann, row = setup
    sid = meeting(ctx)
    assert await ann.tick() == []  # an unshared meeting: nobody
    ctx.repo.put_peer_share(sid, row["person"], {"summary": True}, "me")
    assert await ann.tick() == [row["person"]]
    mine, _ = contacts.tokens(row["secret"], ctx.person.public_hex, row["person"])
    assert ann.calls == [(LISTEN, "/api/share/poke", f"Bearer {mine}")]
    assert await ann.tick() == []  # nothing new


async def test_a_change_to_an_unshared_meeting_pokes_nobody(setup):
    ctx, ann, row = setup
    shared, other = meeting(ctx), meeting(ctx, "Other")
    ctx.repo.put_peer_share(shared, row["person"], {}, "me")
    await ann.tick()
    ann.calls.clear()
    ctx.repo._touch(other)
    assert await ann.tick() == []
    assert ann.calls == []


async def test_a_revoke_pokes_once_then_never(setup):
    ctx, ann, row = setup
    sid = meeting(ctx)
    ctx.repo.put_peer_share(sid, row["person"], {}, "me")
    await ann.tick()
    ann.calls.clear()
    assert ctx.repo.revoke_peer_share(sid, row["person"])
    assert await ann.tick() == [row["person"]]
    ctx.repo._touch(sid)
    assert await ann.tick() == []
    assert len(ann.calls) == 1


async def test_debounce(setup):
    ctx, ann, row = setup
    ann.debounce = 3600
    sid = meeting(ctx)
    ctx.repo.put_peer_share(sid, row["person"], {}, "me")
    assert await ann.tick() == [row["person"]]
    ctx.repo._touch(sid)
    assert await ann.tick() == []  # held back, not lost
    ann._last_poke.clear()
    assert await ann.tick() == [row["person"]]
    assert len(ann.calls) == 2


async def test_an_unreachable_machine_does_not_raise(setup):
    ctx, ann, row = setup

    def refuse(listen, token):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("x"))),
            base_url=f"http://{listen}",
        )

    ann.client_for = refuse
    sid = meeting(ctx)
    ctx.repo.put_peer_share(sid, row["person"], {}, "me")
    assert await ann.tick() == []


async def test_a_machine_without_listen_is_dialed_once(setup, monkeypatch):
    ctx, ann, row = setup
    ctx.repo.set_contact_runtime(row["person"], machines={})
    dialed = []

    async def dial_all(app, r):
        dialed.append(r["person"])
        return {EP_B: {"listen": LISTEN}}

    monkeypatch.setattr(contacts, "dial_all", dial_all)
    ctx.repo.put_peer_share(meeting(ctx), row["person"], {}, "me")
    assert await ann.tick() == [row["person"]]
    assert dialed == [row["person"]]


async def test_team_mode_never_pokes(setup):
    ctx, ann, row = setup
    ctx.settings.team_mode = True
    ctx.repo.put_peer_share(meeting(ctx), row["person"], {}, "me")
    assert await ann.tick() == []
    assert ann.calls == []
    ann.start()  # no loop either
    assert ann._task is None
