"""Obsidian export templates, exporter, and endpoint."""

import os
from datetime import datetime
from pathlib import Path

from mnemosyne.api.routes.export import _tag
from mnemosyne.export.obsidian import ObsidianExporter
from mnemosyne.export.templates import render_meeting_note
from mnemosyne.models.session import Session
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services import notes_media
from mnemosyne.storage.crypto import new_key
from tests.test_notes_media import jpeg, sha, value, webp


def test_render_meeting_note_structure():
    note = render_meeting_note(
        title="Weekly Sync",
        date=datetime(2026, 9, 22, 10, 0),
        participants=["SPEAKER_00", "SPEAKER_01"],
        transcript_segments=[{"speaker": "SPEAKER_00", "start": 61, "text": "hi"}],
        summary="- point",
        notes="my notes",
    )
    assert note.startswith('---\ntitle: "Weekly Sync"\ndate: 2026-09-22\n')
    assert 'participants: ["SPEAKER_00", "SPEAKER_01"]' in note
    assert "## Summary\n\n- point" in note
    assert "## Notes\n\nmy notes" in note
    assert "**[01:01] SPEAKER_00:** hi" in note


def test_render_omits_empty_sections():
    note = render_meeting_note("T", datetime(2026, 1, 1), [], [], "", "")
    for heading in ("## Summary", "## Participants", "## Notes", "## Transcript"):
        assert heading not in note


def test_exporter_writes_sanitized_filename(tmp_path):
    session = Session(
        name='Q3: "plan" / review?',
        created_at=datetime(2026, 9, 22),
        transcript=[TranscriptSegment(text="x", speaker="S", start=0, end=1)],
        participants=["S"],
    )
    path = ObsidianExporter(str(tmp_path), "meetings").export(session)
    assert path == tmp_path / "meetings" / "2026-09-22-Q3 plan  review.md"
    assert path.read_text().startswith("---\n")


def test_exporter_requires_existing_vault(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError):
        ObsidianExporter(str(tmp_path / "missing")).export(Session())


def test_export_endpoint_uses_settings(client, ctx, tmp_path, transcribed_session):
    vault = tmp_path / "vault"
    vault.mkdir()
    resp = client.put(
        "/api/settings", json={"obsidian_vault_path": str(vault), "obsidian_subfolder": "notes"}
    )
    assert resp.json()["obsidian_vault_exists"] is True

    resp = client.post(f"/api/sessions/{transcribed_session['id']}/export/obsidian")
    assert resp.status_code == 200
    assert any(p.suffix == ".md" for p in (vault / "notes").iterdir())


def test_export_without_vault_configured_is_400(client, ctx, transcribed_session):
    ctx.settings.obsidian_vault_path = ""
    resp = client.post(f"/api/sessions/{transcribed_session['id']}/export/obsidian")
    assert resp.status_code == 400


def test_export_missing_vault_is_400(client, ctx, tmp_path, transcribed_session):
    ctx.settings.obsidian_vault_path = str(tmp_path / "missing")
    resp = client.post(f"/api/sessions/{transcribed_session['id']}/export/obsidian")
    assert resp.status_code == 400


def test_block_ids_only_for_items_with_ids():
    from mnemosyne.models.session import ActionItem, SummaryData

    def render(data):
        return render_meeting_note(
            "T", datetime(2026, 1, 1), [], [], "s", "", summary_data=data,
            is_mine=lambda i: i.owner != "Ana",
        )  # fmt: skip

    data = SummaryData(
        decisions=["Go"],
        open_questions=["Why?"],
        action_items=[ActionItem(text="Do it", owner="Me"), ActionItem(text="Ask", owner="Ana")],
    )
    assert "^m-" not in render(data)  # old data: as before
    data.decision_ids, data.question_ids = ["d1"], ["q1"]
    data.action_items[0].id, data.action_items[1].id = "a1", "a2"
    note = render(data)
    lines = note.splitlines()
    for line in (
        "- Go ^m-d1",
        "- Why? ^m-q1",
        "- [ ] Do it ([[Me]]) ^m-a1",
        "- [[Ana]]: Ask ^m-a2",
    ):
        assert line in lines


def test_a_continued_task_is_a_plain_bullet_with_a_link_to_the_later_note():
    from mnemosyne.models.session import ActionItem, SummaryData

    items = [
        ActionItem(id="aaaa0001", text="Update the docs", owner="Ann", due=None),
        ActionItem(id="aaaa0002", text="Send the deck", owner="Ann"),
    ]
    data = SummaryData(action_items=items)
    args = ("T", datetime(2026, 10, 1), [], [], "s", "")
    plain = render_meeting_note(*args, summary_data=data)
    assert "- [ ] Update the docs ([[Ann]]) ^m-aaaa0001\n" in plain
    assert render_meeting_note(*args, summary_data=data, continued={}) == plain
    assert render_meeting_note(*args, summary_data=data, continued={"zzzz": "x"}) == plain
    note = render_meeting_note(
        *args, summary_data=data, continued={"aaaa0001": "2026-10-08-Planning"}
    )
    assert (
        "- Update the docs ([[Ann]]) ↩ continued in [[2026-10-08-Planning]] ^m-aaaa0001\n" in note
    )
    assert "- [ ] Send the deck ([[Ann]]) ^m-aaaa0002\n" in note
    people_off = render_meeting_note(
        *args, summary_data=data, link_people=False, continued={"aaaa0001": "N"}
    )
    assert "- Update the docs (Ann) ↩ continued in [[N]] ^m-aaaa0001\n" in people_off


CHIP_NOTES = (
    "At [12:34](mnemosyne:t/0/754) [@Ada Lovelace](mnemosyne:person/Ada%20Lovelace) agreed;"
    " see [Planning, Oct 14](mnemosyne:meeting/0123456789ab)."
)


def test_notes_chips_in_the_vault_note():
    def render(**kw):
        return render_meeting_note("T", datetime(2026, 1, 1), [], [], "", CHIP_NOTES, **kw)

    def meeting_note(mid: str):
        return ("2026-10-14-Planning", "Planning, Oct 14") if mid == "0123456789ab" else None

    on = render(link_people=True, meeting_note=meeting_note)
    assert (
        "## Notes\n\nAt 12:34 [[Ada Lovelace]] agreed; "
        "see [[2026-10-14-Planning|Planning, Oct 14]]." in on
    )
    off = render(link_people=False, meeting_note=meeting_note)
    assert "At 12:34 @Ada Lovelace agreed; see [[2026-10-14-Planning|Planning, Oct 14]]." in off
    # Meeting not in the vault (or no lookup): its title.
    assert "see Planning, Oct 14." in render(link_people=True)
    assert "mnemosyne:" not in on + off


def test_exporter_passes_the_meeting_lookup_to_the_notes(tmp_path):
    s = Session(name="Now", created_at=datetime(2026, 10, 20, 9, 0), notes=CHIP_NOTES)
    exporter = ObsidianExporter(
        str(tmp_path), meeting_note=lambda _mid: ("2026-10-14-Planning", "Planning")
    )
    note = exporter.render(s)
    assert "[[2026-10-14-Planning|Planning]]" in note and "[[Ada Lovelace]]" in note
    assert "mnemosyne:" not in ObsidianExporter(str(tmp_path), link_people=False).render(s)


# ---- images in the notes (services/notes_media.py) --------------------------------------------


def _image_meeting(ctx, tmp_path, *pictures, caption="Whiteboard"):
    """A saved meeting whose notes embed `pictures` (bytes, stored in its media store), and a vault
    with the settings pointing at it. Returns (session id, attachments folder, note path stem)."""
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    ctx.settings.obsidian_vault_path = str(vault)
    ctx.settings.obsidian_subfolder = "notes"
    session = ctx.repo.save(Session(name="Planning", created_at=datetime(2026, 11, 4, 9, 0)))
    lines = []
    for data in pictures:
        info = notes_media.store(ctx, session.id, sha(data), data)
        lines.append(f"![{caption}](mnemosyne:image/{info.sha256}.{info.ext}@640x480)")
    ctx.sessions.update_notes(session.id, "Intro\n" + "\n".join(lines) + "\n")
    return session.id, vault / "notes" / "attachments", "2026-11-04-Planning"


def _export(client, sid):
    resp = client.post(f"/api/sessions/{sid}/export/obsidian")
    assert resp.status_code == 200, resp.text
    return Path(resp.json()["path"]).read_text(encoding="utf-8")


def test_export_writes_the_notes_images_as_attachments(client, ctx, tmp_path):
    data = webp(pad=b"board")
    sid, attachments, stem = _image_meeting(ctx, tmp_path, data)
    note = _export(client, sid)
    name = f"{stem}-{_tag(sha(data))}.webp"
    assert f"![[{name}|Whiteboard]]" in note
    assert "mnemosyne:" not in note
    assert (attachments / name).read_bytes() == data


def test_export_writes_a_sealed_image_decrypted(client, ctx, tmp_path):
    ctx.master_key = new_key()
    ctx.settings.encrypt_at_rest = True
    data = jpeg(pad=b"secret pixels")
    sid, attachments, stem = _image_meeting(ctx, tmp_path, data)
    assert [p.suffix for p in (ctx.settings.notes_media_dir / sid).iterdir()] == [".enc"]
    note = _export(client, sid)
    name = f"{stem}-{_tag(sha(data))}.jpeg"
    assert f"![[{name}|Whiteboard]]" in note
    assert (attachments / name).read_bytes() == data


def test_export_again_does_not_rewrite_an_unchanged_image(client, ctx, tmp_path):
    data = webp(pad=b"same")
    sid, attachments, stem = _image_meeting(ctx, tmp_path, data)
    _export(client, sid)
    file = attachments / f"{stem}-{_tag(sha(data))}.webp"
    os.utime(file, ns=(1_000_000_000, 1_000_000_000))
    _export(client, sid)
    assert file.stat().st_mtime_ns == 1_000_000_000  # left alone
    # Another size on disk (the user pasted something over it): written again.
    file.write_bytes(b"short")
    _export(client, sid)
    assert file.read_bytes() == data
    assert not list(attachments.glob("*.part"))


def test_export_removes_only_our_stale_files_of_that_stem(client, ctx, tmp_path):
    one, two = webp(pad=b"one"), webp(pad=b"two")
    sid, attachments, stem = _image_meeting(ctx, tmp_path, one, two)
    _export(client, sid)
    stale = attachments / f"{stem}-{_tag(sha(two))}.webp"
    assert stale.exists()
    # The notes drop the second picture; other files sit in the same folder.
    keep = {
        f"{stem}-{'0' * 8}.gif": b"x",  # not our type
        f"{stem}-notes.webp": b"x",  # not our naming
        f"{stem}-{'a' * 8}-extra.webp": b"x",  # a longer name (another note's stem)
        f"2026-11-04-Planning-b-{'a' * 8}.webp": b"x",  # another note's file
        f"2026-11-05-Retro-{'a' * 8}.webp": b"x",  # another note's file
        "diagram.png": b"x",
    }
    for name, body in keep.items():
        (attachments / name).write_bytes(body)
    mine = attachments / f"{stem}-{'c' * 8}.jpeg"  # ours by name, no longer used
    mine.write_bytes(b"old")
    ctx.sessions.update_notes(sid, "Intro\n" + f"![x](mnemosyne:image/{value(one)})\n")
    note = _export(client, sid)
    assert f"![[{stem}-{_tag(sha(one))}.webp|x]]" in note
    assert not stale.exists() and not mine.exists()
    assert (attachments / f"{stem}-{_tag(sha(one))}.webp").exists()
    for name in keep:
        assert (attachments / name).exists(), name
    # No image left in the notes: the last one goes too.
    ctx.sessions.update_notes(sid, "Just words\n")
    _export(client, sid)
    assert not (attachments / f"{stem}-{_tag(sha(one))}.webp").exists()
    for name in keep:
        assert (attachments / name).exists(), name


def test_an_image_whose_bytes_are_not_here_is_the_plain_form(client, ctx, tmp_path):
    data = webp(pad=b"far away")
    sid, attachments, stem = _image_meeting(ctx, tmp_path, data, caption="Far")
    name = f"{stem}-{_tag(sha(data))}.webp"
    notes_media.path(ctx, sid, sha(data)).unlink()  # a row without bytes (synced, not fetched yet)
    # A copy written by an earlier export is kept: the notes still use that image.
    attachments.mkdir(parents=True)
    (attachments / name).write_bytes(data)
    note = _export(client, sid)
    assert "[image: Far]" in note and "![[" not in note and "mnemosyne:" not in note
    assert (attachments / name).exists()


def test_the_preview_shows_images_in_the_plain_form(client, ctx, tmp_path):
    data = webp(pad=b"preview")
    sid, attachments, _ = _image_meeting(ctx, tmp_path, data)
    markdown = client.get(f"/api/sessions/{sid}/export/markdown").json()["markdown"]
    assert "[image: Whiteboard]" in markdown and "mnemosyne:" not in markdown
    assert not attachments.exists()  # a preview writes nothing


def test_the_notes_section_with_embeds_reads_back_like_any_text(client, ctx, tmp_path):
    from mnemosyne.export.note_parse import cut, parse

    data = webp(pad=b"readback")
    sid, _, _ = _image_meeting(ctx, tmp_path, data)
    note = _export(client, sid)
    assert "![[" in note
    rest, _ = cut(note)
    assert "![[" in rest  # the Notes section stays in the text the merge works on
    assert "![[" not in repr(parse(note))  # no projection section picks the embed up
    # Exporting again reads the note back first (ticks, edits): the embed is no trouble.
    assert "![[" in _export(client, sid)


def test_an_attachment_tag_is_never_all_digits():
    """redact_exports would take 8 digits for an account number and break the embed."""
    from mnemosyne.summarization.privacy import redact_identifiers

    digits_first = "12345678" + "9abcdef0" + "0" * 48
    assert _tag(digits_first) == "9abcdef0"
    assert _tag("abcdef01" + "0" * 56) == "abcdef01"
    line = f"![[2026-11-04-Planning-{_tag(digits_first)}.webp|Whiteboard]]"
    assert redact_identifiers(line) == line
