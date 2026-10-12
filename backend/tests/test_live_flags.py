"""Live lines marked "Not said" (routes/live_flags.py, services/live_flags.py): keyboard noise
heard as "yeah" is removed from the final transcript."""

import pytest
from fastapi.testclient import TestClient

from mnemosyne import access
from mnemosyne.api.app import create_app
from mnemosyne.models.live_flag import LiveFlag
from mnemosyne.models.session import Recording, Session
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services.live_flags import covered
from tests.conftest import drain_until_job
from tests.fakes import FakeEngine


def _flag(client, sid, source="mic", start=10.0, end=11.0, text="yeah", **extra):
    return client.post(
        f"/api/sessions/{sid}/live-flags",
        json={"source": source, "start": start, "end": end, "text": text, **extra},
    )


def test_create_list_and_undo(client):
    sid = client.post("/api/sessions", json={"name": "m"}).json()["id"]
    assert client.get(f"/api/sessions/{sid}/live-flags").json() == []
    made = _flag(client, sid)
    assert made.status_code == 200
    flag = made.json()
    assert flag["source"] == "mic" and flag["start"] == 10.0 and flag["end"] == 11.0
    assert flag["text"] == "yeah" and flag["part"] == 0 and flag["id"]
    assert [f["id"] for f in client.get(f"/api/sessions/{sid}/live-flags").json()] == [flag["id"]]
    assert client.delete(f"/api/sessions/{sid}/live-flags/{flag['id']}").status_code == 200
    assert client.delete(f"/api/sessions/{sid}/live-flags/{flag['id']}").status_code == 404
    assert client.get(f"/api/sessions/{sid}/live-flags").json() == []


def test_bad_requests(client):
    sid = client.post("/api/sessions", json={"name": "m"}).json()["id"]
    assert _flag(client, sid, start=5, end=4).status_code == 422
    assert _flag(client, sid, source=" ").status_code == 422
    assert _flag(client, "nope").status_code == 404
    assert client.get("/api/sessions/nope/live-flags").status_code == 404


def test_the_part_being_recorded_is_the_default(client, ctx, fake_pipewire):
    sid = client.post("/api/audio/start", json={"device_ids": [1]}).json()["session_id"]
    ctx.active_recordings[sid].part = 2
    assert _flag(client, sid).json()["part"] == 2
    assert _flag(client, sid, part=1).json()["part"] == 1


def test_flags_go_with_the_meeting(client, ctx):
    sid = client.post("/api/sessions", json={"name": "m"}).json()["id"]
    _flag(client, sid)
    assert client.delete(f"/api/sessions/{sid}").status_code == 200
    assert ctx.repo.list_live_flags(sid) == []


@pytest.fixture
def team(settings, keystore):
    settings.team_mode = True
    app = create_app(settings, keystore=keystore)
    ctx = app.state.ctx
    people = {}
    for name, role in [("Ann", "member"), ("Bob", "member"), ("Rex", "reviewer")]:
        user = ctx.users.add(name, f"{name.lower()}@team.test", role)
        code, _ = ctx.users.invite(user.id)
        people[name] = (user, {"Authorization": f"Bearer {ctx.users.redeem(code, 'test')[1]}"})
    with TestClient(app) as client:
        yield client, ctx, people


def test_only_someone_who_can_change_the_meeting_flags_its_lines(team):
    client, ctx, people = team
    user = people["Ann"][0]
    reset = access.current.set(access.Principal(user.id, user.name, user.role))
    try:
        sid = ctx.repo.save(Session(name="Ann's")).id
    finally:
        access.current.reset(reset)
    ann, bob, rex = (people[n][1] for n in ("Ann", "Bob", "Rex"))
    made = (
        _flag(client, sid, headers=ann)
        if False
        else client.post(
            f"/api/sessions/{sid}/live-flags",
            json={"source": "mic", "start": 1, "end": 2, "text": "x"},
            headers=ann,
        )
    )
    assert made.status_code == 200
    flag = made.json()["id"]
    url = f"/api/sessions/{sid}/live-flags"
    body = {"source": "mic", "start": 1, "end": 2, "text": "x"}
    assert client.post(url, json=body, headers=bob).status_code == 404  # cannot even see it
    assert client.get(url, headers=bob).status_code == 404
    assert client.delete(f"{url}/{flag}", headers=bob).status_code == 404
    # A reviewer reads every meeting but changes only their own.
    assert client.get(url, headers=rex).status_code == 200
    assert client.post(url, json=body, headers=rex).status_code == 403
    assert client.delete(f"{url}/{flag}", headers=rex).status_code == 403
    assert client.delete(f"{url}/{flag}", headers=ann).status_code == 200


# ---- applied to the final transcript -----------------------------------------------------


class SourceEngine(FakeEngine):
    """A FakeEngine that says which source each segment came from, as ComposedEngine does."""

    def __init__(self, tagged: dict[str, list[TranscriptSegment]]):
        super().__init__([])
        self.tagged = tagged
        self.last_segment_sources = []

    async def transcribe_sources(self, sources, on_progress=None):
        self.sources.append(list(sources))
        self.last_segment_sources = [
            (seg, kind) for kind, segs in self.tagged.items() for seg in segs
        ]
        for seg, _ in sorted(self.last_segment_sources, key=lambda p: p[0].start):
            yield seg


def seg(text, start, end, speaker="Me"):
    return TranscriptSegment(text=text, speaker=speaker, start=start, end=end)


def _meeting(client, ctx, tmp_path):
    sid = client.post("/api/sessions", json={"name": "m"}).json()["id"]
    recordings = []
    for kind, n in (("mic", 1), ("system", 2)):
        path = tmp_path / f"{kind}.ogg"
        path.write_bytes(b"x")
        recordings.append(Recording(source=kind, device_id=n, device_name=kind, path=str(path)))
    ctx.sessions.set_audio(sid, str(tmp_path / "mic.ogg"), recordings)
    return sid


def _transcribe(client, sid):
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        job = client.post(f"/api/sessions/{sid}/transcribe").json()
        drain_until_job(ws, job["id"])
    return client.get(f"/api/jobs/{job['id']}").json()


def _events(client, sid):
    return client.get(f"/api/sessions/{sid}/history").json()["events"]


def test_a_flagged_mic_line_is_gone_from_the_final_transcript(client, ctx, tmp_path):
    ctx.settings.auto_summarize = False
    ctx.models._engine = SourceEngine(
        {
            "mic": [
                seg("Good morning", 0.0, 2.0),
                seg("Oh yeah", 10.0, 11.0),  # typing: flagged, 100 % inside
                seg("and the plan", 20.0, 24.0),  # 1 s of 4 s inside: kept
                seg("right then", 30.0, 32.0),  # exactly half inside: dropped
            ],
            "system": [seg("Yeah", 10.2, 11.0, "Ann")],  # same time, other source: kept
        }
    )
    sid = _meeting(client, ctx, tmp_path)
    _flag(client, sid, "mic", 9.9, 11.2, "yeah")
    _flag(client, sid, "mic", 19.0, 21.0, "x")
    _flag(client, sid, "mic", 31.0, 40.0, "y")
    _flag(client, sid, "mic", 50.0, 60.0, "nothing here")

    assert _transcribe(client, sid)["status"] == "completed"
    texts = [(s["text"]) for s in client.get(f"/api/sessions/{sid}").json()["transcript"]]
    assert texts == ["Good morning", "Yeah", "and the plan"]
    removed = [e for e in _events(client, sid) if e["kind"] == "flagged_lines_removed"]
    assert len(removed) == 1 and removed[0]["detail"]["count"] == 2
    # The flags stay, so transcribing again removes the lines again.
    assert len(client.get(f"/api/sessions/{sid}/live-flags").json()) == 4
    assert _transcribe(client, sid)["status"] == "completed"
    texts = [s["text"] for s in client.get(f"/api/sessions/{sid}").json()["transcript"]]
    assert texts == ["Good morning", "Yeah", "and the plan"]
    assert len([e for e in _events(client, sid) if e["kind"] == "flagged_lines_removed"]) == 2


def test_nothing_is_logged_when_no_line_is_covered(client, ctx, tmp_path):
    ctx.settings.auto_summarize = False
    ctx.models._engine = SourceEngine({"mic": [seg("Hello", 0.0, 2.0)]})
    sid = _meeting(client, ctx, tmp_path)
    _flag(client, sid, "mic", 30, 31)
    _flag(client, sid, "system", 0, 2)
    _transcribe(client, sid)
    assert [s["text"] for s in client.get(f"/api/sessions/{sid}").json()["transcript"]] == ["Hello"]
    assert not [e for e in _events(client, sid) if e["kind"] == "flagged_lines_removed"]


def test_an_engine_that_cannot_say_the_source_keeps_everything(client, ctx, tmp_path):
    ctx.settings.auto_summarize = False
    ctx.models._engine = FakeEngine([seg("Oh yeah", 10.0, 11.0)])
    sid = _meeting(client, ctx, tmp_path)
    _flag(client, sid, "mic", 10, 11)
    assert _transcribe(client, sid)["status"] == "completed"
    assert len(client.get(f"/api/sessions/{sid}").json()["transcript"]) == 1


def test_a_failure_applying_flags_does_not_fail_the_transcription(
    client, ctx, tmp_path, monkeypatch
):
    from mnemosyne.services import live_flags

    def boom(*a, **k):
        raise RuntimeError("broken")

    monkeypatch.setattr(live_flags, "drop_flagged", boom)
    ctx.settings.auto_summarize = False
    ctx.models._engine = SourceEngine({"mic": [seg("Oh yeah", 10.0, 11.0)]})
    sid = _meeting(client, ctx, tmp_path)
    _flag(client, sid, "mic", 10, 11)
    assert _transcribe(client, sid)["status"] == "completed"
    assert len(client.get(f"/api/sessions/{sid}").json()["transcript"]) == 1


def test_flags_of_one_part_do_not_touch_another(client, ctx, tmp_path):
    ctx.settings.auto_summarize = False
    ctx.models._engine = SourceEngine({"mic": [seg("Oh yeah", 10.0, 11.0)]})
    sid = _meeting(client, ctx, tmp_path)
    _flag(client, sid, "mic", 10, 11, part=1)  # heard in a part this meeting does not have
    _transcribe(client, sid)
    assert len(client.get(f"/api/sessions/{sid}").json()["transcript"]) == 1


def test_overlap_rule():
    f = LiveFlag(id="a", source="mic", start=10, end=12, text="")
    assert covered(seg("a", 10, 12), f)
    assert covered(seg("a", 11, 13), f)  # exactly half
    assert not covered(seg("a", 11.5, 13.5), f)  # a quarter
    assert covered(seg("a", 11, 11), f) and not covered(seg("a", 13, 13), f)  # a point


@pytest.mark.anyio
@pytest.mark.parametrize("echo_dedup", [False, True])
async def test_the_real_engine_yields_the_objects_it_records_the_source_of(
    ctx, echo_dedup, monkeypatch
):
    """apply() finds a segment's source by identity: a copy made anywhere in
    ComposedEngine.transcribe_sources (dedup, sorting, per-word assignment) would make
    flagged lines stay in silently."""
    from mnemosyne.services import live_flags
    from mnemosyne.transcription.composed import ComposedEngine
    from mnemosyne.transcription.engine import AudioSource
    from tests.fakes import FAKE_SEGMENTS, FakeDiarizer, FakeTranscriber

    # The fake transcriber gives both files the same lines, which the echo dedup would take
    # for bleed; move the mic lines away (10 s later, other words) so both sources stay.
    transcriber = FakeTranscriber()
    original = transcriber.transcribe

    async def transcribe(path, language=None, progress=None):
        segments = await original(path, language, progress)
        if "mic" in path:
            segments = [
                s.model_copy(
                    update={"text": "mic " + s.text, "start": s.start + 10, "end": s.end + 10}
                )
                for s in segments
            ]
        return segments

    monkeypatch.setattr(transcriber, "transcribe", transcribe)
    engine = ComposedEngine(transcriber, FakeDiarizer(), echo_dedup=echo_dedup)
    sources = [
        AudioSource(path="/mic.ogg", kind="mic", speaker_label="Me"),
        AudioSource(path="/sys.ogg", kind="system"),
    ]
    out = [s async for s in engine.transcribe_sources(sources)]
    assert len(out) == 2 * len(FAKE_SEGMENTS)

    recorded = {id(seg): kind for seg, kind in engine.last_segment_sources}
    assert len(engine.last_segment_sources) == len(out)
    for seg in out:
        assert id(seg) in recorded, f"{seg.text!r} is not the object the engine recorded"
        assert recorded[id(seg)] == ("mic" if seg.speaker == "Me" else "system")

    sid = ctx.sessions.create_session("m").id
    first = FAKE_SEGMENTS[0]  # 0.0-1.2 s on the system source, 10.0-11.2 s on the mic
    ctx.repo.add_live_flag(sid, "mic", first.start + 10, first.end + 10, "x")
    ctx.repo.add_live_flag(sid, "system", 50, 60, "elsewhere")
    kept, gone = live_flags.apply(ctx, sid, 0, out, engine)
    assert gone == 1
    assert len(kept) == len(out) - 1
    assert not [s for s in kept if s.text == "mic " + first.text]
    assert [s for s in kept if s.text == first.text]  # the system line at 0 s stays
    assert len([s for s in kept if s.speaker == "Me"]) == len(FAKE_SEGMENTS) - 1
