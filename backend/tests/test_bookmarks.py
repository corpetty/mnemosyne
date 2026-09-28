"""Bookmarks: marked moments (routes/bookmarks.py), in the summary and the Obsidian note."""

from mnemosyne.export.obsidian import ObsidianExporter
from mnemosyne.models.session import Recording
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services.copilot import bookmark_hint


def _lines():
    return [
        TranscriptSegment(text="Hello all", speaker="Ana", start=0, end=4),
        TranscriptSegment(text="Price goes to twelve", speaker="Dan", start=60, end=65),
    ]


def test_mark_now_while_recording_then_edit_and_delete(client, ctx, fake_pipewire):
    sid = client.post("/api/audio/start", json={"device_ids": [1]}).json()["session_id"]
    ctx.active_recordings[sid].started_at -= 42  # recording for 42 s
    marked = client.post(f"/api/sessions/{sid}/bookmarks", json={"note": "pricing"}).json()
    assert 41 <= marked["at"] <= 44 and marked["note"] == "pricing"
    later = client.post(f"/api/sessions/{sid}/bookmarks", json={"at": 10}).json()
    session = client.get(f"/api/sessions/{sid}").json()
    assert [b["id"] for b in session["bookmarks"]] == [later["id"], marked["id"]]  # by time
    b = client.patch(f"/api/sessions/{sid}/bookmarks/{later['id']}", json={"note": " intro "})
    assert b.json()["note"] == "intro"
    assert client.delete(f"/api/sessions/{sid}/bookmarks/{later['id']}").status_code == 200
    assert client.delete(f"/api/sessions/{sid}/bookmarks/{later['id']}").status_code == 404
    client.post(f"/api/audio/stop/{sid}", json={"transcribe": False})
    assert client.post(f"/api/sessions/{sid}/bookmarks", json={}).status_code == 409


def test_a_later_part_is_placed_after_the_earlier_ones(ctx):
    s = ctx.sessions.create_session("m")
    ctx.sessions.set_audio(
        s.id,
        "/x.ogg",
        [
            Recording(source="mic", device_id=1, device_name="m", path="/a", part=0),
            Recording(source="mic", device_id=1, device_name="m", path="/b", part=1, offset=300),
        ],
    )
    ctx.repo.add_bookmark(s.id, 1, 5.0, "here")
    ctx.repo.add_bookmark(s.id, 0, 20.0)
    assert [b.at for b in ctx.sessions.get_session(s.id).bookmarks] == [20.0, 305.0]

    # Combined into another meeting where it became part 2 (services/combine.py).
    other = ctx.sessions.create_session("o")
    ctx.repo.move_bookmarks([(s.id, 1, other.id, 2)])
    assert [b.note for b in ctx.sessions.get_session(other.id).bookmarks] == ["here"]
    assert len(ctx.sessions.get_session(s.id).bookmarks) == 1


def test_marked_moments_reach_the_summary_and_the_note(ctx, tmp_path):
    s = ctx.sessions.create_session("Pricing")
    ctx.sessions.set_transcript(s.id, _lines())
    ctx.repo.add_bookmark(s.id, 0, 61.0, "decision?")
    session = ctx.sessions.get_session(s.id)
    hint = bookmark_hint(session.bookmarks, session.transcript)
    assert "[01:01] Dan: Price goes to twelve (note: decision?)" in hint
    note = ObsidianExporter(str(tmp_path)).render(session)
    assert "## Bookmarks\n\n- 01:01 **decision?** Dan: Price goes to twelve" in note
