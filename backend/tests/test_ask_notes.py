"""Ask answers saved to the vault."""

from datetime import datetime

from mnemosyne.models.ask import Ask, Citation
from mnemosyne.models.session import Session
from mnemosyne.models.transcript import TranscriptSegment


def _seed(ctx, tmp_path, answer="Ships in October [1][2]."):
    vault = tmp_path / "vault"
    vault.mkdir()
    ctx.settings.obsidian_vault_path = str(vault)
    ctx.settings.obsidian_subfolder = "notes"
    s = Session(
        name="Release sync",
        created_at=datetime(2026, 9, 10, 10),
        transcript=[TranscriptSegment(text="talk", speaker="A", start=0, end=5)],
    )
    ctx.repo.save(s)

    def cite(n, start):
        return Citation(
            n=n,
            session_id=s.id,
            session_name="Release sync",
            created_at=s.created_at,
            idx=0 if start is not None else None,
            start=start,
            excerpt="x",
        )

    ask = Ask(
        question='When does it ship? / "soon"',
        answer=answer,
        citations=[cite(1, 65.0), cite(2, None)],
        created_at=datetime(2026, 10, 1, 9),
    )
    ctx.repo.save_ask(ask)
    return vault, ask


def test_ask_note_content_and_rewrite(client, ctx, tmp_path):
    vault, ask = _seed(ctx, tmp_path)
    r = client.post(f"/api/asks/{ask.id}/obsidian")
    assert r.status_code == 200, r.text
    path = vault / "notes" / "asks" / "2026-10-01 When does it ship soon.md"
    assert r.json()["path"] == str(path)
    text = path.read_text()
    assert text.startswith("---\nmnemosyne: ask\ntags: [ask, mnemosyne]\ndate: 2026-10-01\n")
    assert 'question: "When does it ship? / \\"soon\\""' in text
    assert "# When does it ship?" in text
    assert "Ships in October [1][2]." in text
    assert "## Sources" in text
    assert text.count("[[2026-09-10-Release sync|Release sync]]") == 1
    assert "- 2026-09-10 [[2026-09-10-Release sync|Release sync]] (1:05)" in text
    # saving again rewrites our own file
    assert client.post(f"/api/asks/{ask.id}/obsidian").status_code == 200


def test_ask_note_conflict(client, ctx, tmp_path):
    vault, ask = _seed(ctx, tmp_path)
    folder = vault / "notes" / "asks"
    folder.mkdir(parents=True)
    mine = folder / "2026-10-01 When does it ship soon.md"
    mine.write_text("my own note\n")
    r = client.post(f"/api/asks/{ask.id}/obsidian")
    assert r.status_code == 409
    assert mine.read_text() == "my own note\n"


def test_ask_note_without_vault_or_ask(client, ctx, tmp_path):
    vault, ask = _seed(ctx, tmp_path)
    assert client.post("/api/asks/nope/obsidian").status_code == 404
    ctx.settings.obsidian_vault_path = ""
    assert client.post(f"/api/asks/{ask.id}/obsidian").status_code == 400
