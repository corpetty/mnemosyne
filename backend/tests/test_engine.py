"""Speaker assignment, the composed engine, per-source labelling, remote parsing, registry."""

import httpx
import pytest
from src.mnemosyne.config import Settings
from src.mnemosyne.models.session import Recording, Session
from src.mnemosyne.models.transcript import TranscriptSegment, WordSegment
from src.mnemosyne.services.pipeline import sources_for_session
from src.mnemosyne.transcription.assign import assign_speakers
from src.mnemosyne.transcription.composed import ComposedEngine
from src.mnemosyne.transcription.engine import AudioSource, SpeakerTurn
from src.mnemosyne.transcription.registry import build_diarizer, build_transcriber
from src.mnemosyne.transcription.transcribers.remote import RemoteTranscriber, parse_verbose_json

from tests.fakes import FakeDiarizer, FakeTranscriber

# ---- assign -----------------------------------------------------------------


def _seg(start, end, words=None, text="x"):
    return TranscriptSegment(text=text, speaker="UNKNOWN", start=start, end=end, words=words)


def test_assign_by_overlap_and_nearest():
    turns = [
        SpeakerTurn(start=0, end=2, speaker="A"),
        SpeakerTurn(start=2, end=4, speaker="B"),
    ]
    out = assign_speakers([_seg(0.5, 1.5), _seg(1.8, 3.0), _seg(2.5, 3.9), _seg(9, 10)], turns)
    assert [s.speaker for s in out] == ["A", "B", "B", "B"]  # last: nearest


def test_assign_uses_word_majority():
    turns = [
        SpeakerTurn(start=0, end=1, speaker="A"),
        SpeakerTurn(start=1, end=5, speaker="B"),
    ]
    words = [
        WordSegment(word="a", start=0.0, end=0.9),
        WordSegment(word="b", start=1.1, end=2.0),
        WordSegment(word="c", start=2.1, end=3.0),
    ]
    out = assign_speakers([_seg(0, 3, words=words)], turns)
    assert out[0].speaker == "B"


def test_assign_without_turns_is_unknown():
    out = assign_speakers([_seg(0, 1)], [])
    assert out[0].speaker == "UNKNOWN"


# ---- composed engine ----------------------------------------------------------


@pytest.mark.anyio
async def test_composed_diarizes_unlabelled_and_relabels_labelled():
    t, d = FakeTranscriber(), FakeDiarizer()
    engine = ComposedEngine(t, d, echo_dedup=False)  # dedup is covered in test_speakers
    assert engine.name == "fake-transcriber+fake-diarizer"
    assert not engine.is_loaded()

    sources = [
        AudioSource(path="/mic.ogg", kind="mic", speaker_label="Me"),
        AudioSource(path="/sys.ogg", kind="system"),
    ]
    out = [s async for s in engine.transcribe_sources(sources)]

    assert engine.is_loaded()
    assert t.calls == [("/mic.ogg", None), ("/sys.ogg", None)]
    assert d.calls == ["/sys.ogg"]  # labelled mic source is never diarized
    # merged by start time: each fake segment appears twice (once per source)
    starts = [s.start for s in out]
    assert starts == sorted(starts)
    assert {s.speaker for s in out if s.text == "Hello everyone."} == {"Me", "SPEAKER_00"}


@pytest.mark.anyio
async def test_composed_without_diarizer_uses_single_speaker():
    engine = ComposedEngine(FakeTranscriber(), None, language="en")
    out = [s async for s in engine.transcribe_sources([AudioSource(path="/m.ogg")])]
    assert {s.speaker for s in out} == {"SPEAKER_00"}
    assert engine.transcriber.calls == [("/m.ogg", "en")]


@pytest.mark.anyio
async def test_composed_unload():
    t, d = FakeTranscriber(), FakeDiarizer()
    engine = ComposedEngine(t, d)
    await engine.load()
    await engine.unload()
    assert not t.loaded and not d.loaded


# ---- sources_for_session --------------------------------------------------------


def _rec(tmp_path, source, name):
    p = tmp_path / name
    p.write_bytes(b"x")
    return Recording(source=source, device_id=1, device_name=name, path=str(p))


def test_sources_mic_plus_system_labels_mic(tmp_path):
    s = Session(
        audio_file="/mixed.ogg",
        recordings=[_rec(tmp_path, "mic", "mic.ogg"), _rec(tmp_path, "system", "sys.ogg")],
    )
    out = sources_for_session(s, per_source=True, local_speaker_name="Corey")
    assert [(o.kind, o.speaker_label) for o in out] == [("mic", "Corey"), ("system", None)]


def test_sources_lone_mic_is_diarized(tmp_path):
    s = Session(audio_file="/mixed.ogg", recordings=[_rec(tmp_path, "mic", "mic.ogg")])
    out = sources_for_session(s, per_source=True, local_speaker_name="Me")
    assert [(o.kind, o.speaker_label) for o in out] == [("mixed", None)]


def test_sources_falls_back_when_files_missing_or_disabled(tmp_path):
    s = Session(
        audio_file="/mixed.ogg",
        recordings=[
            Recording(source="mic", device_id=1, device_name="m", path="/gone.ogg"),
            Recording(source="system", device_id=2, device_name="s", path="/gone2.ogg"),
        ],
    )
    assert [o.kind for o in sources_for_session(s, True, "Me")] == ["mixed"]
    s2 = Session(
        audio_file="/mixed.ogg",
        recordings=[_rec(tmp_path, "mic", "mic.ogg"), _rec(tmp_path, "system", "sys.ogg")],
    )
    assert [o.kind for o in sources_for_session(s2, False, "Me")] == ["mixed"]
    assert sources_for_session(Session(), True, "Me") == []


def test_pipeline_passes_sources_to_engine(client, ctx, fake_engine, tmp_path):
    from tests.conftest import drain_until_job

    sid = client.post("/api/sessions", json={}).json()["id"]
    ctx.sessions.set_audio(
        sid, "/mixed.ogg", [_rec(tmp_path, "mic", "a.ogg"), _rec(tmp_path, "system", "b.ogg")]
    )
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        job = client.post(f"/api/sessions/{sid}/transcribe").json()
        drain_until_job(ws, job["id"])
    assert [(s.kind, s.speaker_label) for s in fake_engine.sources[0]] == [
        ("mic", "Me"),
        ("system", None),
    ]
    assert client.get(f"/api/jobs/{job['id']}").json()["result"]["sources"] == 2


# ---- remote transcriber ------------------------------------------------------


def test_parse_verbose_json_segments_and_words():
    body = {
        "text": "hello world bye",
        "segments": [
            {"start": 0.0, "end": 1.0, "text": " hello world"},
            {"start": 1.0, "end": 2.0, "text": "bye"},
        ],
        "words": [
            {"word": "hello", "start": 0.0, "end": 0.4, "probability": 0.9},
            {"word": "world", "start": 0.5, "end": 0.9},
            {"word": "bye", "start": 1.2, "end": 1.8},
        ],
    }
    segs = parse_verbose_json(body)
    assert [s.text for s in segs] == ["hello world", "bye"]
    assert [w.word for w in segs[0].words] == ["hello", "world"]
    assert segs[0].words[0].score == 0.9
    assert [w.word for w in segs[1].words] == ["bye"]


def test_parse_verbose_json_text_only():
    segs = parse_verbose_json({"text": "just text", "duration": 4.2})
    assert len(segs) == 1 and segs[0].end == 4.2 and segs[0].words is None
    assert parse_verbose_json({"text": ""}) == []


@pytest.mark.anyio
async def test_remote_transcriber_posts_multipart(tmp_path):
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = await request.aread()
        return httpx.Response(
            200, json={"segments": [{"start": 0, "end": 1, "text": "ok"}], "text": "ok"}
        )

    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"OggS")
    t = RemoteTranscriber(
        "http://stt.local/v1/",
        model="parakeet",
        api_key="k",
        transport=httpx.MockTransport(handler),
    )
    await t.load()
    segs = await t.transcribe(str(audio), language="en")
    assert seen["url"] == "http://stt.local/v1/audio/transcriptions"
    assert seen["auth"] == "Bearer k"
    assert b'name="model"\r\n\r\nparakeet' in seen["body"]
    assert b"verbose_json" in seen["body"] and b"OggS" in seen["body"]
    assert b'name="language"\r\n\r\nen' in seen["body"]
    assert [s.text for s in segs] == ["ok"]


# ---- registry ------------------------------------------------------------------


def test_registry_remote_and_none_need_no_torch(tmp_path):
    s = Settings(
        data_dir=tmp_path, transcriber="remote", remote_stt_url="http://x/v1", diarizer="none"
    )
    t = build_transcriber(s)
    assert t.name == "remote" and t.base_url == "http://x/v1"
    assert build_diarizer(s) is None


def test_registry_rejects_unknown_and_missing_url(tmp_path):
    with pytest.raises(ValueError, match="Unknown transcriber"):
        build_transcriber(Settings(data_dir=tmp_path, transcriber="nope"))
    with pytest.raises(ValueError, match="remote_stt_url"):
        build_transcriber(Settings(data_dir=tmp_path, transcriber="remote"))
    with pytest.raises(ValueError, match="Unknown diarizer"):
        build_diarizer(Settings(data_dir=tmp_path, diarizer="nope"))


def test_changing_engine_settings_unloads(client, ctx, fake_engine):
    fake_engine.loaded = True
    client.put("/api/settings", json={"diarizer": "none"})
    assert ctx.models._engine is None
