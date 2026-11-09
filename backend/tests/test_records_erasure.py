"""Erasure and deletion of a record's audio (services/records.py `seal_erasure`, task 4 of
docs/plans/2026-11-09-records-by-frontier.md): the erasure is sealed before the files go."""

import hashlib
from datetime import datetime, timedelta

from mnemosyne.services import records
from tests.test_combine import _meeting, _wait, needs_ffmpeg
from tests.test_records import _saved


def _here(ctx, tmp_path, name="Weekly", **fields):
    """A saved meeting whose audio is in its own recordings folder, as a recording's is (deleting
    a meeting's audio removes that folder)."""
    sid, path = _saved(ctx, tmp_path, name, **fields)
    if path is None:
        return sid, path
    folder = ctx.settings.recordings_dir / sid
    folder.mkdir(parents=True)
    moved = folder / path.name
    path.replace(moved)
    with ctx.repo._lock, ctx.repo._conn:
        ctx.repo._conn.execute("UPDATE sessions SET audio_file=? WHERE id=?", (str(moved), sid))
        ctx.repo._conn.execute("UPDATE recordings SET path=? WHERE session_id=?", (str(moved), sid))
    return sid, moved


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _old(ctx, sid, days=40):
    with ctx.repo._lock, ctx.repo._conn:
        ctx.repo._conn.execute(
            "UPDATE sessions SET created_at=? WHERE id=?",
            ((datetime.now() - timedelta(days=days)).isoformat(), sid),
        )


def test_a_deleted_audio_is_sealed_first_and_the_record_still_verifies(client, ctx, tmp_path):
    sid, path = _here(ctx, tmp_path)
    digest = _sha(path)
    records.seal(ctx, sid, "transcribed")
    r = client.delete(f"/api/sessions/{sid}/audio", params={"reason": "Client asked"})
    assert r.status_code == 200 and not path.exists()
    seals = ctx.repo.seals(sid)
    assert [s["reason"] for s in seals] == ["transcribed", "audio deleted"]
    erased = seals[-1]["manifest"]["erased"][path.name]
    (row,) = ctx.repo.deletions()
    assert row["what"] == "audio" and row["reason"] == "Client asked"
    assert erased["sha256"] == digest and erased["matched"] is True
    assert erased["reason"] == "Client asked" and erased["by"] == row["by"]
    assert erased["deletion"] in ctx.repo.deletion_ids(sid)
    check = records.verify(ctx, sid)
    assert check.ok, check.problems
    assert [(e.name, e.sha256, e.matched) for e in check.erased] == [(path.name, digest, True)]
    # A later seal carries the erasure along.
    records.seal(ctx, sid, "published")
    assert records.verify(ctx, sid).ok


def test_the_deletion_entry_must_exist_for_an_erasure_to_verify(client, ctx, tmp_path):
    sid, path = _here(ctx, tmp_path)
    records.seal(ctx, sid, "transcribed")
    client.delete(f"/api/sessions/{sid}/audio")
    assert records.verify(ctx, sid).ok
    with ctx.repo._lock, ctx.repo._conn:
        ctx.repo._conn.execute("DELETE FROM deletions WHERE session_id=?", (sid,))
    assert records.verify(ctx, sid).problems == [
        f"The deletion log has no entry for erased audio file {path.name}"
    ]


def test_retention_deletes_logs_and_verifies(ctx, tmp_path):
    sid, path = _here(ctx, tmp_path)
    records.seal(ctx, sid, "transcribed")
    _old(ctx, sid)
    digest = _sha(path)
    ctx.settings.audio_retention_days = 30
    folder = ctx.settings.recordings_dir / sid
    result = ctx.run_retention()
    assert [u.session_id for u in result.sessions] == [sid]
    assert not folder.exists()
    (row,) = ctx.repo.deletions()
    assert (row["what"], row["by"], row["role"]) == ("audio", "retention", "system")
    assert row["reason"] == "older than 30 days" and row["chain_head"]
    check = records.verify(ctx, sid)
    assert check.ok, check.problems
    (erasure,) = check.erased
    assert (erasure.sha256, erasure.by, erasure.reason) == (
        digest,
        "retention",
        "older than 30 days",
    )
    assert ctx.repo.seals(sid)[-1]["manifest"]["erased"][path.name]["deletion"] == (
        next(iter(ctx.repo.deletion_ids(sid)))
    )


def test_a_file_swapped_before_erasure_is_recorded_as_not_matching(client, ctx, tmp_path):
    sid, path = _here(ctx, tmp_path)
    records.seal(ctx, sid, "transcribed")
    sealed = _sha(path)
    path.write_bytes(b"OggS" + b"swapped" * 300)
    swapped = _sha(path)
    # Deleted anyway: retention is policy, and the person asked.
    assert client.delete(f"/api/sessions/{sid}/audio").status_code == 200
    assert not path.exists()
    erased = ctx.repo.seals(sid)[-1]["manifest"]["erased"][path.name]
    assert erased["matched"] is False and erased["sha256"] == swapped != sealed
    check = records.verify(ctx, sid)
    assert not check.ok and not check.erased[0].matched
    assert check.problems == [
        f"Audio file {path.name} no longer matched the record when it was deleted"
    ]


def _add_part(ctx, sid, path, name="part2.ogg", body=b"OggS" + b"p2" * 700):
    """A recording added to the meeting after its last seal (as recording into it again does)."""
    from mnemosyne.models.session import Recording

    extra = path.parent / name
    extra.write_bytes(body)
    session = ctx.repo.get(sid)
    ctx.sessions.set_audio(
        sid,
        session.audio_file,
        [
            *session.recordings,
            Recording(source="mic", device_id=1, device_name="Mic 2", path=str(extra)),
        ],
    )
    return extra


def test_a_part_added_after_the_last_seal_is_entered_then_erased_without_a_problem(
    client, ctx, tmp_path
):
    sid, path = _here(ctx, tmp_path)
    records.seal(ctx, sid, "transcribed")
    extra = _add_part(ctx, sid, path)
    digest = _sha(extra)
    assert client.delete(f"/api/sessions/{sid}/audio").status_code == 200
    assert not extra.exists() and not path.exists()
    seals = ctx.repo.seals(sid)
    assert [s["reason"] for s in seals] == [
        "transcribed",
        records.SEAL_BEFORE_ERASURE,
        "audio deleted",
    ]
    assert extra.name in seals[1]["manifest"]["audio"]  # entered with its digest first
    check = records.verify(ctx, sid)
    assert check.ok, check.problems
    assert {e.name: (e.sha256, e.matched) for e in check.erased} == {
        extra.name: (digest, True),
        path.name: (_sha_of_erased(check, path.name), True),
    }


def _sha_of_erased(check, name):
    return next(e.sha256 for e in check.erased if e.name == name)


def test_a_sealed_file_swapped_while_a_part_is_added_is_still_not_matched(client, ctx, tmp_path):
    sid, path = _here(ctx, tmp_path)
    records.seal(ctx, sid, "transcribed")
    sealed = _sha(path)
    path.write_bytes(b"OggS" + b"swapped" * 300)
    extra = _add_part(ctx, sid, path)
    assert client.delete(f"/api/sessions/{sid}/audio").status_code == 200
    erased = ctx.repo.seals(sid)[-1]["manifest"]["erased"]
    assert erased[path.name]["matched"] is False and erased[path.name]["sha256"] != sealed
    assert erased[extra.name]["matched"] is True
    assert records.verify(ctx, sid).problems == [
        f"Audio file {path.name} no longer matched the record when it was deleted"
    ]


def test_erasing_encrypted_audio_keeps_the_plaintext_digest(client, ctx, tmp_path):
    from tests.test_records import _encrypt

    ctx.master_key = bytes(range(32))
    sid, path = _here(ctx, tmp_path)
    digest = _sha(path)
    records.seal(ctx, sid, "transcribed")
    enc = _encrypt(ctx, sid, path)
    assert records.verify(ctx, sid).ok
    assert client.delete(f"/api/sessions/{sid}/audio").status_code == 200
    assert not enc.exists()
    check = records.verify(ctx, sid)
    assert check.ok, check.problems
    assert [(e.sha256, e.matched) for e in check.erased] == [(digest, True)]


def test_turning_encryption_on_after_a_v1_seal_keeps_the_record_ok(client, ctx, tmp_path):
    from mnemosyne.services import encryption
    from tests.test_records import _encrypt

    ctx.master_key = None
    sid, path = _here(ctx, tmp_path)
    session = ctx.repo.get(sid)
    ctx.repo.add_seal(
        sid, "transcribed", records.content_hash(records.content_of(session)),
        records.audio_hashes(session),
    )  # fmt: skip
    assert records.verify(ctx, sid).format == 1
    digest = _sha(path)
    # The conversion itself needs the whole key setup; what matters here is what is done first.
    ctx.master_key = bytes(range(32))
    encryption._upgrade_record(ctx, sid)
    seals = ctx.repo.seals(sid)
    assert [s["format"] for s in seals] == [1, 2]
    assert seals[-1]["manifest"]["audio"][path.name]["sha256"] == digest
    _encrypt(ctx, sid, path)  # the file as encryption leaves it
    check = records.verify(ctx, sid)
    assert check.ok and check.format == 2, check.problems
    # And a v1 seal that no longer matches is left alone (the problem stays visible).
    other, opath = _here(ctx, tmp_path, "Other")
    session = ctx.repo.get(other)
    ctx.repo.add_seal(
        other, "transcribed", records.content_hash(records.content_of(session)),
        records.audio_hashes(session),
    )  # fmt: skip
    opath.write_bytes(b"OggS" + b"x" * 100)
    encryption._upgrade_record(ctx, other)
    assert [s["format"] for s in ctx.repo.seals(other)] == [1]


def test_erasing_after_a_v1_seal_upgrades_it_first(client, ctx, tmp_path):
    sid, path = _here(ctx, tmp_path)
    session = ctx.repo.get(sid)
    ctx.repo.add_seal(
        sid, "transcribed", records.content_hash(records.content_of(session)),
        records.audio_hashes(session),
    )  # fmt: skip
    assert client.delete(f"/api/sessions/{sid}/audio").status_code == 200
    seals = ctx.repo.seals(sid)
    assert [(s["format"], s["reason"]) for s in seals] == [
        (1, "transcribed"),
        (2, records.UPGRADE_REASON),
        (2, "audio deleted"),
    ]
    check = records.verify(ctx, sid)
    assert check.ok, check.problems
    assert check.erased[0].matched is True


def test_a_meeting_never_sealed_is_sealed_before_its_audio_goes(client, ctx, tmp_path):
    sid, path = _here(ctx, tmp_path)
    assert ctx.repo.seals(sid) == []
    assert client.delete(f"/api/sessions/{sid}/audio").status_code == 200
    assert [s["reason"] for s in ctx.repo.seals(sid)] == [
        records.SEAL_BEFORE_ERASURE,
        "audio deleted",
    ]
    assert records.verify(ctx, sid).ok
    # Audio alone is no record: nothing to seal, the deletion is still logged.
    bare, bpath = _here(ctx, tmp_path, "Bare", transcript=[])
    assert client.delete(f"/api/sessions/{bare}/audio").status_code == 200
    assert not bpath.exists() and ctx.repo.seals(bare) == []
    assert len(ctx.repo.deletion_ids(bare)) == 1


def test_a_deleted_meetings_chain_head_stays_in_the_deletion_log(client, ctx, tmp_path):
    sid, _ = _here(ctx, tmp_path)
    records.seal(ctx, sid, "transcribed")
    head = records.verify(ctx, sid).chain_head
    manifest_hash = ctx.repo.seals(sid)[-1]["content_hash"]
    assert client.delete(f"/api/sessions/{sid}").status_code == 200
    (row,) = [d for d in ctx.repo.deletions() if d["session_id"] == sid]
    assert row["what"] == "meeting"
    assert (row["chain_head"], row["manifest_hash"]) == (head, manifest_hash)
    assert ctx.repo.seals(sid) == []  # the seals went with the meeting; the log names them


def test_a_seal_that_cannot_be_made_refuses_a_manual_deletion(client, ctx, tmp_path):
    ctx.master_key = bytes(range(32))
    sid, path = _here(ctx, tmp_path)
    records.seal(ctx, sid, "transcribed")
    from tests.test_records import _encrypt

    enc = _encrypt(ctx, sid, path)
    ctx.master_key = None  # locked: the file's plaintext cannot be read
    records._plain_digests.clear()
    r = client.delete(f"/api/sessions/{sid}/audio")
    assert r.status_code == 409 and "locked" in r.json()["detail"].lower()
    assert enc.exists() and ctx.repo.get(sid).audio_file  # nothing deleted
    assert ctx.repo.deletions() == [] and len(ctx.repo.seals(sid)) == 1  # nor logged or sealed


def test_a_seal_that_cannot_be_made_skips_the_meeting_in_retention(ctx, tmp_path, monkeypatch):
    ctx.master_key = bytes(range(32))
    ctx.settings.audio_retention_days = 30
    sid, path = _here(ctx, tmp_path, "Stuck")
    records.seal(ctx, sid, "transcribed")
    _old(ctx, sid)
    other, opath = _here(ctx, tmp_path, "Fine")
    records.seal(ctx, other, "transcribed")
    _old(ctx, other)
    stuck = ctx.settings.recordings_dir / sid / path.name
    real = records._plain_entry

    def entry(p, key):
        if p == stuck:
            raise OSError("disk error")
        return real(p, key)

    monkeypatch.setattr(records, "_plain_entry", entry)
    result = ctx.run_retention()
    assert [u.session_id for u in result.sessions] == [other]
    assert stuck.exists() and not (ctx.settings.recordings_dir / other).exists()
    assert ctx.repo.deletion_ids(sid) == set()  # nothing logged for what did not happen
    assert len(ctx.repo.seals(sid)) == 1
    # The next pass, with the disk back, takes it.
    monkeypatch.setattr(records, "_plain_entry", real)
    result = ctx.run_retention()
    assert [u.session_id for u in result.sessions] == [sid]
    assert records.verify(ctx, sid).ok


@needs_ffmpeg
def test_combining_seals_its_target_and_sets_the_replaced_audio_down_as_erased(client, ctx):
    t0 = datetime(2026, 9, 28, 10, 0)
    a = _meeting(ctx, "Planning", 2.0, t0 + timedelta(minutes=10), [("Ana", 0.5, "hello")], {})
    b = _meeting(ctx, "Earlier", 2.0, t0, [("Bob", 0.5, "hi")], {})
    records.seal(ctx, a, "transcribed")
    old = ctx.repo.get(a).audio_file
    job = client.post(f"/api/sessions/{a}/combine", json={"other_id": b}).json()
    done = _wait(client, job["id"])
    assert done["status"] == "completed", done["error"]
    seals = ctx.repo.seals(a)
    assert seals[-1]["reason"] == "combined"
    manifest = seals[-1]["manifest"]
    name = old.rsplit("/", 1)[-1]
    assert manifest["erased"][name]["matched"] is True
    assert manifest["erased"][name]["reason"].startswith("combined with")
    check = records.verify(ctx, a)
    assert check.ok, check.problems
    assert [e.name for e in check.erased] == [name]


def test_nothing_to_erase_when_the_audio_is_not_here(ctx, tmp_path):
    sid, _ = _saved(ctx, tmp_path, "Foreign", origin="peer", audio=False)
    records.seal(ctx, sid, "summarized")
    calls = []
    assert (
        records.seal_erasure(ctx, sid, by="x", reason="y", deletion=lambda: calls.append(1)) is None
    )
    assert calls == [1] and len(ctx.repo.seals(sid)) == 1


def test_deleting_without_the_hook_still_logs(ctx, tmp_path):
    sid, _ = _here(ctx, tmp_path)
    ctx.storage.before_erase = None
    ctx.storage.delete_audio(sid, by="Ana", role="admin", detail="why")
    (row,) = ctx.repo.deletions()
    assert (row["by"], row["role"], row["reason"]) == ("Ana", "admin", "why")


@needs_ffmpeg
def test_combining_a_target_whose_joined_file_was_never_sealed_has_no_problem(client, ctx):
    t0 = datetime(2026, 9, 28, 10, 0)
    a = _meeting(ctx, "Planning", 2.0, t0 + timedelta(minutes=10), [("Ana", 0.5, "hello")], {})
    b = _meeting(ctx, "Earlier", 2.0, t0, [("Bob", 0.5, "hi")], {})
    # Sealed with only the per-source file: the joined mix was added since (never in any seal).
    real = ctx.repo.get(a).audio_file
    ctx.repo.clear_audio(a)
    records.seal(ctx, a, "transcribed")
    ctx.sessions.set_audio(a, real, [])
    job = client.post(f"/api/sessions/{a}/combine", json={"other_id": b}).json()
    done = _wait(client, job["id"])
    assert done["status"] == "completed", done["error"]
    assert records.SEAL_BEFORE_ERASURE in [x["reason"] for x in ctx.repo.seals(a)]
    check = records.verify(ctx, a)
    assert check.ok, check.problems
    assert real.rsplit("/", 1)[-1] in [e.name for e in check.erased]
