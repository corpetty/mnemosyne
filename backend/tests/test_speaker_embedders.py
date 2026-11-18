"""The ONNX speaker embedder (no torch), which embedder a machine runs, the cosine thresholds of
each voice space, and the voices the ONNX diarizer takes from clean turns."""

import sys
import types

import numpy as np
import pytest

from mnemosyne.services import model_downloads
from mnemosyne.transcription import registry, speaker_embedders
from mnemosyne.transcription.diarizers.onnx import OnnxDiarizer
from mnemosyne.transcription.engine import SpeakerTurn
from mnemosyne.transcription.live_speakers import PyannoteEmbedder
from mnemosyne.transcription.speaker_embedders import OnnxEmbedder, thresholds


class _Stream:
    def __init__(self, calls):
        self.calls = calls
        self.samples = None

    def accept_waveform(self, rate, samples):
        self.rate, self.samples = rate, samples
        self.calls.append(("accept", rate, samples))

    def input_finished(self):
        pass


class _Extractor:
    """A stand-in for sherpa_onnx.SpeakerEmbeddingExtractor with a fixed vector."""

    instances: list = []

    def __init__(self, config):
        self.config = config
        self.calls: list = []
        self.ready = True
        self.vector = [0.25, 0.5, 0.75]
        _Extractor.instances.append(self)

    def create_stream(self):
        return _Stream(self.calls)

    def is_ready(self, stream):
        return self.ready

    def compute(self, stream):
        return self.vector


@pytest.fixture
def sherpa(monkeypatch, tmp_path):
    """A stub `sherpa_onnx` module, and a fetch that downloads nothing."""
    _Extractor.instances = []
    mod = types.ModuleType("sherpa_onnx")
    mod.SpeakerEmbeddingExtractor = _Extractor
    mod.SpeakerEmbeddingExtractorConfig = lambda model, num_threads: types.SimpleNamespace(
        model=model, num_threads=num_threads
    )
    monkeypatch.setitem(sys.modules, "sherpa_onnx", mod)
    fetched = []

    def fake_fetch(folder, item, progress=None):
        fetched.append((folder, item))
        return folder / item.name

    monkeypatch.setattr(speaker_embedders, "fetch", fake_fetch)
    return fetched


def test_the_model_is_pinned():
    m = speaker_embedders.MODEL
    assert m.sha256 == "e9848563da86f263117134dfd7ad63c92355b37de492b55e325400c9d9c39012"
    assert m.size == 26_530_550 and m.url.endswith("/wespeaker_en_voxceleb_resnet34_LM.onnx")
    assert OnnxEmbedder.space == "wespeaker-r34" and PyannoteEmbedder.space == "pyannote"


@pytest.mark.anyio
async def test_it_loads_the_pinned_model_with_its_threads(sherpa, tmp_path):
    e = OnnxEmbedder(tmp_path, threads=1)
    assert not e.is_loaded()
    await e.load()
    await e.load()  # once
    assert e.is_loaded() and len(_Extractor.instances) == 1
    assert sherpa == [(tmp_path, speaker_embedders.MODEL)]
    assert _Extractor.instances[0].config.num_threads == 1
    await e.unload()
    assert not e.is_loaded()


@pytest.mark.anyio
async def test_embed_converts_int16_and_keeps_the_rate(sherpa, tmp_path):
    e = OnnxEmbedder(tmp_path)
    pcm = np.full(48000 * 2, 16384, dtype=np.int16)  # 2 s at 48 kHz: sherpa resamples inside
    assert await e.embed(pcm, 48000) == [0.25, 0.5, 0.75]
    (_, rate, samples) = _Extractor.instances[0].calls[0]
    assert rate == 48000 and samples.dtype == np.float32
    assert samples[0] == pytest.approx(0.5) and len(samples) == 96000


@pytest.mark.anyio
async def test_embed_takes_float_audio_as_it_is(sherpa, tmp_path):
    e = OnnxEmbedder(tmp_path)
    await e.embed(np.full(16000, 0.25, dtype=np.float32), 16000)
    assert _Extractor.instances[0].calls[0][2][0] == pytest.approx(0.25)


@pytest.mark.anyio
async def test_embed_skips_short_audio_and_caps_long_audio(sherpa, tmp_path):
    e = OnnxEmbedder(tmp_path)
    assert await e.embed(np.zeros(15999, dtype=np.int16), 16000) is None
    assert _Extractor.instances[0].calls == []  # nothing was sent to the model
    await e.embed(np.zeros(16000 * 90, dtype=np.int16), 16000)
    assert len(_Extractor.instances[0].calls[0][2]) == 16000 * 60


@pytest.mark.anyio
async def test_embed_gives_none_when_the_model_is_not_ready_or_gives_nan(sherpa, tmp_path):
    e = OnnxEmbedder(tmp_path)
    await e.load()
    ex = _Extractor.instances[0]
    ex.ready = False
    assert await e.embed(np.zeros(16000, dtype=np.int16), 16000) is None
    ex.ready, ex.vector = True, [float("nan"), 1.0]
    assert await e.embed(np.zeros(16000, dtype=np.int16), 16000) is None


# ---- which embedder this machine runs -------------------------------------


def _machine(monkeypatch, modules: set[str]):
    monkeypatch.setattr(registry, "installed", lambda *m: all(x in modules for x in m))


@pytest.mark.parametrize(
    ("modules", "token", "diarizer", "expected"),
    [
        ({"pyannote.audio", "torch", "sherpa_onnx"}, "hf_x", "auto", "pyannote"),
        ({"pyannote.audio", "torch", "sherpa_onnx"}, "", "auto", "onnx"),  # no token: no pyannote
        ({"pyannote.audio", "torch"}, "", "auto", "none"),
        ({"sherpa_onnx"}, "", "auto", "onnx"),
        ({"sherpa_onnx"}, "", "onnx", "onnx"),  # a saved onnx diarizer gets voices now
        ({"sherpa_onnx"}, "", "nemotron", "onnx"),
        ({"pyannote.audio", "torch"}, "hf_x", "pyannote", "pyannote"),
        ({"pyannote.audio", "torch", "sherpa_onnx"}, "hf_x", "none", "none"),
        ({"sherpa_onnx"}, "", "demo", "none"),
        (set(), "", "auto", "none"),
    ],
)
def test_resolve_speaker_embedder(settings, monkeypatch, modules, token, diarizer, expected):
    _machine(monkeypatch, modules)
    settings.hf_token, settings.diarizer = token, diarizer
    assert registry.resolve_speaker_embedder(settings) == expected


def test_build_speaker_embedder(settings, monkeypatch):
    _machine(monkeypatch, {"sherpa_onnx"})
    settings.hf_token = ""
    e = registry.build_speaker_embedder(settings, threads=1)
    assert isinstance(e, OnnxEmbedder) and e.threads == 1 and not e.is_loaded()
    assert e.models_dir == settings.models_dir / "diarization"
    assert registry.build_speaker_embedder(settings).threads == speaker_embedders.default_threads()
    assert registry.build_live_embedder is registry.build_speaker_embedder  # the old name
    _machine(monkeypatch, {"pyannote.audio", "torch", "sherpa_onnx"})
    settings.hf_token = "hf_x"
    p = registry.build_speaker_embedder(settings)
    assert isinstance(p, PyannoteEmbedder) and p.hf_token == "hf_x"
    _machine(monkeypatch, set())
    assert registry.build_speaker_embedder(settings) is None


def test_the_onnx_diarizer_gets_the_embedder(settings, monkeypatch):
    _machine(monkeypatch, {"sherpa_onnx"})
    settings.diarizer, settings.hf_token = "onnx", ""
    d = registry.build_diarizer(settings)
    assert isinstance(d.embedder, OnnxEmbedder)
    _machine(monkeypatch, {"pyannote.audio", "torch", "sherpa_onnx", "nemo"})
    monkeypatch.setattr(registry, "nemotron_available", lambda: True)
    settings.diarizer = "nemotron"
    assert isinstance(registry.build_diarizer(settings).embedder, OnnxEmbedder)


def test_the_embedder_model_is_part_of_the_onnx_download(settings):
    assert model_downloads.DIARIZER_MB["onnx"] == 75


def test_prefetch_loads_the_embedder_with_the_diarizer(settings, monkeypatch):
    """`mnemosyne-backend prefetch` loads build_diarizer(...) for diarizer=onnx without a token."""
    _machine(monkeypatch, {"sherpa_onnx", "pyannote.audio", "torch"})
    settings.diarizer, settings.hf_token = "onnx", ""
    d = registry.build_diarizer(settings)
    assert isinstance(d, OnnxDiarizer) and isinstance(d.embedder, OnnxEmbedder)


# ---- thresholds per space --------------------------------------------------


def test_thresholds_per_space(settings):
    settings.live_speaker_threshold, settings.speaker_match_threshold = 0.3, 0.5
    py = thresholds("pyannote", settings)
    assert (py.cluster, py.match) == (0.3, 0.5)  # the two settings are pyannote's
    ws = thresholds("wespeaker-r34", settings)
    assert (ws.cluster, ws.match) == (0.45, 0.6)  # constants, equal to pyannote's defaults
    assert thresholds("something-else", settings) == py
    settings.live_speaker_threshold, settings.speaker_match_threshold = 0.9, 0.9
    assert thresholds("wespeaker-r34", settings) == ws


# ---- the voices of the ONNX diarizer ---------------------------------------


class _FakeEmbedder:
    space = "wespeaker-r34"
    name = "fake"

    def __init__(self, vectors):
        self.vectors = vectors
        self.seen = []
        self.loaded = False

    def is_loaded(self):
        return self.loaded

    async def load(self):
        self.loaded = True

    async def unload(self):
        self.loaded = False

    async def embed(self, pcm, sample_rate):
        self.seen.append((len(pcm), sample_rate))
        return self.vectors.pop(0)


@pytest.mark.anyio
async def test_embed_speakers_averages_the_clean_turns(tmp_path):
    emb = _FakeEmbedder([[2.0, 0.0], [0.0, 2.0], [0.0, 3.0], None])
    d = OnnxDiarizer(tmp_path, embedder=emb)
    turns = [
        SpeakerTurn(start=0.0, end=3.0, speaker="SPEAKER_00"),
        SpeakerTurn(start=3.0, end=5.0, speaker="SPEAKER_00"),
        SpeakerTurn(start=5.0, end=8.0, speaker="SPEAKER_01"),
        SpeakerTurn(start=8.5, end=9.9, speaker="SPEAKER_02"),  # overlapped by 03: not clean
        SpeakerTurn(start=9.0, end=9.5, speaker="SPEAKER_03"),  # too short
    ]
    voices = await d._embed_speakers(np.zeros(16000 * 10, dtype=np.float32), turns)
    # SPEAKER_00: longest turn first, the unit vectors (1,0) and (0,1) average to (.5,.5);
    # SPEAKER_01 has one turn; speakers without a clean turn or a vector get none.
    assert set(voices) == {"SPEAKER_00", "SPEAKER_01"}
    assert voices["SPEAKER_00"] == pytest.approx([0.5, 0.5])
    assert voices["SPEAKER_01"] == pytest.approx([0.0, 1.0])
    assert emb.seen[0] == (3 * 16000, 16000)


@pytest.mark.anyio
async def test_the_diarizer_loads_its_embedder_and_survives_it_failing(tmp_path, monkeypatch):
    from mnemosyne.transcription.diarizers import onnx

    monkeypatch.setattr(onnx, "model_files", lambda folder, progress=None: (tmp_path, tmp_path))
    mod = types.ModuleType("sherpa_onnx")
    mod.SpeakerEmbeddingExtractor = lambda config: object()
    mod.SpeakerEmbeddingExtractorConfig = lambda **kw: kw
    monkeypatch.setitem(sys.modules, "sherpa_onnx", mod)

    emb = _FakeEmbedder([])
    d = OnnxDiarizer(tmp_path, embedder=emb)
    await d.load()
    assert d.is_loaded() and emb.loaded
    await d.unload()
    assert not emb.loaded and d.embedder is emb

    class Broken(_FakeEmbedder):
        async def load(self):
            raise RuntimeError("offline")

    d = OnnxDiarizer(tmp_path, embedder=Broken([]))
    await d.load()
    assert d.is_loaded() and d.embedder is None  # diarizing goes on, without voices
