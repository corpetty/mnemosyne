"""Online speaker clustering and live speaker labels (no models)."""

import wave

import numpy as np
import pytest

from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.transcription.live import LiveSource, LiveTranscriber
from mnemosyne.transcription.live_speakers import OnlineClusterer, cosine

A = [1.0, 0.0, 0.0, 0.0]
A2 = [0.95, 0.2, 0.0, 0.0]
B = [0.0, 1.0, 0.0, 0.0]
C = [0.0, 0.0, 1.0, 0.0]


def test_cosine_basics():
    assert cosine(A, A) == pytest.approx(1.0)
    assert cosine(A, B) == pytest.approx(0.0)
    assert cosine(A, [1.0, 0.0]) == -1.0


def test_clusters_by_similarity():
    c = OnlineClusterer(threshold=0.5)
    assert c.assign(A) == ("Speaker 1", [])
    assert c.assign(B) == ("Speaker 2", [])
    assert c.assign(A2) == ("Speaker 1", [])
    assert c.assign(C) == ("Speaker 3", [])
    assert [cl.count for cl in c.clusters] == [2, 1, 1]


def test_max_speakers_caps_new_clusters():
    c = OnlineClusterer(threshold=0.9, max_speakers=2)
    c.assign(A)
    c.assign(B)
    label, _ = c.assign(C)
    assert len(c.clusters) == 2 and label in ("Speaker 1", "Speaker 2")


def test_known_voices_named_and_unique():
    c = OnlineClusterer(threshold=0.5, known_threshold=0.6, known={"Alice": A, "Bob": B})
    label, renames = c.assign(A2)
    assert label == "Alice" and renames == [("Speaker 1", "Alice")]
    assert c.assign(B)[0] == "Bob"
    assert c.assign(C)[0] == "Speaker 3"  # unknown voice stays anonymous
    # a second cluster close to Alice cannot also be called Alice
    c2 = OnlineClusterer(threshold=0.99, merge_margin=0.5, known={"Alice": A})
    c2.assign(A)
    assert c2.assign(A2)[0] == "Speaker 2"


def test_converging_clusters_merge_with_relabel():
    c = OnlineClusterer(threshold=0.8, merge_margin=0.02)
    assert c.assign([1.0, 0.0])[0] == "Speaker 1"
    assert c.assign([0.6, 0.8])[0] == "Speaker 2"  # 0.6 < 0.8: split
    renames = []
    for _ in range(8):  # more speech drifts Speaker 2 toward Speaker 1
        label, r = c.assign([0.9, 0.436])
        renames += r
    assert ("Speaker 2", "Speaker 1") in renames
    assert label == "Speaker 1" and len(c.clusters) == 1


class FakeEmbedder:
    name = "fake"

    def __init__(self, table):
        self.table = table  # first sample value -> vector
        self.loaded = False

    def is_loaded(self):
        return self.loaded

    async def load(self):
        self.loaded = True

    async def unload(self):
        self.loaded = False

    async def embed(self, pcm, sample_rate):
        if pcm.size < sample_rate:  # < 1 s
            return None
        return self.table[int(pcm[0])]


class ScriptedTranscriber:
    """Returns fixed buffer-relative segments on the first call only."""

    name = "scripted"

    def __init__(self, segments):
        self.segments = segments
        self.calls = 0

    def is_loaded(self):
        return True

    async def load(self):
        pass

    async def unload(self):
        pass

    async def transcribe(self, path, language=None):
        self.calls += 1
        return list(self.segments) if self.calls == 1 else []


RATE = 16000


def _wav(path, chunks):
    """chunks: list of (seconds, value) -> constant-valued PCM runs."""
    pcm = np.concatenate([np.full(int(RATE * s), v, dtype=np.int16) for s, v in chunks])
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())


@pytest.mark.anyio
async def test_live_labels_by_voice_and_relabels(tmp_path):
    path = tmp_path / "sys.wav"
    # 0-2 s voice 1, 2-4 s voice 2, 4-4.5 s voice 1 (too short to embed), 4.5-8 s pad
    _wav(path, [(2, 1), (2, 2), (0.5, 1), (3.5, 0)])
    segs = [
        TranscriptSegment(text="hello", speaker="UNKNOWN", start=0.0, end=2.0),
        TranscriptSegment(text="hi there", speaker="UNKNOWN", start=2.0, end=4.0),
        TranscriptSegment(text="ok", speaker="UNKNOWN", start=4.0, end=4.5),
    ]
    events = []
    clusterer = OnlineClusterer(threshold=0.5, known={"Bob": B})
    live = LiveTranscriber(
        ScriptedTranscriber(segs),
        [LiveSource(path=path, speaker="Remote", kind="system", diarize=True)],
        events.append,
        "s1",
        embedder=FakeEmbedder({1: A, 2: B}),
        clusterer=clusterer,
    )
    await live.tick()
    labels = [e["segment"]["speaker"] for e in events if e["type"] == "live_segment"]
    assert labels == ["Speaker 1", "Bob", "Bob"]  # short line inherits the previous speaker
    assert [(e["old"], e["new"]) for e in events if e["type"] == "live_relabel"] == [
        ("Speaker 2", "Bob")
    ]


@pytest.mark.anyio
async def test_non_diarized_source_keeps_channel_label(tmp_path):
    path = tmp_path / "mic.wav"
    _wav(path, [(2, 1), (6, 0)])
    events = []
    live = LiveTranscriber(
        ScriptedTranscriber([TranscriptSegment(text="me", speaker="U", start=0, end=2)]),
        [LiveSource(path=path, speaker="Me", kind="mic", diarize=False)],
        events.append,
        "s1",
        embedder=FakeEmbedder({1: A}),
        clusterer=OnlineClusterer(),
    )
    await live.tick()
    assert [e["segment"]["speaker"] for e in events if e["type"] == "live_segment"] == ["Me"]


@pytest.mark.anyio
async def test_embedder_load_failure_falls_back(tmp_path):
    class Broken(FakeEmbedder):
        async def load(self):
            raise RuntimeError("no model")

    path = tmp_path / "sys.wav"
    _wav(path, [(2, 1), (6, 0)])
    events = []
    live = LiveTranscriber(
        ScriptedTranscriber([TranscriptSegment(text="x", speaker="U", start=0, end=2)]),
        [LiveSource(path=path, speaker="Remote", kind="system", diarize=True)],
        events.append,
        "s1",
        interval=0.01,
        embedder=Broken({1: A}),
        clusterer=OnlineClusterer(),
    )
    import asyncio

    task = asyncio.create_task(live.run())
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    statuses = [e["message"] for e in events if e["type"] == "live_status"]
    assert "Live (speaker detection unavailable)" in statuses
    assert [e["segment"]["speaker"] for e in events if e["type"] == "live_segment"] == ["Remote"]
