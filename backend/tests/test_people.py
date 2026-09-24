"""People across meetings."""

from datetime import datetime

from mnemosyne.models.session import ActionItem, Session, SummaryData
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services.people import is_person


def _seg(speaker, start, end):
    return TranscriptSegment(text="words here", speaker=speaker, start=start, end=end)


def _seed(ctx):
    a = Session(
        name="Release sync",
        created_at=datetime(2026, 9, 21, 10),
        participants=["Alice", "SPEAKER_01", "Me"],
        attendees=["alice", "Bob"],
        transcript=[_seg("Alice", 0, 30), _seg("SPEAKER_01", 30, 40), _seg("Me", 40, 50)],
        summary="s",
        summary_data=SummaryData(
            decisions=["Ship in October"],
            action_items=[
                ActionItem(text="Update docs", owner="Alice"),
                ActionItem(text="Tag rc1", owner="alice", done=True),
                ActionItem(text="Book room", owner="SPEAKER_01"),
            ],
        ),
    )
    b = Session(
        name="Hiring",
        created_at=datetime(2026, 9, 23, 14),
        participants=["Carol"],
        attendees=["Bob"],
        transcript=[_seg("Carol", 0, 10)],
    )
    ctx.repo.save(a)
    ctx.repo.save(b)
    ctx.repo.upsert_speaker_sample("Carol", [0.1, 0.2])
    return a, b


def test_is_person():
    for generic in ["SPEAKER_03", "Speaker 2", "speaker", "UNKNOWN", "Me", "remote", "", None]:
        assert not is_person(generic)
    assert is_person("Alice") and not is_person("Host", ("host",))


def test_people_list(client, ctx):
    _seed(ctx)
    people = {p["name"]: p for p in client.get("/api/people").json()}
    assert set(people) == {"Alice", "Bob", "Carol"}
    assert people["Alice"]["meetings"] == 1 and people["Alice"]["open_tasks"] == 1
    assert people["Bob"]["meetings"] == 2 and people["Bob"]["last_seen"].startswith("2026-09-23")
    assert people["Carol"]["has_voice"] is True
    assert list(people)[0] in ("Bob", "Carol")  # most recently seen first


def test_person_detail(client, ctx):
    a, b = _seed(ctx)
    d = client.get("/api/people/ALICE").json()
    assert d["name"] == "Alice"
    [m] = d["meetings"]
    assert m["id"] == a.id and m["role"] == "both" and m["talk_seconds"] == 30.0
    assert m["share"] == 0.6 and d["total_talk_seconds"] == 30.0
    assert [t["text"] for t in d["open_tasks"]] == ["Update docs"]
    assert [t["text"] for t in d["done_tasks"]] == ["Tag rc1"]
    assert [x["text"] for x in d["decisions"]] == ["Ship in October"]
    bob = client.get("/api/people/Bob").json()
    assert [m["role"] for m in bob["meetings"]] == ["invited", "invited"]
    assert all(m["talk_seconds"] is None for m in bob["meetings"])
    assert client.get("/api/people/SPEAKER_01").status_code == 404
    assert client.get("/api/people/Nobody").status_code == 404


def test_person_notes(client, ctx, tmp_path):
    from mnemosyne.export.people_notes import MARKER

    a, _ = _seed(ctx)
    vault = tmp_path / "vault"
    (vault / "People").mkdir(parents=True)
    (vault / "People" / "Bob.md").write_text("my own Bob note")
    ctx.settings.obsidian_vault_path = str(vault)
    ctx.settings.obsidian_people_notes = True
    r = client.post(f"/api/sessions/{a.id}/export/obsidian", json={})
    assert r.status_code == 200, r.text
    folder = vault / "meetings/mnemosyne/people"
    alice = (folder / "Alice.md").read_text()
    assert alice.startswith(f"---\n{MARKER}\n")
    assert "- [ ] Update docs · [[2026-09-21-Release sync|Release sync]]" in alice
    assert "- 2026-09-21 [[2026-09-21-Release sync|Release sync]] (spoke 0:30)" in alice
    assert "- [x] Tag rc1" in alice
    assert not (folder / "Bob.md").exists()  # the user already has a Bob note
    assert (vault / "People" / "Bob.md").read_text() == "my own Bob note"
    # A hand-written note in our folder is never overwritten.
    (folder / "Alice.md").write_text("edited by hand")
    client.post(f"/api/sessions/{a.id}/export/obsidian", json={})
    assert (folder / "Alice.md").read_text() == "edited by hand"
