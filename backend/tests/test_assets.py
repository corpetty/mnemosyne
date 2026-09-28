"""Resources: a library of links and files attached to meetings (services/assets.py)."""

import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from mnemosyne.api.app import create_app
from mnemosyne.services.assets import extract_text, link_title, links_in, resources_hint


def test_links_are_shared_between_meetings(client, ctx):
    a = ctx.sessions.create_session("Kickoff").id
    b = ctx.sessions.create_session("Review").id
    doc = client.post(
        "/api/assets/link", json={"url": "https://docs.example.com/d/plan", "session_id": a}
    ).json()
    assert doc["title"] == "docs.example.com/plan" and doc["kind"] == "link"
    same = client.post("/api/assets/link", json={"url": "https://docs.example.com/d/plan"}).json()
    assert same["id"] == doc["id"]  # one library entry per address
    # A week later: attach it from the library to another meeting.
    lib = client.get("/api/assets", params={"q": "plan"}).json()
    assert [(x["asset"]["id"], x["used"]) for x in lib] == [(doc["id"], 1)]
    assert [
        x["id"]
        for x in client.post(f"/api/sessions/{b}/assets", json={"asset_id": doc["id"]}).json()
    ] == [doc["id"]]
    assert client.get("/api/assets").json()[0]["used"] == 2
    assert client.delete(f"/api/sessions/{a}/assets/{doc['id']}").json() == []
    assert (
        client.patch(f"/api/assets/{doc['id']}", json={"title": "The plan"}).json()["title"]
        == "The plan"
    )
    assert client.delete(f"/api/assets/{doc['id']}").status_code == 200
    assert client.get(f"/api/sessions/{b}").json()["assets"] == []
    assert client.post("/api/assets/link", json={"url": "not a link"}).status_code == 400


def test_a_file_is_kept_read_and_encrypted(settings, keystore, tmp_path):
    app = create_app(settings, keystore=keystore)
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={"name": "Design"}).json()["id"]
        up = client.post(
            "/api/assets/file",
            files={"file": ("notes.md", b"# Launch\nShip on the 20th.", "text/markdown")},
            data={"session_id": sid},
        ).json()
        assert up["kind"] == "file" and up["has_text"] and up["size"] == 26
        assert client.get(f"/api/assets/{up['id']}/file").content == b"# Launch\nShip on the 20th."
        client.post("/api/encryption/enable")  # existing files get encrypted
        stored = list((settings.data_dir / "assets" / up["id"]).iterdir())
        assert [p.suffix for p in stored] == [".enc"]
        assert b"Launch" not in stored[0].read_bytes()
        assert client.get(f"/api/assets/{up['id']}/file").content.startswith(b"# Launch")
        new = client.post(
            "/api/assets/file",
            files={"file": ("deck.bin", b"\x00\x01", "application/octet-stream")},
        ).json()
        assert not new["has_text"]  # not a text file
        assert next((settings.data_dir / "assets" / new["id"]).iterdir()).suffix == ".enc"
        hint = resources_hint(app.state.ctx, sid)
        assert "- notes.md" in hint and "Ship on the 20th." in hint
        client.post("/api/encryption/disable")
        assert next((settings.data_dir / "assets" / up["id"]).iterdir()).name == "notes.md"


def test_text_from_word_files(tmp_path):
    docx = tmp_path / "a.docx"
    with zipfile.ZipFile(docx, "w") as z:
        z.writestr(
            "word/document.xml",
            "<w:document><w:body><w:p><w:r><w:t>First point</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>Second</w:t></w:r></w:p></w:body></w:document>",
        )
    assert extract_text(docx, "a.docx") == "First point\nSecond"
    assert extract_text(tmp_path, "photo.jpg") is None


def test_calendar_links_but_not_the_join_link():
    text = (
        "Join: https://meet.google.com/abc-defg-hij\nDeck: https://docs.google.com/presentation/d/1x.\n"
        "Zoom https://us02web.zoom.us/j/123 and https://github.com/org/repo/issues/7"
    )
    assert links_in(text) == [
        "https://docs.google.com/presentation/d/1x",
        "https://github.com/org/repo/issues/7",
    ]
    assert link_title("https://github.com/org/repo/issues/7") == "github.com/7"


def test_resources_in_the_note_and_the_backup(client, ctx, tmp_path):
    sid = ctx.sessions.create_session("Review").id
    client.post(
        "/api/assets/link",
        json={"url": "https://x.example/spec", "title": "Spec", "session_id": sid},
    )
    f = client.post(
        "/api/assets/file",
        files={"file": ("plan.txt", b"plan", "text/plain")},
        data={"session_id": sid},
    ).json()
    vault = tmp_path / "vault"
    vault.mkdir()
    ctx.settings.obsidian_vault_path = str(vault)
    note = Path(client.post(f"/api/sessions/{sid}/export/obsidian").json()["path"]).read_text()
    assert "## Resources\n\n- [Spec](https://x.example/spec)\n- [[" in note
    copied = vault / ctx.settings.obsidian_subfolder / "attachments" / f"{f['id']}-plan.txt"
    assert copied.read_bytes() == b"plan"

    job = client.post("/api/backup").json()
    import time

    while (done := client.get(f"/api/jobs/{job['id']}").json())["status"] not in (
        "completed",
        "failed",
    ):
        time.sleep(0.05)
    assert done["status"] == "completed", done["error"]
    name = client.get("/api/backup").json()["backups"][0]["name"]
    import tarfile

    with tarfile.open(Path(ctx.settings.backup_dir) / name) as tar:
        assert any(n.startswith(f"assets/{f['id']}/") for n in tar.getnames())
