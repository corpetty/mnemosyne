"""Financial identifiers (SSN, account, routing and card numbers, dates of birth): redacted in
exports, optionally in stored transcripts, and hidden from cloud LLMs."""

from datetime import datetime

import pytest

from mnemosyne.models.session import ActionItem, Session, SummaryData
from mnemosyne.models.transcript import TranscriptSegment, WordSegment
from mnemosyne.summarization.privacy import Redactor, redact_identifiers, redact_transcript
from tests.conftest import drain_until_job
from tests.fakes import FakeEngine


@pytest.mark.parametrize(
    "said, expected",
    [
        # Social Security numbers
        ("My social is 123-45-6789.", "My social is [SSN]."),
        ("It's 123 45 6789, I think.", "It's [SSN], I think."),
        ("My SSN is 123456789.", "My SSN is [SSN]."),
        (
            "Sure, my social is four five six, seven eight, nine zero one two.",
            "Sure, my social is [SSN].",
        ),
        (
            "Social security number: four five six seventy-eight ninety-one twelve",
            "Social security number: [SSN]",
        ),
        (
            "My social is 123-45-6789 and hers is nine eight seven, six five, four three two one.",
            "My social is [SSN] and hers is [SSN].",
        ),
        # Account numbers
        ("The account number is 12345678.", "The account number is [account ••5678]."),
        ("Wire it to 000123456789, please.", "Wire it to [account ••6789], please."),
        (
            "My checking account is one two three four five six seven eight nine.",
            "My checking account is [account ••6789].",
        ),
        (
            "Brokerage account number double five two three one four seven.",
            "Brokerage account number [account ••3147].",
        ),
        ("It's the account ending in 1234.", "It's the account ending in [account ••1234]."),
        (
            "The IRA ending in four three two one, yes.",
            "The IRA ending in [account ••4321], yes.",
        ),
        # Routing numbers (021000021 passes the ABA checksum)
        ("Routing number is 021000021.", "Routing number is [routing number]."),
        (
            "The routing number is oh two one, oh oh oh, oh two one.",
            "The routing number is [routing number].",
        ),
        (
            "Routing 021000021 and account 12345678.",
            "Routing [routing number] and account [account ••5678].",
        ),
        # Payment cards (Luhn)
        (
            "Card number 4242 4242 4242 4242, expires 12/28.",
            "Card number [card ••4242], expires 12/28.",
        ),
        ("It's 4111-1111-1111-1111.", "It's [card ••1111]."),
        ("The card ending 4242 was declined.", "The card ending [card ••4242] was declined."),
        (
            "The Visa, last four digits are 4242.",
            "The Visa, last four digits are [card ••4242].",
        ),
        (
            "My card is four two four two four two four two four two four two four two four two",
            "My card is [card ••4242]",
        ),
        # Dates of birth
        ("My date of birth is 3/15/1962.", "My date of birth is [date of birth]."),
        ("DOB: 1962-03-15.", "DOB: [date of birth]."),
        (
            "I was born on March third, nineteen sixty-two.",
            "I was born on [date of birth].",
        ),
        ("She was born on the 3rd of March 1962.", "She was born on [date of birth]."),
        ("My birthday's March 3rd.", "My birthday's [date of birth]."),
        ("Date of birth oh three one five six two.", "Date of birth [date of birth]."),
    ],
)
def test_identifiers_are_redacted(said, expected):
    assert redact_identifiers(said) == expected


@pytest.mark.parametrize(
    "said",
    [
        "We moved $250,000 into the account last year.",
        "We moved two hundred fifty thousand into the account.",
        "The account balance is 125000.",
        "About 1500000 in assets under management.",
        "Returns were 7.5% in 2024 and 12 percent in 2023.",
        "Let's meet at 12:30 on 2026-10-01, or 10/02/2026.",
        "The review is on March 3rd, 2026; she turns 65 in 2027.",
        "Call me at 415-555-0100 or +1 (415) 555-0100.",
        "My cell is 4155550100.",
        "Max out the 401k and the Roth IRA this year.",
        "The fiscal year ending 2024 was a good one.",
        "One two three four, can you hear me?",
        "I need about twenty five minutes, maybe thirty.",
        "Population 1 000 000.",
        "Double five, two, three, one, four.",
        "Invoice INV123456789 is paid.",
        "She was born in 1962.",
        "We saved 3 accounts: 2 checking, 1 savings.",
        "Already redacted: [SSN], [account ••1234], [card ••4242].",
    ],
)
def test_ordinary_numbers_are_left_alone(said):
    assert redact_identifiers(said) == said


def test_cue_in_the_previous_line():
    assert redact_identifiers("Four five six, seven eight, nine zero one two.") == (
        "Four five six, seven eight, nine zero one two."
    )
    assert redact_identifiers("Four five six, seven eight, nine zero one two.", "Your social?") == (
        "[SSN]."
    )


def _seg(text, start, speaker="A", words=None):
    return TranscriptSegment(text=text, speaker=speaker, start=start, end=start + 3, words=words)


def test_redact_transcript_lines_and_words():
    words = [
        WordSegment(word=w, start=10 + i, end=10.5 + i)
        for i, w in enumerate("It's four five six, seven eight, nine zero one two.".split())
    ]
    segments = [
        _seg("What's your social security number?", 0, "Advisor"),
        _seg("It's four five six, seven eight, nine zero one two.", 10, "Client", words),
        _seg("Thanks.", 20, "Advisor"),
    ]
    out = redact_transcript(segments)
    assert [s.text for s in out] == [segments[0].text, "It's [SSN].", "Thanks."]
    assert [w.word for w in out[1].words] == ["It's", "[SSN]."]
    assert (out[1].words[1].start, out[1].words[1].end) == (11, 19.5)  # the digits' times
    assert out[0] is segments[0] and out[2] is segments[2]  # untouched lines stay as they were


def test_redactor_round_trip_with_identifiers():
    r = Redactor(["Alice"])
    text = (
        "Alice's SSN is 123-45-6789, card 4242 4242 4242 4242, account ending in 1234, "
        "routing 021000021, born on 3/15/1962; call +1 (415) 555-0100."
    )
    out = r.redact(text)
    for secret in ("6789", "4242", "1234", "021000021", "1962", "555-0100", "Alice"):
        assert secret not in out
    assert "[SSN_1]" in out and "[CARD_1]" in out and "[ACCOUNT_1]" in out
    assert "[ROUTING_1]" in out and "[DOB_1]" in out and "[PHONE_1]" in out
    assert r.restore(out) == text
    assert r.restore("[SSN_1] is on file") == "123-45-6789 is on file"


def _meeting(ctx):
    s = Session(
        name="Annual review",
        created_at=datetime(2026, 9, 30, 10),
        participants=["Advisor", "Client"],
        transcript=[
            _seg("What's the account number?", 0, "Advisor"),
            _seg("One two three four five six seven eight.", 5, "Client"),
            _seg("And your social is 123-45-6789, right? Meet at 10:30.", 10, "Advisor"),
        ],
        summary="Moved $250,000 to account 98765432.",
        summary_data=SummaryData(
            action_items=[ActionItem(text="Update card 4111 1111 1111 1111", owner="Me")]
        ),
    )
    return ctx.repo.save(s)


def test_markdown_export_is_redacted_by_default(client, ctx):
    s = _meeting(ctx)
    md = client.get(f"/api/sessions/{s.id}/export/markdown").json()["markdown"]
    assert "[account ••5678]" in md and "[SSN]" in md and "[account ••5432]" in md
    assert "[card ••1111]" in md
    for secret in ("six seven eight", "123-45-6789", "98765432", "4111 1111"):
        assert secret not in md
    assert "$250,000" in md and "10:30" in md and "date: 2026-09-30" in md

    ctx.settings.redact_exports = False
    md = client.get(f"/api/sessions/{s.id}/export/markdown").json()["markdown"]
    assert "123-45-6789" in md and "98765432" in md


def test_obsidian_export_and_daily_note_are_redacted(client, ctx, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    client.put(
        "/api/settings",
        json={
            "obsidian_vault_path": str(vault),
            "obsidian_daily_notes": True,
            "obsidian_daily_folder": "Daily",
            "local_speaker_name": "Me",
        },
    )
    s = _meeting(ctx)
    assert client.post(f"/api/sessions/{s.id}/export/obsidian").status_code == 200
    written = "\n".join(p.read_text() for p in vault.rglob("*.md"))
    assert "[SSN]" in written and "[card ••1111]" in written
    for secret in ("123-45-6789", "98765432", "4111 1111"):
        assert secret not in written
    daily = (vault / "Daily" / "2026-09-30.md").read_text()
    assert "[account ••5432]" in daily and "[card ••1111]" in daily


def _transcribe(client, ctx, segments) -> dict:
    ctx.models._engine = FakeEngine(segments)
    sid = client.post("/api/sessions", json={"name": "Client call"}).json()["id"]
    ctx.sessions.set_audio(sid, "/fake/mixed.ogg", [])
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        job = client.post(f"/api/sessions/{sid}/transcribe").json()
        events = drain_until_job(ws, job["id"])
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "completed"
    return {"session": client.get(f"/api/sessions/{sid}").json(), "events": events}


_CALL = [
    _seg("Can I get your date of birth and social?", 0, "Advisor"),
    _seg(
        "Born March third, nineteen sixty-two. Social is four five six, seven eight, nine zero "
        "one two.",
        4,
        "Client",
        [
            WordSegment(word=w, start=4 + i * 0.3, end=4.2 + i * 0.3)
            for i, w in enumerate(
                "Born March third, nineteen sixty-two. Social is four five six, seven eight, nine "
                "zero one two.".split()
            )
        ],
    ),
    _seg("We moved $250,000 in 2024.", 12, "Advisor"),
]


def test_stored_transcript_is_redacted_when_on(client, ctx):
    ctx.settings.redact_stored_transcripts = True
    result = _transcribe(client, ctx, _CALL)
    stored = result["session"]["transcript"]
    assert stored[1]["text"] == "Born [date of birth]. Social is [SSN]."
    assert [w["word"] for w in stored[1]["words"]] == [
        "Born",
        "[date of birth].",
        "Social",
        "is",
        "[SSN].",
    ]
    assert stored[2]["text"] == "We moved $250,000 in 2024."
    streamed = [e["segment"]["text"] for e in result["events"] if e["type"] == "transcription"]
    assert not any("four five six" in t for t in streamed)  # never shown unredacted either
    assert ctx.repo.get(result["session"]["id"]).transcript[1].text.endswith("[SSN].")


def test_stored_transcript_is_kept_as_said_by_default(client, ctx):
    result = _transcribe(client, ctx, _CALL)
    assert "four five six" in result["session"]["transcript"][1]["text"]
