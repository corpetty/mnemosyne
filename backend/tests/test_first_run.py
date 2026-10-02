"""The first run works on any machine: engines that fit it (registry.resolve_*), speakers on the
CPU (diarizers/onnx.py), and a sample meeting to try (routes/audio.py /sample)."""

import numpy as np
import pytest

from mnemosyne.transcription import registry
from mnemosyne.transcription.diarizers.onnx import merge_small_clusters
from tests.conftest import drain_until_job


def _machine(monkeypatch, modules: set[str], cuda: bool = False, nemotron: bool = False):
    monkeypatch.setattr(registry, "installed", lambda *m: all(x in modules for x in m))
    monkeypatch.setattr(registry, "cuda_works", lambda: cuda)
    monkeypatch.setattr(registry, "nemotron_available", lambda: nemotron)


def test_auto_picks_what_the_machine_runs(settings, monkeypatch):
    settings.transcriber = "auto"
    _machine(monkeypatch, {"onnx_asr", "sherpa_onnx"})
    assert registry.resolve_transcriber(settings) == "parakeet"
    assert registry.resolve_diarizer(settings) == "onnx"
    _machine(
        monkeypatch, {"onnx_asr", "whisperx", "torch", "sherpa_onnx"}, cuda=True, nemotron=True
    )
    assert registry.resolve_transcriber(settings) == "whisperx"
    assert registry.resolve_diarizer(settings) == "nemotron"
    _machine(monkeypatch, {"onnx_asr"})
    assert registry.resolve_diarizer(settings) == "none"  # nothing to tell speakers apart


def test_a_saved_choice_this_machine_cannot_run_falls_back(settings, monkeypatch):
    _machine(monkeypatch, {"onnx_asr", "sherpa_onnx"})
    settings.transcriber = "whisperx"
    settings.diarizer = "pyannote"
    assert registry.resolve_transcriber(settings) == "parakeet"
    assert registry.resolve_diarizer(settings) == "onnx"
    settings.transcriber = "remote"
    assert registry.resolve_transcriber(settings) == "remote"
    settings.diarizer = "none"
    assert registry.resolve_diarizer(settings) == "none"


def test_pyannote_without_its_token_gives_way_to_the_cpu_model(settings, monkeypatch):
    _machine(monkeypatch, {"pyannote.audio", "torch", "sherpa_onnx"})
    settings.diarizer = "auto"
    settings.hf_token = ""
    assert registry.resolve_diarizer(settings) == "onnx"
    settings.hf_token = "hf_x"
    assert registry.resolve_diarizer(settings) == "pyannote"


def test_small_clusters_join_the_closest_speaker():
    a, b = np.array([1.0, 0.0]), np.array([0.0, 1.0])
    near_b = np.array([0.1, 0.99]) / np.linalg.norm([0.1, 0.99])
    turns = [(0, 30, 0), (30, 60, 1), (60, 61, 2), (61, 90, 0)]
    merged = merge_small_clusters(turns, {0: a, 1: b, 2: near_b}.get)
    assert [k for *_, k in merged] == [0, 1, 1, 0]
    # Nothing small: unchanged.
    assert merge_small_clusters(turns[:2], {0: a, 1: b}.get) == turns[:2]


def test_the_sample_meeting_is_imported_credited_and_transcribed(client, ctx, fake_engine):
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        r = client.post("/api/audio/sample")
        assert r.status_code == 200, r.text
        body = r.json()
        drain_until_job(ws, body["job_id"])
    session = client.get(f"/api/sessions/{body['session']['id']}").json()
    assert session["name"] == "Sample meeting (AMI corpus)"
    assert "AMI Meeting Corpus" in session["notes"] and "CC BY 4.0" in session["notes"]
    assert session["transcript"]
    # No summary model answers here: the sample is not summarized (and does not fail).
    kinds = [j["kind"] for j in client.get("/api/jobs").json()]
    assert "summarize" not in kinds


def test_system_says_which_engines_run_here(client, monkeypatch):
    _machine(monkeypatch, {"onnx_asr", "sherpa_onnx"})
    client.put("/api/settings", json={"transcriber": "auto", "diarizer": "auto"})
    info = client.get("/api/system").json()
    assert info["transcriber_in_use"] == "parakeet" and info["diarizer_in_use"] == "onnx"


@pytest.mark.parametrize("kind", ["onnx"])
def test_the_onnx_diarizer_is_built_without_importing_it(settings, monkeypatch, kind):
    _machine(monkeypatch, {"sherpa_onnx"})
    settings.diarizer = kind
    d = registry.build_diarizer(settings)
    assert d is not None and d.name == "onnx" and not d.is_loaded()
    assert d.models_dir == settings.models_dir / "diarization"
