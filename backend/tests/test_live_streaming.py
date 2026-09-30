"""Live speaker labels from Nemotron streaming, with a scripted fake stream (no NeMo)."""

import wave
from pathlib import Path

import numpy as np
import pytest

from mnemosyne.models.transcript import TranscriptSegment, WordSegment
from mnemosyne.transcription.diarizers.nemotron_stream import SpeakerFrames
from mnemosyne.transcription.live import LiveSource, LiveTranscriber
from mnemosyne.transcription.live_rediarize import LiveRediarizer
from mnemosyne.transcription.live_streaming import (
    LiveSpeakerNames,
    SpeakerTimeline,
    word_speakers,
)

RATE = 48000
pytestmark = pytest.mark.anyio


class FakeStream:
    """Scores audio `latency` seconds behind what it was given, with speakers from `script`:
    (start, end, index) spans; nobody speaks elsewhere."""

    def __init__(self, script, latency=1.04, fail_after=None):
        self.script = script
        self.latency = latency
        self.fail_after = fail_after
        self.samples = 0
        self.done = 0
        self.pushes = 0
        self.flushed = False

    def _frames(self, upto: float):
        end = int(upto * 100)
        if end <= self.done:
            return None
        probs = np.zeros((end - self.done, 8), dtype=np.float32)
        for start, stop, index in self.script:
            a, b = max(int(start * 100), self.done), min(int(stop * 100), end)
            if b > a:
                probs[a - self.done : b - self.done, index] = 0.9
        out = SpeakerFrames(start=self.done, probs=probs)
        self.done = end
        return out

    async def push(self, pcm, rate):
        self.pushes += 1
        if self.fail_after is not None and self.pushes > self.fail_after:
            raise RuntimeError("CUDA error: device lost")
        self.samples += pcm.size
        return self._frames(max(0.0, self.samples / rate - self.latency))

    async def flush(self):
        self.flushed = True
        return self._frames(self.samples / RATE)

    def stats(self):
        return {"steps": self.pushes}


class DurationTranscriber:
    """One segment per whole second of audio [i, i+0.8] with text `w<i>`."""

    name = "fake-live"

    def __init__(self):
        self.calls = 0

    def is_loaded(self):
        return True

    async def load(self):
        pass

    async def transcribe(self, path, language=None):
        self.calls += 1
        with wave.open(path, "rb") as w:
            seconds = w.getnframes() / w.getframerate()
        return [
            TranscriptSegment(text=f"w{i}", speaker="UNKNOWN", start=i, end=i + 0.8)
            for i in range(int(seconds))
        ]


class WordTranscriber(DurationTranscriber):
    """One segment over the first 3.6 s: eight words, 0.45 s apart."""

    async def transcribe(self, path, language=None):
        self.calls += 1
        words = [WordSegment(word=f"x{i}", start=0.45 * i, end=0.45 * i + 0.4) for i in range(8)]
        return [
            TranscriptSegment(
                text=" ".join(w.word for w in words),
                speaker="UNKNOWN",
                start=0.0,
                end=3.6,
                words=words,
            )
        ]


def _wav(tmp_path) -> Path:
    path = tmp_path / "live.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
    return path


def _append(path: Path, seconds: float, value: int = 1000):
    with path.open("ab") as f:
        f.write(np.full(int(RATE * seconds), value, dtype=np.int16).tobytes())


def _live(path, stream, transcriber=None, **kw):
    events = []
    source = LiveSource(path=path, speaker="Remote", kind="system", diarize=True, stream=stream)
    live = LiveTranscriber(
        transcriber=transcriber or DurationTranscriber(),
        sources=[source],
        emit=events.append,
        session_id="s1",
        adaptive=False,
        **kw,
    )
    return live, source, events


def _segments(events):
    return [
        (e["segment"]["text"], e["segment"]["speaker"])
        for e in events
        if e["type"] == "live_segment"
    ]


async def test_lines_take_the_speaker_the_stream_heard(tmp_path):
    path = _wav(tmp_path)
    stream = FakeStream([(0, 3, 0), (3, 10, 1)])
    live, source, events = _live(path, stream)
    _append(path, 6.0)
    await live.tick()
    assert _segments(events) == [
        ("w0", "Speaker 1"),
        ("w1", "Speaker 1"),
        ("w2", "Speaker 1"),
        ("w3", "Speaker 2"),
        ("w4", "Speaker 2"),
    ]
    assert live.pending == []  # all of them ended before the stream's frontier
    assert source.timeline.frontier == pytest.approx(6.0 - 1.04, abs=0.01)


async def test_labels_skip_names_of_other_sources_and_stay_stable(tmp_path):
    path = _wav(tmp_path)
    live, _, events = _live(path, FakeStream([(0, 2, 3), (2, 4, 0), (4, 20, 3)]))
    live.sources[0].speaker = "Speaker 1"  # a source label that looks like a stream label
    _append(path, 6.0)
    await live.tick()
    labels = [s for _, s in _segments(events)]
    assert labels == ["Speaker 2", "Speaker 2", "Speaker 3", "Speaker 3", "Speaker 2"]


async def test_lines_past_the_frontier_are_corrected_on_the_next_tick(tmp_path):
    path = _wav(tmp_path)
    live, _, events = _live(path, FakeStream([(0, 4, 0), (4, 20, 1)], latency=2.0))
    _append(path, 6.0)
    await live.tick()
    # w4 [4, 4.8] ends past the frontier (4.0): shown as whoever spoke before, pending.
    assert _segments(events)[-1] == ("w4", "Speaker 1")
    assert len(live.pending) == 1
    events.clear()
    _append(path, 3.0)
    await live.tick()
    labels = [e for e in events if e["type"] == "live_labels"]
    assert labels == [
        {
            "type": "live_labels",
            "session_id": "s1",
            "source": "system",
            "labels": [{"start": 4.0, "old": "Speaker 1", "new": "Speaker 2"}],
        }
    ]
    assert live.committed[4].speaker == "Speaker 2"


async def test_silence_is_still_streamed(tmp_path):
    path = _wav(tmp_path)
    stream = FakeStream([])
    transcriber = DurationTranscriber()
    live, source, _ = _live(path, stream, transcriber)
    _append(path, 3.0, value=0)
    await live.tick()
    assert transcriber.calls == 0  # the silence skip saves the transcriber...
    assert stream.samples == 3 * RATE  # ...but the stream hears everything
    assert source.timeline.frames == int((3.0 - 1.04) * 100)


async def test_a_line_is_split_where_the_speaker_changes(tmp_path):
    path = _wav(tmp_path)
    live, _, events = _live(path, FakeStream([(0, 2.0, 0), (2.0, 10, 1)]), WordTranscriber())
    _append(path, 6.0)
    await live.tick()
    assert _segments(events) == [
        ("x0 x1 x2 x3 x4", "Speaker 1"),
        ("x5 x6 x7", "Speaker 2"),
    ]
    starts = [e["segment"]["start"] for e in events if e["type"] == "live_segment"]
    assert starts == [0.0, 2.25]


async def test_one_word_is_not_split_off(tmp_path):
    path = _wav(tmp_path)
    # Speaker 1 only under x3 (1.35-1.75 s).
    script = [(0, 1.3, 0), (1.3, 1.8, 1), (1.8, 10, 0)]
    live, _, events = _live(path, FakeStream(script), WordTranscriber())
    _append(path, 6.0)
    await live.tick()
    assert _segments(events) == [("x0 x1 x2 x3 x4 x5 x6 x7", "Speaker 1")]


async def test_stop_flushes_the_stream_before_the_last_lines(tmp_path):
    path = _wav(tmp_path)
    stream = FakeStream([(0, 3, 0), (3, 10, 1)], latency=3.0)
    live, source, events = _live(path, stream)
    _append(path, 4.0)
    await live.tick(flush=True)
    assert stream.flushed
    assert source.timeline.frontier == pytest.approx(4.0)
    assert _segments(events) == [
        ("w0", "Speaker 1"),
        ("w1", "Speaker 1"),
        ("w2", "Speaker 1"),
        ("w3", "Speaker 2"),
    ]
    assert live.pending == []


async def test_a_failing_stream_hands_over_to_voice_clustering(tmp_path):
    path = _wav(tmp_path)
    live, source, events = _live(path, FakeStream([(0, 20, 0)], fail_after=1))
    _append(path, 3.0)
    await live.tick()
    assert _segments(events)[0][1] == "Speaker 1"
    _append(path, 3.0)
    await live.tick()
    assert source.stream is None
    status = [e["message"] for e in events if e["type"] == "live_status"]
    assert status and "Nemotron failed" in status[-1]
    # No embedder here: the source's own label, and the lines already shown keep theirs.
    assert _segments(events)[-1][1] == "Remote"
    assert live.committed[0].speaker == "Speaker 1"


async def test_status_says_nemotron(tmp_path):
    import asyncio

    path = _wav(tmp_path)
    live, _, events = _live(path, FakeStream([]))
    task = asyncio.create_task(live.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert any(e.get("message") == "Live · speakers by Nemotron" for e in events)


async def test_rename_relabels_lines_and_future_labels(tmp_path):
    path = _wav(tmp_path)
    live, _, events = _live(path, FakeStream([(0, 20, 0)]))
    _append(path, 4.0)
    await live.tick()
    live.rename("Speaker 1", "Dana")
    assert {s.speaker for s in live.committed} == {"Dana"}
    assert {"type": "live_relabel", "session_id": "s1", "old": "Speaker 1", "new": "Dana"} in events
    _append(path, 2.0)
    await live.tick()
    assert _segments(events)[-1][1] == "Dana"


def test_rediarizer_leaves_streamed_sources_alone(tmp_path):
    live, source, _ = _live(_wav(tmp_path), FakeStream([]))
    other = LiveSource(path=tmp_path / "mic.wav", speaker="Me", kind="mic", diarize=True)
    live.sources.append(other)
    r = LiveRediarizer(diarizer=None, live=live, emit=lambda e: None, session_id="s1")
    assert r.sources == [other]


# ---- timeline and names ----------------------------------------------------------------


def _timeline(spans, seconds=10.0, overlap=()):
    tl = SpeakerTimeline()
    n = int(seconds * 100)
    probs = np.zeros((n, 8), dtype=np.float32)
    for start, end, index in spans:
        probs[int(start * 100) : int(end * 100), index] = 0.8
    for start, end, index in overlap:
        probs[int(start * 100) : int(end * 100), index] = 0.7
    # In two pieces, like chunks.
    tl.add(SpeakerFrames(0, probs[: n // 3]))
    tl.add(SpeakerFrames(n // 3, probs[n // 3 :]))
    return tl


def test_timeline_speakers_turns_and_clean_seconds():
    tl = _timeline([(1, 3, 0), (3, 5, 2), (6, 7, 0)], overlap=[(2, 2.5, 2)])
    assert tl.frontier == pytest.approx(10.0)
    assert tl.speaker_between(0, 1) is None
    assert tl.speaker_between(2.5, 4.0) == 2
    assert tl.speakers() == [0, 2]
    assert [(t.start, t.end, t.speaker) for t in tl.turns()] == [
        (1.0, 3.0, "0"),
        (3.0, 5.0, "2"),
        (6.0, 7.0, "0"),
    ]
    clean = tl.turns(clean=True)
    assert (clean[0].start, clean[0].end) == (1.0, 2.0)
    assert (clean[1].start, clean[1].end) == (2.5, 3.0)
    assert tl.seconds(0) == pytest.approx(2.5)
    assert tl.seconds(0, clean=False) == pytest.approx(3.0)
    with pytest.raises(ValueError):
        tl.add(SpeakerFrames(5, np.zeros((1, 8), dtype=np.float32)))


def test_timeline_grows_past_its_first_allocation():
    tl = _timeline([(0, 90, 1)], seconds=90.0)
    assert tl.frames == 9000
    assert tl.speaker_between(80, 85) == 1


def test_word_speakers_fill_gaps_and_absorb_short_runs():
    words = [WordSegment(word=f"w{i}", start=0.3 * i, end=0.3 * i + 0.2) for i in range(12)]
    # Words in the silence between speakers take the one before them.
    gap = _timeline([(0, 1, 0), (2, 5, 1)])
    assert word_speakers(words, gap) == [0] * 7 + [1] * 5
    # One word of someone else inside a run is not a speaker change.
    blip = _timeline([(0, 1.2, 0), (1.2, 1.4, 1), (1.4, 5, 0)])
    assert word_speakers(words, blip) == [0] * 12
    # Two words are.
    two = _timeline([(0, 1.2, 0), (1.2, 1.7, 1), (1.7, 5, 0)])
    assert word_speakers(words, two) == [0] * 4 + [1] * 2 + [0] * 6


def test_names_allocate_and_rename():
    names = LiveSpeakerNames()
    assert names.label("system", 0) == "Speaker 1"
    assert names.label("system", 3, taken={"Speaker 2"}) == "Speaker 3"
    assert names.label("mic", 0) == "Speaker 2"
    assert names.label("system", 0) == "Speaker 1"
    assert names.rename("Speaker 1", "Dana")
    assert names.label("system", 0) == "Dana"
    assert not names.rename("Speaker 3", "Dana")  # someone else's name already
    assert not names.rename("nobody", "Eve")
