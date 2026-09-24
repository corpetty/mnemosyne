"""Parakeet token -> word merging (pure function, no model)."""

from mnemosyne.transcription.transcribers.parakeet import _words_from_tokens


def test_tokens_merge_into_words_with_absolute_times():
    tokens = ["▁Yeah", ",", "▁that", "'s", "▁all"]
    stamps = [0.0, 0.2, 0.4, 0.5, 0.9]
    words = _words_from_tokens(tokens, stamps, seg_start=3.0, seg_end=4.5)
    assert [(w.word, w.start, w.end) for w in words] == [
        ("Yeah,", 3.0, 3.4),
        ("that's", 3.4, 3.9),
        ("all", 3.9, 4.5),
    ]


def test_missing_or_mismatched_timestamps_gives_none():
    assert _words_from_tokens(None, None, 0, 1) is None
    assert _words_from_tokens(["▁a"], [0.0, 0.1], 0, 1) is None
    assert _words_from_tokens([], [], 0, 1) is None
