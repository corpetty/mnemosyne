"""Talk time and turn statistics."""

from mnemosyne.models.session import Session
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services.stats import meeting_stats


def _s(speaker, start, end, text="one two three"):
    return TranscriptSegment(text=text, speaker=speaker, start=start, end=end)


SEGS = [
    _s("Alice", 0, 10, "a " * 20),
    _s("Alice", 10, 20, "a " * 20),  # same turn
    _s("Bob", 25, 30),
    _s("Alice", 29, 40),  # overlaps Bob by 1 s
    _s("Bob", 50, 50),  # zero length: ignored
]


def test_meeting_stats():
    st = meeting_stats(SEGS)
    assert st.duration_seconds == 40
    assert st.speech_seconds == 35  # 0-20 and 25-40
    assert st.silence_seconds == 5
    assert st.turns == 3
    assert [t.speaker for t in st.timeline] == ["Alice", "Bob", "Alice"]
    assert st.timeline[0].end == 20 and st.timeline[2].first_idx == 3
    alice, bob = st.speakers
    assert alice.speaker == "Alice" and alice.talk_seconds == 31 and alice.turns == 2
    assert alice.longest_turn_seconds == 20
    assert alice.share == round(31 / 36, 3)
    assert bob.words == 3 and bob.words_per_minute == 36.0


def test_empty_transcript():
    st = meeting_stats([])
    assert st.duration_seconds == 0 and st.speakers == [] and st.timeline == []


def test_stats_route(client, ctx):
    s = ctx.repo.save(Session(name="x", transcript=SEGS))
    body = client.get(f"/api/sessions/{s.id}/stats").json()
    assert body["turns"] == 3 and body["speakers"][0]["speaker"] == "Alice"
    assert client.get("/api/sessions/nope/stats").status_code == 404
