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


def _decided(ctx, name, day, decisions):
    return ctx.repo.save(
        Session(
            name=name,
            created_at=datetime(2026, 9, day, 10),
            summary="s",
            summary_data=SummaryData(
                decisions=[t for _, t in decisions], decision_ids=[i for i, _ in decisions]
            ),
        )
    )


def test_decisions_log_shows_the_superseded_chain(client, ctx, tmp_path):
    from mnemosyne.services import decision_links

    vault = tmp_path / "vault"
    vault.mkdir()
    ctx.settings.obsidian_vault_path = str(vault)
    ctx.settings.obsidian_subfolder = "notes"
    ctx.settings.obsidian_decisions_log = True
    _decided(ctx, "Plan one", 1, [("d1", "Ship in October"), ("d2", "Hire a designer")])
    m2 = _decided(ctx, "Plan two", 8, [("e1", "Ship in October"), ("e2", "Use Rust")])
    m3 = _decided(ctx, "Plan three", 15, [("f1", "Ship in November")])
    decision_links.link_items(ctx, m2.id)  # same words
    decision_links.link_pair(ctx, (m2.id, "e1"), (m3.id, "f1"))  # a superseding wording
    assert client.post(f"/api/sessions/{m3.id}/export/obsidian").status_code == 200
    lines = (vault / "notes" / "Decisions.md").read_text().splitlines()
    assert lines[lines.index("## 2026-09-15 [[2026-09-15-Plan three|Plan three]]") + 2 :][:3] == [
        "- Ship in November",
        "  - earlier: Ship in October ([[2026-09-08-Plan two]])",
        "  - earlier: Ship in October ([[2026-09-01-Plan one]])",
    ]
    two = lines.index("## 2026-09-08 [[2026-09-08-Plan two|Plan two]]")
    assert lines[two + 2 : two + 4] == [
        "- Ship in October → superseded 2026-09-15 in [[2026-09-15-Plan three]]",
        "- Use Rust",  # unlinked: exactly as before
    ]
    one = lines.index("## 2026-09-01 [[2026-09-01-Plan one|Plan one]]")
    assert lines[one + 2 : one + 4] == [
        "- Ship in October → superseded 2026-09-08 in [[2026-09-08-Plan two]]",
        "- Hire a designer",
    ]


def test_decisions_log_without_links_is_unchanged(ctx):
    from mnemosyne.export.topic_notes import render_decisions

    m = _decided(ctx, "Solo", 3, [("d1", "Do it")])
    text = render_decisions([(m, m.summary_data.decisions)], index={})
    assert text == render_decisions([(m, m.summary_data.decisions)])
    assert "- Do it\n" in text and "superseded" not in text and "earlier" not in text
