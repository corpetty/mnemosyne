"""The backend's sections in the live notes (services/notes_sections.py): ref values, cutting a
markdown into own text and sections by the sidecar's refs, composing changed lines and the
`set_text` refs for them. Pure: no sidecar, no repository (a stub for `own_notes`)."""

import pytest

from mnemosyne.services import notes_sections as ns

DOC_LINES = [
    "# Plan",  # 0
    "",  # 1
    "Our own line",  # 2
    "",  # 3
    "## Copilot",  # 4 sec:copilot
    "- [Copilot](mnemosyne:by/copilot) a point",  # 5 cp:0
    "",  # 6
    "## Summary",  # 7 sec:summary
    "",  # 8
    "Body text.",  # 9
    "",  # 10
    "### Decisions",  # 11 sec:decisions
    "- Ship it",  # 12 sum:d1
    "- my own aside",  # 13 a person's line between two items
    "- Hire Bob",  # 14 sum:d2
    "",  # 15
    "### Action items",  # 16 sec:action_items
    "- [ ] Ada sends the deck (Ada)",  # 17 sum:a1
    "",  # the empty piece after the last newline
]
DOC = "\n".join(DOC_LINES)
DOC_REFS = {
    4: "sec:copilot",
    5: "cp:0",
    7: "sec:summary",
    11: "sec:decisions",
    12: "sum:d1",
    14: "sum:d2",
    16: "sec:action_items",
    17: "sum:a1",
}


def refs_list(by_line: dict[int, str]) -> list[dict]:
    """As the sidecar reports them (the toy's index = md_line; here only md_line matters)."""
    return [
        {"line": format(n, "064x"), "ref": ref, "index": i, "md_line": n}
        for i, (n, ref) in enumerate(sorted(by_line.items()))
    ]


def without(lines: list[str], refs: dict[int, str], gone: int) -> tuple[str, dict[int, str]]:
    """The document with line `gone` deleted (and the refs below it moved up)."""
    kept = lines[:gone] + lines[gone + 1 :]
    moved = {(n if n < gone else n - 1): r for n, r in refs.items() if n != gone}
    return "\n".join(kept), moved


# ---- ref values ------------------------------------------------------------------------------


def test_ref_values_and_their_parsers():
    assert ns.sec_ref(ns.DECISIONS) == "sec:decisions" and ns.sum_ref("ab12cd34") == "sum:ab12cd34"
    assert ns.cp_ref(7) == "cp:7"
    assert ns.sec("sec:open_questions") == "open_questions" and ns.sec("sec:later") == "later"
    assert [ns.sec(x) for x in ("sec:", "sum:x", None, "")] == [None, None, None, None]
    assert ns.sum_id("sum:ab12cd34") == "ab12cd34" and ns.sum_id("sec:summary") is None
    assert ns.sum_id("sum:") is None
    assert ns.cp_n("cp:12") == 12
    assert [ns.cp_n(x) for x in ("cp:", "cp:-1", "cp:1a", "cp:²", "sum:1", None)] == [None] * 6
    assert ns.SECTION_NAMES == ("summary", "decisions", "action_items", "open_questions", "copilot")


def test_refs_from_a_reply_a_list_or_refs():
    good = {"line": "ab" * 32, "ref": "sum:d1", "index": 3, "md_line": 5}
    bad = [
        {"line": "ab", "ref": "", "index": 0, "md_line": 0},  # an empty ref
        {"line": "ab", "ref": "x", "index": True, "md_line": 0},
        {"line": 1, "ref": "x", "index": 0, "md_line": 0},
        {"ref": "x", "index": 0, "md_line": 0},
        "sum:d1",
    ]
    want = [ns.Ref("ab" * 32, "sum:d1", 3, 5)]
    assert ns.parse_refs({"markdown": "", "refs": [good, *bad]}) == want
    assert ns.parse_refs([good]) == want
    assert ns.parse_refs(want) == want
    assert ns.parse_refs({"markdown": ""}) == [] and ns.parse_refs(None) == []
    assert ns.parse_refs("refs") == []


# ---- split -------------------------------------------------------------------------------------


def test_split_with_gap_lines_and_a_persons_line_between_items():
    s = ns.split(DOC, {"refs": refs_list(DOC_REFS)})
    assert s.own == "# Plan\n\nOur own line\n" and s.own_end == 4
    assert [x.md for x in s.lines] == DOC_LINES
    assert [(x.md_line, x.ref) for x in s.lines if x.ref] == sorted(DOC_REFS.items())
    assert s.lines[12].line == format(12, "064x")
    assert [sec.name for sec in s.sections] == [
        "copilot",
        "summary",
        "decisions",
        "action_items",
    ]

    copilot = s.section(ns.COPILOT)
    assert copilot is not None and copilot.heading.md_line == 4
    assert [x.md_line for x in copilot.lines] == [5, 6]
    assert [x.ref for x in copilot.items()] == ["cp:0"]
    assert copilot.last.md_line == 5  # the blank gap after it is not where a line goes

    summary = s.section(ns.SUMMARY)
    assert summary is not None and [x.md for x in summary.lines] == ["", "Body text.", ""]
    assert summary.items() == [] and summary.last.md == "Body text."

    decisions = s.section(ns.DECISIONS)
    assert decisions is not None
    assert [(x.md, x.ref) for x in decisions.lines] == [
        ("- Ship it", "sum:d1"),
        ("- my own aside", None),  # kept in the section, never an item
        ("- Hire Bob", "sum:d2"),
        ("", None),
    ]
    assert [x.ref for x in decisions.items()] == ["sum:d1", "sum:d2"]
    assert decisions.last.ref == "sum:d2"

    actions = s.section(ns.ACTION_ITEMS)
    assert actions is not None
    assert [x.md_line for x in actions.lines] == [17]  # the empty last piece is no line
    assert s.section(ns.OPEN_QUESTIONS) is None

    found = s.find("sum:a1")
    assert found is not None and found.md_line == 17 and s.find("sum:zz") is None
    assert [x.ref for x in s.with_prefix(ns.SUM)] == ["sum:d1", "sum:d2", "sum:a1"]
    assert [x.ref for x in s.with_prefix(ns.CP)] == ["cp:0"]


def test_split_with_a_missing_heading():
    # "### Decisions" deleted: its items belong to the Summary section, no Decisions section.
    md, refs = without(DOC_LINES, DOC_REFS, 11)
    s = ns.split(md, refs_list(refs))
    assert s.section(ns.DECISIONS) is None
    summary = s.section(ns.SUMMARY)
    assert summary is not None
    assert [x.ref for x in summary.items()] == ["sum:d1", "sum:d2"]
    assert s.own == "# Plan\n\nOur own line\n"
    found = s.find("sum:d2")
    assert found is not None and found.md == "- Hire Bob"

    # The first heading deleted: what was under it is the person's own text now.
    md, refs = without(DOC_LINES, DOC_REFS, 4)
    s = ns.split(md, refs_list(refs))
    assert s.section(ns.COPILOT) is None and s.sections[0].name == ns.SUMMARY
    assert s.own == "# Plan\n\nOur own line\n\n- [Copilot](mnemosyne:by/copilot) a point\n"
    assert [x.ref for x in s.with_prefix(ns.CP)] == ["cp:0"]  # still findable


def test_split_without_sections_and_odd_refs():
    s = ns.split("Just notes\n\nmore\n", [])
    assert s.own == "Just notes\n\nmore\n" and s.sections == [] and s.own_end == 4
    s = ns.split("Just notes", None)
    assert s.own == "Just notes"
    # A ref past the end is dropped; two at one line: the first.
    refs = refs_list({0: "sec:summary", 9: "sum:x"}) + [
        {"line": "00", "ref": "sum:y", "index": 1, "md_line": 0}
    ]
    s = ns.split("## Summary\n", refs)
    assert [(x.md_line, x.ref) for x in s.lines] == [(0, "sec:summary"), (1, None)]
    assert s.own == "" and s.sections[0].lines == []
    assert ns.own_text("Mine\n\n\n## Summary\nx\n", refs_list({3: "sec:summary"})) == "Mine\n"


def test_own_notes_by_the_stored_refs():
    class Repo:
        def __init__(self, markdown, refs):
            self.markdown, self.refs = markdown, refs

        def notes_refs(self, session_id, markdown=None):
            assert session_id == "s1"
            if self.refs is None or (markdown is not None and markdown != self.markdown):
                return None
            return self.refs

    repo = Repo(DOC, refs_list(DOC_REFS))
    assert ns.own_notes(repo, "s1", DOC) == "# Plan\n\nOur own line\n"
    # Written around the document since: the refs do not describe this text, it is all own.
    assert ns.own_notes(repo, "s1", DOC + "typed elsewhere\n") == DOC + "typed elsewhere\n"
    assert ns.own_notes(Repo(DOC, None), "s1", DOC) == DOC  # no refs stored
    assert ns.own_notes(Repo(DOC, []), "s1", DOC) == DOC


# ---- compose -----------------------------------------------------------------------------------


def test_compose_without_edits_gives_the_same_markdown_and_its_refs():
    s = ns.split(DOC, refs_list(DOC_REFS))
    md, refs = ns.compose(s.lines, [])
    assert md == DOC
    # Every non-blank line has a key: its ref or "" (so a diff cannot move a ref to it).
    assert refs == {str(n): DOC_REFS.get(n, "") for n, line in enumerate(DOC_LINES) if line.strip()}


def test_compose_replace_delete_insert_by_md_line():
    s = ns.split(DOC, refs_list(DOC_REFS))
    d1 = s.find("sum:d1")
    d2 = s.find("sum:d2")
    cp = s.find("cp:0")
    assert d1 and d2 and cp
    md, refs = ns.compose(
        s.lines,
        [
            ns.replace(d1.md_line, "- Ship it in October"),  # reworded: keeps its ref
            ns.delete(d2.md_line),
            ns.insert_after(d1.md_line, ns.New("- Hire Cy", "sum:d3")),
            ns.insert_after(d1.md_line, ("- and Dee", "sum:d4")),  # after the one before
            ns.retag(cp, ""),  # the copilot's line is the person's now
            ns.insert_after(-1, "Top line"),
            ns.append(ns.New("### Open questions", "sec:open_questions"), ("- Budget?", "sum:q1")),
        ],
    )
    assert md.split("\n") == [
        "Top line",
        "# Plan",
        "",
        "Our own line",
        "",
        "## Copilot",
        "- [Copilot](mnemosyne:by/copilot) a point",
        "",
        "## Summary",
        "",
        "Body text.",
        "",
        "### Decisions",
        "- Ship it in October",
        "- Hire Cy",
        "- and Dee",
        "- my own aside",
        "",
        "### Action items",
        "- [ ] Ada sends the deck (Ada)",
        "### Open questions",
        "- Budget?",
        "",
    ]
    # Keyed by the NEW markdown's lines; the blank ones have none.
    assert {n: r for n, r in refs.items() if r} == {
        "5": "sec:copilot",
        "8": "sec:summary",
        "12": "sec:decisions",
        "13": "sum:d1",
        "14": "sum:d3",
        "15": "sum:d4",
        "18": "sec:action_items",
        "19": "sum:a1",
        "20": "sec:open_questions",
        "21": "sum:q1",
    }
    assert {n for n, r in refs.items() if not r} == {"0", "1", "3", "6", "10", "16"}

    # The result splits back into the same sections.
    again = ns.split(
        md, [{"line": "", "ref": r, "index": 0, "md_line": int(n)} for n, r in refs.items() if r]
    )
    decisions = again.section(ns.DECISIONS)
    assert decisions is not None
    assert [x.ref for x in decisions.items()] == ["sum:d1", "sum:d3", "sum:d4"]
    assert again.section(ns.OPEN_QUESTIONS) is not None


def test_compose_replace_can_set_or_clear_a_ref():
    s = ns.split("## Copilot\n- a point\n", refs_list({0: "sec:copilot", 1: "cp:0"}))
    md, refs = ns.compose(s.lines, [ns.replace(1, "- a point, edited", "")])
    assert md == "## Copilot\n- a point, edited\n" and refs == {"0": "sec:copilot", "1": ""}
    md, refs = ns.compose(s.lines, [ns.replace(1, "- b", "cp:9")])
    assert refs == {"0": "sec:copilot", "1": "cp:9"}


def test_compose_append_at_the_end_of_any_document():
    empty = ns.split("", [])
    assert ns.compose(empty.lines, [ns.append(("## Copilot", "sec:copilot"), "- x")]) == (
        "## Copilot\n- x\n",
        {"0": "sec:copilot", "1": ""},
    )
    open_end = ns.split("no newline", [])
    assert ns.compose(open_end.lines, [ns.append("- x")])[0] == "no newline\n- x\n"
    closed = ns.split("a\n\n", [])
    assert ns.compose(closed.lines, [ns.append("- x")])[0] == "a\n\n- x\n"
    assert ns.compose([], [ns.append("- x")])[0] == "- x\n"


def test_compose_refuses_a_line_that_is_not_there_or_edited_twice():
    s = ns.split("a\nb\n", [])
    with pytest.raises(ValueError):
        ns.compose(s.lines, [ns.delete(5)])
    with pytest.raises(ValueError):
        ns.compose(s.lines, [ns.insert_after(-2, "x")])
    with pytest.raises(ValueError):
        ns.compose(s.lines, [ns.delete(0), ns.replace(0, "c")])
    with pytest.raises(ValueError):
        ns.compose(s.lines, [ns.Edit("move", 0)])
