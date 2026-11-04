"""Images in the notes (services/notes_media.py, api/routes/notes_media.py): the upload checks,
who may upload and read, sealed storage, the fold hook, the sweep of unused blobs. The sidecar is
the `notes doc` stand-in of tests/fake_link.py, whose answers carry `media`."""

import base64
import hashlib
import json
import sqlite3
import struct
import time
from datetime import datetime, timedelta

import pytest

from mnemosyne.api.routes import notes_media as media_routes
from mnemosyne.models.session import Session
from mnemosyne.services import notes_media, shared_notes, sharing, sync_notes
from mnemosyne.storage.crypto import new_key
from mnemosyne.storage.sqlite import SessionRepository
from tests import fake_link
from tests.test_shared_notes import _team_meeting, team  # noqa: F401 (a fixture)


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    return fake_link.install(tmp_path, monkeypatch)


def webp(w: int = 640, h: int = 480, pad: bytes = b"") -> bytes:
    """A WebP header (VP8X) the sniffer and the size reader accept."""
    body = b"VP8X" + struct.pack("<I", 10) + b"\0\0\0\0"
    body += (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little") + pad
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body


def jpeg(w: int = 800, h: int = 600, pad: bytes = b"") -> bytes:
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0\x00\x11\x08" + struct.pack(">HH", h, w) + b"\x03" + b"\x01\x22\x00" * 3
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9" + pad


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def value(data: bytes, ext: str = "webp", w: int = 640, h: int = 480) -> str:
    return f"{sha(data)}.{ext}@{w}x{h}"


def line(v: str, caption: str = "") -> str:
    return f"![{caption}](mnemosyne:image/{v})\n"


def meeting(ctx, notes: str = "") -> str:
    return ctx.repo.save(Session(name="Planning", notes=notes)).id


def put(client, sid: str, data: bytes, kind: str = "image/webp", digest: str | None = None, **kw):
    return client.put(
        f"/api/sessions/{sid}/notes/media/{digest or sha(data)}",
        content=data,
        headers={"Content-Type": kind, **kw.pop("headers", {})},
        **kw,
    )


def age(ctx, sid: str, digest: str, days: float) -> None:
    """Make a row look last seen `days` ago."""
    at = (datetime.now() - timedelta(days=days)).isoformat()
    with ctx.repo._lock, ctx.repo._conn:
        ctx.repo._conn.execute(
            "UPDATE notes_media SET last_seen_at=? WHERE session_id=? AND sha256=?",
            (at, sid, digest),
        )


def row(ctx, sid: str, digest: str) -> dict | None:
    return next((r for r in ctx.repo.media_rows(sid) if r["sha256"] == digest), None)


def wait_for(check, timeout: float = 5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if result := check():
            return result
        time.sleep(0.02)
    raise AssertionError("timed out")


# ---- values and bytes -------------------------------------------------------------------------


def test_values_types_and_sizes():
    v = "a" * 64 + ".webp@640x480"
    assert notes_media.parse_value(v) == ("a" * 64, "webp", 640, 480)
    for bad in (
        "A" * 64 + ".webp@1x1",
        "a" * 64 + ".png@1x1",
        "a" * 64 + ".webp@0x1",
        "a" * 64 + ".webp@123456x1",
        "a" * 63 + ".jpeg@1x1",
        None,
    ):
        assert notes_media.parse_value(bad) is None
    md = "# Notes\n" + line(v, "a \\] caption") + "![x](https://example.com/a.png)\n" + line(v)
    md += line("b" * 64 + ".jpeg@2x3") + line("not-a-value")
    assert notes_media.values_in(md) == [v, "b" * 64 + ".jpeg@2x3"]
    assert notes_media.sniff(webp()) == "webp" and notes_media.sniff(jpeg()) == "jpeg"
    assert notes_media.sniff(b"\x89PNG\r\n\x1a\n" + b"\0" * 20) is None
    assert notes_media.dimensions(webp(1920, 1080), "webp") == (1920, 1080)
    assert notes_media.dimensions(jpeg(800, 600), "jpeg") == (800, 600)
    assert notes_media.dimensions(b"RIFF\0\0\0\0WEBPjunk", "webp") is None


# ---- upload and read ----------------------------------------------------------------------------


def test_upload_read_and_the_event(client, ctx):
    sid = meeting(ctx)
    seen: list[dict] = []
    publish = ctx.bus.publish
    ctx.bus.publish = lambda e: (seen.append(e), publish(e))
    before = ctx.repo.current_sync_seq()
    data = webp(320, 200, b"x" * 100)
    r = put(client, sid, data)
    assert r.status_code == 201, r.text
    assert r.json() == {"sha256": sha(data), "ext": "webp", "size": len(data)}
    assert ctx.repo.current_sync_seq() > before  # a new version of the meeting
    stored = row(ctx, sid, sha(data))
    assert (stored["ext"], stored["width"], stored["height"], stored["size"], stored["by"]) == (
        "webp",
        320,
        200,
        len(data),
        "",
    )
    assert (ctx.settings.notes_media_dir / sid / f"{sha(data)}.webp").read_bytes() == data
    wait_for(lambda: {"type": "notes_media", "session_id": sid, "sha256": sha(data)} in seen)

    again = put(client, sid, data)
    assert again.status_code == 200 and again.json()["sha256"] == sha(data)
    assert len(ctx.repo.media_rows(sid)) == 1

    got = client.get(f"/api/sessions/{sid}/notes/media/{sha(data)}")
    assert got.status_code == 200 and got.content == data
    assert got.headers["content-type"] == "image/webp"
    assert got.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert got.headers["content-length"] == str(len(data))

    pic = jpeg(pad=b"y")
    assert put(client, sid, pic, "image/jpeg").json()["ext"] == "jpeg"
    got = client.get(f"/api/sessions/{sid}/notes/media/{sha(pic)}")
    assert got.headers["content-type"] == "image/jpeg" and got.content == pic

    assert client.get(f"/api/sessions/{sid}/notes/media/{'c' * 64}").status_code == 404
    assert client.get(f"/api/sessions/{sid}/notes/media/nope").status_code == 404


def test_upload_checks(client, ctx, monkeypatch):
    sid = meeting(ctx)
    data = webp()
    assert put(client, sid, data, digest="abc").status_code == 400
    assert put(client, sid, data, digest="A" * 64).status_code == 400
    assert put(client, sid, data, digest="d" * 64).status_code == 400  # not its hash
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 40
    assert put(client, sid, png, "image/png").status_code == 415
    assert put(client, sid, png, "image/jpeg").status_code == 415  # wrong magic
    assert put(client, sid, data, "image/jpeg").status_code == 415  # says jpeg, is webp
    big = jpeg(pad=b"z" * notes_media.IMAGE_MAX_BYTES)
    assert put(client, sid, big, "image/jpeg").status_code == 413
    assert put(client, "nosuchmeeting", data).status_code == 404
    assert ctx.repo.media_rows(sid) == []

    # The meeting's total, the new image included.
    monkeypatch.setattr(notes_media, "MEETING_MEDIA_MAX_BYTES", 300)
    first, second = webp(pad=b"1" * 100), webp(pad=b"2" * 200)
    assert put(client, sid, first).status_code == 201
    r = put(client, sid, second)
    assert r.status_code == 413 and "50 MB" in r.json()["detail"]
    assert put(client, sid, first).status_code == 200  # what is here still answers
    assert not (ctx.settings.notes_media_dir / sid / f"{sha(second)}.webp").exists()


def test_a_meeting_shared_with_me_sends_images_to_its_owner(client, ctx, monkeypatch):
    sid = meeting(ctx)
    monkeypatch.setattr(media_routes.shared_notes, "may_edit", lambda *_: True)
    monkeypatch.setattr(media_routes.shared_with_me, "shared_from", lambda *_: "a" * 64)
    r = put(client, sid, webp())
    assert r.status_code == 409 and "owner" in r.json()["detail"]


def test_readers_load_and_editors_upload(team):  # noqa: F811
    client, ctx, people = team
    ann, bob = people["Ann"], people["Bob"]
    sid = _team_meeting(ctx, ann[0])
    sharing.set_shares(ctx, sid, {bob[0].id})

    def auth(who):
        return {"headers": {"Authorization": f"Bearer {people[who][1]}"}}

    data = webp(pad=b"team")
    r = put(client, sid, data, **auth("Ann"))
    assert r.status_code == 201, r.text
    assert row(ctx, sid, sha(data))["by"] == ann[0].id
    shared = webp(pad=b"bob")
    assert put(client, sid, shared, **auth("Bob")).status_code == 201  # shared with: edits
    url = f"/api/sessions/{sid}/notes/media/{sha(data)}"
    assert put(client, sid, webp(pad=b"rita"), **auth("Rita")).status_code == 403  # reviewer
    assert client.get(url, **auth("Rita")).content == data  # ... reads
    assert put(client, sid, webp(pad=b"cat"), **auth("Cat")).status_code == 404  # hidden
    assert client.get(url, **auth("Cat")).status_code == 404
    assert client.get(url, **auth("Bob")).status_code == 200


def test_sealed_when_encryption_is_on(client, ctx):
    sid = meeting(ctx)
    ctx.master_key = new_key()
    ctx.settings.encrypt_at_rest = True
    data = jpeg(pad=b"secret pixels")
    assert put(client, sid, data, "image/jpeg").status_code == 201
    folder = ctx.settings.notes_media_dir / sid
    assert [p.name for p in folder.iterdir()] == [f"{sha(data)}.jpeg.enc"]
    assert b"secret pixels" not in (folder / f"{sha(data)}.jpeg.enc").read_bytes()
    url = f"/api/sessions/{sid}/notes/media/{sha(data)}"
    got = client.get(url)
    assert got.content == data and got.headers["content-type"] == "image/jpeg"
    assert notes_media.read(ctx, sid, sha(data)) == data
    assert put(client, sid, data, "image/jpeg").status_code == 200
    ctx.master_key = None  # the key is gone
    assert client.get(url).status_code == 423


# ---- the fold hook ------------------------------------------------------------------------------


def test_the_fold_hook_touches_rows_and_reports_missing_bytes(client, ctx, sidecar, monkeypatch):
    sid = meeting(ctx)
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(notes_media, "MISSING_HOOKS", [lambda app, s, v: calls.append((s, v))])
    data = webp(pad=b"hook")
    assert put(client, sid, data).status_code == 201
    age(ctx, sid, sha(data), 6)
    absent = value(b"never uploaded", "jpeg", 10, 20)
    notes = "# Notes\n" + line(value(data), "The board") + line(absent) + line(value(data))
    r = client.post(f"/api/sessions/{sid}/notes", json={"notes": notes})
    assert r.status_code == 200, r.text
    assert "mnemosyne:image/" in r.json()["notes"]  # the markdown keeps the lines
    seen_at = datetime.fromisoformat(row(ctx, sid, sha(data))["last_seen_at"])
    assert datetime.now() - seen_at < timedelta(minutes=1)
    assert calls == [(sid, [absent])]
    assert notes_media.missing(ctx, sid) == [absent]
    assert sidecar.doc_requests[-1]["op"] == "set_text"


def test_an_adopted_document_from_sync_is_seen(client, ctx, sidecar, monkeypatch):
    sid = meeting(ctx)
    calls: list[list[str]] = []
    monkeypatch.setattr(notes_media, "MISSING_HOOKS", [lambda app, s, v: calls.append(v)])
    v = value(b"on the other computer")
    state = {"frames": [{"id": "x", "ops": [["set", "Notes\n" + line(v)]]}], "artifacts": []}
    theirs = base64.b64encode(json.dumps(state).encode()).decode()
    with shared_notes.hub(ctx).lock(sid):
        assert sync_notes.adopt(ctx, sid, theirs, "Desk")
    assert calls == [[v]]


def test_seen_ignores_bad_values_and_a_sidecar_without_media(ctx, monkeypatch):
    sid = meeting(ctx)
    calls = []
    monkeypatch.setattr(
        notes_media, "MISSING_HOOKS", [lambda *a: 1 / 0, lambda *a: calls.append(a)]
    )
    notes_media.seen(ctx, sid, None)
    notes_media.seen(ctx, sid, ["junk", 3, None])
    assert calls == []
    v = value(b"x")
    notes_media.seen(ctx, sid, [v, v])  # a failing hook does not stop the next one
    assert calls == [(ctx, sid, [v])]


def test_stored_hooks_hear_new_blobs_only(client, ctx, monkeypatch):
    sid = meeting(ctx)
    calls = []
    monkeypatch.setattr(notes_media, "ON_STORED", [lambda app, s, info: calls.append(info)])
    data = webp(pad=b"stored")
    put(client, sid, data)
    put(client, sid, data)
    assert [(i.sha256, i.created) for i in calls] == [(sha(data), True)]


# ---- the sweep ----------------------------------------------------------------------------------


def test_the_sweep_deletes_only_what_no_document_shows(client, ctx, sidecar):
    shown_sid, other_sid = meeting(ctx), meeting(ctx)
    shown, unused, fresh = webp(pad=b"shown"), webp(pad=b"unused"), webp(pad=b"fresh")
    in_text = webp(pad=b"text")
    for data in (shown, unused, fresh):
        assert put(client, shown_sid, data).status_code == 201
    assert put(client, other_sid, in_text).status_code == 201
    r = client.post(f"/api/sessions/{shown_sid}/notes", json={"notes": line(value(shown))})
    assert r.status_code == 200
    # Nobody has opened the meetings for 8 days: no fold touched the rows since.
    for sid, data in ((shown_sid, shown), (shown_sid, unused), (other_sid, in_text)):
        age(ctx, sid, sha(data), 8)
    age(ctx, shown_sid, sha(fresh), 1)
    # A meeting without a document whose notes show the image (written around one) keeps it.
    ctx.repo.update_fields(other_sid, notes=line(value(in_text)))

    assert notes_media.sweep(ctx) == 1
    assert {r["sha256"] for r in ctx.repo.media_rows(shown_sid)} == {sha(shown), sha(fresh)}
    assert notes_media.path(ctx, shown_sid, sha(unused)) is None
    assert notes_media.path(ctx, shown_sid, sha(shown)) is not None
    seen_at = datetime.fromisoformat(row(ctx, shown_sid, sha(shown))["last_seen_at"])
    assert datetime.now() - seen_at < timedelta(minutes=1)  # touched by the sweep's fold
    assert row(ctx, other_sid, sha(in_text)) is not None
    # Eight days later the fresh one is unused too.
    assert notes_media.sweep(ctx, datetime.now() + timedelta(days=8)) == 1
    assert {r["sha256"] for r in ctx.repo.media_rows(shown_sid)} == {sha(shown)}


def test_no_sweep_without_knowledge(client, ctx, sidecar, monkeypatch):
    sid = meeting(ctx)
    data = webp(pad=b"keep")
    put(client, sid, data)
    client.post(f"/api/sessions/{sid}/notes", json={"notes": "Nothing here\n"})
    age(ctx, sid, sha(data), 30)
    real = shared_notes._run_doc

    def old_sidecar(binary, request):  # an older sidecar: answers without `media`
        reply = real(binary, request)
        reply.pop("media", None)
        return reply

    monkeypatch.setattr(shared_notes, "_run_doc", old_sidecar)
    assert notes_media.sweep(ctx) == 0
    monkeypatch.setattr(shared_notes, "_run_doc", real)
    monkeypatch.setattr(shared_notes, "available", lambda: False)  # no sidecar at all
    assert notes_media.sweep(ctx) == 0
    assert row(ctx, sid, sha(data)) is not None
    monkeypatch.setattr(shared_notes, "available", lambda: True)
    assert notes_media.sweep(ctx) == 1
    assert ctx.repo.media_rows(sid) == []


# ---- the folder ---------------------------------------------------------------------------------


def test_move_and_remove_a_meetings_images(client, ctx):
    src, dst = meeting(ctx), meeting(ctx)
    a, b = webp(pad=b"a"), webp(pad=b"b")
    put(client, src, a)
    put(client, src, b)
    put(client, dst, b)
    assert notes_media.move_session(ctx, src, dst) == 1
    assert ctx.repo.media_rows(src) == [] and not (ctx.settings.notes_media_dir / src).exists()
    assert {r["sha256"] for r in ctx.repo.media_rows(dst)} == {sha(a), sha(b)}
    assert notes_media.read(ctx, dst, sha(a)) == a
    assert notes_media.total_bytes(ctx, dst) == len(a) + len(b)
    assert ctx.repo.media_total(dst) == len(a) + len(b)
    notes_media.remove_session(ctx, dst)
    assert notes_media.path(ctx, dst, sha(a)) is None
    with pytest.raises(ValueError):
        notes_media.path(ctx, "..", sha(a))


def test_an_old_database_gains_the_table(tmp_path):
    path = tmp_path / "db.sqlite"
    SessionRepository(path).close()
    raw = sqlite3.connect(path)
    raw.execute("DROP TABLE notes_media")
    raw.commit()
    raw.close()
    repo = SessionRepository(path)
    sid = repo.save(Session(name="Old")).id
    assert repo.put_media_row(sid, "a" * 64, "webp", 1, 1, 10)
    assert repo.media_total(sid) == 10
    assert not repo.put_media_row("nosuchmeeting", "a" * 64, "webp", 1, 1, 10)
    repo.delete(sid)
    raw = sqlite3.connect(path)
    assert raw.execute("SELECT count(*) FROM notes_media").fetchone()[0] == 0  # cascade
    raw.close()
    repo.close()
