"""Benchmark scoring: alignment, word error rate, speaker accuracy, references."""

import json
import shutil

import pytest

from mnemosyne.bench import (
    align,
    load_reference,
    normalize,
    score,
    speaker_accuracy,
    synthesize,
    word_errors,
)


def test_normalize():
    assert normalize("Don’t STOP, it's 5 o'clock!") == ["don't", "stop", "it's", "5", "o'clock"]


def test_align_and_wer():
    ref = "the cat sat on the mat".split()
    pairs = align(ref, "the cat sat on mat".split())
    assert [i for i, j in pairs if j is None] == [4]  # the second "the" was dropped
    e = word_errors(ref, "a cat sat on the mat today".split())
    assert (e.substitutions, e.deletions, e.insertions) == (1, 0, 1)
    assert e.wer == pytest.approx(2 / 6)
    assert word_errors(ref, ref).wer == 0.0
    assert word_errors([], ["x"]).wer == 0.0


def test_speaker_accuracy_finds_best_mapping():
    ref = [("hello", "Alice"), ("there", "Alice"), ("hi", "Bob"), ("back", "Bob")]
    hyp = [
        ("hello", "SPEAKER_01"),
        ("there", "SPEAKER_01"),
        ("hi", "SPEAKER_00"),
        ("back", "SPEAKER_01"),
    ]
    acc, mapping = speaker_accuracy(ref, hyp)
    assert mapping == {"SPEAKER_01": "Alice", "SPEAKER_00": "Bob"}
    assert acc == 0.75


def test_score_segments():
    reference = [
        {"speaker": "Alice", "text": "Ship it in October."},
        {"speaker": "Bob", "text": "Agreed."},
    ]
    hyp = [{"speaker": "S1", "text": "ship it in october"}, {"speaker": "S2", "text": "agree"}]
    errors, acc, mapping = score(reference, hyp)
    assert errors.substitutions == 1 and errors.reference_words == 5
    assert acc == 1.0 and mapping == {"S1": "Alice", "S2": "Bob"}
    _, acc, _ = score([{"text": "no speakers"}], [{"speaker": "S", "text": "no speakers"}])
    assert acc is None


def test_load_reference(tmp_path):
    (tmp_path / "r.txt").write_text("Alice: hello there\n\nBob: hi\njust text\n")
    assert load_reference(tmp_path / "r.txt") == [
        {"speaker": "Alice", "text": "hello there"},
        {"speaker": "Bob", "text": "hi"},
        {"speaker": "", "text": "just text"},
    ]
    (tmp_path / "r.json").write_text(json.dumps({"transcript": [{"speaker": "A", "text": "x"}]}))
    assert load_reference(tmp_path / "r.json") == [{"speaker": "A", "text": "x"}]


@pytest.mark.skipif(not shutil.which("espeak-ng") or not shutil.which("ffmpeg"), reason="espeak")
def test_synthesize(tmp_path):
    audio, reference = synthesize(tmp_path)
    assert audio.exists() and len(reference) == 6
    assert {r["speaker"] for r in reference} == {"Alice", "Bob"}
    assert reference[1]["start"] > reference[0]["end"]
