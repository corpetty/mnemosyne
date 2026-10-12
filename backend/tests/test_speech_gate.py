"""The mic speech gate: phantom lines from clicks and silence (transcription/speech_gate.py).
The real Silero model is never loaded here: `speech_frames` is replaced by chosen probabilities."""

import logging
import wave
from pathlib import Path

import numpy as np
import pytest

from mnemosyne.models.transcript import TranscriptSegment, WordSegment
from mnemosyne.transcription import speech_gate
from mnemosyne.transcription.composed import ComposedEngine
from mnemosyne.transcription.engine import AudioSource
from mnemosyne.transcription.live import LiveSource, LiveTranscriber
from tests.fakes import FakeTranscriber

PER_SECOND = 16000 / 512  # 31.25 frames


def probs(*speech: int, size: int = 2000) -> np.ndarray:
    """Frame probabilities: 0.9 at the given frame indexes, 0.1 elsewhere."""
    p = np.full(size, 0.1, dtype=np.float32)
    p[list(speech)] = 0.9
    return p


def run(first: int, count: int) -> list[int]:
    return list(range(first, first + count))


def line(start=10.0, end=10.5, words=True, speaker="Me", text="Yeah."):
    w = [WordSegment(word=text, start=start, end=end)] if words else None
    return TranscriptSegment(text=text, speaker=speaker, start=start, end=end, words=w)


# Words 10.0-10.5 are padded to 9.7-10.8: frames 303 up to and including 338.


def test_seven_speech_frames_are_noise_eight_are_speech():
    seg = line()
    assert speech_gate.split_noise([seg], probs(*run(310, 7))) == ([], [seg])
    assert speech_gate.split_noise([seg], probs(*run(310, 8))) == ([seg], [])


def test_the_padding_counts_and_stops_at_a_third_of_a_second():
    seg = line()
    inside = run(303, 8)  # 9.70 s on
    assert speech_gate.split_noise([seg], probs(*inside)) == ([seg], [])
    outside = run(295, 8)  # ends before 9.7 s
    assert speech_gate.split_noise([seg], probs(*outside)) == ([], [seg])
    after = run(339, 8)  # begins after 10.8 s
    assert speech_gate.split_noise([seg], probs(*after)) == ([], [seg])


def test_word_timings_are_used_not_the_padded_segment_end():
    # A single word is padded to a second: the segment ends at 11.0 but the word at 10.5.
    padded = TranscriptSegment(
        text="Yeah.",
        speaker="Me",
        start=10.0,
        end=11.0,
        words=[WordSegment(word="Yeah.", start=10.0, end=10.5)],
    )
    late = probs(*run(342, 10))  # 10.94-11.2 s: only the padded end reaches it
    assert speech_gate.split_noise([padded], late) == ([], [padded])
    bare = padded.model_copy(update={"words": None})
    assert speech_gate.split_noise([bare], late) == ([bare], [])


def test_segments_beyond_the_audio_cannot_be_judged_and_stay():
    seg = line(start=100.0, end=100.5)
    assert speech_gate.split_noise([seg], probs(size=100)) == ([seg], [])
    assert speech_gate.split_noise([seg], np.zeros(0, np.float32)) == ([seg], [])
    assert speech_gate.split_noise([seg], None) == ([seg], [])


def test_drop_noise_takes_samples_and_keeps_object_identity(monkeypatch):
    keep, drop = line(10, 10.5), line(20, 20.5)
    monkeypatch.setattr(
        speech_gate, "speech_frames", lambda samples, rate: probs(*run(310, 9), size=1000)
    )
    kept, dropped = speech_gate.drop_noise([keep, drop], np.zeros(16000, np.float32), 16000)
    assert kept[0] is keep and dropped[0] is drop
    assert len(kept) == 1 and len(dropped) == 1


def test_unavailable_silero_keeps_everything_and_logs_once(caplog):
    # conftest makes the model unavailable
    segs = [line(), line(30, 30.5)]
    with caplog.at_level(logging.WARNING, logger=speech_gate.logger.name):
        for _ in range(3):
            assert speech_gate.speech_frames(np.zeros(16000, np.int16), 16000) is None
        assert speech_gate.drop_noise(segs, "/no/such/mic.wav") == (segs, [])
        assert speech_gate.drop_noise(segs, np.zeros(16000, np.float32)) == (segs, [])
    warnings = [r for r in caplog.records if "Speech detection unavailable" in r.getMessage()]
    assert len(warnings) == 1


def test_a_failing_audio_decode_keeps_everything(monkeypatch):
    monkeypatch.setattr(speech_gate, "available", lambda: True)
    segs = [line()]
    assert speech_gate.drop_noise(segs, "/no/such/mic.wav") == (segs, [])


def test_resampling_to_16k_keeps_duration():
    x = np.zeros(48000 * 2, np.int16)
    assert speech_gate._to_16k(x, 48000).size == 32000
    assert speech_gate._to_16k(x[:44100], 44100).size == 16000
    assert speech_gate._to_16k(np.zeros(8000, np.float32), 16000).size == 8000


# ---- the composed engine ------------------------------------------------------------------------


def _engine(noise_text="Yeah.", gate=True):
    real = TranscriptSegment(
        text="We should ship it on Friday.",
        speaker="UNKNOWN",
        start=1.0,
        end=3.0,
        words=[WordSegment(word="ship", start=1.0, end=3.0)],
    )
    phantom = TranscriptSegment(
        text=noise_text,
        speaker="UNKNOWN",
        start=20.0,
        end=21.0,
        words=[WordSegment(word=noise_text, start=20.0, end=20.4)],
    )
    return ComposedEngine(
        FakeTranscriber([real, phantom]), None, echo_dedup=False, mic_speech_gate=gate
    )


def _only_speech_around_the_first_line(monkeypatch):
    monkeypatch.setattr(speech_gate, "available", lambda: True)
    monkeypatch.setattr(
        speech_gate, "speech_frames", lambda samples, rate: probs(*run(35, 60), size=2000)
    )
    monkeypatch.setattr(
        "mnemosyne.audio.mixer.decode_audio", lambda path, sample_rate=48000: np.zeros(16000)
    )


async def _collect(engine, sources):
    return [s async for s in engine.transcribe_sources(sources)]


@pytest.mark.anyio
async def test_mic_phantoms_are_dropped_and_system_lines_are_not(monkeypatch):
    _only_speech_around_the_first_line(monkeypatch)
    mic = AudioSource(path="mic.wav", kind="mic", speaker_label="Me")
    system = AudioSource(path="sys.wav", kind="system", speaker_label=None)
    engine = _engine()
    out = await _collect(engine, [mic, system])
    # The fake gives both sources the same two lines: the mic's phantom goes, the system's stays.
    assert sorted((s.text, s.speaker) for s in out) == sorted(
        [
            ("We should ship it on Friday.", "Me"),
            ("Yeah.", "SPEAKER_00"),
            ("We should ship it on Friday.", "SPEAKER_00"),
        ]
    )
    assert engine.last_dropped_noise == 1
    # What was yielded is what last_segment_sources holds, by identity.
    by_id = {id(seg): kind for seg, kind in engine.last_segment_sources}
    assert all(id(s) in by_id for s in out)
    assert sorted(by_id[id(s)] for s in out) == ["mic", "system", "system"]


@pytest.mark.anyio
async def test_setting_off_drops_nothing(monkeypatch):
    _only_speech_around_the_first_line(monkeypatch)
    mic = AudioSource(path="mic.wav", kind="mic", speaker_label="Me")
    engine = _engine(gate=False)
    out = await _collect(engine, [mic])
    assert len(out) == 2 and engine.last_dropped_noise == 0


@pytest.mark.anyio
async def test_unavailable_silero_drops_nothing():
    mic = AudioSource(path="mic.wav", kind="mic", speaker_label="Me")
    engine = _engine()
    out = await _collect(engine, [mic])
    assert len(out) == 2 and engine.last_dropped_noise == 0


# ---- live ---------------------------------------------------------------------------------------

RATE = 48000


class Counting(FakeTranscriber):
    name = "counting"

    def __init__(self):
        super().__init__([])
        self.calls = 0

    async def transcribe(self, path, language=None, progress=None):
        self.calls += 1
        with wave.open(path, "rb") as w:
            seconds = w.getnframes() / w.getframerate()
        return [TranscriptSegment(text="w0", speaker="UNKNOWN", start=0.0, end=seconds - 2)]


def _wav(path: Path, seconds: float) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(np.full(int(RATE * seconds), 1000, dtype=np.int16).tobytes())


@pytest.mark.anyio
@pytest.mark.parametrize(("frames", "transcribed"), [(2, False), (3, True)])
async def test_live_mic_buffer_needs_three_speech_frames(
    tmp_path, monkeypatch, frames, transcribed
):
    monkeypatch.setattr(
        speech_gate, "speech_frames", lambda samples, rate: probs(*run(10, frames), size=300)
    )
    path = tmp_path / "mic.wav"
    _wav(path, 4.0)
    t, events = Counting(), []
    live = LiveTranscriber(t, [LiveSource(path=path, speaker="Me", kind="mic")], events.append, "s")
    await live.tick()
    assert (t.calls == 1) is transcribed
    if not transcribed:
        assert not live.committed


@pytest.mark.anyio
async def test_live_system_audio_and_gate_off_are_not_gated(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(
        speech_gate, "speech_frames", lambda samples, rate: called.append(1) or probs(size=300)
    )
    path = tmp_path / "audio.wav"
    _wav(path, 4.0)
    t = Counting()
    system = LiveTranscriber(t, [LiveSource(path=path, speaker="S", kind="system")], print, "s")
    await system.tick()
    off = LiveTranscriber(
        t, [LiveSource(path=path, speaker="Me", kind="mic")], print, "s", mic_speech_gate=False
    )
    await off.tick()
    assert t.calls == 2 and not called


@pytest.mark.anyio
async def test_live_mic_line_without_speech_is_not_shown_but_the_buffer_moves_on(
    tmp_path, monkeypatch
):
    # Three frames at 0.3 s pass the buffer test; a line at 0.0-2.0 s (words 0.0-0.4) has them
    # in its span but fewer than eight.
    monkeypatch.setattr(
        speech_gate, "speech_frames", lambda samples, rate: probs(*run(10, 3), size=300)
    )

    class Words(Counting):
        async def transcribe(self, path, language=None, progress=None):
            self.calls += 1
            return [
                TranscriptSegment(
                    text="Yeah.",
                    speaker="UNKNOWN",
                    start=0.0,
                    end=1.0,
                    words=[WordSegment(word="Yeah.", start=0.0, end=0.4)],
                )
            ]

    path = tmp_path / "mic.wav"
    _wav(path, 4.0)
    events = []
    source = LiveSource(path=path, speaker="Me", kind="mic")
    live = LiveTranscriber(Words(), [source], events.append, "s")
    await live.tick()
    assert [e for e in events if e["type"] == "live_segment"] == []
    assert live.committed == []
    assert source.buffer_start == pytest.approx(1.0)  # the line was committed, then not shown


@pytest.mark.anyio
async def test_live_mic_line_with_speech_is_shown(tmp_path, monkeypatch):
    monkeypatch.setattr(
        speech_gate, "speech_frames", lambda samples, rate: probs(*run(2, 20), size=300)
    )
    path = tmp_path / "mic.wav"
    _wav(path, 4.0)

    class Words(Counting):
        async def transcribe(self, path, language=None, progress=None):
            return [
                TranscriptSegment(
                    text="Okay then.",
                    speaker="UNKNOWN",
                    start=0.0,
                    end=1.0,
                    words=[WordSegment(word="Okay", start=0.0, end=0.6)],
                )
            ]

    events = []
    live = LiveTranscriber(
        Words(), [LiveSource(path=path, speaker="Me", kind="mic")], events.append, "s"
    )
    await live.tick()
    assert [e["segment"]["text"] for e in events if e["type"] == "live_segment"] == ["Okay then."]
