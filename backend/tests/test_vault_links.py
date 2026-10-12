"""Words that are the name of a note in the vault become [[links]] (obsidian_link_notes)."""

from datetime import datetime

import pytest

from mnemosyne.api.routes.export import build_exporter, export_session
from mnemosyne.export import vault_index
from mnemosyne.export.links import link_known_notes, link_topic
from mnemosyne.export.people_notes import write_person_notes
from mnemosyne.export.templates import render_meeting_note
from mnemosyne.export.vault_index import format_regex, linkable, linkable_notes, vault_stems
from mnemosyne.models.session import ActionItem, Chapter, Session, SummaryData
from mnemosyne.services.vault_sync import parse_ticks, strip_links

NAMES = {"Status App", "Status", "Logos", "v0.3", "Acme Corp", "Nim-lang"}


@pytest.fixture(autouse=True)
def fresh_scans():
    vault_index._scans.clear()
    yield
    vault_index._scans.clear()


def link(text, names=NAMES):
    return link_known_notes(text, names)


# --- the text function ---------------------------------------------------------------------


def test_a_name_becomes_a_link_once():
    assert link("We use Logos. Logos is great.") == "We use [[Logos]]. Logos is great."


def test_longest_name_first():
    assert link("The Status App and Status") == "The [[Status App]] and [[Status]]"
    assert link("Status of the Status App") == "[[Status]] of the [[Status App]]"
    # the longer name already linked: its words do not link the shorter one
    assert (
        link("Status App, then the Status App again") == "[[Status App]], then the Status App again"
    )


def test_case_sensitive():
    assert link("logos and LOGOS") == "logos and LOGOS"
    assert link("status app") == "status app"


def test_whole_words_only():
    assert link("Statuses and Logosphere and ALogos") == "Statuses and Logosphere and ALogos"
    assert link("Status-App and Status_x") == "Status-App and Status_x"
    assert link('(Logos), Logos! and "Logos"') == '([[Logos]]), Logos! and "Logos"'


def test_names_with_dots_hyphens_and_spaces():
    assert link("Released in v0.3.") == "Released in [[v0.3]]."
    assert link("Released in v0.35 and v0.3.1") == "Released in v0.35 and v0.3.1"
    assert link("Ask Acme Corp today") == "Ask [[Acme Corp]] today"
    assert link("Acme  Corp") == "Acme  Corp"  # not the name
    assert link("About Nim-lang.") == "About [[Nim-lang]]."


def test_not_a_tag_a_mention_or_a_path():
    assert link("#Logos @Logos docs/Logos") == "#Logos @Logos docs/Logos"


def test_existing_links_code_and_urls_are_left_alone():
    text = (
        "[[Logos]] and [[Other|Logos]] and ![[Logos]] "
        "[Logos](https://logos.co/Logos) <https://logos.co/Logos> https://x.io/Logos?Logos "
        "`Logos` ```\nLogos\n``` and www.Logos.co then Logos"
    )
    out = link(text)
    assert out.endswith("then [[Logos]]")
    assert out.count("[[Logos]]") == 3  # the existing ones and the one new link
    assert out[: -len("[[Logos]]")] == text[: -len("Logos")]


def test_name_next_to_an_existing_link():
    assert (
        link("[[Logos]] Logos") == "[[Logos]] [[Logos]]"
    )  # the existing one is not ours: not counted
    assert link("see [Status App](x) and Status App") == "see [Status App](x) and [[Status App]]"


def test_seen_spans_the_parts_of_a_section():
    seen: set[str] = set()
    one = link_known_notes("Logos first", NAMES, seen)
    two = link_known_notes("Logos second, Status", NAMES, seen)
    assert (one, two) == ("[[Logos]] first", "Logos second, [[Status]]")
    assert seen == {"Logos", "Status"}


def test_nothing_to_link():
    assert link("") == ""
    assert link("text", set()) == "text"
    assert link("no known words here") == "no known words here"


def test_special_characters_in_names_are_escaped():
    assert link_known_notes("C++ and (a+b)", {"C++"}) == "[[C++]] and (a+b)"
    assert link_known_notes("a.b and axb", {"a.b"}) == "[[a.b]] and axb"


def test_link_topic():
    assert link_topic("Logos", NAMES) == "[[Logos]]"
    assert link_topic("logos", NAMES) == "logos"


# --- which notes qualify -------------------------------------------------------------------


def test_linkable_rules():
    assert linkable("Logos") and linkable("Status App") and linkable("v0.3") and linkable("e-mail")
    assert not linkable("AI")  # under 3 characters
    assert not linkable("logos")  # a single lowercase word
    assert not linkable("Home") and not linkable("Inbox") and not linkable("TODO")
    assert not linkable("2026-10-12") and not linkable("2026-10-12 Standup")
    assert not linkable("123") and not linkable("a|b") and not linkable(" Logos")
    assert linkable("Meeting ideas")  # only a whole single common word is refused


def test_daily_note_formats_are_not_linked():
    daily = format_regex("dddd, MMMM Do YYYY")
    assert daily.fullmatch("Monday, October 12th 2026")
    assert not linkable("Monday, October 12th 2026", daily)
    assert linkable("Monday standup", daily)
    assert format_regex("YYYY[W]WW").fullmatch("2026W42")
    assert format_regex("DD.MM.YYYY").fullmatch("12.10.2026")
    assert not format_regex("DD.MM.YYYY").fullmatch("12x10x2026")


def _vault(tmp_path):
    v = tmp_path / "vault"
    for rel in [
        "Logos.md",
        "projects/Status App.md",
        "projects/logos-notes.md",
        "daily/Monday, October 12th 2026.md",
        "daily/2026-10-12.md",
        "meetings/mnemosyne/2026-10-11-Sync.md",
        "meetings/mnemosyne/people/Ana.md",
        "meetings/mnemosyne/topics/Roadmap.md",
        "design/Planning Board.md",
        ".obsidian/Config.md",
        "Home.md",
        "ab.md",
    ]:
        (v / rel).parent.mkdir(parents=True, exist_ok=True)
        (v / rel).write_text("x", encoding="utf-8")
    (v / ".obsidian" / "daily-notes.json").write_text('{"format": "dddd, MMMM Do YYYY"}')
    return v


def test_linkable_notes_skips_our_folders_dates_and_config(tmp_path):
    v = _vault(tmp_path)
    assert linkable_notes(v, ["meetings/mnemosyne"]) == {
        "Logos",
        "Status App",
        "logos-notes",
        "Planning Board",
    }


def test_vault_stems_excludes_folders_and_obsidian(tmp_path):
    v = _vault(tmp_path)
    everything = vault_stems(v)
    assert "Config" not in everything and "Ana" in everything and "Roadmap" in everything
    assert "Ana" not in vault_stems(v, [v / "meetings/mnemosyne/people"])
    assert "Ana" not in vault_stems(v, ["meetings/mnemosyne"])
    assert "Roadmap" in vault_stems(v, [v / "meetings/mnemosyne/people"])
    assert "Logos" in vault_stems(v, ["meetings/mnemosyne", ""])  # blank: no folder


def test_the_scan_is_cached(tmp_path):
    v = _vault(tmp_path)
    assert "Brand New" not in vault_stems(v)
    (v / "Brand New.md").write_text("x")
    assert "Brand New" not in vault_stems(v)  # cached
    vault_index._scans.clear()
    assert "Brand New" in vault_stems(v)


def test_person_notes_still_skip_people_with_a_note(tmp_path, ctx):
    v = tmp_path / "vault"
    (v / "People").mkdir(parents=True)
    (v / "People" / "Ben Hall.md").write_text("mine")
    ctx.repo.save(
        Session(name="Sync", created_at=datetime(2026, 10, 1), participants=["Ana", "Ben Hall"])
    )
    written = write_person_notes(ctx.repo, v, "meetings/mnemosyne", {"Ana", "Ben Hall"})
    assert [p.stem for p in written] == ["Ana"]  # Ben has a note of his own
    # and our own people folder is not looked at: Ana's note is rewritten
    assert [p.stem for p in write_person_notes(ctx.repo, v, "meetings/mnemosyne", {"Ana"})] == [
        "Ana"
    ]


# --- the note ------------------------------------------------------------------------------


def _note(known, **kw):
    data = SummaryData(
        decisions=["Ship Logos, then Logos again"],
        action_items=[
            ActionItem(text="Update the Status App docs", owner="Corey"),
            ActionItem(text="Ask Acme Corp about Status App", owner="Ana"),
            ActionItem(text="Check Logos"),
        ],
        open_questions=["Is v0.3 ready? Status?"],
        topics=["Logos", "pricing"],
        chapters=[Chapter(start=0, title="Intro to Logos"), Chapter(start=60, title="Logos again")],
    )
    return render_meeting_note(
        "Sync",
        datetime(2026, 10, 12, 9),
        ["Corey", "Ana"],
        [{"speaker": "Corey", "start": 0, "text": "Logos and Status App"}],
        "Logos is Status App. Logos again.",
        "",
        summary_data=data,
        known_notes=known,
        is_mine=lambda i: i.owner != "Ana",
        **kw,
    )


def test_note_links_each_section_once():
    note = _note({"Logos", "Status App", "Acme Corp", "v0.3", "Status"})
    assert "[[Logos]] is [[Status App]]. Logos again." in note
    assert "- Ship [[Logos]], then Logos again" in note
    assert "- [ ] Update the [[Status App]] docs ([[Corey]])" in note
    assert "- [[Ana]]: Ask [[Acme Corp]] about Status App" in note  # Status App: once per section
    assert "- [ ] Check [[Logos]]" in note
    assert "- Is [[v0.3]] ready? [[Status]]?" in note
    assert "Intro to [[Logos]]" in note and "00:00" in note and "Logos again" in note
    assert "01:00 Logos again" in note
    assert 'topics: ["[[Logos]]", "pricing"]' in note
    # the transcript is never linked, nor the participants' own links doubled
    assert "**[00:00] Corey:** Logos and Status App" in note
    assert "**Participants:** [[Corey]], [[Ana]]" in note


def test_note_unchanged_without_known_notes():
    plain = _note(None)
    assert "[[Logos]]" not in plain and "[[Status App]]" not in plain
    assert 'topics: ["Logos", "pricing"]' in plain
    assert _note(set()) == plain


# --- through the exporter ------------------------------------------------------------------


def _session(ctx, **kw):
    s = Session(
        name="Sync",
        created_at=datetime(2026, 10, 12, 9),
        summary="Planning Board and Logos.",
        summary_data=SummaryData(
            action_items=[ActionItem(text="Fix the Planning Board column", owner="Corey")],
            topics=["Logos"],
        ),
        **kw,
    )
    ctx.repo.save(s)
    return ctx.sessions.get_session(s.id)


def _setup(ctx, tmp_path):
    v = _vault(tmp_path)
    st = ctx.settings
    st.obsidian_vault_path = str(v)
    st.obsidian_subfolder = "meetings/mnemosyne"
    st.local_speaker_name = "Corey"
    st.obsidian_people_notes = False
    st.obsidian_daily_notes = False
    return v


def test_export_links_notes_of_the_vault(ctx, tmp_path):
    v = _setup(ctx, tmp_path)
    s = _session(ctx)
    path = export_session(ctx, s, str(v))
    text = path.read_text(encoding="utf-8")
    assert "[[Planning Board]] and [[Logos]]." in text
    assert "- [ ] Fix the [[Planning Board]] column ([[Corey]])" in text
    assert 'topics: ["[[Logos]]"]' in text
    # our own people note "Ana" is not a link target
    assert "[[Roadmap]]" not in text and "[[Ana]]" not in text


def test_the_setting_turns_links_off(ctx, tmp_path):
    v = _setup(ctx, tmp_path)
    ctx.settings.obsidian_link_notes = False
    text = export_session(ctx, _session(ctx), str(v)).read_text(encoding="utf-8")
    assert "[[Planning Board]]" not in text and "[[Logos]]" not in text
    assert 'topics: ["Logos"]' in text


def test_on_by_default_and_preview_without_a_vault_scans_nothing(ctx, tmp_path):
    assert ctx.settings.obsidian_link_notes is True
    ctx.settings.obsidian_vault_path = ""
    assert build_exporter(ctx, ".", _session(ctx)).known_notes is None


def test_a_vault_that_cannot_be_read_gives_a_note_without_links(ctx, tmp_path):
    v = _setup(ctx, tmp_path)
    exporter = build_exporter(ctx, str(v), _session(ctx))

    def boom():
        raise OSError("nope")

    exporter.known_notes = boom
    assert "[[Planning Board]]" not in exporter.render(_session(ctx))


# --- ticks still match -----------------------------------------------------------------------


def test_strip_links():
    assert strip_links("a [[B]] c [[D|e]] f") == "a B c e f"
    assert strip_links("none") == "none"


def test_ticks_of_linked_items_match_their_text():
    note = (
        "---\nsource: mnemosyne\n---\n\n## Action Items\n\n"
        "- [x] Update the [[Status App]] docs ([[Corey]]) [issue](https://x.io/1) 📅 2026-10-12\n"
        "- [ ] Ask [[Acme Corp|Acme]] about [[Logos]] ^abc\n\n"
        "### Others\n\n- [[Ana]]: Fix [[Logos]]\n"
    )
    assert [(t.text, t.done) for t in parse_ticks(note)] == [
        ("Update the Status App docs", True),
        ("Ask Acme about Logos", False),
    ]


def test_a_tick_in_a_linked_note_is_taken_and_written(ctx, tmp_path):
    from mnemosyne.services.vault_sync import apply_vault_ticks, note_path, write_tick

    v = _setup(ctx, tmp_path)
    s = _session(ctx)
    path = export_session(ctx, s, str(v))
    assert path == note_path(ctx, s, str(v))
    assert "Fix the [[Planning Board]] column" in path.read_text(encoding="utf-8")
    # ticked in the app: that one line of the linked note is rewritten
    assert write_tick(ctx, s, 0, True) is True
    assert "- [x] Fix the [[Planning Board]] column ([[Corey]]) ✅" in path.read_text(
        encoding="utf-8"
    )
    # reopened in Obsidian (the note is newer than the meeting): taken back
    import os
    import time

    ctx.repo.set_action_item_done(s.id, 0, True)
    text = path.read_text(encoding="utf-8").replace("- [x] Fix", "- [ ] Fix")
    path.write_text(text, encoding="utf-8")
    later = time.time() + 5
    os.utime(path, (later, later))
    assert apply_vault_ticks(ctx, ctx.sessions.get_session(s.id), str(v)) is True
    assert ctx.sessions.get_session(s.id).summary_data.action_items[0].done is False
