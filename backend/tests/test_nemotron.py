"""Nemotron diarizer glue (no NeMo, no models)."""

import wave

import numpy as np
import pytest

from mnemosyne.config import Settings
from mnemosyne.transcription.diarizers.nemotron import (
    NemotronDiarizer,
    embedding_spans,
    parse_segments,
)
from mnemosyne.transcription.engine import SpeakerTurn
from mnemosyne.transcription.registry import build_diarizer

RATE = 16000


def test_parse_segments_labels_like_pyannote():
    turns = parse_segments(["3.0 4.5 speaker_1", "0.00 2.25 speaker_0", "5 6 speaker_10"])
    assert [(t.start, t.end, t.speaker) for t in turns] == [
        (0.0, 2.25, "SPEAKER_00"),
        (3.0, 4.5, "SPEAKER_01"),
        (5.0, 6.0, "SPEAKER_10"),
    ]


def test_embedding_spans_skip_short_and_overlapping_turns():
    turns = [
        SpeakerTurn(start=0, end=3, speaker="SPEAKER_00"),
        SpeakerTurn(start=2, end=5, speaker="SPEAKER_01"),  # overlaps the first
        SpeakerTurn(start=6, end=6.5, speaker="SPEAKER_01"),  # too short
        SpeakerTurn(start=7, end=9, speaker="SPEAKER_01"),
        SpeakerTurn(start=10, end=15, speaker="SPEAKER_00"),
    ]
    assert embedding_spans(turns) == {"SPEAKER_00": [(10, 15)], "SPEAKER_01": [(7, 9)]}


def test_embedding_spans_longest_first_and_capped():
    turns = [SpeakerTurn(start=i * 50, end=i * 50 + 40, speaker="SPEAKER_00") for i in range(3)]
    turns.append(SpeakerTurn(start=200, end=245, speaker="SPEAKER_00"))
    assert embedding_spans(turns) == {"SPEAKER_00": [(200, 245), (0, 15)]}


class FakeModel:
    def __init__(self, lines):
        self.lines = lines
        self.calls = []

    def diarize(self, audio, sample_rate, batch_size, verbose):
        self.calls.append((len(audio[0]), sample_rate))
        return [self.lines]


class FakeEmbedder:
    """Embeds a constant-valued clip as a one-hot on that value."""

    def __init__(self):
        self.unloaded = False

    async def load(self):
        pass

    async def unload(self):
        self.unloaded = True

    async def embed(self, pcm, sample_rate):
        vec = [0.0] * 4
        vec[round(float(pcm.mean()) * 32768)] = 2.0
        return vec


def _wav(path, chunks):
    pcm = np.concatenate([np.full(int(RATE * s), v, dtype=np.int16) for s, v in chunks])
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())


@pytest.mark.anyio
async def test_diarize_returns_turns_and_per_speaker_embeddings(tmp_path):
    path = tmp_path / "sys.wav"
    _wav(path, [(2, 1), (2, 2), (1, 0)])
    diarizer = NemotronDiarizer(embedder=FakeEmbedder())
    diarizer._model = FakeModel(["0.0 2.0 speaker_0", "2.0 4.0 speaker_1"])
    progress = []

    result = await diarizer.diarize(str(path), max_speakers=10, progress=progress.append)

    assert diarizer._model.calls == [(5 * RATE, RATE)]
    assert [t.speaker for t in result.turns] == ["SPEAKER_00", "SPEAKER_01"]
    assert result.embeddings == {"SPEAKER_00": [0, 1, 0, 0], "SPEAKER_01": [0, 0, 1, 0]}
    assert progress[-1] == 1.0


@pytest.mark.anyio
async def test_diarize_without_embedder_has_no_embeddings(tmp_path):
    path = tmp_path / "sys.wav"
    _wav(path, [(2, 1)])
    diarizer = NemotronDiarizer()
    diarizer._model = FakeModel(["0.0 2.0 speaker_0"])
    result = await diarizer.diarize(str(path))
    assert len(result.turns) == 1 and result.embeddings == {}


def test_registry_builds_nemotron_without_loading_nemo(tmp_path):
    diarizer = build_diarizer(Settings(data_dir=tmp_path, diarizer="nemotron"))
    assert diarizer.name == "nemotron" and not diarizer.is_loaded()


@pytest.mark.parametrize(
    ("setting", "available", "expected"),
    [
        ("auto", True, "nemotron"),
        ("auto", False, "pyannote"),
        ("pyannote", True, "pyannote"),
        ("none", True, "none"),
    ],
)
def test_auto_prefers_nemotron_when_available(tmp_path, monkeypatch, setting, available, expected):
    from mnemosyne.transcription import registry

    monkeypatch.setattr(registry, "nemotron_available", lambda: available)
    s = Settings(data_dir=tmp_path, diarizer=setting)
    assert registry.resolve_diarizer(s) == expected


def _config(tmp_path, monkeypatch, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    monkeypatch.setenv("MNEMOSYNE_CONFIG_FILE", str(path))
    return path


def test_old_config_moves_pyannote_to_auto(tmp_path, monkeypatch):
    from mnemosyne.config import CONFIG_VERSION, load_settings

    path = _config(tmp_path, monkeypatch, f'diarizer = "pyannote"\ndata_dir = "{tmp_path}"\n')
    s = load_settings()
    assert s.diarizer == "auto" and s.config_version == CONFIG_VERSION
    assert 'diarizer = "auto"' in path.read_text()
    # Choosing pyannote again after the migration sticks.
    path.write_text(path.read_text().replace('"auto"', '"pyannote"'))
    assert load_settings().diarizer == "pyannote"


def test_old_config_keeps_other_diarizers_and_env_override(tmp_path, monkeypatch):
    from mnemosyne.config import load_settings

    _config(tmp_path, monkeypatch, f'diarizer = "none"\ndata_dir = "{tmp_path}"\n')
    assert load_settings().diarizer == "none"
    _config(tmp_path, monkeypatch, f'diarizer = "pyannote"\ndata_dir = "{tmp_path}"\n')
    monkeypatch.setenv("DIARIZER", "pyannote")
    assert load_settings().diarizer == "pyannote"


def test_fresh_install_defaults_to_auto_without_writing_config(tmp_path, monkeypatch):
    from mnemosyne.config import load_settings

    monkeypatch.setenv("MNEMOSYNE_CONFIG_FILE", str(tmp_path / "config.toml"))
    monkeypatch.setenv("MNEMOSYNE_DATA_DIR", str(tmp_path / "data"))
    assert load_settings().diarizer == "auto"
    assert not (tmp_path / "config.toml").exists()
