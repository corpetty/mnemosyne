"""A narration engine in its own venv (services/tts_install.py), driven over stdin/stdout by
synthesis/workers.py. `--engine kitten` is KittenTTS 2, `--engine qwen3` Qwen3-TTS Base.

Run by that venv's Python, never imported by the backend: it must not import mnemosyne, which is
not installed there. One JSON object per line in, one per line out:

    {"op": "load"}                                        -> {"ok": true, "sample_rate": 24000}
    {"op": "speak", "text": ..., "reference": wav, "reference_text": ..., "language": "en",
     "out": wav}                                          -> {"ok": true, "watermarked": true}
    {"op": "detect", "path": wav}                         -> {"ok": true, "score": 0.97}
    {"op": "quit"}

An error answers {"ok": false, "error": "..."} and the worker carries on. Logs go to stderr.
`--prefetch` downloads everything once (the installer's step) and exits.

The model is loaded from the local copy the installer pinned (`--model`, a directory), never by
repository name, and the backend runs this with HF_HUB_OFFLINE=1: nothing reaches the Hub after
the install. Every output is watermarked with AudioSeal (MIT, facebook/audioseal) before it is
written; neither engine marks anything itself (KittenTTS's `resemble-perth` dependency is unused).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback

# AudioSeal's bundled Moshi layers torch.compile themselves on first use, which needs a C++
# compiler (cl, gcc) people do not have: found in Task 0, 2026-11-20. Read at import time.
os.environ.setdefault("NO_TORCH_COMPILE", "1")

AUDIOSEAL_GENERATOR = "audioseal_wm_16bits"
AUDIOSEAL_DETECTOR = "audioseal_detector_16bits"
# Qwen3-TTS takes the language by name; Mnemosyne keeps codes.
QWEN_LANGUAGES = {
    "en": "English",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "de": "German",
    "fr": "French",
    "ru": "Russian",
    "pt": "Portuguese",
    "es": "Spanish",
    "it": "Italian",
}


def log(*parts) -> None:
    print("[tts-worker]", *parts, file=sys.stderr, flush=True)


class Kitten:
    """KittenTTS 2 (kittenml)."""

    repo = "KittenML/kitten-tts-2"

    def __init__(self, model_dir: str, backend: str | None, weights: str | None):
        self.model_dir, self.backend, self.weights = model_dir, backend, weights
        self.tts = None

    def load(self) -> int:
        if self.tts is None:
            from kittenml import KittenTTS

            kwargs = {"weights": self.weights} if self.weights else {}
            if self.backend:
                kwargs["backend"] = self.backend
            self.tts = KittenTTS(self.model_dir, **kwargs)
            log("KittenTTS loaded", self.model_dir, kwargs)
        return int(self.tts.sample_rate)

    def generate(self, req: dict):
        return self.tts.generate(
            req["text"],
            reference=req["reference"],
            reference_text=req.get("reference_text") or None,
            preset=req.get("preset") or None,
        )

    def prefetch(self, revision: str, extra_revision: str) -> None:
        """The model at its revision, only the files the config names for this weight variant
        (the repository also carries the other variants and a llama.cpp bundle: 7.7 GB in all,
        about 1.1 GB needed), then the Chatterbox-Turbo vocoder it loads at its revision."""
        from huggingface_hub import hf_hub_download, snapshot_download

        config = hf_hub_download(
            self.repo, "config.json", revision=revision, local_dir=self.model_dir
        )
        with open(config) as f:
            config = json.load(f)
        try:
            from kittenml.get_model import _kitten2_files  # kittenml's own list for its loader

            patterns = _kitten2_files(config, None, self.weights or None)
        except ImportError:
            patterns = None
        snapshot_download(
            repo_id=self.repo, revision=revision, local_dir=self.model_dir, allow_patterns=patterns
        )
        hf_hub_download(
            "ResembleAI/chatterbox-turbo", "s3gen_meanflow.safetensors", revision=extra_revision
        )


class Qwen3:
    """Qwen3-TTS Base (qwen-tts), voice cloning from a reference clip and its transcript."""

    def __init__(self, model_dir: str, repo: str):
        self.model_dir, self.repo = model_dir, repo
        self.tts = None

    def load(self) -> int:
        if self.tts is None:
            import torch
            from qwen_tts import Qwen3TTSModel

            if torch.cuda.is_available():
                device, dtype = "cuda:0", torch.bfloat16
            elif torch.backends.mps.is_available():
                device, dtype = "mps", torch.float16
            else:
                device, dtype = "cpu", torch.float32
            self.tts = Qwen3TTSModel.from_pretrained(
                self.model_dir, device_map=device, dtype=dtype, attn_implementation="sdpa"
            )
            log("Qwen3-TTS loaded", self.model_dir, device, dtype)
        return 24000

    def generate(self, req: dict):
        import numpy as np

        language = QWEN_LANGUAGES.get((req.get("language") or "en")[:2].lower(), "English")
        wavs, rate = self.tts.generate_voice_clone(
            text=req["text"],
            language=language,
            ref_audio=req["reference"],
            ref_text=req.get("reference_text") or None,
            # About 75 s of audio: a chunk is a few sentences, so this only ends a runaway.
            max_new_tokens=900,
        )
        audio = np.asarray(wavs[0], dtype=np.float32)
        if rate != 24000:
            import torch
            import torchaudio.functional as F

            audio = F.resample(torch.from_numpy(audio), rate, 24000).numpy()
        return audio

    def prefetch(self, revision: str, extra_revision: str) -> None:
        """The model at its revision (its speech tokenizer is a subfolder of the repository)."""
        from huggingface_hub import snapshot_download

        snapshot_download(repo_id=self.repo, revision=revision, local_dir=self.model_dir)


class Worker:
    def __init__(self, engine):
        self.engine = engine
        self.rate = None
        self.generator = None
        self.detector = None

    def load(self) -> dict:
        self.rate = self.engine.load()
        if self.generator is None:
            from audioseal import AudioSeal

            self.generator = AudioSeal.load_generator(AUDIOSEAL_GENERATOR)
        return {"sample_rate": self.rate}

    def _mark(self, audio, rate: int):
        """The audio with AudioSeal's watermark added (at 16 kHz, the rate it was trained on,
        then brought back to `rate`)."""
        import torch
        import torchaudio.functional as F

        wav = torch.from_numpy(audio).float().reshape(1, 1, -1)
        low = F.resample(wav, rate, 16000) if rate != 16000 else wav
        with torch.no_grad():
            mark = self.generator.get_watermark(low, sample_rate=16000)
        if rate != 16000:
            mark = F.resample(mark, 16000, rate)
        mark = mark[..., : wav.shape[-1]]
        if mark.shape[-1] < wav.shape[-1]:
            mark = torch.nn.functional.pad(mark, (0, wav.shape[-1] - mark.shape[-1]))
        return (wav + mark).clamp(-1, 1).reshape(-1).numpy()

    def speak(self, req: dict) -> dict:
        import soundfile as sf

        self.load()
        audio = self._mark(self.engine.generate(req), self.rate)
        sf.write(req["out"], audio, self.rate)
        return {"sample_rate": self.rate, "watermarked": True}

    def detect(self, req: dict) -> dict:
        import soundfile as sf
        import torch
        import torchaudio.functional as F

        if self.detector is None:
            from audioseal import AudioSeal

            self.detector = AudioSeal.load_detector(AUDIOSEAL_DETECTOR)
        audio, rate = sf.read(req["path"], dtype="float32", always_2d=True)
        wav = torch.from_numpy(audio.mean(axis=1)).reshape(1, 1, -1)
        if rate != 16000:
            wav = F.resample(wav, rate, 16000)
        with torch.no_grad():
            score, _ = self.detector.detect_watermark(wav, sample_rate=16000)
        return {"score": float(score)}


def prefetch(engine, revision: str, extra_revision: str, audioseal_revision: str) -> None:
    """The installer's download step: the engine's model (and what it loads) at the pinned
    revisions, AudioSeal's generator and detector at theirs into AUDIOSEAL_CACHE_DIR, then one
    load of everything, which must need nothing more."""
    engine.prefetch(revision, extra_revision)
    _pin_audioseal(audioseal_revision, os.environ["AUDIOSEAL_CACHE_DIR"])
    Worker(engine).load()
    from audioseal import AudioSeal

    AudioSeal.load_detector(AUDIOSEAL_DETECTOR)
    log("prefetched")


def _pin_audioseal(revision: str, cache_dir: str) -> None:
    """AudioSeal's model cards name `.../resolve/main/<file>`, which torch.hub keeps as
    `<cache>/audioseal/<sha1 of the URL path>[:24]`. Put the file of the pinned revision exactly
    there, so AudioSeal loads it and never fetches `main` (nor anything once offline)."""
    import hashlib
    import shutil
    from pathlib import Path
    from urllib.parse import urlparse

    from audioseal.loader import load_local_model_config
    from huggingface_hub import hf_hub_download

    root = Path(cache_dir) / "audioseal"  # audioseal.loader._get_cache_dir adds "audioseal"
    staging = root / "pinned"
    for card in (AUDIOSEAL_GENERATOR, AUDIOSEAL_DETECTOR):
        url = str(load_local_model_config(card)["checkpoint"])
        dest = root / hashlib.sha1(urlparse(url).path.encode()).hexdigest()[:24]
        if dest.exists():
            continue
        name = url.rsplit("/", 1)[1]
        got = hf_hub_download("facebook/audioseal", name, revision=revision, local_dir=staging)
        root.mkdir(parents=True, exist_ok=True)
        shutil.move(got, dest)
    shutil.rmtree(staging, ignore_errors=True)


def build(args):
    if args.engine == "kitten":
        return Kitten(args.model, args.backend or None, args.weights or None)
    if args.engine == "qwen3":
        return Qwen3(args.model, args.repo)
    raise SystemExit(f"unknown engine {args.engine!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", default="kitten", choices=["kitten", "qwen3"])
    parser.add_argument("--model", required=True, help="the local model directory")
    parser.add_argument("--repo", default="", help="the model's repository (qwen3)")
    parser.add_argument("--backend", default="", help="kittenml backend: torch (default) or mlx")
    parser.add_argument("--weights", default="", help="kittenml weights variant (emb4: smaller)")
    parser.add_argument("--prefetch", action="store_true")
    parser.add_argument("--revision", default="")
    parser.add_argument("--vocoder-revision", dest="extra_revision", default="")
    parser.add_argument("--audioseal-revision", default="")
    args = parser.parse_args()
    engine = build(args)
    if args.prefetch:
        prefetch(engine, args.revision, args.extra_revision, args.audioseal_revision)
        return 0
    # The answers go on a private copy of stdout; file descriptor 1 itself now points at stderr,
    # so whatever the libraries print (qwen-tts's dependencies warn on stdout, and their
    # subprocesses inherit fd 1) lands in the log instead of breaking the protocol. Found in the
    # first real Qwen3-TTS run, 2026-11-20.
    answers = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    worker = Worker(engine)
    ops = {"load": lambda r: worker.load(), "speak": worker.speak, "detect": worker.detect}
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            if req.get("op") == "quit":
                break
            answer = {"ok": True, **ops[req["op"]](req)}
        except Exception as e:
            log(traceback.format_exc())
            answer = {"ok": False, "error": f"{type(e).__name__}: {e}"[:500]}
        print(json.dumps(answer), file=answers, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
