"""Narration engines that run as a worker in their own venv (tts_worker.py over stdin/stdout).

Each engine is an `EngineSpec`: its model and pinned revisions, its licence, where it lives
(`<data_dir>/tts/<name>`), its hashed requirement files (requirements/<name>-<platform>.txt) and
how it wants its reference (several clips joined, or one). `WorkerSynthesizer` is the backend's
side of the protocol for any of them; services/tts_install.py installs them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .engine import RefClip, Speech, SynthesisError, VoicePrompt, pick_reference

logger = logging.getLogger(__name__)

WORKER = Path(__file__).with_name("tts_worker.py")
REQUIREMENTS = Path(__file__).with_name("requirements")
AUDIOSEAL_REVISION = "3c19eba53390776cf2cc9ed5f6c9ac67ce72ecba"  # facebook/audioseal, MIT
START_TIMEOUT = 600.0  # the first load reads gigabytes of weights
SPEAK_TIMEOUT = 600.0  # one chunk; a slow CPU is several times slower than real time


@dataclass(frozen=True)
class EngineSpec:
    name: str  # the setting's value and the folder under <data_dir>/tts
    title: str
    space: str  # prepared prompts are kept per space (synthesis/engine.py)
    repo: str
    revision: str
    license_name: str
    license_url: str
    open_license: bool  # False: the person accepts the licence before installing
    attribution: str  # to show where the engine is used ("" when none is asked for)
    size_note: str  # what the install takes, in words
    extra_revision: str = ""  # a second model the engine loads (KittenTTS: its vocoder)
    reference: str = "joined"  # joined: the best clips, about 20 s; single: the one best clip
    cuda: bool = True  # has a CUDA requirement set for Linux with NVIDIA
    worker_args: tuple[str, ...] = field(default_factory=tuple)

    @property
    def model(self) -> str:
        return f"{self.repo}@{self.revision[:12]}"

    def home(self, data_dir: Path) -> Path:
        return Path(data_dir) / "tts" / self.name

    def requirements(self, platform: str) -> Path:
        """platform: linux, linux-cuda or macos."""
        return REQUIREMENTS / f"{self.name}-{platform}.txt"


def python_of(root: Path) -> Path:
    venv = root / "venv"
    return venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


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


def worker_command(spec: EngineSpec, root: Path, extra: list[str] | None = None) -> list[str]:
    args = [str(python_of(root)), "-I", str(WORKER), "--engine", spec.name]
    args += ["--model", str(root / "model"), "--repo", spec.repo, *spec.worker_args]
    return args + (extra or [])


class WorkerSynthesizer:
    """A `Synthesizer` (engine.py) whose model runs in a worker process, started on first use."""

    def __init__(self, spec: EngineSpec, data_dir: Path, extra_args: list[str] | None = None):
        self.spec = spec
        self.name = spec.name
        self.space = spec.space
        self.model = spec.model
        self.root = spec.home(data_dir)
        self.extra_args = list(extra_args or [])
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
            raise SynthesisError(f"{self.spec.title} is not installed (Settings → Voices)")
        log = open(self.root / "worker.log", "ab")  # noqa: SIM115 - the process owns it
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *worker_command(self.spec, self.root, self.extra_args),
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
        title = self.spec.title
        if proc is None or proc.stdin is None or proc.stdout is None:
            raise SynthesisError(f"The {title} worker is not running")
        proc.stdin.write((json.dumps(request) + "\n").encode())
        await proc.stdin.drain()
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout)
        except TimeoutError as e:
            await self._stop()
            raise SynthesisError(f"{title} did not answer in time") from e
        if not line:
            await self._stop()
            raise SynthesisError(f"{title} stopped (see {self.root / 'worker.log'})")
        answer = json.loads(line)
        if not answer.get("ok"):
            raise SynthesisError(f"{title}: {answer.get('error', 'failed')}")
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
        """The reference the engine conditions on. joined: the best clips (about 20 s, KittenTTS
        takes 5-30 s) with their transcripts. single: the one best clip of 8-20 s (Qwen3-TTS
        clones best from one clean clip; joined clips were not tried). A voice whose owner
        picked a reference clip hands only that one in (services/voices.py)."""
        from ..audio.mixer import decode_audio

        if self.spec.reference == "single" and len(clips) > 1:
            fitting = [c for c in clips if 8 <= c.seconds <= 20 and c.text.strip()] or clips
            chosen = [max(fitting, key=lambda c: (c.quality, c.seconds))]
        else:
            chosen = pick_reference(clips) or clips[:1]
        if not chosen or not chosen[0].text.strip():
            raise SynthesisError("The voice has no usable clips")
        rate = 24000
        gap = np.zeros(int(rate * 0.3), dtype=np.float32)
        parts = []
        for clip in chosen:
            parts += [await asyncio.to_thread(decode_audio, clip.path, rate), gap]
        audio = np.concatenate(parts[:-1])
        workdir.mkdir(parents=True, exist_ok=True)
        dest = workdir / f"prompt-{self.space}.wav"
        await asyncio.to_thread(write_wav, dest, audio, rate)
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
                        "language": prompt.extra.get("language", "en"),
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


def write_wav(path: Path, audio: np.ndarray, rate: int) -> None:
    import wave

    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def remove(spec: EngineSpec, data_dir: Path) -> None:
    shutil.rmtree(spec.home(data_dir), ignore_errors=True)
