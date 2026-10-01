"""Supervision (services/supervision.py): compliance phrases flagged, a reviewer's queue."""

import io
import json
import zipfile

from fastapi.testclient import TestClient

from mnemosyne.api.app import create_app
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services import supervision
from tests.conftest import drain_until_job

LINES = [
    TranscriptSegment(text="Thanks for coming in today.", speaker="Advisor", start=0.0, end=2.0),
    TranscriptSegment(text="This fund is basically risk-free.", speaker="Advisor", start=2, end=5),
    TranscriptSegment(text="So I can’t lose money?", speaker="Client", start=5.0, end=7.0),
    TranscriptSegment(text="Nothing is guaranteed.", speaker="Advisor", start=7.0, end=9.0),
]


def _transcribe(client, ctx, fake_engine, tmp_path, name="Review"):
    fake_engine.segments = list(LINES)
    sid = client.post("/api/sessions", json={"name": name}).json()["id"]
    audio = tmp_path / f"{sid}_mixed.ogg"
    audio.write_bytes(b"OggS" + b"\x01" * 4000)
    ctx.sessions.set_audio(sid, str(audio), [])
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        job = client.post(f"/api/sessions/{sid}/transcribe").json()
        drain_until_job(ws, job["id"])
    return sid


def test_find_matches_whole_phrases_either_apostrophe():
    found = supervision.find(LINES, ["risk-free", "can't lose", "guarantee", "guaranteed"])
    assert [(f["idx"], f["phrase"]) for f in found] == [
        (1, "risk-free"),
        (2, "can't lose"),
        (3, "guaranteed"),  # not "guarantee": whole words only
    ]


def test_off_by_default_outside_firm_mode(client, ctx, fake_engine, tmp_path):
    sid = _transcribe(client, ctx, fake_engine, tmp_path)
    assert ctx.repo.flags(sid) == []


def test_a_transcription_is_flagged_and_reviewed(client, ctx, fake_engine, tmp_path):
    ctx.settings.supervision = True
    sid = _transcribe(client, ctx, fake_engine, tmp_path)
    [item] = client.get("/api/supervision").json()
    assert item["session_id"] == sid and item["flags"] == 3 and not item["reviewed"]
    assert item["phrases"] == ["can't lose", "guaranteed", "risk-free"]

    got = client.get(f"/api/sessions/{sid}/supervision").json()
    assert [f["speaker"] for f in got["flags"]] == ["Advisor", "Client", "Advisor"]
    r = client.post(f"/api/sessions/{sid}/supervision/review", json={"note": "Context fine"})
    assert r.json()["reviewed"] and r.json()["reviews"][0]["note"] == "Context fine"
    assert client.get("/api/supervision").json()[0]["reviewed"]
    assert "supervision_reviewed" in [e["kind"] for e in ctx.repo.events(sid)]


def test_edits_add_flags_and_never_hide_them(client, ctx, fake_engine, tmp_path):
    ctx.settings.supervision = True
    sid = _transcribe(client, ctx, fake_engine, tmp_path)
    client.post(f"/api/sessions/{sid}/supervision/review", json={})
    # Editing the flagged line away keeps its flag; the meeting stays reviewed.
    client.patch(f"/api/sessions/{sid}/segments/1", json={"text": "This fund is safe-ish."})
    flags = client.get(f"/api/sessions/{sid}/supervision").json()
    assert any(f["phrase"] == "risk-free" for f in flags["flags"]) and flags["reviewed"]
    # Deleting an earlier line renumbers the rest without making new flags.
    client.delete(f"/api/sessions/{sid}/segments/0")
    flags = client.get(f"/api/sessions/{sid}/supervision").json()
    assert len(flags["flags"]) == 3 and flags["reviewed"]
    assert {f["phrase"]: f["idx"] for f in flags["flags"]}["can't lose"] == 1
    # A new phrase said in an edit puts the meeting back in the queue.
    client.patch(f"/api/sessions/{sid}/segments/0", json={"text": "You should buy this."})
    ctx.settings.compliance_phrases += ", you should buy"
    supervision.scan(ctx, sid)
    assert not client.get("/api/supervision").json()[0]["reviewed"]


def test_a_new_transcription_replaces_the_flags(client, ctx, fake_engine, tmp_path):
    ctx.settings.supervision = True
    sid = _transcribe(client, ctx, fake_engine, tmp_path)
    fake_engine.segments = [LINES[0], LINES[3]]
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        drain_until_job(ws, client.post(f"/api/sessions/{sid}/transcribe").json()["id"])
    assert [f["phrase"] for f in ctx.repo.flags(sid)] == ["guaranteed"]


def test_changing_the_phrases_rescans_every_meeting(client, ctx, fake_engine, tmp_path):
    sid = _transcribe(client, ctx, fake_engine, tmp_path)  # supervision off: no flags
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        r = client.put("/api/settings", json={"supervision": True, "compliance_phrases": "fund"})
        assert r.status_code == 200
        [job] = [j for j in ctx.jobs.list() if j.kind == "supervision_scan"]
        drain_until_job(ws, job.id)
    assert [f["phrase"] for f in ctx.repo.flags(sid)] == ["fund"]
    assert client.get("/api/users/me").json()["supervision"] is True


def test_advisors_have_no_queue_and_reviewers_see_everyone(
    settings, keystore, tmp_path, fake_engine
):
    settings.firm_mode = True
    app = create_app(settings, keystore=keystore)
    ctx = app.state.ctx
    ctx.models._engine = fake_engine
    tokens = {}
    for name, role in [("Ann", "advisor"), ("Rev", "reviewer")]:
        user = ctx.users.add(name, "", role)
        code, _ = ctx.users.invite(user.id)
        tokens[name] = {"Authorization": f"Bearer {ctx.users.redeem(code, 'x')[1]}"}
    with TestClient(app) as client:
        client.headers.update(tokens["Ann"])
        sid = _transcribe(client, ctx, fake_engine, tmp_path, "Ann's")
        assert client.get("/api/supervision").status_code == 403
        assert client.post(f"/api/sessions/{sid}/supervision/review", json={}).status_code == 403
        client.headers.update(tokens["Rev"])
        [item] = client.get("/api/supervision").json()
        assert item["owner"] == "Ann" and item["flags"] == 3
        r = client.post(f"/api/sessions/{sid}/supervision/review", json={"note": "ok"})
        assert r.json()["reviews"][0]["by"] == "Rev"


def test_flags_and_reviews_go_into_an_exam_export(client, ctx, fake_engine, tmp_path):
    ctx.settings.supervision = True
    sid = _transcribe(client, ctx, fake_engine, tmp_path)
    client.post(f"/api/sessions/{sid}/supervision/review", json={"note": "Checked"})
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        job = client.post("/api/records/export", json={"session_ids": [sid]}).json()
        drain_until_job(ws, job["id"])
    export_id = client.get(f"/api/jobs/{job['id']}").json()["result"]["export_id"]
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/api/records/exports/{export_id}").content))
    [name] = [n for n in z.namelist() if n.endswith("supervision.json")]
    data = json.loads(z.read(name))
    assert len(data["flags"]) == 3 and data["reviews"][0]["note"] == "Checked"
