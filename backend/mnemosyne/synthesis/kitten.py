"""KittenTTS 2 (KittenML, 1.7B, zero-shot cloning from 5-30 s): the first real narration engine.

Chosen after Corey asked to evaluate it on the plan's PR (2026-11-20): one model for every
machine we support (PyTorch on Linux CPU or GPU, MLX on Apple silicon), cloning from a short
reference plus its transcript, 24 kHz output. Its weights are under the Stellon Labs Community
License, not an open-source licence: free for people and organisations under US$1M in annual
revenue and under US$1M in funding, commercial use must register with Stellon Labs, the AUP is
incorporated by reference, outputs may not train other models, and "Powered by KittenML" must
be shown. So the install asks the person to accept that licence first (tts_install.py), and the
Voices settings show the attribution.

It needs torch and transformers 5, which do not belong in the backend's environment, so it runs
in its own venv (`<data_dir>/tts/kitten`, services/tts_install.py) as a worker process
(kitten_worker.py) spoken to over stdin/stdout, started on first use and stopped when idle.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

from .engine import RefClip, Speech, SynthesisError, VoicePrompt, pick_reference

logger = logging.getLogger(__name__)

WORKER = Path(__file__).with_name("kitten_worker.py")
MODEL_REPO = "KittenML/kitten-tts-2"
# Pinned 2026-11-20 (the Hub's head of each repository then).
MODEL_REVISION = "c93cb7c271baa51d9ae8204d5277988906f235a2"
VOCODER_REVISION = "749d1c1a46eb10492095d68fbcf55691ccf137cd"  # ResembleAI/chatterbox-turbo, MIT
AUDIOSEAL_REVISION = "3c19eba53390776cf2cc9ed5f6c9ac67ce72ecba"  # facebook/audioseal, MIT
LICENSE_URL = "https://huggingface.co/KittenML/kitten-tts-2/blob/main/LICENSE.md"
LICENSE_NAME = "Stellon Labs Community License"
ATTRIBUTION = "Powered by KittenML"
SPACE = "kitten-tts-2"
START_TIMEOUT = 600.0  # the first load reads ~1 GB of weights
SPEAK_TIMEOUT = 600.0  # one chunk; a slow CPU is a few times slower than real time


def home(data_dir: Path) -> Path:
    return Path(data_dir) / "tts" / "kitten"


def python_of(root: Path) -> Path:
    venv = root / "venv"
    return venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def installed(data_dir: Path) -> bool:
    root = home(data_dir)
    return (root / "installed").is_file() and python_of(root).exists()


def worker_env(root: Path, offline: bool = True) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "VIRTUAL_ENV"))}
    env["HF_HUB_CACHE"] = str(root / "cache")
    # AudioSeal keeps its weights here, not in ~/.cache: inside the install, removed with it.
    env["AUDIOSEAL_CACHE_DIR"] = str(root / "cache" / "audioseal")
    # The rest of the Hub client (its xet transfer logs and staging) too, not ~/.cache/huggingface.
    env["HF_HOME"] = str(root / "cache" / "hf-home")
    env["HF_HUB_DISABLE_TELEMETRY"] = "1"
    if offline:
        env["HF_HUB_OFFLINE"] = "1"
    return env


class KittenSynthesizer:
    name = "kitten"
    space = SPACE
    model = f"{MODEL_REPO}@{MODEL_REVISION[:12]}"

    def __init__(self, data_dir: Path, weights: str = ""):
        self.root = home(data_dir)
        self.weights = weights  # "" = the lossless default, "emb4" = half the size
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self.sample_rate = 24000

    def is_loaded(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def load(self) -> None:
        async with self._lock:
            await self._start()

    async def _start(self) -> None:
        if self.is_loaded():
            return
        if not (self.root / "installed").is_file():
            raise SynthesisError("KittenTTS is not installed (Settings → Voices)")
        args = [str(python_of(self.root)), "-I", str(WORKER), "--model", str(self.root / "model")]
        if sys.platform == "darwin":
            args += ["--backend", "mlx"]
        if self.weights:
            args += ["--weights", self.weights]
        log = open(self.root / "worker.log", "ab")  # noqa: SIM115 - the process owns it
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=log,
                env=worker_env(self.root),
                cwd=str(self.root),
            )
        finally:
            log.close()
        try:
            answer = await self._ask({"op": "load"}, START_TIMEOUT)
        except Exception:
            await self._stop()
            raise
        self.sample_rate = int(answer.get("sample_rate", 24000))

    async def _ask(self, request: dict, timeout: float) -> dict:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdout is None:
            raise SynthesisError("The KittenTTS worker is not running")
        proc.stdin.write((json.dumps(request) + "\n").encode())
        await proc.stdin.drain()
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout)
        except TimeoutError as e:
            await self._stop()
            raise SynthesisError("KittenTTS did not answer in time") from e
        if not line:
            await self._stop()
            raise SynthesisError(f"KittenTTS stopped (see {self.root / 'worker.log'})")
        answer = json.loads(line)
        if not answer.get("ok"):
            raise SynthesisError(f"KittenTTS: {answer.get('error', 'failed')}")
        return answer

    async def unload(self) -> None:
        async with self._lock:
            await self._stop()

    async def _stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.write(b'{"op": "quit"}\n')
                await proc.stdin.drain()
            await asyncio.wait_for(proc.wait(), 10)
        except Exception:
            proc.kill()
            await proc.wait()

    async def prepare(self, clips: list[RefClip], workdir: Path) -> VoicePrompt:
        """One reference WAV of the best clips joined (about 20 s, the model takes 5-30 s), with
        their transcripts joined the same way."""
        chosen = pick_reference(clips)
        if not chosen:
            raise SynthesisError("The voice has no usable clips")
        from ..audio.mixer import decode_audio

        rate = 24000
        gap = np.zeros(int(rate * 0.3), dtype=np.float32)
        parts = []
        for clip in chosen:
            parts += [await asyncio.to_thread(decode_audio, clip.path, rate), gap]
        audio = np.concatenate(parts[:-1])
        workdir.mkdir(parents=True, exist_ok=True)
        dest = workdir / f"prompt-{self.space}.wav"
        await asyncio.to_thread(_write_wav, dest, audio, rate)
        return VoicePrompt(self.space, dest, " ".join(c.text.strip() for c in chosen))

    async def speak(self, prompt: VoicePrompt, text: str) -> Speech:
        from ..audio.mixer import decode_audio

        async with self._lock:
            await self._start()
            with tempfile.TemporaryDirectory(dir=self.root) as tmp:
                out = Path(tmp) / "out.wav"
                answer = await self._ask(
                    {
                        "op": "speak",
                        "text": text,
                        "reference": str(prompt.path),
                        "reference_text": prompt.text,
                        "out": str(out),
                    },
                    SPEAK_TIMEOUT,
                )
                samples = await asyncio.to_thread(decode_audio, out, self.sample_rate)
        return Speech(samples, self.sample_rate, watermarked=bool(answer.get("watermarked")))

    async def detect(self, path: Path) -> float:
        """AudioSeal's score (0..1) that `path` carries the watermark."""
        async with self._lock:
            await self._start()
            return float((await self._ask({"op": "detect", "path": str(path)}, 120))["score"])


def _write_wav(path: Path, audio: np.ndarray, rate: int) -> None:
    import wave

    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def remove(data_dir: Path) -> None:
    shutil.rmtree(home(data_dir), ignore_errors=True)
