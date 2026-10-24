"""The recipient's pull of shared meetings (services/share_pull.py, repo.apply_shared): an owner
and a recipient app in this process, contacts of each other with one secret and real person
keys, the recipient pulling from the owner through `httpx.ASGITransport`. No network, no iroh;
notes documents through the stand-in sidecar (tests/fake_link.py)."""

import base64
from datetime import datetime

import httpx
import pytest

from mnemosyne.api.app import create_app
from mnemosyne.config import Settings
from mnemosyne.models.contact import Card
from mnemosyne.models.session import Session, SummaryData
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services import contacts, shared_notes, shares
from mnemosyne.services.person import PersonKeys
from tests import fake_link

pytestmark = pytest.mark.anyio

NOW = "2026-10-24T10:00:00"
ENDPOINTS = {"Owner": "a1" * 32, "Recipient": "b2" * 32}


def ticket(endpoint_id: str) -> str:
    raw = b"\x00" + bytes.fromhex(endpoint_id) + b"\x00"
    return "endpoint" + base64.b32encode(raw).decode().lower().rstrip("=")


class Node:
    def __init__(self, tmp_path, name: str):
        self.name = name
        self.endpoint = ENDPOINTS[name]
        settings = Settings(
            data_dir=tmp_path / name / "data",
            backup_dir=str(tmp_path / name / "backups"),
            api_token=f"admin-{name}",
            local_speaker_name=name,
        )
        from mnemosyne.storage.crypto import MemoryKeyStore

        self.app = create_app(settings, keystore=MemoryKeyStore())
        self.ctx = self.app.state.ctx
        self.ctx.share_pull.interval = None  # tests run the rounds
        self.ctx.share_push.debounce = None  # ... and the pushes (test_share_push.py)
        self.ctx.sync_peers.set_name(name)

    @property
    def repo(self):
        return self.ctx.repo

    @property
    def person(self) -> str:
        return self.ctx.person.public_hex

    @property
    def puller(self):
        return self.ctx.share_pull

    def card(self) -> Card:
        body = {
            "person": self.person,
            "name": self.name,
            "machines": [{"name": self.name, "ticket": ticket(self.endpoint)}],
            "at": "2026-10-23T10:00:00+00:00",
        }
        return Card(**body, sig=self.ctx.person.sign(PersonKeys.canonical(body)))

    def admin(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {self.ctx.settings.api_token}"},
        )


def befriend(owner: Node, recipient: Node) -> None:
    """Pair the two as contact pairing ends: one secret, each holding the other's signed card and
    CONTACT devices; the recipient's sidecar has 'dialed' the owner's machine."""
    secret = contacts.new_secret()
    contacts.upsert_contact(owner.ctx, recipient.card(), secret)
    contacts.upsert_contact(recipient.ctx, owner.card(), secret)
    recipient.repo.set_contact_runtime(
        owner.person, machines={owner.endpoint: {"listen": "owner-machine"}}
    )
    recipient.puller.client_for = to_app(owner.app)
    recipient.secret = secret


def to_app(app, seen: list | None = None):
    def client_for(listen: str, token: str) -> httpx.AsyncClient:
        hooks = {}
        if seen is not None:

            async def record(request):
                seen.append(request)

            hooks = {"request": [record]}
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=f"http://{listen}",
            headers={"Authorization": f"Bearer {token}"},
            event_hooks=hooks,
        )

    return client_for


@pytest.fixture
async def pair(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMOSYNE_CONFIG_FILE", str(tmp_path / "config.toml"))
    owner, recipient = Node(tmp_path, "Owner"), Node(tmp_path, "Recipient")
    befriend(owner, recipient)
    yield owner, recipient
    for n in (owner, recipient):
        await n.puller.stop()
        await n.ctx.jobs.shutdown()
        n.repo.close()


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr(shared_notes, "FOLD_DELAY", 1000.0)
    monkeypatch.setattr(shared_notes, "FOLD_EVERY", 1000.0)
    return fake_link.install(tmp_path, monkeypatch)


def meeting(node: Node, name="Release sync", notes="Scratch.\n", **kw) -> str:
    s = Session(
        name=name,
        created_at=datetime(2026, 10, 20, 10, 0),
        summary="We ship on Friday.",
        summary_data=SummaryData(decisions=["Ship on Friday"]),
        notes=notes,
        attendees=["Ana", "Ben"],
        participants=["Ana"],
        transcript=[TranscriptSegment(text="We ship on Friday.", speaker="Ana", start=0, end=4)],
        **kw,
    )
    return node.repo.save(s).id


def share(owner: Node, recipient: Node, sid: str, **parts) -> None:
    owner.repo.put_peer_share(sid, recipient.person, parts, by="Owner")


async def pull(owner: Node, recipient: Node):
    return await recipient.puller.pull(owner.person)


# ---- arriving -----------------------------------------------------------------------------


async def test_a_share_arrives_with_exactly_the_chosen_parts(pair):
    owner, recipient = pair
    sid = meeting(owner)
    private = meeting(owner, "Not shared")
    share(owner, recipient, sid)  # defaults: no transcript
    st = await pull(owner, recipient)
    assert st.state == "ok" and st.error is None and st.received == 1
    copy = recipient.repo.get(sid)
    assert copy is not None and recipient.repo.get(private) is None
    assert copy.shared_from == owner.person
    assert copy.transcript == [] and copy.audio_file is None
    assert copy.status.value == "completed" and copy.owner_id == "" and copy.origin == ""
    assert copy.summary == "We ship on Friday."
    assert copy.summary_data.decisions == ["Ship on Friday"]
    assert copy.attendees == ["Ana", "Ben"] and copy.notes == "Scratch.\n"
    assert copy.name == "Release sync"
    held = recipient.repo.shared_meeting(sid)
    assert held["person"] == owner.person and held["parts"]["transcript"] is False
    assert held["shared_with"] == ["Recipient"]
    # The API says whose it is, and it never enters this library's change feed.
    async with recipient.admin() as c:
        r = await c.get(f"/api/sessions/{sid}")
    assert r.status_code == 200 and r.json()["shared_from"] == owner.person
    changes, _, _ = recipient.repo.changes_since(0, limit=100)
    assert sid not in [c[0] for c in changes]
    # One history line on arrival; a re-pull adds none.
    await pull(owner, recipient)
    kinds = [e["kind"] for e in recipient.repo.events(sid)]
    assert kinds.count("shared_with_me") == 1 and "share_updated" not in kinds


async def test_the_transcript_only_when_ticked_and_gone_when_unticked(pair):
    owner, recipient = pair
    sid = meeting(owner)
    share(owner, recipient, sid, transcript=True)
    await pull(owner, recipient)
    assert [s.text for s in recipient.repo.get(sid).transcript] == ["We ship on Friday."]
    share(owner, recipient, sid, transcript=False)
    st = await pull(owner, recipient)
    assert st.updated == 1
    assert recipient.repo.get(sid).transcript == []
    assert recipient.repo.shared_meeting(sid)["parts"]["transcript"] is False
    updated = [e for e in recipient.repo.events(sid) if e["kind"] == "share_updated"]
    assert len(updated) == 1 and "transcript" not in updated[0]["detail"]["parts"]
    # A change that is not to the parts logs nothing more.
    owner.repo.update_fields(sid, summary="Changed")
    await pull(owner, recipient)
    assert [e["kind"] for e in recipient.repo.events(sid)].count("share_updated") == 1


async def test_a_repull_with_no_change_writes_nothing(pair, monkeypatch):
    owner, recipient = pair
    sid = meeting(owner)
    share(owner, recipient, sid)
    await pull(owner, recipient)
    calls = []
    real = recipient.repo.apply_shared
    monkeypatch.setattr(
        recipient.repo, "apply_shared", lambda sm, p: calls.append(sm.id) or real(sm, p)
    )
    # Even read from the start, the same digest is skipped.
    recipient.repo.set_contact_runtime(owner.person, cursors={})
    st = await pull(owner, recipient)
    assert st.state == "ok" and calls == [] and st.received == st.updated == 0


async def test_a_change_on_the_owner_updates_the_copy(pair):
    owner, recipient = pair
    sid = meeting(owner)
    share(owner, recipient, sid)
    await pull(owner, recipient)
    owner.repo.update_fields(sid, summary="We ship on Monday.")
    st = await pull(owner, recipient)
    assert st.updated == 1 and recipient.repo.get(sid).summary == "We ship on Monday."


async def test_comments_are_replaced_whole_by_the_owners(pair):
    owner, recipient = pair
    sid = meeting(owner)
    share(owner, recipient, sid)
    await pull(owner, recipient)
    from mnemosyne.models.sync_session import SyncComment

    stray = SyncComment(id="c0ffee000009", text="local only", created_at=NOW, updated_at=NOW)
    sm = shares.build_shared_meeting(owner.ctx, sid, recipient.person)
    recipient.repo.apply_shared(sm.model_copy(update={"comments": [stray]}), owner.person)
    assert [c.id for c in recipient.repo.list_comments(sid)] == ["c0ffee000009"]
    owner.repo.update_fields(sid, summary="Changed")
    await pull(owner, recipient)
    assert recipient.repo.list_comments(sid) == []


# ---- forgetting ---------------------------------------------------------------------------


async def test_an_unshare_deletes_the_copy(pair):
    owner, recipient = pair
    gone, kept = meeting(owner, "Gone"), meeting(owner, "Kept")
    share(owner, recipient, gone)
    share(owner, recipient, kept)
    await pull(owner, recipient)
    assert owner.repo.revoke_peer_share(gone, recipient.person)
    st = await pull(owner, recipient)
    assert st.deleted == 1
    assert recipient.repo.get(gone) is None and recipient.repo.get(kept) is not None
    assert recipient.repo.shared_meeting(gone) is None
    # No sync tombstone: our other computers keep pulling it themselves.
    assert not recipient.repo.has_tombstone(gone)


async def test_deleting_on_the_owner_deletes_the_copy(pair):
    owner, recipient = pair
    sid = meeting(owner)
    share(owner, recipient, sid)
    await pull(owner, recipient)
    owner.ctx.sessions.delete_session(sid)
    st = await pull(owner, recipient)
    assert st.deleted == 1 and recipient.repo.get(sid) is None


async def test_a_resharing_brings_it_back(pair):
    owner, recipient = pair
    sid = meeting(owner)
    share(owner, recipient, sid)
    await pull(owner, recipient)
    owner.repo.revoke_peer_share(sid, recipient.person)
    await pull(owner, recipient)
    share(owner, recipient, sid)
    st = await pull(owner, recipient)
    assert st.received == 1 and recipient.repo.get(sid) is not None


# ---- refusing -----------------------------------------------------------------------------


async def test_a_tampered_meeting_is_refused_and_not_stored(pair, monkeypatch, caplog):
    owner, recipient = pair
    sid = meeting(owner)
    share(owner, recipient, sid)
    real = shares.build_shared_meeting

    def tampered(app, session_id, person):
        sm = real(app, session_id, person)
        return sm.model_copy(update={"summary": "We never ship."})

    monkeypatch.setattr("mnemosyne.api.routes.share.build_shared_meeting", tampered)
    st = await pull(owner, recipient)
    assert st.state == "ok" and st.refused == 1
    assert recipient.repo.get(sid) is None and recipient.repo.shared_meeting(sid) is None
    assert "does not verify" in caplog.text


async def test_a_clash_with_an_own_meeting_is_skipped(pair):
    owner, recipient = pair
    sid = meeting(owner)
    mine = Session(id=sid, name="My own", created_at=datetime(2026, 1, 1), summary="Mine.")
    recipient.repo.save(mine)
    share(owner, recipient, sid)
    st = await pull(owner, recipient)
    assert st.state == "ok" and sid in st.clashes
    got = recipient.repo.get(sid)
    assert got.name == "My own" and got.shared_from == "" and got.summary == "Mine."
    # Its unshare does not delete our own meeting either.
    owner.repo.revoke_peer_share(sid, recipient.person)
    await pull(owner, recipient)
    assert recipient.repo.get(sid) is not None


async def test_removed_contacts_are_not_pulled(pair):
    owner, recipient = pair
    sid = meeting(owner)
    share(owner, recipient, sid)
    await contacts.forget_contact(recipient.ctx, owner.person)
    assert await pull(owner, recipient) is None
    assert recipient.repo.get(sid) is None


# ---- cursors, pokes, status -------------------------------------------------------------


async def test_cursors_are_kept_per_machine_across_rounds(pair):
    owner, recipient = pair
    seen: list[httpx.Request] = []
    recipient.puller.client_for = to_app(owner.app, seen)
    recipient.puller.page_size = 1
    ids = [meeting(owner, f"M{i}") for i in range(3)]
    for sid in ids:
        share(owner, recipient, sid)
    st = await pull(owner, recipient)
    assert st.received == 3
    pages = [r for r in seen if r.url.path == "/api/share/changes"]
    assert len(pages) == 3  # one meeting a page, until `more` is false
    cursor = recipient.repo.contact(owner.person)["cursors"][owner.endpoint]
    assert cursor["since"] > 0
    seen.clear()
    await pull(owner, recipient)
    [again] = [r for r in seen if r.url.path == "/api/share/changes"]
    assert again.url.params["since"] == str(cursor["since"])
    assert [r.url.path for r in seen][0] == "/api/share/hello"


async def test_the_poke_route_pulls_from_the_contact(pair):
    owner, recipient = pair
    sid = meeting(owner)
    share(owner, recipient, sid)
    # The token the owner presents to the recipient.
    presented, _ = contacts.tokens(recipient.secret, owner.person, recipient.person)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=recipient.app),
        base_url="http://recipient",
        headers={"Authorization": f"Bearer {presented}"},
    ) as c:
        r = await c.post("/api/share/poke")
    assert r.status_code == 204
    await recipient.puller.drain()
    assert recipient.repo.get(sid).shared_from == owner.person


async def test_pull_now_route_and_the_contact_list_say_how_it_went(pair):
    owner, recipient = pair
    share(owner, recipient, meeting(owner))
    async with recipient.admin() as c:
        before = (await c.get("/api/contacts")).json()["contacts"]
        assert before[0]["pull"] is None
        r = await c.post(f"/api/contacts/{owner.person}/pull")
        assert r.status_code == 200 and r.json()["state"] == "ok" and r.json()["at"]
        after = (await c.get("/api/contacts")).json()["contacts"]
        assert after[0]["pull"]["state"] == "ok"
        assert (await c.post(f"/api/contacts/{'f' * 64}/pull")).status_code == 404


async def test_an_unreachable_owner_is_a_state_not_an_exception(pair):
    owner, recipient = pair

    def down(request):
        raise httpx.ConnectError("connection refused", request=request)

    recipient.puller.client_for = lambda listen, token: httpx.AsyncClient(
        transport=httpx.MockTransport(down), base_url=f"http://{listen}"
    )
    st = await pull(owner, recipient)
    assert st.state == "unreachable" and "Owner" in st.error
    # Nothing dialed at all: the same.
    recipient.repo.set_contact_runtime(owner.person, machines={})
    st = await pull(owner, recipient)
    assert st.state == "unreachable"


async def test_a_newer_protocol_is_skew(pair, monkeypatch):
    owner, recipient = pair
    monkeypatch.setattr("mnemosyne.api.routes.share.SHARE_PROTOCOL", 99)
    st = await pull(owner, recipient)
    assert st.state == "skew" and st.error == "Update Mnemosyne to see what Owner shares"


async def test_a_refused_token_says_pair_again(pair):
    owner, recipient = pair
    owner.ctx.pairing.revoke_contact(recipient.person)
    st = await pull(owner, recipient)
    assert st.state == "error" and "pair again" in st.error


# ---- the notes document -------------------------------------------------------------------


def _fold(snapshot_b64: str, sid: str) -> str:
    return shared_notes._run_doc(
        shared_notes._binary(),
        {
            "op": "fold",
            "notes": shared_notes.notes_origin(sid),
            "snapshot": snapshot_b64,
            "frames": [],
        },
    )["markdown"]


async def test_the_notes_document_arrives_and_merges(pair, sidecar):
    owner, recipient = pair
    sid = meeting(owner, notes="Typed by the owner\n")
    share(owner, recipient, sid)
    await pull(owner, recipient)
    doc = recipient.repo.notes_doc(sid)
    assert doc is not None and doc["snapshot"] is not None
    snap = base64.b64encode(bytes(doc["snapshot"])).decode()
    assert _fold(snap, sid) == "Typed by the owner\n"
    assert recipient.repo.get(sid).notes == "Typed by the owner\n"
    # Never seeded here from the text: the document is the owner's.
    assert shared_notes.needs_doc_from_sync(recipient.ctx, sid) is False

    # The owner types more: the next pull unites the documents.
    assert shared_notes.set_markdown_blocking(owner.ctx, sid, "Typed by the owner\nMore\n")
    shared_notes.fold_blocking(owner.ctx, sid)
    st = await pull(owner, recipient)
    assert st.updated == 1
    assert recipient.repo.get(sid).notes == "Typed by the owner\nMore\n"
    doc = recipient.repo.notes_doc(sid)
    assert _fold(base64.b64encode(bytes(doc["snapshot"])).decode(), sid) == (
        "Typed by the owner\nMore\n"
    )


async def test_notes_unshared_drop_the_document(pair, sidecar):
    owner, recipient = pair
    sid = meeting(owner, notes="Private later\n")
    share(owner, recipient, sid)
    await pull(owner, recipient)
    assert recipient.repo.has_notes_doc(sid)
    share(owner, recipient, sid, notes=False)
    await pull(owner, recipient)
    assert not recipient.repo.has_notes_doc(sid) and recipient.repo.get(sid).notes == ""
