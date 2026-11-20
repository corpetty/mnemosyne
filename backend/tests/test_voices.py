"""Making a narration voice: recordings checked, the same person throughout, live consent, the
owner's alone, and narrating with it (services/voices.py, services/narration.py,
api/routes/voices.py). Voices are pitches here (tests/voice_fakes.py); nothing is played."""

from __future__ import annotations

import shutil

import numpy as np
import pytest

from mnemosyne import access
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services import voices
from tests.conftest import drain_until_job
from tests.voice_fakes import RATE, speech, voice_models, wav_bytes  # noqa: F401 (a fixture)

DEE = 150.0  # Hz: the enrolling person
SOMEONE_ELSE = 290.0

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
# Pitches as voices, the demo synthesizer (tests/voice_fakes.py).
pytestmark = pytest.mark.usefixtures("voice_models")


def run_job(client, method: str, url: str, **kwargs) -> dict:
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        resp = getattr(client, method)(url, **kwargs)
        assert resp.status_code == 200, resp.text
        drain_until_job(ws, resp.json()["id"])
    return client.get(f"/api/jobs/{resp.json()['id']}").json()


def upload(client, voice_id: str, samples: np.ndarray, **form) -> dict:
    job = run_job(
        client,
        "post",
        f"/api/voices/{voice_id}/audio",
        files={"file": ("take.wav", wav_bytes(samples), "audio/wav")},
        data={k: str(v).lower() if isinstance(v, bool) else v for k, v in form.items()},
    )
    assert job["status"] == "completed", job
    return job["result"]["voice"]


def consent(client, fake_live_transcriber, voice_id: str, freq: float, read_as_shown=True):
    challenge = client.post(f"/api/voices/{voice_id}/consent/challenge", json={}).json()
    said = challenge["text"] if read_as_shown else "I agree to all of this."
    fake_live_transcriber.segments = [
        TranscriptSegment(text=said, speaker="UNKNOWN", start=0, end=9)
    ]
    job = run_job(
        client,
        "post",
        f"/api/voices/{voice_id}/consent",
        files={"file": ("consent.wav", wav_bytes(speech(freq, 9)), "audio/wav")},
    )
    assert job["status"] == "completed", job
    return challenge, job["result"]


def make_ready_voice(client, fake_live_transcriber) -> dict:
    voice = client.post("/api/voices", json={"name": "Dee"}).json()
    upload(client, voice["id"], speech(DEE, 50))  # cut into pieces of 6-20 s
    challenge, result = consent(client, fake_live_transcriber, voice["id"], DEE)
    assert result["passed"], result
    return result["voice"]


def test_a_long_upload_becomes_checked_pieces(client):
    voice = client.post("/api/voices", json={"name": "Dee"}).json()
    assert voice["ready"] is False and "consent" in voice["needs"]
    voice = upload(client, voice["id"], speech(DEE, 50))
    assert len(voice["clips"]) >= 3
    assert all(voices.PIECE_MIN <= c["seconds"] <= voices.CLIP_MAX for c in voice["clips"])
    assert all(c["accepted"] and c["text"] for c in voice["clips"])
    assert voice["seconds"] >= voices.READY_SECONDS
    assert voice["needs"] == "the consent check"  # enough audio; consent is what is missing


def test_bad_takes_are_kept_with_the_reason_and_never_used(client):
    vid = client.post("/api/voices", json={"name": "Dee"}).json()["id"]
    quiet = upload(client, vid, speech(DEE, 6, level=0.002), single=True, text="Hello there.")
    assert quiet["clips"][0]["accepted"] is False
    assert "quiet" in quiet["clips"][0]["problem"].lower()
    clipped = upload(client, vid, np.clip(speech(DEE, 6) * 8, -1, 1), single=True, text="Hi.")
    assert "loud" in clipped["clips"][1]["problem"].lower()
    assert clipped["seconds"] == 0


def test_another_persons_voice_is_refused(client):
    vid = client.post("/api/voices", json={"name": "Dee"}).json()["id"]
    upload(client, vid, speech(DEE, 12), single=True, text="The quick brown fox.")
    voice = upload(client, vid, speech(SOMEONE_ELSE, 12), single=True, text="Jumps over.")
    assert [c["accepted"] for c in voice["clips"]] == [True, False]
    assert "different person" in voice["clips"][1]["problem"]


def test_consent_needs_the_words_read_live_in_the_same_voice(client, fake_live_transcriber):
    vid = client.post("/api/voices", json={"name": "Dee"}).json()["id"]
    upload(client, vid, speech(DEE, 40))
    # Someone else reading the statement: refused.
    _, other = consent(client, fake_live_transcriber, vid, SOMEONE_ELSE)
    assert other["passed"] is False and "does not match" in other["problem"]
    # The right voice, but not the statement shown (the random words missing): refused.
    _, wrong = consent(client, fake_live_transcriber, vid, DEE, read_as_shown=False)
    assert wrong["passed"] is False and "Did not hear" in wrong["problem"]
    assert wrong["voice"]["consented"] is False
    # The statement as shown, in the voice: the voice is ready.
    challenge, ok = consent(client, fake_live_transcriber, vid, DEE)
    assert ok["passed"] and ok["voice"]["ready"] and ok["voice"]["consented"]
    assert len(challenge["words"]) == 4 and all(w in challenge["text"] for w in challenge["words"])
    # A challenge is used once.
    job = run_job(
        client,
        "post",
        f"/api/voices/{vid}/consent",
        files={"file": ("c.wav", wav_bytes(speech(DEE, 9)), "audio/wav")},
    )
    assert job["status"] == "failed" and "statement again" in job["error"]


def test_consent_is_checked_against_recordings(client, fake_live_transcriber):
    vid = client.post("/api/voices", json={"name": "Dee"}).json()["id"]
    challenge = client.post(f"/api/voices/{vid}/consent/challenge", json={}).json()
    fake_live_transcriber.segments = [
        TranscriptSegment(text=challenge["text"], speaker="UNKNOWN", start=0, end=9)
    ]
    job = run_job(
        client,
        "post",
        f"/api/voices/{vid}/consent",
        files={"file": ("c.wav", wav_bytes(speech(DEE, 9)), "audio/wav")},
    )
    assert job["status"] == "failed" and "Record the voice first" in job["error"]


def test_a_voice_is_its_owners_alone(client, ctx, fake_live_transcriber):
    voice = make_ready_voice(client, fake_live_transcriber)
    # Seen as someone else (a team member): not there.
    token = access.current.set(access.Principal(user_id="sam", name="Sam", role=access.MEMBER))
    try:
        assert voices.list_own(ctx) == []
        with pytest.raises(voices.VoiceNotFound):
            voices.get_own(ctx, voice["id"])
        with pytest.raises(voices.VoiceNotFound):
            voices.delete(ctx, voice["id"])
    finally:
        access.current.reset(token)
    # An admin may delete it (not use it).
    token = access.current.set(access.Principal(user_id="ann", name="Ann", role=access.ADMIN))
    try:
        with pytest.raises(voices.VoiceNotFound):
            voices.get_own(ctx, voice["id"])
        voices.delete(ctx, voice["id"])
    finally:
        access.current.reset(token)
    assert client.get("/api/voices").json() == []
    assert not (ctx.settings.voices_dir / voice["id"]).exists()


def test_correcting_and_removing_recordings(client, ctx):
    vid = client.post("/api/voices", json={"name": "Dee"}).json()["id"]
    voice = upload(client, vid, speech(DEE, 10), single=True, text="Hello there.")
    fixed = client.patch(f"/api/voices/{vid}/clips/0", json={"text": "Hello  there, friend."})
    assert fixed.json()["clips"][0]["text"] == "Hello there, friend."
    assert client.get(f"/api/voices/{vid}/clips/0/audio").status_code == 200
    gone = client.delete(f"/api/voices/{vid}/clips/0").json()
    assert gone["clips"] == [] and voice["clips"]
    assert list((ctx.settings.voices_dir / vid).glob("clip-*")) == []


def test_narrating_a_summary(client, ctx, fake_live_transcriber):
    voice = make_ready_voice(client, fake_live_transcriber)
    sid = ctx.sessions.create_session("Weekly sync").id
    ctx.repo.update_fields(sid, summary="# Weekly sync\n## Decisions\n- Ship v2 on Friday\n")
    ctx.settings.narration_pronunciations = "v2 -> version two"
    job = run_job(
        client,
        "post",
        "/api/narrations",
        json={"voice_id": voice["id"], "source_kind": "summary", "source_id": sid, "check": False},
    )
    assert job["status"] == "completed", job
    nid = job["result"]["narration_id"]
    got = client.get(f"/api/narrations/{nid}").json()
    assert got["title"] == "Weekly sync: summary" and got["voice_name"] == "Dee"
    assert got["engine"] == "demo" and got["watermarked"] is True
    texts = [c["text"] for c in got["chunks"]]
    assert "version two" in " ".join(texts) and "v2" not in " ".join(texts)
    pairs = zip(got["chunks"], got["chunks"][1:], strict=False)
    assert all(a["end"] <= b["start"] for a, b in pairs)
    assert got["seconds"] > 0
    audio = client.get(f"/api/narrations/{nid}/audio")
    assert audio.status_code == 200 and len(audio.content) > 1000
    listed = client.get("/api/narrations", params={"source_kind": "summary", "source_id": sid})
    assert [n["id"] for n in listed.json()] == [nid]
    kinds = [e["kind"] for e in ctx.repo.events(sid)]
    assert "narrated" in kinds
    # The narration outlives its voice, and is deleted on its own.
    client.delete(f"/api/voices/{voice['id']}")
    assert client.get(f"/api/narrations/{nid}").json()["voice_id"] is None
    assert client.delete(f"/api/narrations/{nid}").status_code == 200
    assert client.get(f"/api/narrations/{nid}").status_code == 404


def test_narrating_refuses_what_it_cannot_do(client, ctx, fake_live_transcriber):
    vid = client.post("/api/voices", json={"name": "Dee"}).json()["id"]
    body = {"voice_id": vid, "source_kind": "text", "text": "Hello."}
    not_ready = client.post("/api/narrations", json=body)
    assert not_ready.status_code == 400 and "not ready" in not_ready.json()["detail"]
    voice = make_ready_voice(client, fake_live_transcriber)
    sid = ctx.sessions.create_session("No summary").id
    for request, status, words in [
        ({"source_kind": "summary", "source_id": sid}, 400, "no summary"),
        ({"source_kind": "summary", "source_id": "nope"}, 400, "No such meeting"),
        ({"source_kind": "text", "text": "   "}, 400, "Write something"),
        ({"source_kind": "digest", "source_id": "nope"}, 400, "No such digest"),
    ]:
        resp = client.post("/api/narrations", json={"voice_id": voice["id"], **request})
        assert resp.status_code == status and words in resp.json()["detail"], resp.json()
    ctx.settings.synthesizer = "none"
    from mnemosyne.services import narration

    narration.forget_engine(ctx)
    resp = client.post(
        "/api/narrations", json={"voice_id": voice["id"], "source_kind": "text", "text": "Hi."}
    )
    assert resp.status_code == 409 and "engine" in resp.json()["detail"]


def test_the_round_trip_redoes_a_chunk_that_came_out_wrong(client, ctx, fake_live_transcriber):
    from mnemosyne.services import narration

    voice = make_ready_voice(client, fake_live_transcriber)
    synth = narration.engine(ctx)
    heard = iter(["something else entirely", "Hello from the narration test, read twice."])

    async def transcribe(app, path, language):
        return next(heard, "Hello from the narration test, read twice.")

    import mnemosyne.services.voices as v

    original = v.transcribe
    v.transcribe = transcribe
    try:
        job = run_job(
            client,
            "post",
            "/api/narrations",
            json={
                "voice_id": voice["id"],
                "source_kind": "text",
                "text": "Hello from the narration test, read twice.",
            },
        )
    finally:
        v.transcribe = original
    assert job["status"] == "completed", job
    got = client.get(f"/api/narrations/{job['result']['narration_id']}").json()
    assert synth.spoken[-2:] == [got["chunks"][0]["text"]] * 2  # spoken again once
    assert got["chunks"][0]["flagged"] is False  # the second take was right


def test_encryption_seals_voice_files(client, ctx, fake_live_transcriber):
    from mnemosyne.services import encryption

    voice = make_ready_voice(client, fake_live_transcriber)
    encryption.enable(ctx)
    folder = ctx.settings.voices_dir / voice["id"]
    assert all(p.name.endswith(".enc") for p in folder.iterdir() if p.is_file())
    assert client.get(f"/api/voices/{voice['id']}/clips/0/audio").status_code == 200
    job = run_job(
        client,
        "post",
        "/api/narrations",
        json={"voice_id": voice["id"], "source_kind": "text", "text": "Sealed.", "check": False},
    )
    assert job["status"] == "completed", job
    stored = ctx.repo.get_narration_row(job["result"]["narration_id"])["path"]
    assert stored.endswith(".enc")
    encryption.disable(ctx)
    assert not any(p.name.endswith(".enc") for p in folder.iterdir())


@needs_ffmpeg
def test_an_imported_narration_never_teaches_a_voice_profile(client, ctx, fake_live_transcriber):
    voice = make_ready_voice(client, fake_live_transcriber)
    job = run_job(
        client,
        "post",
        "/api/narrations",
        json={"voice_id": voice["id"], "source_kind": "text", "text": "Hi.", "check": False},
    )
    audio = client.get(f"/api/narrations/{job['result']['narration_id']}/audio").content
    imported = client.post(
        "/api/audio/import",
        files={"file": ("narration.ogg", audio, "audio/ogg")},
        data={"transcribe": "false"},
    ).json()
    sid = imported["session"]["id"]
    assert "synthetic_audio" in [e["kind"] for e in ctx.repo.events(sid)]
    from mnemosyne.services.speaker_service import has_synthetic

    assert has_synthetic(ctx.repo, sid)


def test_measures_of_a_recording():
    good = voices.measure(speech(DEE, 8), RATE)
    assert good.problem() == "" and 0.3 < good.quality() <= 1
    assert "short" in voices.measure(speech(DEE, 2), RATE).problem().lower()
    silence = np.random.default_rng(1).normal(0, 1e-4, RATE * 5).astype(np.float32)
    assert voices.measure(silence, RATE).problem()
    assert voices.text_match("I am Dee, amber river.", "i am dee amber river") == 1.0
    assert voices.text_match("I am Dee, amber river.", "I am Dee") == 0.6
