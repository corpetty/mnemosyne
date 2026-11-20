"""What a narration says: markdown made speakable, cut into chunks, names respelled
(synthesis/text.py), and which reference clips an engine is given (synthesis/engine.py)."""

from pathlib import Path

from mnemosyne.synthesis.engine import RefClip, pick_reference
from mnemosyne.synthesis.text import (
    CHUNK_CHARS,
    MIN_CHARS,
    chunks,
    parse_pronunciations,
    respell,
    speakable,
)

SUMMARY = """---
tags: [meeting]
---
# Weekly sync

## Decisions
- Ship **v2** on [[Friday]] ^m-1a2b
- Move the [budget doc](https://docs.example.com/x) to the *shared* drive

## Action items
- [ ] Ada: send the [[People/Grace Hopper|Grace]] notes by Monday
- [x] Corey: book the room

| Owner | Task |
|---|---|
| Ada | Draft |

![diagram](mnemosyne:image/abc.webp@10x10)

```python
print("never read")
```
> A quoted line
See https://example.com/raw for more
"""


def test_markdown_becomes_sentences():
    text = speakable(SUMMARY).split("\n")
    assert text == [
        "Weekly sync.",
        "Decisions.",
        "Ship v2 on Friday.",
        "Move the budget doc to the shared drive.",
        "Action items.",
        "Ada: send the Grace notes by Monday.",
        "Corey: book the room.",
        "Owner, Task.",
        "Ada, Draft.",
        "A quoted line.",
        "See for more.",
    ]


def test_nothing_unspeakable_survives():
    text = speakable(SUMMARY)
    for gone in ("[[", "]]", "http", "**", "^m-", "print", "|", "#", "[ ]", "mnemosyne:"):
        assert gone not in text


def test_chunks_are_whole_sentences_of_bounded_size():
    paragraph = " ".join(f"This is sentence number {i} of a long paragraph." for i in range(40))
    pieces = chunks(paragraph)
    assert len(pieces) > 3
    assert all(len(p) <= CHUNK_CHARS for p in pieces)
    assert all(p.endswith(".") for p in pieces)
    assert " ".join(pieces) == paragraph


def test_a_short_heading_is_read_with_what_follows():
    pieces = chunks("Decisions.\nWe agreed to ship the second version on Friday after the review.")
    assert pieces == ["Decisions. We agreed to ship the second version on Friday after the review."]
    assert all(len(p) >= MIN_CHARS for p in pieces)


def test_a_sentence_too_long_is_cut_at_commas_then_spaces():
    sentence = ", ".join(["a clause with several words in it"] * 20) + "."
    pieces = chunks(sentence, limit=100)
    assert all(len(p) <= 100 for p in pieces)
    assert " ".join(pieces).replace(" ,", ",") == sentence


def test_pronunciations_longest_first_whole_words():
    rules = parse_pronunciations(
        "# how names are said\nMnemosyne -> nee-MOSS-ih-nee\nCorey -> Kory\n"
        "Corey Petty -> Kory Petty, the co-host\nnot a rule\n"
    )
    assert respell("Mnemosyne and Corey Petty met Corey.", rules) == (
        "nee-MOSS-ih-nee and Kory Petty, the co-host met Kory."
    )
    assert respell("Coreys", rules) == "Coreys"  # whole words only


def test_reference_clips_best_first_about_twenty_seconds():
    def clip(name, seconds, quality, text="words"):
        return RefClip(Path(name), text, seconds, quality)

    clips = [
        clip("a", 8, 0.5),
        clip("b", 8, 0.9),
        clip("c", 8, 0.8),
        clip("d", 8, 0.7),
        clip("e", 30, 1.0, text=""),  # no transcript: never used
    ]
    chosen = [c.path.name for c in pick_reference(clips)]
    assert chosen == ["b", "c", "d"]  # 24 s: stops once past 20
    assert pick_reference([clip("long", 45, 1.0)])[0].path.name == "long"  # one clip is enough
