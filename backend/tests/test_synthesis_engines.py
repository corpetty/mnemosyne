"""The narration engines' side of their worker (synthesis/workers.py, kitten.py, qwen3.py), their
installs (services/tts_install.py) and which one runs (synthesis/registry.py). A stand-in worker
plays the engine: no model, no torch, nothing played."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import numpy as np
import pytest

from mnemosyne.config import Settings
from mnemosyne.services import tts_install
from mnemosyne.synthesis import kitten, qwen3, workers
from mnemosyne.synthesis.engine import RefClip, SynthesisError
from mnemosyne.synthesis.registry import build_synthesizer, resolve_synthesizer
from tests.voice_fakes import read_wav, speech, wav_bytes

FAKE_WORKER = r"""
import json, sys, wave
sys.stdout.reconfigure(line_buffering=True)
engine = sys.argv[sys.argv.index("--engine") + 1]
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
        assert req["language"] == "en"
        with wave.open(req["out"], "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
            w.writeframes(b"\x10\x00" * 24000)
        print(json.dumps({"ok": True, "sample_rate": 24000, "watermarked": True, "engine": engine}))
        continue
    print(json.dumps({"ok": False, "error": "unknown op"}))
"""

SPECS = [qwen3.SPEC, kitten.SPEC]
PLATFORMS = ("linux", "linux-cuda", "macos")


@pytest.fixture
def fake_worker(tmp_path, monkeypatch):
    from tests.voice_fakes import _decode

    worker = tmp_path / "fake_worker.py"
    worker.write_text(FAKE_WORKER)
    monkeypatch.setattr(workers, "WORKER", worker)
    monkeypatch.setattr(workers, "python_of", lambda root: Path(sys.executable))
    from mnemosyne.audio import mixer

    monkeypatch.setattr(mixer, "decode_audio", _decode)  # WAV only, no ffmpeg needed
    for spec in SPECS:
        (spec.home(tmp_path) / "model").mkdir(parents=True)
        (spec.home(tmp_path) / "installed").write_text("{}")
    return tmp_path


@pytest.mark.parametrize("make", [kitten.KittenSynthesizer, qwen3.Qwen3Synthesizer])
def test_an_engine_speaks_through_its_worker(fake_worker, tmp_path, make):
    clip = tmp_path / "clip.wav"
    clip.write_bytes(wav_bytes(speech(150, 10)))

    async def go():
        synth = make(fake_worker)
        prompt = await synth.prepare([RefClip(clip, "Hello there.", 10.0, 0.9)], tmp_path / "v")
        assert prompt.space == synth.spec.space and prompt.text == "Hello there."
        samples, rate = read_wav(prompt.path)
        assert rate == 24000 and abs(samples.size / rate - 10.0) < 0.05
        prompt.extra["language"] = "en"
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


def test_qwen3_takes_one_reference_kitten_joins_several(fake_worker, tmp_path):
    clips = []
    for i, (seconds, quality) in enumerate([(12, 0.6), (9, 0.9), (25, 1.0), (10, 0.8)]):
        path = tmp_path / f"c{i}.wav"
        path.write_bytes(wav_bytes(speech(150, seconds)))
        clips.append(RefClip(path, f"clip {i}", float(seconds), quality))

    async def go():
        q = await qwen3.Qwen3Synthesizer(fake_worker).prepare(clips, tmp_path / "q")
        k = await kitten.KittenSynthesizer(fake_worker).prepare(clips, tmp_path / "k")
        return q, k

    q, k = asyncio.run(go())
    assert q.text == "clip 1"  # the best of the 8-20 s clips; the 25 s one is too long for it
    assert k.text == "clip 2"  # KittenTTS: best first until about 20 s; its best is 25 s alone


def test_not_installed_says_so(tmp_path):
    synth = qwen3.Qwen3Synthesizer(tmp_path)
    with pytest.raises(SynthesisError, match="Qwen3-TTS 1.7B is not installed"):
        asyncio.run(synth.load())


def test_the_worker_command_names_the_engine_and_its_model(tmp_path):
    root = qwen3.SPEC.home(tmp_path)
    cmd = workers.worker_command(qwen3.SPEC, root)
    assert cmd[1:5] == ["-I", str(workers.WORKER), "--engine", "qwen3"]
    assert cmd[cmd.index("--model") + 1] == str(root / "model")
    assert cmd[cmd.index("--repo") + 1] == "Qwen/Qwen3-TTS-12Hz-1.7B-Base"


def test_the_worker_runs_offline_without_the_backends_python_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONHOME", "/somewhere")
    monkeypatch.setenv("VIRTUAL_ENV", "/backend/.venv")
    env = workers.worker_env(tmp_path)
    assert env["HF_HUB_OFFLINE"] == "1" and env["HF_HUB_CACHE"] == str(tmp_path / "cache")
    assert "PYTHONHOME" not in env and "VIRTUAL_ENV" not in env
    assert "HF_HUB_OFFLINE" not in workers.worker_env(tmp_path, offline=False)
    # Nothing outside the install: AudioSeal's weights and the Hub client's own files are in it
    # (by default they go to ~/.cache/audioseal and ~/.cache/huggingface).
    for key in ("AUDIOSEAL_CACHE_DIR", "HF_HOME"):
        assert Path(env[key]).is_relative_to(tmp_path / "cache")


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.name)
def test_every_platform_is_pinned_with_hashes(spec):
    for plat in PLATFORMS:
        file = spec.requirements(plat)
        text = file.read_text()
        assert "audioseal==0.2.1" in text and "--hash=sha256:" in tts_install._pin("torch", file)
        engine_pin = "kittenml==0.9.8" if spec.name == "kitten" else "qwen-tts==0.1.1"
        assert engine_pin in text, file.name
    assert "+cpu" in tts_install._pin("torch", spec.requirements("linux"))
    assert "+cu128" in tts_install._pin("torch", spec.requirements("linux-cuda"))
    with pytest.raises(ValueError):
        tts_install._pin("not-a-package", spec.requirements("linux"))


def test_the_platform_follows_the_machine(monkeypatch):
    monkeypatch.setattr(tts_install, "on_mac", lambda: False)
    monkeypatch.setattr(tts_install, "has_nvidia", lambda: True)
    assert tts_install.platform(qwen3.SPEC) == "linux-cuda"
    monkeypatch.setattr(tts_install, "has_nvidia", lambda: False)
    assert tts_install.platform(qwen3.SPEC) == "linux"
    monkeypatch.setattr(tts_install, "on_mac", lambda: True)
    assert tts_install.platform(kitten.SPEC) == "macos"


def test_only_kitten_needs_its_licence_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(tts_install, "uv_binary", lambda: None)
    with pytest.raises(tts_install.LicenseNotAccepted):
        asyncio.run(
            tts_install.install(kitten.SPEC, tmp_path, lambda m, f: None, accepted_license=False)
        )
    assert not kitten.SPEC.home(tmp_path).exists()
    # Qwen3-TTS is Apache-2.0: no licence to accept, so the install goes on (to needing uv).
    with pytest.raises(RuntimeError, match="needs uv"):
        asyncio.run(
            tts_install.install(qwen3.SPEC, tmp_path, lambda m, f: None, accepted_license=False)
        )


def test_installed_means_this_pin_set_and_revision(tmp_path, monkeypatch):
    spec = qwen3.SPEC
    root = spec.home(tmp_path)
    root.mkdir(parents=True)
    monkeypatch.setattr(tts_install, "python_of", lambda root: Path(sys.executable))
    assert not tts_install.installed(spec, tmp_path)
    stamp = {
        "platform": "linux-cuda",
        "requirements": tts_install.requirements_hash(spec, "linux-cuda"),
        "revision": spec.revision,
    }
    (root / "installed").write_text(json.dumps(stamp))
    assert tts_install.installed(spec, tmp_path)
    (root / "installed").write_text(json.dumps({**stamp, "revision": "older"}))
    assert not tts_install.installed(spec, tmp_path)  # a new pinned model installs again
    (root / "installed").write_text(json.dumps({**stamp, "requirements": "older"}))
    assert not tts_install.installed(spec, tmp_path)  # so do new pins
    assert not tts_install.installed(kitten.SPEC, tmp_path)  # engines are installed one by one


def test_verify_catches_a_changed_model_file(tmp_path):
    spec = kitten.SPEC
    root = spec.home(tmp_path)
    (root / "model" / "lm").mkdir(parents=True)
    (root / "cache" / "hf-home").mkdir(parents=True)
    weights = root / "model" / "lm" / "model.safetensors"
    weights.write_bytes(b"weights")
    (root / "cache" / "hf-home" / "xet.log").write_text("logs change; they are not the model")
    files = tts_install.manifest(root)
    assert list(files) == [str(Path("model/lm/model.safetensors"))]
    (root / "installed").write_text(json.dumps({"files": files}))
    tts_install.verify(spec, tmp_path)
    weights.write_bytes(b"tampered")
    with pytest.raises(tts_install.ModelMismatch):
        tts_install.verify(spec, tmp_path)


def test_which_engine(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    assert resolve_synthesizer(settings) == "none" and build_synthesizer(settings) is None
    settings.synthesizer = "demo"
    assert build_synthesizer(settings).name == "demo"
    settings.synthesizer = "auto"
    present = {"kitten"}
    monkeypatch.setattr(tts_install, "installed", lambda spec, data_dir: spec.name in present)
    assert resolve_synthesizer(settings) == "kitten"  # the only one installed
    present.add("qwen3")
    assert resolve_synthesizer(settings) == "qwen3"  # both: Qwen3-TTS, the recommended one
    assert isinstance(build_synthesizer(settings), qwen3.Qwen3Synthesizer)
    settings.synthesizer = "kitten"
    assert isinstance(build_synthesizer(settings), kitten.KittenSynthesizer)
    present.discard("kitten")
    assert resolve_synthesizer(settings) == "none"  # chosen but not installed
    settings.synthesizer = "none"
    assert resolve_synthesizer(settings) == "none"


def test_the_worker_script_never_imports_the_backend():
    source = workers.WORKER.read_text()
    assert "mnemosyne" not in [
        line.split()[1].split(".")[0]
        for line in source.splitlines()
        if line.strip().startswith(("import ", "from "))
    ]
    assert "AudioSeal.load_generator" in source  # every output is watermarked
    assert "os.dup2(2, 1)" in source  # libraries printing on stdout cannot break the protocol
