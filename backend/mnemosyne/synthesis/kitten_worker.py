"""KittenTTS 2 in its own venv (services/tts_install.py), driven over stdin/stdout by kitten.py.

Run by that venv's Python, never imported by the backend: it must not import mnemosyne, which is
not installed there. One JSON object per line in, one per line out:

    {"op": "load"}                                        -> {"ok": true, "sample_rate": 24000}
    {"op": "speak", "text": ..., "reference": wav, "reference_text": ..., "out": wav}
                                                          -> {"ok": true, "watermarked": true}
    {"op": "detect", "path": wav}                         -> {"ok": true, "score": 0.97}
    {"op": "quit"}

An error answers {"ok": false, "error": "..."} and the worker carries on. Logs go to stderr.
`--prefetch` downloads everything once (the installer's step) and exits.

The model is loaded from the local copy the installer pinned (`--model`, a directory), never by
repository name, and the backend runs this with HF_HUB_OFFLINE=1: nothing reaches the Hub after
the install. Every output is watermarked with AudioSeal (MIT, facebook/audioseal) before it is
written; KittenTTS itself marks nothing (its `resemble-perth` dependency is unused).
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

MODEL_REPO = "KittenML/kitten-tts-2"
AUDIOSEAL_GENERATOR = "audioseal_wm_16bits"
AUDIOSEAL_DETECTOR = "audioseal_detector_16bits"


def log(*parts) -> None:
    print("[kitten-worker]", *parts, file=sys.stderr, flush=True)


class Worker:
    def __init__(self, model_dir: str, backend: str | None, weights: str | None):
        self.model_dir = model_dir
        self.backend = backend
        self.weights = weights
        self.tts = None
        self.generator = None
        self.detector = None

    def load(self) -> dict:
        if self.tts is None:
            from kittenml import KittenTTS

            kwargs = {"weights": self.weights} if self.weights else {}
            if self.backend:
                kwargs["backend"] = self.backend
            self.tts = KittenTTS(self.model_dir, **kwargs)
            log("model loaded", self.model_dir, kwargs)
        if self.generator is None:
            from audioseal import AudioSeal

            self.generator = AudioSeal.load_generator(AUDIOSEAL_GENERATOR)
        return {"sample_rate": int(self.tts.sample_rate)}

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
        audio = self.tts.generate(
            req["text"],
            reference=req["reference"],
            reference_text=req.get("reference_text") or None,
            preset=req.get("preset") or None,
        )
        rate = int(self.tts.sample_rate)
        audio = self._mark(audio, rate)
        sf.write(req["out"], audio, rate)
        return {"sample_rate": rate, "watermarked": True}

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


def prefetch(
    model_dir: str, revision: str, vocoder_revision: str, audioseal_revision: str, weights: str
) -> None:
    """The installer's download step: the model at its pinned revision into `model_dir`, then
    the vocoder at its revision into the HF cache, AudioSeal's generator and detector at theirs
    into AUDIOSEAL_CACHE_DIR, then one load of everything (which needs nothing more)."""
    from huggingface_hub import hf_hub_download, snapshot_download

    # Only the files the config names for this weight variant: the repository also carries the
    # other variants and a llama.cpp bundle (7.7 GB in all, about 1.1 GB needed).
    config = hf_hub_download(MODEL_REPO, "config.json", revision=revision, local_dir=model_dir)
    with open(config) as f:
        config = json.load(f)
    try:
        from kittenml.get_model import _kitten2_files  # kittenml's own list for its loader

        patterns = _kitten2_files(config, None, weights or None)
    except ImportError:
        patterns = None
    snapshot_download(
        repo_id=MODEL_REPO, revision=revision, local_dir=model_dir, allow_patterns=patterns
    )
    hf_hub_download(
        "ResembleAI/chatterbox-turbo", "s3gen_meanflow.safetensors", revision=vocoder_revision
    )
    _pin_audioseal(audioseal_revision, os.environ["AUDIOSEAL_CACHE_DIR"])
    worker = Worker(model_dir, None, weights or None)
    worker.load()
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, help="the local model directory")
    parser.add_argument("--backend", default="", help="kittenml backend: torch (default) or mlx")
    parser.add_argument("--weights", default="", help="kittenml weights variant (emb4: smaller)")
    parser.add_argument("--prefetch", action="store_true")
    parser.add_argument("--revision", default="")
    parser.add_argument("--vocoder-revision", default="")
    parser.add_argument("--audioseal-revision", default="")
    args = parser.parse_args()
    if args.prefetch:
        prefetch(
            args.model, args.revision, args.vocoder_revision, args.audioseal_revision, args.weights
        )
        return 0
    worker = Worker(args.model, args.backend or None, args.weights or None)
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
        print(json.dumps(answer), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
