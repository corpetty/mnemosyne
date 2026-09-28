"""A corrected transcript line teaches the glossary (glossary.py, routes/glossary.py)."""

from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.transcription.glossary import add_correction, parse_glossary, suggest_corrections


def test_a_fixed_name_is_suggested_a_typo_is_not():
    g = parse_glossary("")
    assert suggest_corrections("Thanks okay for, good point.", "Thanks Okafor, good point.", g) == [
        ("okay for", "Okafor")
    ]
    assert suggest_corrections("we walk you the node", "we Waku the node", g) == [
        ("walk you", "Waku")
    ]
    assert suggest_corrections("teh plan is good", "the plan is good", g) == []  # no capital
    assert suggest_corrections("the plan is good", "The plan is good", g) == []  # case only
    assert suggest_corrections("a b c d e f", "Completely New Words Here Now Really", g) == []


def test_nothing_the_glossary_already_fixes():
    g = parse_glossary("okay for -> Okafor")
    assert suggest_corrections("hi okay for", "hi Okafor", g) == []


def test_entries_are_added_once():
    text = add_correction("Waku\n", "okay for", "Okafor")
    assert text == "Waku\nokay for -> Okafor\n"
    assert add_correction(text, "Okay For", "Okafor") == text


def test_adding_a_correction_fixes_the_meeting(client, ctx):
    s = ctx.sessions.create_session("m")
    ctx.sessions.set_transcript(
        s.id,
        [
            TranscriptSegment(text="Thanks Okafor.", speaker="A", start=0, end=1),
            TranscriptSegment(text="okay for will send it", speaker="B", start=1, end=2),
            TranscriptSegment(text="Okay, for now yes", speaker="A", start=2, end=3),
        ],
    )
    hint = client.post(
        "/api/glossary/suggest", json={"before": "Thanks okay for.", "after": "Thanks Okafor."}
    ).json()
    assert hint == [{"heard": "okay for", "correct": "Okafor"}]
    res = client.post("/api/glossary/corrections", json={**hint[0], "session_id": s.id}).json()
    assert res["fixed_lines"] == 1
    assert "okay for -> Okafor" in res["glossary"] and "okay for -> Okafor" in ctx.settings.glossary
    lines = [x["text"] for x in client.get(f"/api/sessions/{s.id}").json()["transcript"]]
    assert lines == ["Thanks Okafor.", "Okafor will send it", "Okay, for now yes"]
    bad = client.post("/api/glossary/corrections", json={"heard": "a -> b", "correct": "c"})
    assert bad.status_code == 400
