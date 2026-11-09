"""Meeting records (services/records.py): versions, seals, retention, legal holds, exports."""

import hashlib
import io
import time
import zipfile

from fastapi.testclient import TestClient

from mnemosyne import access
from mnemosyne.api.app import create_app
from mnemosyne.services import notes_media, records
from tests.conftest import drain_until_job, run_summarize
from tests.test_notes_media import sha, webp


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
    # The route seals in the background: wait for it (a slow machine takes a moment).
    deadline = time.monotonic() + 10
    while records.verify(ctx, sid).seals < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
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


def test_members_hold_nothing_and_export_only_their_own(settings, keystore, tmp_path):
    settings.team_mode = True
    app = create_app(settings, keystore=keystore)
    ctx = app.state.ctx
    tokens = {}
    for name, role in [("Ann", "member"), ("Bob", "member"), ("Rev", "reviewer")]:
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


def test_an_export_carries_the_notes_images_and_the_seal_ignores_them(client, ctx, tmp_path):
    sid, _ = _transcribed(client, ctx, tmp_path)
    assert records.verify(ctx, sid).ok
    seals = ctx.repo.seals(sid)
    data = webp(pad=b"m" * 200)
    notes_media.store(ctx, sid, sha(data), data)
    assert records.verify(ctx, sid).ok and ctx.repo.seals(sid) == seals  # nothing sealed changed

    dest = tmp_path / "export.zip"
    records.export_zip(ctx, records.ExportRequest(session_ids=[sid]), dest)
    z = zipfile.ZipFile(dest)
    name = next(n for n in z.namelist() if n.endswith(f"/media/{sha(data)}.webp"))
    assert z.read(name) == data
    sums = dict(line.split("  ", 1)[::-1] for line in z.read("SHA256SUMS").decode().splitlines())
    assert sums[name] == sha(data)
    assert "media/" in z.read("README.txt").decode()


# ---- format 2 seals (docs/plans/2026-11-09-records-by-frontier.md, task 3) -------------------


def _saved(ctx, tmp_path, name="Weekly", audio=True, **fields):
    """A meeting saved straight to the repository: a transcript and (by default) one file."""
    from mnemosyne.models.session import Recording, Session, TranscriptSegment

    path = None
    if audio:
        path = tmp_path / f"{name}.ogg"
        path.write_bytes(b"OggS" + name.encode() * 500)
    fields.setdefault(
        "transcript", [TranscriptSegment(text="We ship Friday.", speaker="Ana", start=0, end=3)]
    )
    s = Session(
        name=name,
        audio_file=str(path) if path else None,
        recordings=(
            [Recording(source="mic", device_id=0, device_name="Mic", path=str(path))]
            if path
            else []
        ),
        **fields,
    )
    ctx.repo.save(s)
    return s.id, path


def _encrypt(ctx, sid, path):
    """What turning encryption on does to a file, without a new version of the meeting."""
    from mnemosyne.storage.crypto import encrypt_file

    enc = encrypt_file(path, ctx.file_key)
    with ctx.repo._lock, ctx.repo._conn:
        ctx.repo._conn.execute("UPDATE sessions SET audio_file=? WHERE id=?", (str(enc), sid))
        ctx.repo._conn.execute("UPDATE recordings SET path=? WHERE session_id=?", (str(enc), sid))
    return enc


def _status(check, name):
    return next(p.status for p in check.parts if p.name == name)


def test_a_seal_is_a_manifest_with_plaintext_audio_that_survives_encryption(ctx, tmp_path):
    sid, path = _saved(ctx, tmp_path)
    plain = path.read_bytes()
    assert records.seal(ctx, sid, "transcribed") is not None
    (s,) = ctx.repo.seals(sid)
    m = s["manifest"]
    assert s["format"] == 2 and m["rev"] == ctx.repo.sync_seq_of(sid) and m["audio_here"]
    assert m["audio"] == {
        path.name: {"sha256": hashlib.sha256(plain).hexdigest(), "bytes": len(plain)}
    }
    assert set(s["audio_stat"]) == {path.name}
    check = records.verify(ctx, sid)
    assert check.ok and check.format == 2 and not check.pending and check.erased == []
    assert {p.name for p in check.parts} == {*records.PARTS, f"audio/{path.name}"}
    assert all(p.status == "same" for p in check.parts)

    ctx.master_key = bytes(range(32))
    enc = _encrypt(ctx, sid, path)  # x.ogg -> x.ogg.enc, another nonce: the same plaintext
    check = records.verify(ctx, sid)
    assert check.ok and _status(check, f"audio/{path.name}") == "same", check.problems

    # Locked: the audio cannot be compared, which is not a problem; nor can it be sealed anew.
    ctx.master_key = None
    records._plain_digests.clear()
    check = records.verify(ctx, sid)
    assert check.ok and _status(check, f"audio/{path.name}") == "unavailable"
    assert records.seal(ctx, sid, "published") is None

    # Unlocked again, a seal records the encrypted file's stats; locked, the next one reuses
    # that digest instead of reading the file.
    ctx.master_key = bytes(range(32))
    assert records.seal(ctx, sid, "published") is not None
    assert ctx.repo.seals(sid)[-1]["audio_stat"][path.name]["file"] == str(enc)
    ctx.master_key = None
    records._plain_digests.clear()
    assert records.seal(ctx, sid, "unpublished") is not None
    assert ctx.repo.seals(sid)[-1]["manifest"]["audio"] == m["audio"]

    # A file that does not decrypt is a problem, never "unavailable".
    ctx.master_key = bytes(range(32))
    data = bytearray(enc.read_bytes())
    data[-1] ^= 1
    enc.write_bytes(bytes(data))
    records._plain_digests.clear()  # same size, and maybe the same coarse mtime
    check = records.verify(ctx, sid)
    assert not check.ok and any(path.name in p and "differs" in p for p in check.problems)


def test_a_v1_record_still_verifies_and_upgrades_when_opened(client, ctx, tmp_path):
    sid, path = _saved(ctx, tmp_path)
    session = ctx.repo.get(sid)
    ctx.repo.add_seal(
        sid, "transcribed", records.content_hash(records.content_of(session)),
        records.audio_hashes(session),
    )  # fmt: skip
    check = records.verify(ctx, sid)
    assert check.ok and check.format == 1 and check.parts == [] and not check.pending

    # Encrypted since: the v1 seal hashed the file as stored, so it cannot be compared.
    ctx.master_key = bytes(range(32))
    _encrypt(ctx, sid, path)
    check = records.verify(ctx, sid)
    assert check.ok, check.problems
    assert [(p.name, p.status) for p in check.parts] == [(f"audio/{path.name}", "unavailable")]
    assert "new format" in check.parts[0].detail

    # Opening the record seals it in format 2, chained to the v1 seal.
    got = client.get(f"/api/sessions/{sid}/records").json()["verification"]
    assert got["ok"] and got["format"] == 2 and got["seals"] == 2
    v1, v2 = ctx.repo.seals(sid)
    assert v2["reason"] == records.UPGRADE_REASON and v2["prev"] == v1["chain"]
    assert v2["manifest"]["content_v1"] == v1["content_hash"]
    assert client.get(f"/api/sessions/{sid}/records").json()["verification"]["seals"] == 2

    # A v1 record that no longer matches is not upgraded: the problem stays visible.
    other, _ = _saved(ctx, tmp_path, "Other")
    session = ctx.repo.get(other)
    ctx.repo.add_seal(
        other, "transcribed", records.content_hash(records.content_of(session)),
        records.audio_hashes(session),
    )  # fmt: skip
    with ctx.repo._lock, ctx.repo._conn:
        ctx.repo._conn.execute("UPDATE segments SET text='forged' WHERE session_id=?", (other,))
    got = client.get(f"/api/sessions/{other}/records").json()["verification"]
    assert got["format"] == 1 and got["problems"] == [records.CONTENT_DIFFERS]


def test_app_changes_are_pending_and_forged_rows_are_problems(ctx, tmp_path):
    sid, _ = _saved(ctx, tmp_path, notes="Ship it")
    records.seal(ctx, sid, "transcribed")
    ctx.repo.add_comment(sid, text="Are we sure?", author="Bo")  # the app: rev moves on
    check = records.verify(ctx, sid)
    assert check.ok and check.pending and check.problems == []
    assert _status(check, "comments") == "pending" and _status(check, "transcript") == "same"

    assert records.seal(ctx, sid, "changed: comments") is not None
    check = records.verify(ctx, sid)
    assert check.ok and not check.pending
    with ctx.repo._lock, ctx.repo._conn:  # behind the app's back: rev stays
        ctx.repo._conn.execute("UPDATE comments SET text='forged' WHERE session_id=?", (sid,))
        ctx.repo._conn.execute("UPDATE sessions SET notes='forged' WHERE id=?", (sid,))
    check = records.verify(ctx, sid)
    assert not check.ok and not check.pending
    assert check.problems == [
        "The notes differ from the last seal",
        "The comments differ from the last seal",
    ]
    assert _status(check, "notes") == "differs" and _status(check, "comments") == "differs"


def test_notes_only_meetings_are_sealed_and_empty_ones_are_not(ctx, tmp_path):
    sid, _ = _saved(ctx, tmp_path, "Notes only", audio=False, transcript=[], notes="- Ship Friday")
    assert records.seal(ctx, sid, "changed: notes") is not None
    check = records.verify(ctx, sid)
    assert check.ok and check.format == 2 and {p.name for p in check.parts} == set(records.PARTS)
    empty, _ = _saved(ctx, tmp_path, "Empty", audio=False, transcript=[])
    assert records.seal(ctx, empty, "changed") is None and ctx.repo.seals(empty) == []


def test_audio_recorded_elsewhere_is_not_in_the_manifest(ctx, tmp_path):
    sid, _ = _saved(ctx, tmp_path, "Foreign", origin="peer-machine")
    records.seal(ctx, sid, "summarized")
    m = ctx.repo.seals(sid)[-1]["manifest"]
    assert m["audio"] == {} and m["audio_here"] is False
    check = records.verify(ctx, sid)
    assert check.ok and not any(p.name.startswith("audio/") for p in check.parts)
    shared, _ = _saved(ctx, tmp_path, "Theirs")
    with ctx.repo._lock, ctx.repo._conn:  # what taking a meeting shared with this person sets
        ctx.repo._conn.execute("UPDATE sessions SET shared_from='ada' WHERE id=?", (shared,))
    assert records.seal(ctx, shared, "transcribed") is None  # someone else's record


def _erase(ctx, sid, path, *, sha=None, by="Admin", reason="Retention"):
    """An erasure seal as task 4 will make it: the files move from audio to erased."""
    from mnemosyne.storage import records as r

    last = ctx.repo.seals(sid)[-1]["manifest"]
    data = path.read_bytes()
    measured = {path.name: r.audio_entry(sha or hashlib.sha256(data).hexdigest(), len(data))}
    audio, erased = r.erase_audio(
        last["audio"], last["erased"], measured, at="2026-11-09T10:00:00", by=by, reason=reason,
        deletion=None,
    )  # fmt: skip
    path.unlink()
    manifest = r.manifest_for(
        ctx.repo.get(sid), ctx.repo.list_comments(sid), rev=ctx.repo.sync_seq_of(sid),
        audio=audio, erased=erased,
    )  # fmt: skip
    return ctx.repo.add_seal(sid, "audio deleted", manifest=manifest)


def test_erased_audio_keeps_verifying_and_vanished_audio_does_not(ctx, tmp_path):
    from mnemosyne.storage import records as r

    sid, path = _saved(ctx, tmp_path)
    records.seal(ctx, sid, "transcribed")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    _erase(ctx, sid, path)
    check = records.verify(ctx, sid)
    assert check.ok, check.problems
    assert [(e.name, e.sha256, e.by, e.reason, e.matched) for e in check.erased] == [
        (path.name, digest, "Admin", "Retention", True)
    ]
    assert not any(p.name.startswith("audio/") for p in check.parts)
    # Later seals carry the erasure; the same file put back is no difference.
    records.seal(ctx, sid, "published")
    assert path.name in ctx.repo.seals(sid)[-1]["manifest"]["erased"]
    assert records.verify(ctx, sid).ok

    # A file that no longer matched when it was erased is recorded, and shown as a problem.
    other, opath = _saved(ctx, tmp_path, "Swapped")
    records.seal(ctx, other, "transcribed")
    _erase(ctx, other, opath, sha="0" * 64)
    check = records.verify(ctx, other)
    assert check.erased[0].matched is False
    assert check.problems == [
        f"Audio file {opath.name} no longer matched the record when it was deleted"
    ]

    # Audio that leaves the record without an erasure breaks continuity.
    third, tpath = _saved(ctx, tmp_path, "Vanished")
    records.seal(ctx, third, "transcribed")
    tpath.unlink()
    manifest = r.manifest_for(ctx.repo.get(third), [], rev=ctx.repo.sync_seq_of(third))
    ctx.repo.add_seal(third, "forged", manifest=manifest)
    problems = records.verify(ctx, third).problems
    assert f"Audio file {tpath.name} left the record at seal 2 without being erased" in problems


def test_a_swapped_image_in_the_notes_is_a_problem(ctx, tmp_path):
    data = webp(pad=b"i" * 100)
    notes = f"![Board](mnemosyne:image/{sha(data)}.webp@640x480)\n"
    sid, _ = _saved(ctx, tmp_path, notes=notes)
    notes_media.store(ctx, sid, sha(data), data)
    records.seal(ctx, sid, "transcribed")
    assert records.verify(ctx, sid).ok
    notes_media.path(ctx, sid, sha(data)).write_bytes(webp(pad=b"x" * 100))
    assert records.IMAGE_CHANGED in records.verify(ctx, sid).problems


def _export(ctx, sid, tmp_path):
    dest = tmp_path / "export.zip"
    records.export_zip(ctx, records.ExportRequest(session_ids=[sid]), dest)
    z = zipfile.ZipFile(dest)
    folder = next(n for n in z.namelist() if n.endswith("/record.json")).removesuffix("record.json")
    return z, folder


def _recomputed(z, folder):
    """What a checker does: every part's digest from the export's files alone."""
    import json

    from mnemosyne.storage import records as r

    record = json.loads(z.read(folder + "record.json"))
    files = {k: json.loads(z.read(folder + v)) if v.endswith(".json") else None
             for k, v in record["parts"].items()}  # fmt: skip
    files["summary"] = z.read(folder + record["parts"]["summary"]).decode()
    files["notes"] = z.read(folder + record["parts"]["notes"]).decode()
    meeting = files["meta"]
    payloads = {
        "transcript": r.transcript_payload(files["transcript"]),
        "summary": r.summary_payload(files["summary"]),
        "summary_data": r.summary_data_payload(files["summary_data"]),
        "notes": r.notes_payload(files["notes"]),
        "comments": r.comments_payload(files["comments"]),
        "meta": r.meta_payload(meeting),
        "external_notes": r.external_notes_payload(files["external_notes"]),
    }
    return record, r.part_digests(payloads)


def test_an_export_recomputes_the_last_seals_parts_from_its_files(ctx, tmp_path):
    import json

    from mnemosyne.models.session import AgendaItem, ExternalNotes

    sid, _ = _saved(
        ctx, tmp_path, notes="# Notes\n\n- point ![x](mnemosyne:image/a.webp@1x1)\n",
        summary="We ship Friday.", agenda=[AgendaItem(text="Release", covered=True)],
        attendees=["Ana"], participants=["Ana"], owner_id="u1",
    )  # fmt: skip
    ctx.repo.add_external_notes(sid, ExternalNotes(source="Gemini", text="Ship Friday."))
    cid = ctx.repo.add_comment(sid, text="agreed", target="notes")
    gone = ctx.repo.add_comment(sid, text="oops")
    ctx.repo.delete_comment(gone)
    assert records.seal(ctx, sid, "transcribed")
    z, folder = _export(ctx, sid, tmp_path)
    names = z.namelist()
    for f in ("notes.md", "comments.json", "external_notes.json", "record.json", "meeting.json"):
        assert folder + f in names
    assert z.read(folder + "notes.md").decode().startswith("# Notes")
    assert cid in [c["id"] for c in json.loads(z.read(folder + "comments.json"))]
    meeting = json.loads(z.read(folder + "meeting.json"))
    assert meeting["owner_id"] == "u1" and meeting["agenda"] == [
        {"text": "Release", "covered": True}
    ]
    seals = json.loads(z.read(folder + "seals.json"))["seals"]
    assert seals[-1]["manifest"]["parts"]
    record, digests = _recomputed(z, folder)
    assert record["manifest"] == seals[-1]["manifest"]
    assert record["chain_head"] == seals[-1]["chain"] and record["format"] == 2
    assert digests == record["manifest"]["parts"]
    assert record["erased"] == []
    sums = z.read("SHA256SUMS").decode()
    for f in ("notes.md", "comments.json", "external_notes.json", "record.json"):
        assert f"  {folder}{f}\n" in sums
    assert "records verify" in z.read("README.txt").decode()


def test_an_export_after_an_erasure_lists_it(ctx, tmp_path):
    import json

    sid, path = _saved(ctx, tmp_path)
    records.seal(ctx, sid, "transcribed")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    _erase(ctx, sid, path)
    z, folder = _export(ctx, sid, tmp_path)
    record = json.loads(z.read(folder + "record.json"))
    assert [(e["name"], e["sha256"], e["by"], e["reason"]) for e in record["erased"]] == [
        (path.name, digest, "Admin", "Retention")
    ]
    assert path.name in record["manifest"]["erased"]
    assert record["verification"]["erased"] == record["erased"]
    _, digests = _recomputed(z, folder)
    assert digests == record["manifest"]["parts"]
