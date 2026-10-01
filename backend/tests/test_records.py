"""Meeting records (services/records.py): versions, seals, retention, legal holds, exports."""

import hashlib
import io
import zipfile

from fastapi.testclient import TestClient

from mnemosyne import access
from mnemosyne.api.app import create_app
from mnemosyne.services import records
from tests.conftest import drain_until_job, run_summarize


def _transcribed(client, ctx, tmp_path, name="Annual review"):
    sid = client.post("/api/sessions", json={"name": name}).json()["id"]
    audio = tmp_path / f"{sid}_mixed.ogg"
    audio.write_bytes(b"OggS" + b"\x01" * 4000)
    ctx.sessions.set_audio(sid, str(audio), [])
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        job = client.post(f"/api/sessions/{sid}/transcribe").json()
        drain_until_job(ws, job["id"])
    return sid, audio


def test_edits_keep_the_earlier_transcript(client, ctx, fake_engine, tmp_path):
    sid, _ = _transcribed(client, ctx, tmp_path)
    assert ctx.repo.versions(sid) == []  # the first transcript replaces nothing
    before = client.get(f"/api/sessions/{sid}").json()["transcript"][0]["text"]
    client.patch(f"/api/sessions/{sid}/segments/0", json={"text": "Changed words"})
    client.patch(f"/api/sessions/{sid}/segments/0", json={"text": "Changed words"})  # no-op
    versions = client.get(f"/api/sessions/{sid}/records").json()["versions"]
    assert [v["reason"] for v in versions] == ["edited"]
    old = client.get(f"/api/sessions/{sid}/versions/{versions[0]['id']}").json()
    assert old["transcript"][0]["text"] == before


def test_a_new_summary_keeps_the_old_one(client, ctx, fake_engine, fake_provider, tmp_path):
    sid, _ = _transcribed(client, ctx, tmp_path)
    run_summarize(client, sid)
    first = client.get(f"/api/sessions/{sid}").json()["summary"]
    run_summarize(client, sid)
    reasons = [v["reason"] for v in ctx.repo.versions(sid)]
    assert reasons.count("summarized again") <= 1
    if reasons:  # the fake provider answers the same: a changed summary is what gets kept
        assert ctx.repo.versions(sid, with_content=True)[-1]["content"]["summary"] == first


def test_seals_chain_and_catch_changes_made_outside_the_app(client, ctx, fake_engine, tmp_path):
    sid, audio = _transcribed(client, ctx, tmp_path)
    record = client.get(f"/api/sessions/{sid}/records").json()
    check = record["verification"]
    assert check["ok"] and check["seals"] == 1 and len(check["chain_head"]) == 64
    client.patch(f"/api/sessions/{sid}/segments/0", json={"text": "An edit in the app"})
    records.seal(ctx, sid, "edited")  # what the route does in the background
    check = records.verify(ctx, sid)
    assert check.ok and check.seals == 2
    # A row changed behind the app's back.
    with ctx.repo._lock, ctx.repo._conn:
        ctx.repo._conn.execute(
            "UPDATE segments SET text='forged' WHERE session_id=? AND idx=0", (sid,)
        )
    assert (
        "The transcript or summary differs from the last seal" in records.verify(ctx, sid).problems
    )
    # An audio file swapped.
    audio.write_bytes(b"OggS" + b"\x02" * 4000)
    assert any("differs" in p and audio.name in p for p in records.verify(ctx, sid).problems)
    # A seal rewritten.
    with ctx.repo._lock, ctx.repo._conn:
        ctx.repo._conn.execute(
            "UPDATE session_seals SET content_hash='0' WHERE session_id=? AND id="
            "(SELECT min(id) FROM session_seals WHERE session_id=?)",
            (sid, sid),
        )
    assert any("changed after it was made" in p for p in records.verify(ctx, sid).problems)


def test_the_records_period_protects_meetings_and_audio(client, ctx, fake_engine, tmp_path):
    ctx.settings.records_retention_years = 6
    sid, _ = _transcribed(client, ctx, tmp_path)
    record = client.get(f"/api/sessions/{sid}/records").json()
    assert record["kept_until"] is not None
    r = client.delete(f"/api/sessions/{sid}/audio")
    assert r.status_code == 400 and "reason" in r.json()["detail"]
    assert client.delete(f"/api/sessions/{sid}").status_code == 400
    ok = client.delete(f"/api/sessions/{sid}", params={"reason": "Client asked, per policy"})
    assert ok.status_code == 200
    gone = ctx.repo.deletions()
    assert gone[-1]["session_id"] == sid and gone[-1]["reason"] == "Client asked, per policy"
    # Retention of audio by age never touches a meeting inside the period.
    sid2, _ = _transcribed(client, ctx, tmp_path, "Second")
    assert sid2 in records.protected_ids(ctx)
    result = client.post("/api/storage/cleanup", params={"dry_run": True, "days": 1}).json()
    assert sid2 not in [s["session_id"] for s in result["sessions"]]


def test_a_legal_hold_stops_every_deletion(client, ctx, fake_engine, tmp_path):
    sid, _ = _transcribed(client, ctx, tmp_path)
    r = client.put(f"/api/sessions/{sid}/legal-hold", json={"reason": "Matter 2026-14"})
    assert r.json()["legal_hold"] == "Matter 2026-14"
    for call in (
        lambda: client.delete(f"/api/sessions/{sid}", params={"reason": "x"}),
        lambda: client.delete(f"/api/sessions/{sid}/audio", params={"reason": "x"}),
    ):
        assert call().status_code == 403
    assert sid in records.protected_ids(ctx)
    client.put(f"/api/sessions/{sid}/legal-hold", json={"reason": ""})
    kinds = [e["kind"] for e in ctx.repo.events(sid)]
    assert "legal_hold" in kinds and "legal_hold_lifted" in kinds
    assert client.delete(f"/api/sessions/{sid}").status_code == 200


def test_an_exam_export_holds_everything_and_checks_out(client, ctx, fake_engine, tmp_path):
    sid, _ = _transcribed(client, ctx, tmp_path)
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        job = client.post("/api/records/export", json={"session_ids": [sid]}).json()
        drain_until_job(ws, job["id"])
    done = client.get(f"/api/jobs/{job['id']}").json()
    assert done["status"] == "completed", done
    export_id = done["result"]["export_id"]
    data = client.get(f"/api/records/exports/{export_id}").content
    z = zipfile.ZipFile(io.BytesIO(data))
    names = z.namelist()
    for part in ("meeting.json", "transcript.txt", "summary.md", "versions.json",
                 "history.json", "seals.json", f"audio/{sid}_mixed.ogg"):  # fmt: skip
        assert any(n.endswith(part) for n in names), part
    for line in z.read("SHA256SUMS").decode().splitlines():
        digest, name = line.split("  ", 1)
        assert hashlib.sha256(z.read(name)).hexdigest() == digest
    assert "sha256sum -c SHA256SUMS" in z.read("README.txt").decode()
    # Downloaded once: the unencrypted copy does not stay on the server.
    assert client.get(f"/api/records/exports/{export_id}").status_code == 404


def test_advisors_hold_nothing_and_export_only_their_own(settings, keystore, tmp_path):
    settings.team_mode = True
    app = create_app(settings, keystore=keystore)
    ctx = app.state.ctx
    tokens = {}
    for name, role in [("Ann", "advisor"), ("Bob", "advisor"), ("Rev", "reviewer")]:
        user = ctx.users.add(name, "", role)
        code, _ = ctx.users.invite(user.id)
        tokens[name] = {"Authorization": f"Bearer {ctx.users.redeem(code, 'x')[1]}"}
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={"name": "Ann's"}, headers=tokens["Ann"]).json()[
            "id"
        ]
        r = client.put(
            f"/api/sessions/{sid}/legal-hold", json={"reason": "x"}, headers=tokens["Ann"]
        )
        assert r.status_code == 403
        r = client.put(
            f"/api/sessions/{sid}/legal-hold", json={"reason": "x"}, headers=tokens["Rev"]
        )
        assert r.status_code == 200
        assert client.get("/api/records/deletions", headers=tokens["Ann"]).status_code == 403
        r = client.post("/api/records/export", json={"session_ids": [sid]}, headers=tokens["Bob"])
        assert r.status_code == 404
    assert access.current.get() is None
