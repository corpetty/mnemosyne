"""Where a reader's passage of a publication's notes is in the live document
(services/notes_anchor.py): R (the rendering the reader selected in) aligned with the fold's
plain text, the landing span verified by redacting it again."""

import re
from pathlib import Path

from mnemosyne.models.publication import PublicationSelection
from mnemosyne.models.session import Session
from mnemosyne.services import notes_anchor
from mnemosyne.services.comments import plain_markdown
from mnemosyne.services.publishing import Redaction

SHA = "ab" * 32
CONTEXT = 32  # viewer/src/comments.ts CONTEXT_CHARS

# The document's plain text, as the sidecar answers it: no markdown markers, chips as their
# text, a table's cells separated by tabs, an image line as its caption, the backend's summary
# section at the end (line 9 is its `sec:summary` heading).
PLAIN = (
    "Planning\n"  # 0
    "Ada Lovelace will send the budget.\n"  # 1
    "Ask @Ada Lovelace about the Roadmap sync before Friday\n"  # 2
    "Owner\tTask\n"  # 3
    "Ada Lovelace\tBudget review\n"  # 4
    "Whiteboard\n"  # 5
    "After the photo we agreed to ship.\n"  # 6
    "Ship it. Then ask Grace.\n"  # 7
    "Later: ship it. Then ask Grace.\n"  # 8
    "Summary\n"  # 9
    "A summary-only sentence lives here.\n"  # 10
)
REFS = [{"line": "9" * 64, "ref": "sec:summary", "index": 9, "md_line": 17}]

# The manifest's notes for the same document (Ada hidden: "Person 2"), as build_content makes
# them: chips as their text, the image line kept, no summary section.
PUBLISHED = (
    "# Planning\n\n"
    "Person 2 will send the budget.\n\n"
    "- [ ] Ask @Person 2 about the Roadmap sync before Friday\n\n"
    "| Owner | Task |\n| --- | --- |\n| Person 2 | Budget review |\n\n"
    f"![Whiteboard](mnemosyne:image/{SHA}.webp@640x480)\n\n"
    "After the photo we agreed to ship.\n\n"
    "Ship it. Then ask Grace.\n\n"
    "Later: ship it. Then ask Grace.\n"
)

# The notes box's textContent in the viewer (marked: blocks and cells on lines of their own, no
# list markers or pipes; the picture did not load, so its placeholder text).
TEXT_CONTENT = "\n".join(
    [
        "Planning",
        "Person 2 will send the budget.",
        "",
        "Ask @Person 2 about the Roadmap sync before Friday",
        "",
        "Owner",
        "Task",
        "Person 2",
        "Budget review",
        "[image: Whiteboard]",
        "After the photo we agreed to ship.",
        "Ship it. Then ask Grace.",
        "Later: ship it. Then ask Grace.",
    ]
)


def redact(text: str) -> str:
    return text.replace("Ada Lovelace", "Person 2")


def passage(exact: str, nth: int = 0, full: str = TEXT_CONTENT) -> dict:
    """The anchor the viewer sends (comments.ts passageAnchor over the box's textContent)."""
    s = [m.start() for m in re.finditer(re.escape(exact), full)][nth]
    e = s + len(exact)
    return {
        "target": "notes",
        "target_id": None,
        "quote_id": None,
        "exact": exact,
        "prefix": full[max(0, s - CONTEXT) : s],
        "suffix": full[e : e + CONTEXT],
    }


def where(anchor: dict, plain: str = PLAIN, published: str = PUBLISHED) -> str | None:
    found = notes_anchor.locate(plain, REFS, anchor, notes_anchor.as_read(published), redact)
    return None if found is None else plain[found[0] : found[1]]


def test_a_passage_as_is():
    assert where(passage("will send the budget")) == "will send the budget"
    # Across two blocks: the reader's newline is whitespace, as the document's line end is.
    assert where(passage("budget.\n\nAsk")) == "budget.\nAsk"


def test_a_redacted_name_lands_on_the_real_text():
    assert where(passage("Person 2 will send")) == "Ada Lovelace will send"
    # Part of the replacement: the whole name, never half of it.
    assert where(passage("son 2 will")) == "Ada Lovelace will"


def test_inside_a_chips_text():
    assert where(passage("Roadmap sync")) == "Roadmap sync"
    assert where(passage("@Person 2 about")) == "@Ada Lovelace about"


def test_in_a_table_cell():
    assert where(passage("Budget review")) == "Budget review"
    assert where(passage("Person 2", nth=2)) == "Ada Lovelace"
    found = notes_anchor.locate(
        PLAIN, REFS, passage("Budget review"), notes_anchor.as_read(PUBLISHED), redact
    )
    assert found is not None and PLAIN[found[0] - 1] == "\t"


def test_after_an_image_line():
    assert where(passage("After the photo")) == "After the photo"
    # Selected from the image's text on: the caption's line, then the text.
    assert where(passage("Whiteboard]\nAfter")) == "Whiteboard\nAfter"


def test_a_passage_only_in_a_summary_section_is_not_placed():
    full = TEXT_CONTENT + "\nA summary-only sentence lives here."
    assert where(passage("summary-only sentence", full=full)) is None


def test_the_section_is_never_the_landing_place():
    # The same words in the person's notes and in the summary below: the notes' line.
    plain = PLAIN.replace("A summary-only sentence lives here.", "After the photo we agreed")
    found = notes_anchor.locate(
        plain, REFS, passage("After the photo we agreed"), notes_anchor.as_read(PUBLISHED), redact
    )
    assert found is not None and found[0] == plain.index("After the photo")


def test_prefix_and_suffix_choose_among_repeats():
    first = notes_anchor.locate(
        PLAIN, REFS, passage("Then ask Grace", 0), notes_anchor.as_read(PUBLISHED), redact
    )
    second = notes_anchor.locate(
        PLAIN, REFS, passage("Then ask Grace", 1), notes_anchor.as_read(PUBLISHED), redact
    )
    assert first is not None and second is not None
    assert first[0] == PLAIN.index("Then ask Grace") < second[0] == PLAIN.rindex("Then ask Grace")
    # Case differs, words are the same: the prefix still picks the later line.
    assert where(passage("ship it", 0)) == "ship it"


def test_a_passage_the_notes_no_longer_contain():
    old = PUBLISHED + "\nLunch is at noon in the big room.\n"
    full = TEXT_CONTENT + "\nLunch is at noon in the big room."
    assert where(passage("Lunch is at noon", full=full), published=old) is None
    # Reworded since: the verification refuses a span that does not read as the passage.
    plain = PLAIN.replace("will send the budget", "may send a draft")
    assert where(passage("will send the budget"), plain=plain) is None


def test_nothing_to_place():
    assert where({"exact": "   "}) is None
    assert notes_anchor.locate("", REFS, passage("Planning"), "Planning", redact) is None
    over = "x" * (notes_anchor.MAX_CHARS + 1)
    assert notes_anchor.locate(over, [], passage("Planning"), "Planning", redact) is None


def test_without_refs_every_line_is_own():
    found = notes_anchor.locate(
        PLAIN, None, passage("Planning"), notes_anchor.as_read(PUBLISHED), redact
    )
    assert found == (0, len("Planning"))


def test_as_read_is_plain_already():
    r = notes_anchor.as_read(PUBLISHED)
    assert plain_markdown(r) == r
    assert "[image: Whiteboard]" in r and "mnemosyne:" not in r
    # Inside a code fence an image line is text, as in prepareNotes.
    fenced = f"```\n![x](mnemosyne:image/{SHA}.webp@1x1)\n```\n"
    assert "[image: x]" not in notes_anchor.as_read(fenced)
    assert notes_anchor.as_read(f"![a \\[b\\]](mnemosyne:image/{SHA}.jpeg@1x1)") == "[image: a [b]]"
    assert notes_anchor.as_read(f"![](mnemosyne:image/{SHA}.jpeg@1x1)") == "[image]"


# viewer/src/notes.ts, copied: the Python twin must change with it.
VIEWER_IMAGE_TEXT = """export function imageText(caption: string): string {
	return caption ? `[image: ${caption}]` : '[image]';
}"""
VIEWER_IMAGE_LINE = (
    r"const IMAGE_LINE = /^!\[((?:[^\]\\\n]|\\.)*)\]\(mnemosyne:image\/([0-9a-f]{64})"
    r"\.(?:webp|jpeg)@\d+x\d+\)$/;"
)


def test_the_image_twin_matches_the_viewer():
    source = (Path(__file__).parents[2] / "viewer" / "src" / "notes.ts").read_text()
    assert VIEWER_IMAGE_TEXT in source
    assert VIEWER_IMAGE_LINE in source
    assert notes_anchor.image_text("Whiteboard") == "[image: Whiteboard]"
    assert notes_anchor.image_text("") == "[image]"
    ts = VIEWER_IMAGE_LINE.removeprefix("const IMAGE_LINE = /").removesuffix("/;")
    assert notes_anchor._IMAGE_LINE.pattern == ts.replace("\\/", "/")


def test_locator_with_the_real_redaction(ctx):
    session = ctx.repo.save(
        Session(
            name="Planning",
            attendees=["Corey Petty", "Ada Lovelace"],
            notes="Ada Lovelace will send the budget.\n\nCorey Petty reviews it.\n",
        )
    )
    sel = PublicationSelection(notes=True, attendees=["Corey Petty"])
    shown = Redaction(session, sel).text("Ada Lovelace")
    assert shown != "Ada Lovelace"  # hidden: "Person N"
    full = f"{shown} will send the budget.\nCorey Petty reviews it."
    locate = notes_anchor.locator(ctx, session, sel, passage(f"{shown} will", full=full))
    plain = "Ada Lovelace will send the budget.\nCorey Petty reviews it.\n"
    found = locate(plain, [])
    assert found is not None and plain[found[0] : found[1]] == "Ada Lovelace will"
    # Notes not published: nothing to find them in.
    off = notes_anchor.locator(
        ctx, session, sel.model_copy(update={"notes": False}), {"exact": "x"}
    )
    assert off(plain, []) is None
