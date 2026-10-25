"""Chips in the meeting notes (services/notes_chips.py): `plain_notes`, `obsidian_notes`, and the
readers that use them (the summary's notes hint)."""

from mnemosyne.services.notes_chips import (
    exported_meeting_note,
    obsidian_notes,
    parse_chip,
    plain_notes,
)
from tests.conftest import run_summarize

MEETING = "0123456789ab"


def test_parse_chip_is_strict():
    assert parse_chip("mnemosyne:t/1/754") == ("time", "1/754")
    assert parse_chip("mnemosyne:person/Ada%20Lovelace") == ("person", "Ada Lovelace")
    assert parse_chip(f"mnemosyne:meeting/{MEETING}") == ("meeting", MEETING)
    for bad in (
        "mnemosyne:t/1",
        "mnemosyne:t/1/2/3",
        "mnemosyne:t/-1/2",
        "mnemosyne:t/1/2.5",
        "mnemosyne:t/\u0661/2",  # a non-ASCII digit
        "mnemosyne:t/99999999999999999999/1",  # beyond Number.isSafeInteger
        "mnemosyne:person/",
        "mnemosyne:person/Ada/Lovelace",
        "mnemosyne:person/100%",  # a bad percent escape
        "mnemosyne:person/%zz",
        "mnemosyne:person/%FF",  # not UTF-8
        "mnemosyne:meeting/0123456",  # too short
        "mnemosyne:meeting/0123456789ABCDEF",  # uppercase
        f"mnemosyne:meeting/{MEETING}x",
        "mnemosyne:other/1",
        "https://example.com",
        "MNEMOSYNE:t/1/2",
        " mnemosyne:t/1/2",
    ):
        assert parse_chip(bad) is None, bad


def test_plain_notes_replaces_the_three_chips():
    md = (
        "Said at [12:34](mnemosyne:t/0/754) by [@Ada Lovelace](mnemosyne:person/Ada%20Lovelace),"
        f" as in [Planning, Oct 14](mnemosyne:meeting/{MEETING}).\n- [ ] [@Bo](mnemosyne:person/Bo)"
        " do it"
    )
    assert plain_notes(md) == (
        "Said at 12:34 by @Ada Lovelace, as in Planning, Oct 14.\n- [ ] @Bo do it"
    )
    assert "mnemosyne:" not in plain_notes(md)


def test_plain_notes_unescapes_the_link_text():
    # notes-core escapes brackets inside a link text, and `*`, `_`, `&`, `|`, a backslash...
    md = f"[Q3 \\[draft\\] \\& co \\*\\* a\\\\b](mnemosyne:meeting/{MEETING}) next"
    assert plain_notes(md) == "Q3 [draft] & co ** a\\b next"
    # A `]` inside a code span of the text does not end it.
    assert plain_notes(f"[`a]b`](mnemosyne:meeting/{MEETING})") == "`a]b`"


def test_plain_notes_leaves_everything_else_alone():
    md = (
        "[site](https://example.com) and [odd](mnemosyne:t/x/1) and "
        "[bad](mnemosyne:person/100%) and [x](mnemosyne:meeting/ABC) "
        '[titled](mnemosyne:t/1/2 "title") `[a](mnemosyne:t/1/2)` \\[b](mnemosyne:t/1/2) '
        "[open](mnemosyne:t/1/2 and [unclosed text"
    )
    assert plain_notes(md) == md
    assert plain_notes("") == "" and plain_notes("no links at all") == "no links at all"


def test_plain_notes_handles_neighbours_and_angle_destinations():
    # Adjacent chips, a chip next to an ordinary link, and a destination in `<...>`.
    md = (
        "[1:00](mnemosyne:t/0/60)[2:00](<mnemosyne:t/0/120>) "
        "[x](https://a.b) [3:00](mnemosyne:t/0/180)"
    )
    assert plain_notes(md) == "1:002:00 [x](https://a.b) 3:00"
    # Parentheses in a person's name are escaped in the destination, as render.rs writes them.
    md = "[@Ada (Countess)](mnemosyne:person/Ada%20\\(Countess\\)) ok"
    assert plain_notes(md) == "@Ada (Countess) ok"


def test_plain_notes_keeps_a_table_row_a_row():
    md = f"| when | who |\n| --- | --- |\n| [a\\|b](mnemosyne:meeting/{MEETING}) | x |"
    assert plain_notes(md).splitlines()[-1] == "| a\\|b | x |"


def test_obsidian_notes_people_links_on_and_off():
    md = "[12:34](mnemosyne:t/0/754) [@Ada Lovelace](mnemosyne:person/Ada%20Lovelace) ok"
    assert obsidian_notes(md, True, None) == "12:34 [[Ada Lovelace]] ok"
    assert obsidian_notes(md, False, None) == "12:34 @Ada Lovelace ok"
    # A name a wikilink cannot hold stays text.
    odd = "[@A|B](mnemosyne:person/A%7CB)"
    assert obsidian_notes(odd, True, None) == "@A|B"


def test_obsidian_notes_meeting_chip_links_the_exported_note():
    md = (
        f"see [Planning, Oct 14](mnemosyne:meeting/{MEETING}) "
        "and [Other](mnemosyne:meeting/ffffffff)"
    )
    seen = []

    def meeting_note(mid: str):
        seen.append(mid)
        return ("2026-10-14-Planning", "Planning (Oct 14)") if mid == MEETING else None

    out = obsidian_notes(md, True, meeting_note)
    assert out == "see [[2026-10-14-Planning|Planning (Oct 14)]] and Other"
    assert seen == [MEETING, "ffffffff"]
    # No resolver, or one that fails: the link text.
    assert obsidian_notes(md, True, None) == "see Planning, Oct 14 and Other"

    def broken(_mid: str):
        raise RuntimeError("no database")

    assert obsidian_notes(md, True, broken) == "see Planning, Oct 14 and Other"
    # A title with wikilink syntax is cleaned; a stem with it is not linked.
    clean = obsidian_notes(md, True, lambda _m: ("2026-note", "A [b] | c"))
    assert clean.startswith("see [[2026-note|A b c]]")
    assert obsidian_notes(md, True, lambda _m: ("a#b", "t")).startswith("see Planning, Oct 14")
    # Title equal to the stem: no alias.
    assert obsidian_notes(md, True, lambda _m: ("same", "same")).startswith("see [[same]]")


def test_obsidian_notes_in_a_table_escapes_the_pipe():
    md = f"| [x](mnemosyne:meeting/{MEETING}) |"
    assert obsidian_notes(md, True, lambda _m: ("stem", "Title")) == "| [[stem\\|Title]] |"


def test_exported_meeting_note_needs_a_vault_record(ctx):
    s = ctx.sessions.create_session("Planning")
    assert exported_meeting_note(ctx.repo, s.id) is None  # never exported
    ctx.repo.put_vault_note(s.id, "/vault/meetings/2026-10-14-Planning.md", b"", {}, "sha")
    assert exported_meeting_note(ctx.repo, s.id) == ("2026-10-14-Planning", "Planning")
    assert exported_meeting_note(ctx.repo, "nope") is None


def test_summary_reads_the_notes_chips_as_text(client, ctx, fake_provider, transcribed_session):
    sid = transcribed_session["id"]
    notes = (
        "- [@Ada Lovelace](mnemosyne:person/Ada%20Lovelace) owns the budget at "
        f"[12:34](mnemosyne:t/0/754), see [Planning](mnemosyne:meeting/{MEETING}) "
        "[site](https://example.com)"
    )
    client.post(f"/api/sessions/{sid}/notes", json={"notes": notes})
    assert run_summarize(client, sid, {"provider": "fake"})["status"] == "completed"
    prompt = fake_provider.calls[-1]["system_prompt"]
    assert (
        "- @Ada Lovelace owns the budget at 12:34, see Planning [site](https://example.com)"
        in prompt
    )
    assert "mnemosyne:" not in prompt
