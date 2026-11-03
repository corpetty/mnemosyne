"""Using a paired computer's models (services/peer_compute.py, api/routes/compute.py,
summarization/peer.py, transcription/peer.py): two backends in this process, paired for library
sync, A (a laptop) borrowing B's (a desktop's) LLMs and transcription engine through
`httpx.ASGITransport`. No network, no iroh."""

import os

import httpx
import pytest

from mnemosyne.api.app import create_app
from mnemosyne.config import Settings
from mnemosyne.jobs import JobStatus
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services.sync_peers import SyncPeer, now_iso
from mnemosyne.storage.crypto import MemoryKeyStore
from mnemosyne.summarization.peer import PeerUnavailable
from mnemosyne.summarization.privacy import is_cloud
from mnemosyne.transcription.engine import AudioSource
from mnemosyne.transcription.peer import PeerEngine
from tests.fakes import FakeEngine, FakeProvider

pytestmark = pytest.mark.anyio

ENDPOINTS = {"A": "a" * 64, "B": "b" * 64}
DESKTOP_SEGMENTS = [
    TranscriptSegment(text="Transcribed on the desktop.", speaker="SPEAKER_00", start=0, end=2),
    TranscriptSegment(text="With its big GPU.", speaker="SPEAKER_01", start=2.5, end=4),
]
LAPTOP_SEGMENTS = [TranscriptSegment(text="Done here.", speaker="SPEAKER_00", start=0, end=1)]


class Node:
    def __init__(self, tmp_path, name: str):
        self.name = name
        settings = Settings(
            data_dir=tmp_path / name / "data",
            backup_dir=str(tmp_path / name / "backups"),
            api_token=f"admin-{name}",
        )
        self.app = create_app(settings, keystore=MemoryKeyStore())
        self.ctx = self.app.state.ctx
        self.ctx.sync.sync_interval = None
        self.ctx.sync_peers.set_name(name)

    @property
    def id(self) -> str:
        return self.ctx.sync_peers.self_info().id

    def client(self, token: str | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {token or self.ctx.settings.api_token}"},
        )


def to_app(app):
    def client_for(peer: SyncPeer) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://peer",
            headers={"Authorization": f"Bearer {peer.token}"},
            timeout=60,
        )

    return client_for


def pair(x: Node, y: Node) -> None:
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
        me.ctx.sync.client_for = to_app(other.app)


@pytest.fixture
async def nodes(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMOSYNE_CONFIG_FILE", str(tmp_path / "config.toml"))
    a, b = Node(tmp_path, "A"), Node(tmp_path, "B")
    pair(a, b)
    # B, the desktop: an LLM server (lent) and a cloud key (never lent), and a fast engine.
    b.ctx.summarizer.providers = {
        "vllm": FakeProvider(models=["Qwen/Qwen3-32B", "small"]),
        "openai": FakeProvider(models=["gpt-x"]),
    }
    b.ctx.settings.default_provider = "vllm"
    b.ctx.models._engine = FakeEngine(segments=DESKTOP_SEGMENTS)
    a.ctx.models._engine = FakeEngine(segments=LAPTOP_SEGMENTS)
    yield a, b
    for n in (a, b):
        await n.ctx.jobs.shutdown()
        n.ctx.repo.close()


def peer_name(node: Node) -> str:
    return f"peer:{node.id}"


# ---- LLMs ----------------------------------------------------------------------------------


async def test_the_desktops_local_models_are_listed_on_the_laptop(nodes):
    a, b = nodes
    async with a.client() as client:
        listed = (await client.get("/api/models")).json()
    peer = next(p for p in listed if p["provider"] == peer_name(b))
    assert peer["label"] == "B (paired computer)"
    assert peer["models"] == ["vllm/Qwen/Qwen3-32B", "vllm/small"]  # never openai/...
    assert not is_cloud(peer_name(b))
    own = next(p for p in listed if p["provider"] == "ollama")
    assert own["label"] == "ollama"


async def test_a_completion_runs_on_the_desktop(nodes):
    a, b = nodes
    desktop = b.ctx.summarizer.providers["vllm"]
    desktop.reply = "From the desktop"
    text = await a.ctx.summarizer.complete(
        "system", "user", peer_name(b), "vllm/Qwen/Qwen3-32B", think=False
    )
    assert text == "From the desktop"
    assert desktop.calls[-1]["model"] == "Qwen/Qwen3-32B"
    assert desktop.calls[-1]["think"] is False


async def test_a_blank_model_uses_the_desktops_default(nodes):
    a, b = nodes
    b.ctx.settings.default_model = "small"
    model = await a.ctx.summarizer.resolve_model(peer_name(b), "")
    assert model == "vllm/small"
    await a.ctx.summarizer.complete("s", "u", peer_name(b), "")
    assert b.ctx.summarizer.providers["vllm"].calls[-1]["model"] == "small"


async def test_a_summary_with_the_desktops_model(nodes):
    a, b = nodes
    b.ctx.summarizer.providers["vllm"].summary = '{"summary": "Planned the launch."}'
    segments = [s.model_dump() for s in DESKTOP_SEGMENTS]
    out = await a.ctx.summarizer.summarize(segments, peer_name(b), "vllm/Qwen/Qwen3-32B")
    assert out["summary"] == "Planned the launch."
    assert out["data"].provider == peer_name(b)


async def test_the_desktop_never_lends_its_cloud_keys(nodes):
    a, b = nodes
    # Not a lent provider in front: the whole name is a model of the desktop's default.
    await a.ctx.summarizer.complete("s", "u", peer_name(b), "openai/gpt-x")
    assert b.ctx.summarizer.providers["vllm"].calls[-1]["model"] == "openai/gpt-x"
    async with a.ctx.sync.client_for(a.ctx.sync_peers.get(b.id)) as as_a:
        body = {"provider": "openai", "model": "gpt-x", "system": "s", "user": "u"}
        r = await as_a.post("/api/compute/complete", json=body)
    assert r.status_code == 400 and "does not lend 'openai'" in r.json()["detail"]
    assert b.ctx.summarizer.providers["openai"].calls == []


async def test_only_a_synced_computer_may_borrow(nodes):
    _, b = nodes
    async with b.client() as admin:  # the admin token is not a synced computer
        assert (await admin.get("/api/compute/models")).status_code == 403
    b.ctx.settings.team_mode = True
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=b.app),
        base_url="http://peer",
        headers={"Authorization": f"Bearer {nodes[0].ctx.sync_peers.get(b.id).token}"},
    ) as as_a:
        assert (await as_a.get("/api/compute/models")).status_code == 409


async def test_an_unreachable_desktop_lists_no_models(nodes):
    a, b = nodes
    a.ctx.sync_peers.update(b.id, listen=None)  # not dialed, and no sidecar running
    assert await a.ctx.summarizer.providers[peer_name(b)].list_models() == []
    with pytest.raises(PeerUnavailable, match="B could not be reached"):
        await a.ctx.summarizer.complete("s", "u", peer_name(b), "vllm/small")


async def test_a_computer_no_longer_paired_has_no_provider(nodes):
    a, b = nodes
    a.ctx.sync_peers.remove(b.id)
    assert peer_name(b) not in a.ctx.summarizer.providers


async def test_borrowed_models_are_never_lent_again(nodes):
    a, b = nodes
    # A's own providers are what it lends: B's peer provider on A is not among them.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a.app),
        base_url="http://peer",
        headers={"Authorization": f"Bearer {b.ctx.sync_peers.get(a.id).token}"},
    ) as as_b:
        lent = (await as_b.get("/api/compute/models")).json()["providers"]
    assert all(not p["provider"].startswith("peer:") for p in lent)


async def test_the_budget_follows_the_desktops_model(nodes):
    a, b = nodes
    local = FakeProvider(models=["qwen3-4b"])
    local.max_chars = 20_000
    b.ctx.summarizer.providers = {**b.ctx.summarizer.local_providers, "local": local}
    provider = a.ctx.summarizer.providers[peer_name(b)]
    await provider.list_models()
    chunk = a.ctx.settings.summary_chunk_chars
    assert a.ctx.summarizer.budget(provider, "local/qwen3-4b") == min(20_000, chunk)
    assert a.ctx.summarizer.budget(provider, "vllm/small") == chunk


# ---- transcription -------------------------------------------------------------------------


def audio(tmp_path, name="meeting_mixed.ogg") -> str:
    path = tmp_path / name
    path.write_bytes(b"OggS" + b"\x01" * 4000)
    return str(path)


async def test_the_engine_is_the_desktops_when_chosen(nodes):
    a, b = nodes
    assert isinstance(await a.ctx.models.job_engine(), FakeEngine)  # not chosen: ours
    a.ctx.settings.transcribe_on = b.id
    engine = await a.ctx.models.job_engine()
    assert isinstance(engine, PeerEngine)
    a.ctx.settings.transcribe_on = "gone"  # no longer paired: ours
    assert isinstance(await a.ctx.models.job_engine(), FakeEngine)


async def test_the_desktop_transcribes_and_keeps_no_audio(nodes, tmp_path):
    a, b = nodes
    a.ctx.settings.transcribe_on = b.id
    a.ctx.settings.language = "en"
    engine = await a.ctx.models.job_engine()
    engine.poll_interval = 0.01
    mic, system = audio(tmp_path, "s_mic.ogg"), audio(tmp_path, "s_system.ogg")
    stages = []
    sources = [AudioSource(mic, "mic", "Me"), AudioSource(system, "system")]
    segments = [
        s async for s in engine.transcribe_sources(sources, lambda st, f: stages.append(st))
    ]
    assert [s.text for s in segments] == [s.text for s in DESKTOP_SEGMENTS]
    assert not engine.used_fallback
    assert stages[0] == "Sending the audio to B"
    desktop = b.ctx.models._engine
    sent = desktop.sources[-1]
    assert [(s.kind, s.speaker_label) for s in sent] == [("mic", "Me"), ("system", None)]
    assert all(not os.path.exists(s.path) for s in sent)  # scratch removed
    assert b.ctx.compute_host.tasks == {}  # fetched, then forgotten
    job = next(j for j in b.ctx.jobs.list() if j.kind == "peer_transcribe")
    assert job.status == JobStatus.COMPLETED and job.session_id is None
    assert engine.name.endswith("on B")


async def test_an_unreachable_desktop_falls_back_to_this_computer(nodes, tmp_path):
    a, b = nodes
    a.ctx.settings.transcribe_on = b.id
    a.ctx.sync_peers.update(b.id, listen=None)
    engine = await a.ctx.models.job_engine()
    stages = []
    segments = [
        s
        async for s in engine.transcribe_sources(
            [AudioSource(audio(tmp_path))], lambda st, f: stages.append(st)
        )
    ]
    assert [s.text for s in segments] == ["Done here."]
    assert engine.used_fallback
    assert any(st.endswith("Transcribing on this computer") for st in stages)


async def test_without_fallback_the_job_fails_with_the_reason(nodes, tmp_path):
    a, b = nodes
    a.ctx.settings.transcribe_on = b.id
    a.ctx.settings.transcribe_on_fallback = False
    b.ctx.models._engine.fail = True
    engine = await a.ctx.models.job_engine()
    engine.poll_interval = 0.01
    with pytest.raises(RuntimeError, match="B could not transcribe: fake engine failure"):
        [s async for s in engine.transcribe_sources([AudioSource(audio(tmp_path))])]


async def test_a_meeting_on_the_laptop_is_transcribed_by_the_desktop(nodes, tmp_path):
    a, b = nodes
    a.ctx.settings.transcribe_on = b.id
    async with a.client() as client:
        sid = (await client.post("/api/sessions", json={"name": "Launch"})).json()["id"]
        a.ctx.sessions.set_audio(sid, audio(tmp_path, f"{sid}_mixed.ogg"), [])
        job = (await client.post(f"/api/sessions/{sid}/transcribe")).json()
        done = await a.ctx.jobs.wait(job["id"])
        assert done.status == JobStatus.COMPLETED, done.error
        session = (await client.get(f"/api/sessions/{sid}")).json()
    assert [s["text"] for s in session["transcript"]] == [s.text for s in DESKTOP_SEGMENTS]
    assert a.ctx.models._engine.sources == []  # the laptop's own engine never ran


async def test_a_task_belongs_to_the_computer_that_sent_it(nodes, tmp_path):
    a, b = nodes
    async with a.ctx.sync.client_for(a.ctx.sync_peers.get(b.id)) as as_a:
        with open(audio(tmp_path), "rb") as f:
            r = await as_a.post(
                "/api/compute/transcribe",
                data={"spec": '{"sources": [{"kind": "mixed"}]}'},
                files=[("files", ("x.ogg", f, "application/octet-stream"))],
            )
    task_id = r.json()["id"]
    await b.ctx.jobs.wait(b.ctx.compute_host.tasks[task_id].job_id)
    other = SyncPeer(
        id="c" * 12, name="C", endpoint_id="c" * 64, ticket="t", token="x", created_at=now_iso()
    )
    assert b.ctx.compute_host.get(task_id, other) is None
    assert b.ctx.compute_host.get(task_id, b.ctx.sync_peers.get(a.id)) is not None


async def test_bad_uploads_are_refused(nodes, tmp_path):
    a, b = nodes
    async with a.ctx.sync.client_for(a.ctx.sync_peers.get(b.id)) as as_a:
        with open(audio(tmp_path), "rb") as f:
            files = [("files", ("x.ogg", f, "application/octet-stream"))]
            two = '{"sources": [{"kind": "mixed"}, {"kind": "mic"}]}'
            r = await as_a.post("/api/compute/transcribe", data={"spec": two}, files=files)
            assert r.status_code == 400
            f.seek(0)
            odd = '{"sources": [{"kind": "video"}]}'
            r = await as_a.post("/api/compute/transcribe", data={"spec": odd}, files=files)
            assert r.status_code == 400
    assert b.ctx.compute_host.tasks == {}
