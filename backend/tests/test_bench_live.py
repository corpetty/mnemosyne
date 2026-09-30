"""Live speaker-label benchmark: scoring, event bookkeeping and a replay with fakes."""

import asyncio

import numpy as np
import pytest

from mnemosyne.bench_live import (
    CachingTranscriber,
    Line,
    Recorder,
    label_accuracy,
    reference_speaker,
    replay,
    score_lines,
    wav_header,
)
from mnemosyne.config import Settings
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.transcription.live import WavTail

TURNS = [
    {"speaker": "A", "start": 0.0, "end": 4.0},
    {"speaker": "B", "start": 4.0, "end": 8.0},
    {"speaker": "A", "start": 7.5, "end": 9.0},
]


def test_reference_speaker_is_the_most_overlapping_turn():
    assert reference_speaker(1, 2, TURNS) == "A"
    assert reference_speaker(3.5, 5, TURNS) == "B"
    assert reference_speaker(20, 21, TURNS) is None


def test_label_accuracy_uses_the_best_one_to_one_mapping_and_weights():
    pairs = [("A", "s1", 3.0), ("B", "s2", 2.0), ("B", "s1", 1.0), ("A", "s3", 1.0)]
    acc, mapping = label_accuracy(pairs)
    assert mapping == {"s1": "A", "s2": "B"}
    assert acc == pytest.approx(5 / 7)
    assert label_accuracy([]) == (0.0, {})


def test_recorder_follows_renames_but_not_corrections():
    r = Recorder()
    seg = {"text": "hi there", "start": 0.0, "end": 1.0, "speaker": "Speaker 1"}
    r({"type": "live_segment", "segment": seg})
    r({"type": "live_relabel", "old": "Speaker 1", "new": "Dana"})
    r({"type": "live_labels", "labels": [{"start": 0.0, "old": "Dana", "new": "Eve"}]})
    assert r.lines[0].shown == "Dana"


def test_score_lines_weights_by_words_and_skips_unreferenced_lines():
    lines = [
        Line(0.5, 2.0, "one two three", "x", "x"),
        Line(4.5, 6.0, "four five", "x", "y"),
        Line(20, 21, "nobody here", "z", "z"),
    ]
    r = score_lines("m", lines, TURNS, diarize_seconds=0.0, replay_seconds=0.0)
    assert r.scored_words == 5
    assert r.shown_accuracy == pytest.approx(3 / 5)
    assert r.final_accuracy == 1.0
    assert r.labels == 3


def test_wav_header_is_readable_while_growing(tmp_path):
    path = tmp_path / "g.wav"
    path.write_bytes(wav_header(16000) + np.arange(10, dtype="<i2").tobytes())
    tail = WavTail(path)
    assert tail.read_new().tolist() == list(range(10))
    assert tail.sample_rate == 16000


class CountingTranscriber:
    name = "counting"

    def __init__(self):
        self.calls = 0
        self.loaded = False

    def is_loaded(self):
        return self.loaded

    async def load(self):
        self.loaded = True

    async def transcribe(self, path, language=None):
        import wave

        self.calls += 1
        with wave.open(path, "rb") as w:
            seconds = w.getnframes() / w.getframerate()
        return [
            TranscriptSegment(text=f"w{i} word", speaker="UNKNOWN", start=i, end=i + 0.8)
            for i in range(int(seconds))
        ]


def test_caching_transcriber_transcribes_the_same_audio_once(tmp_path):
    inner = CountingTranscriber()
    cache = tmp_path / "cache.json"
    t = CachingTranscriber(inner, cache)
    wav = tmp_path / "a.wav"

    async def twice():
        a = await t.transcribe(str(wav))
        b = await t.transcribe(str(wav))
        return a, b

    import wave

    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(np.ones(32000, dtype="<i2").tobytes())
    a, b = asyncio.run(twice())
    assert inner.calls == 1 and a == b and len(a) == 2
    t.save()
    again = CachingTranscriber(CountingTranscriber(), cache)
    asyncio.run(again.transcribe(str(wav)))
    assert again.inner.calls == 0


def test_replay_scores_a_recording(tmp_path, monkeypatch):
    mode = "clustering"
    monkeypatch.setattr("mnemosyne.bench_live.WORK_DIR", tmp_path)
    pcm = np.full(16000 * 8, 0.1, dtype=np.float32)
    transcriber = CachingTranscriber(CountingTranscriber(), tmp_path / "c.json")
    models = {"embedder": None}
    turns = [{"speaker": "A", "start": 0.0, "end": 4.0}, {"speaker": "B", "start": 4.0, "end": 8.0}]
    r = asyncio.run(replay(Settings(), pcm, turns, mode, transcriber, models))
    assert r.lines == 8
    # No embedder: every line is the source's label.
    assert r.labels == 1 and r.final_accuracy == pytest.approx(0.5)
    assert not list(tmp_path.glob("replay-*.wav"))
