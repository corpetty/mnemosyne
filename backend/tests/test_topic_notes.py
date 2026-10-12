"""Topic notes and the decisions log in the vault."""

from datetime import datetime

from mnemosyne.models.session import ActionItem, Session, SummaryData
from mnemosyne.models.transcript import TranscriptSegment


def _s(name, day, decisions=(), topics=("Waku migration",), items=()):
    return Session(
        name=name,
        created_at=datetime(2026, 9, day, 10),
        transcript=[TranscriptSegment(text="talk", speaker="A", start=0, end=5)],
        summary=f"{name} summary. More.",
        summary_data=SummaryData(
            topics=list(topics),
            decisions=list(decisions),
            action_items=[ActionItem(text=t, owner=o) for t, o in items],
            open_questions=["Is a second rc needed for the migration?"] if day == 10 else [],
        ),
    )


def _seed(ctx, vault):
    vault.mkdir()
    ctx.settings.obsidian_vault_path = str(vault)
    ctx.settings.obsidian_subfolder = "notes"
    a = _s(
        "Release sync",
        10,
        ["Ship the Waku migration"],
        items=[("Update migration docs", "Me"), ("Book migration review", "Zed")],
    )
    b = _s("Infra weekly", 17, ["Move store nodes after the migration"])
    c = _s("Hiring", 20, ["Final round Thursday"], topics=["hiring"])
    for s in (a, b, c):
        ctx.repo.save(s)
        ctx.index.index_session(s.id)


def test_topic_note_content(client, ctx, tmp_path):
    vault = tmp_path / "vault"
    _seed(ctx, vault)
    r = client.post(
        "/api/topics/obsidian", json={"query": "Waku migration", "explanation": "It is on track."}
    )
    assert r.status_code == 200, r.text
    path = vault / "notes" / "topics" / "Waku migration.md"
    assert r.json()["path"] == str(path)
    text = path.read_text()
    assert text.startswith("---\nmnemosyne: topic\n")
    assert "# Waku migration" in text and "It is on track." in text
    assert text.index("2026-09-10") < text.index("2026-09-17")
    assert "### 2026-09-10 [[2026-09-10-Release sync|Release sync]]" in text
    assert "- Ship the Waku migration" in text
    assert "Is a second rc" in text
    assert "Hiring" not in text
    # not the recorder's: plain bullet with owner
    assert "- [ ] Update migration docs (Me)" in text
    assert "- Book migration review (Zed)" in text and "[ ] Book" not in text


def test_topic_note_rewrites_ours_but_not_the_users(client, ctx, tmp_path):
    vault = tmp_path / "vault"
    _seed(ctx, vault)
    assert client.post("/api/topics/obsidian", json={"query": "Waku migration"}).status_code == 200
    again = client.post(
        "/api/topics/obsidian", json={"query": "Waku migration", "explanation": "Second."}
    )
    assert again.status_code == 200
    assert "Second." in (vault / "notes" / "topics" / "Waku migration.md").read_text()

    (vault / "Waku migration.md").write_text("mine")
    (vault / "notes" / "topics" / "Waku migration.md").unlink()
    r = client.post("/api/topics/obsidian", json={"query": "Waku migration"})
    assert r.status_code == 409
    assert not (vault / "notes" / "topics" / "Waku migration.md").exists()

    (vault / "Waku migration.md").unlink()
    (vault / "notes" / "topics" / "Waku migration.md").write_text("my own text")
    assert client.post("/api/topics/obsidian", json={"query": "Waku migration"}).status_code == 409
    assert (vault / "notes" / "topics" / "Waku migration.md").read_text() == "my own text"


def test_topic_note_needs_vault(client, ctx):
    ctx.settings.obsidian_vault_path = ""
    assert client.post("/api/topics/obsidian", json={"query": "anything"}).status_code == 400


def test_decisions_log(client, ctx, tmp_path):
    vault = tmp_path / "vault"
    _seed(ctx, vault)
    (vault / "Store nodes.md").write_text("x")
    ids = [s.id for s in ctx.repo.list_summaries()]
    assert len(ids) == 3

    ctx.settings.obsidian_decisions_log = False
    client.post(f"/api/sessions/{ids[0]}/export/obsidian")
    assert not (vault / "notes" / "Decisions.md").exists()

    ctx.settings.obsidian_decisions_log = True
    client.post(f"/api/sessions/{ids[0]}/export/obsidian")
    text = (vault / "notes" / "Decisions.md").read_text()
    assert text.startswith("---\nmnemosyne: decisions\n")
    assert text.index("2026-09-20") < text.index("2026-09-17") < text.index("2026-09-10")
    assert "## 2026-09-20 [[2026-09-20-Hiring|Hiring]]" in text
    assert "- Final round Thursday" in text


def test_decisions_log_keeps_users_file(client, ctx, tmp_path):
    vault = tmp_path / "vault"
    _seed(ctx, vault)
    (vault / "notes").mkdir()
    (vault / "notes" / "Decisions.md").write_text("my decisions")
    ctx.settings.obsidian_decisions_log = True
    sid = ctx.repo.list_summaries()[0].id
    assert client.post(f"/api/sessions/{sid}/export/obsidian").status_code == 200
    assert (vault / "notes" / "Decisions.md").read_text() == "my decisions"
