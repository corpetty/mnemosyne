"""Speaker-bleed removal between the mic and system transcripts."""

from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.transcription.dedup import normalize, remove_echo, similarity


def _seg(text, start, end, speaker="Me"):
    return TranscriptSegment(text=text, speaker=speaker, start=start, end=end)


def test_normalize_and_similarity():
    assert normalize("Hello, World!  it's") == "hello world it's"
    assert similarity("that's all we need", "So, that's all we need. Thanks") == 1.0
    assert similarity("completely different words", "nothing alike here at all") < 0.5
    assert similarity("", "x") == 0.0


def test_drops_mic_segments_that_repeat_system_audio():
    mic = [
        _seg("Yeah that's all we need.", 3.2, 4.8),  # bleed (system said it at 3.0-4.5)
        _seg("I think we should ship it.", 6.0, 8.0),  # genuinely local
        _seg("Okay", 10.4, 10.9),  # bleed of a short reply
    ]
    system = [
        _seg("That's all we need.", 3.0, 4.5, "SPEAKER_00"),
        _seg("Sounds good. Okay.", 10.0, 11.0, "SPEAKER_01"),
    ]
    kept, dropped = remove_echo(mic, system)
    assert [s.text for s in kept] == ["I think we should ship it."]
    assert [s.text for s in dropped] == ["Yeah that's all we need.", "Okay"]


def test_same_words_far_apart_in_time_are_kept():
    mic = [_seg("that's all we need", 30.0, 31.0)]
    system = [_seg("that's all we need", 3.0, 4.0, "S")]
    kept, dropped = remove_echo(mic, system)
    assert len(kept) == 1 and not dropped


def test_threshold_and_empty_inputs():
    mic = [_seg("we should probably ship the release today", 1.0, 3.0)]
    system = [_seg("we should probably wait", 1.0, 3.0, "S")]
    assert remove_echo(mic, system, threshold=0.95)[0] == mic
    assert remove_echo([], system) == ([], [])
    assert remove_echo(mic, []) == (mic, [])
