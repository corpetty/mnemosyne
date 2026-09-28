"""Correcting live speaker labels by re-diarizing the recording so far (live_rediarize.py)."""

import wave

import numpy as np
import pytest

from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.transcription.engine import DiarizationResult, SpeakerTurn
from mnemosyne.transcription.live import LiveSource, LiveTranscriber
from mnemosyne.transcription.live_rediarize import LiveRediarizer
from mnemosyne.transcription.live_speakers import Cluster, OnlineClusterer

RATE = 16000


class FakeDiarizer:
    name = "nemotron"

    def __init__(self, turns, embeddings=None, fail=False):
        self.turns = turns
        self.embeddings = embeddings or {}
        self.fail = fail
        self.calls = []

    async def load(self):
        pass

    async def diarize_pcm(self, pcm, sample_rate):
        if self.fail:
            raise RuntimeError("CUDA out of memory")
        self.calls.append((pcm.size, sample_rate))
        return DiarizationResult(turns=self.turns, embeddings=self.embeddings)


def recording(path, seconds):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(np.zeros(int(RATE * seconds), dtype=np.int16).tobytes())
    return path


def live_with(tmp_path, lines, seconds=40.0, clusterer=None):
    source = LiveSource(
        path=recording(tmp_path / "sys.wav", seconds), speaker="Remote", kind="system", diarize=True
    )
    live = LiveTranscriber(None, [source], lambda e: None, "s1", clusterer=clusterer)
    for start, end, speaker in lines:
        live.committed.append(TranscriptSegment(text="x", speaker=speaker, start=start, end=end))
        live.committed_kind.append("system")
    return live, source


def turns(*spec):
    return [SpeakerTurn(start=a, end=b, speaker=s) for a, b, s in spec]


@pytest.mark.anyio
async def test_a_pass_corrects_labels_and_keeps_the_names_people_saw(tmp_path):
    # Clustering called the second person "Speaker 2" but got one of their lines wrong.
    live, source = live_with(
        tmp_path,
        [(0, 5, "Speaker 1"), (5, 9, "Speaker 1"), (10, 15, "Speaker 2"), (16, 20, "Speaker 2")],
    )
    events = []
    live.emit = events.append
    diarizer = FakeDiarizer(turns((0, 4.8, "SPEAKER_00"), (4.8, 20, "SPEAKER_01")))
    r = LiveRediarizer(diarizer, live, events.append, "s1")

    assert await r.pass_once(source) == 1
    assert [s.speaker for s in live.committed] == [
        "Speaker 1",
        "Speaker 2",
        "Speaker 2",
        "Speaker 2",
    ]
    (event,) = events
    assert event["type"] == "live_labels" and event["source"] == "system"
    assert event["labels"] == [{"start": 5, "old": "Speaker 1", "new": "Speaker 2"}]
    assert diarizer.calls == [(RATE * 40, RATE)]


@pytest.mark.anyio
async def test_voice_profiles_name_speakers(tmp_path):
    live, source = live_with(tmp_path, [(0, 5, "Speaker 1"), (6, 9, "Speaker 1")])
    diarizer = FakeDiarizer(
        turns((0, 5.5, "SPEAKER_00"), (5.5, 9, "SPEAKER_01")), embeddings={"SPEAKER_01": [1.0, 0.0]}
    )
    r = LiveRediarizer(
        diarizer,
        live,
        lambda e: None,
        "s1",
        match_names=lambda emb: {"SPEAKER_01": "Ana"} if emb else {},
    )
    await r.pass_once(source)
    assert [s.speaker for s in live.committed] == ["Speaker 1", "Ana"]


@pytest.mark.anyio
async def test_new_speakers_get_new_numbers_not_the_channel_label(tmp_path):
    # Lines still carry the channel label (no embedder): nothing to keep, so number them.
    live, source = live_with(tmp_path, [(0, 5, "Remote"), (6, 9, "Remote")])
    diarizer = FakeDiarizer(turns((0, 5.5, "SPEAKER_00"), (5.5, 9, "SPEAKER_01")))
    await LiveRediarizer(diarizer, live, lambda e: None, "s1").pass_once(source)
    assert [s.speaker for s in live.committed] == ["Speaker 1", "Speaker 2"]


@pytest.mark.anyio
async def test_the_clusterer_learns_the_corrected_names(tmp_path):
    clusterer = OnlineClusterer()
    clusterer.clusters = [
        Cluster(number=1, total=np.array([1.0, 0.0])),
        Cluster(number=3, total=np.array([0.0, 1.0])),
    ]
    live, source = live_with(
        tmp_path,
        [(0, 5, "Speaker 1"), (6, 9, "Speaker 3"), (10, 14, "Speaker 3")],
        clusterer=clusterer,
    )
    diarizer = FakeDiarizer(
        turns((0, 5.5, "SPEAKER_00"), (5.5, 14, "SPEAKER_01")),
        embeddings={"SPEAKER_01": [0.0, 1.0]},
    )
    r = LiveRediarizer(
        diarizer, live, lambda e: None, "s1", match_names=lambda emb: {"SPEAKER_01": "Ana"}
    )
    await r.pass_once(source)
    assert [c.label for c in clusterer.clusters] == ["Speaker 1", "Ana"]


@pytest.mark.anyio
async def test_short_recordings_and_later_lines_are_left_alone(tmp_path):
    live, source = live_with(tmp_path, [(0, 5, "Speaker 1")], seconds=10)
    diarizer = FakeDiarizer(turns((0, 10, "SPEAKER_00")))
    assert await LiveRediarizer(diarizer, live, lambda e: None, "s1").pass_once(source) == 0
    assert diarizer.calls == []  # under min_seconds: not even diarized


@pytest.mark.anyio
async def test_a_failing_diarizer_stops_quietly(tmp_path):
    live, _ = live_with(tmp_path, [(0, 5, "Speaker 1")])
    r = LiveRediarizer(FakeDiarizer([], fail=True), live, lambda e: None, "s1", interval=0.01)
    await r.run()  # returns instead of raising or looping
    assert live.committed[0].speaker == "Speaker 1"


def test_only_used_with_nemotron_unless_asked(monkeypatch):
    from mnemosyne.config import Settings
    from mnemosyne.transcription import registry

    monkeypatch.setattr(registry, "nemotron_available", lambda: False)
    assert registry.build_live_rediarizer(Settings(diarizer="auto")) is None
    assert registry.build_live_rediarizer(Settings(live_rediarize="off")) is None
    assert registry.build_live_rediarizer(Settings(live_diarization=False)) is None
