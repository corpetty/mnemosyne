"""The standalone export checker (services/records_check.py, `records verify`)."""

import hashlib
import json
import zipfile

from mnemosyne.services import records, records_check
from mnemosyne.storage import records as r


def _saved(ctx, tmp_path, name="Weekly", **fields):
    from mnemosyne.models.session import Recording, Session, TranscriptSegment

    path = tmp_path / f"{name}.ogg"
    path.write_bytes(b"OggS" + name.encode() * 500)
    fields.setdefault(
        "transcript", [TranscriptSegment(text="We ship Friday.", speaker="Ana", start=0, end=3)]
    )
    s = Session(
        name=name,
        audio_file=str(path),
        recordings=[Recording(source="mic", device_id=0, device_name="Mic", path=str(path))],
        notes="# Notes\n\n- point\n",
        summary="We ship Friday.",
        **fields,
    )
    ctx.repo.save(s)
    return s.id, path


def _export(ctx, sid, tmp_path, name="export.zip"):
    dest = tmp_path / name
    records.export_zip(ctx, records.ExportRequest(session_ids=[sid]), dest)
    return dest


def _tampered(src, dest, suffix, mutate=lambda b: bytes([b[0] ^ 1]) + b[1:], resum=False):
    """Copy a zip, changing the file ending in `suffix` (and optionally its SHA256SUMS line)."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dest, "w") as zout:
        target = next(n for n in zin.namelist() if n.endswith(suffix))
        new = mutate(zin.read(target))
        for n in zin.namelist():
            data = zin.read(n)
            if n == target:
                data = new
            elif n == "SHA256SUMS" and resum:
                old = hashlib.sha256(zin.read(target)).hexdigest()
                data = data.decode().replace(old, hashlib.sha256(new).hexdigest()).encode()
            zout.writestr(n, data)
    return dest


def _problems(result):
    return [p for m in result.meetings for p in m.problems] + result.problems


def test_a_fresh_export_passes(ctx, tmp_path):
    sid, _ = _saved(ctx, tmp_path)
    records.seal(ctx, sid, "transcribed")
    result = records_check.check_export(_export(ctx, sid, tmp_path))
    assert result.ok, _problems(result)
    assert len(result.meetings) == 1 and result.meetings[0].format == 2
    text = records_check.format_text(result)
    assert ": ok (" in text and "1 meetings checked, 1 ok, 0 with problems" in text
    assert records_check.check_export(_export(ctx, sid, tmp_path)).to_json()["ok"] is True


def test_one_changed_byte_is_named_in_every_file(ctx, tmp_path):
    sid, path = _saved(ctx, tmp_path)
    records.seal(ctx, sid, "transcribed")
    good = _export(ctx, sid, tmp_path)
    for suffix in ("transcript.json", "notes.md", f"audio/{path.name}", "seals.json"):
        bad = _tampered(good, tmp_path / "bad.zip", "/" + suffix)
        result = records_check.check_export(bad)
        assert not result.ok
        assert any(suffix in p and "SHA256SUMS" in p for p in _problems(result)), suffix
        # Even with SHA256SUMS fixed up, the record itself catches it.
        fixed = _tampered(good, tmp_path / "fixed.zip", "/" + suffix, resum=True)
        result = records_check.check_export(fixed)
        assert not result.ok, suffix
        assert not any("SHA256SUMS" in p for p in _problems(result)), suffix
        if suffix == "seals.json":
            assert any(
                "Seal 1" in p or "seals" in p or "record.json" in p for p in _problems(result)
            )
        elif suffix.startswith("audio/"):
            assert any(path.name in p and "differs" in p for p in _problems(result))
        else:
            part = suffix.split(".")[0]
            named = (f"The {part}", suffix)  # differs, or no longer readable
            assert any(n in p for p in _problems(result) for n in named), _problems(result)


def test_an_export_after_an_erasure_passes_and_lists_it(ctx, tmp_path):
    sid, path = _saved(ctx, tmp_path)
    records.seal(ctx, sid, "transcribed")
    last = ctx.repo.seals(sid)[-1]["manifest"]
    data = path.read_bytes()
    measured = {path.name: r.audio_entry(hashlib.sha256(data).hexdigest(), len(data))}
    audio, erased = r.erase_audio(
        last["audio"], last["erased"], measured, at="2026-11-09T10:00:00", by="Admin",
        reason="Retention", deletion=None,
    )  # fmt: skip
    path.unlink()
    manifest = r.manifest_for(
        ctx.repo.get(sid), ctx.repo.list_comments(sid), rev=ctx.repo.sync_seq_of(sid),
        audio=audio, erased=erased,
    )  # fmt: skip
    ctx.repo.add_seal(sid, "audio deleted", manifest=manifest)
    result = records_check.check_export(_export(ctx, sid, tmp_path))
    assert result.ok, _problems(result)
    [e] = result.meetings[0].erased
    assert (e.name, e.by, e.reason) == (path.name, "Admin", "Retention")
    assert e.sha256 == hashlib.sha256(data).hexdigest()
    assert f"erased: {path.name}" in records_check.format_text(result)


def test_pending_changes_are_a_note_not_a_failure(ctx, tmp_path):
    sid, _ = _saved(ctx, tmp_path)
    records.seal(ctx, sid, "transcribed")
    ctx.repo.add_comment(sid, text="after the seal")
    result = records_check.check_export(_export(ctx, sid, tmp_path))
    assert result.ok, _problems(result)
    assert result.meetings[0].notes == [f"comments: {records_check.NOT_COVERED}"]


def test_unlisted_files_a_broken_chain_and_unreadable_exports(ctx, tmp_path, capsys):
    sid, _ = _saved(ctx, tmp_path)
    records.seal(ctx, sid, "transcribed")
    records.seal(ctx, sid, "published")
    good = _export(ctx, sid, tmp_path)

    def drop_prev(b):
        doc = json.loads(b)
        doc["seals"][1]["prev"] = "0" * 64
        return json.dumps(doc).encode()

    bad = _tampered(good, tmp_path / "bad.zip", "/seals.json", drop_prev, resum=True)
    assert any("Seal 2" in p for p in _problems(records_check.check_export(bad)))

    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"not a zip")
    assert records_check.main([str(junk)]) == 2
    assert records_check.main([str(tmp_path / "missing.zip")]) == 2
    assert records_check.main([str(good), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["meetings"][0]["format"] == 2
    assert records_check.main([str(bad)]) == 1


def test_an_unpacked_folder_is_checked_too(ctx, tmp_path):
    sid, _ = _saved(ctx, tmp_path)
    records.seal(ctx, sid, "transcribed")
    folder = tmp_path / "unpacked"
    zipfile.ZipFile(_export(ctx, sid, tmp_path)).extractall(folder)
    assert records_check.check_export(folder).ok
