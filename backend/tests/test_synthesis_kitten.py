"""The KittenTTS engine's side of its worker (synthesis/kitten.py), its install checks
(services/tts_install.py) and which engine runs (synthesis/registry.py). A stand-in worker plays
KittenTTS: no model, no torch, nothing played."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import numpy as np
import pytest

from mnemosyne.config import Settings
from mnemosyne.services import tts_install
from mnemosyne.synthesis import kitten
from mnemosyne.synthesis.engine import RefClip, SynthesisError
from mnemosyne.synthesis.registry import build_synthesizer, resolve_synthesizer
from tests.voice_fakes import read_wav, speech, wav_bytes

FAKE_WORKER = r"""
import json, sys, wave
sys.stdout.reconfigure(line_buffering=True)
for line in sys.stdin:
    req = json.loads(line)
    op = req["op"]
    if op == "quit":
        break
    if op == "load":
        print(json.dumps({"ok": True, "sample_rate": 24000})); continue
    if op == "speak":
        if req["text"] == "boom":
            print(json.dumps({"ok": False, "error": "RuntimeError: boom"})); continue
        if req["text"] == "die":
            sys.exit(3)
        assert req["reference_text"] and open(req["reference"], "rb").read(4) == b"RIFF"
        with wave.open(req["out"], "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
            w.writeframes(b"\x10\x00" * 24000)
        print(json.dumps({"ok": True, "sample_rate": 24000, "watermarked": True})); continue
    print(json.dumps({"ok": False, "error": "unknown op"}))
"""


@pytest.fixture
def fake_install(tmp_path, monkeypatch):
    from tests.voice_fakes import _decode

    root = kitten.home(tmp_path)
    (root / "model").mkdir(parents=True)
    (root / "installed").write_text("{}")
    worker = tmp_path / "fake_worker.py"
    worker.write_text(FAKE_WORKER)
    monkeypatch.setattr(kitten, "WORKER", worker)
    monkeypatch.setattr(kitten, "python_of", lambda root: Path(sys.executable))
    from mnemosyne.audio import mixer

    monkeypatch.setattr(mixer, "decode_audio", _decode)  # WAV only, no ffmpeg needed
    return tmp_path


def test_the_engine_speaks_through_its_worker(fake_install, tmp_path):
    clip = tmp_path / "clip.wav"
    clip.write_bytes(wav_bytes(speech(150, 8)))

    async def go():
        synth = kitten.KittenSynthesizer(fake_install)
        prompt = await synth.prepare([RefClip(clip, "Hello there.", 8.0, 0.9)], tmp_path / "v")
        assert prompt.space == kitten.SPACE and prompt.text == "Hello there."
        samples, rate = read_wav(prompt.path)
        assert rate == 24000 and abs(samples.size / rate - 8.0) < 0.05
        speech_out = await synth.speak(prompt, "Narrate this.")
        assert speech_out.watermarked and speech_out.sample_rate == 24000
        assert speech_out.samples.size == 24000 and np.all(speech_out.samples > 0)
        with pytest.raises(SynthesisError, match="boom"):
            await synth.speak(prompt, "boom")
        assert synth.is_loaded()  # an error answer leaves the worker running
        with pytest.raises(SynthesisError, match="stopped"):
            await synth.speak(prompt, "die")
        assert not synth.is_loaded()
        again = await synth.speak(prompt, "Back again.")  # started anew
        assert again.samples.size == 24000
        await synth.unload()
        assert not synth.is_loaded()

    asyncio.run(go())


def test_not_installed_says_so(tmp_path):
    synth = kitten.KittenSynthesizer(tmp_path)
    with pytest.raises(SynthesisError, match="not installed"):
        asyncio.run(synth.load())


def test_the_worker_runs_offline_without_the_backends_python_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONHOME", "/somewhere")
    monkeypatch.setenv("VIRTUAL_ENV", "/backend/.venv")
    env = kitten.worker_env(tmp_path)
    assert env["HF_HUB_OFFLINE"] == "1" and env["HF_HUB_CACHE"] == str(tmp_path / "cache")
    assert "PYTHONHOME" not in env and "VIRTUAL_ENV" not in env
    assert "HF_HUB_OFFLINE" not in kitten.worker_env(tmp_path, offline=False)


def test_requirements_are_pinned_with_hashes():
    for file in (tts_install.LINUX_REQUIREMENTS, tts_install.MAC_REQUIREMENTS):
        text = file.read_text()
        assert "kittenml==0.9.8" in text and "audioseal==0.2.1" in text
        assert "--hash=sha256:" in tts_install._pin("torch", file)
    assert "+cpu" in tts_install._pin("torch", tts_install.LINUX_REQUIREMENTS)
    with pytest.raises(ValueError):
        tts_install._pin("not-a-package", tts_install.LINUX_REQUIREMENTS)


def test_install_needs_the_licence_accepted(tmp_path):
    with pytest.raises(tts_install.LicenseNotAccepted):
        asyncio.run(tts_install.install(tmp_path, lambda m, f: None, accepted_license=False))
    assert not kitten.home(tmp_path).exists()


def test_installed_means_this_pin_set_and_revision(tmp_path, monkeypatch):
    root = kitten.home(tmp_path)
    root.mkdir(parents=True)
    monkeypatch.setattr(kitten, "python_of", lambda root: Path(sys.executable))
    assert not tts_install.installed(tmp_path)
    stamp = {"requirements": tts_install.requirements_hash(), "revision": kitten.MODEL_REVISION}
    (root / "installed").write_text(json.dumps(stamp))
    assert tts_install.installed(tmp_path)
    (root / "installed").write_text(json.dumps({**stamp, "revision": "older"}))
    assert not tts_install.installed(tmp_path)  # a new pinned model installs again


def test_verify_catches_a_changed_model_file(tmp_path):
    root = kitten.home(tmp_path)
    (root / "model" / "lm").mkdir(parents=True)
    weights = root / "model" / "lm" / "model.safetensors"
    weights.write_bytes(b"weights")
    (root / "installed").write_text(json.dumps({"files": tts_install.manifest(root)}))
    tts_install.verify(tmp_path)
    weights.write_bytes(b"tampered")
    with pytest.raises(tts_install.ModelMismatch):
        tts_install.verify(tmp_path)


def test_which_engine(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    assert resolve_synthesizer(settings) == "none" and build_synthesizer(settings) is None
    settings.synthesizer = "demo"
    assert build_synthesizer(settings).name == "demo"
    settings.synthesizer = "auto"
    monkeypatch.setattr(tts_install, "installed", lambda data_dir: True)
    assert resolve_synthesizer(settings) == "kitten"
    assert isinstance(build_synthesizer(settings), kitten.KittenSynthesizer)
    settings.synthesizer = "none"
    assert resolve_synthesizer(settings) == "none"


def test_the_worker_script_never_imports_the_backend():
    source = kitten.WORKER.read_text()
    assert "mnemosyne" not in [
        line.split()[1].split(".")[0]
        for line in source.splitlines()
        if line.strip().startswith(("import ", "from "))
    ]
    assert "AudioSeal.load_generator" in source  # every output is watermarked
