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
import sys
import traceback

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


def prefetch(model_dir: str, revision: str, vocoder_revision: str) -> None:
    """The installer's download step: the model at its pinned revision into `model_dir`, then
    one load (which fetches the vocoder and AudioSeal into the HF cache)."""
    from huggingface_hub import snapshot_download

    snapshot_download(repo_id=MODEL_REPO, revision=revision, local_dir=model_dir)
    from huggingface_hub import hf_hub_download

    hf_hub_download(
        "ResembleAI/chatterbox-turbo", "s3gen_meanflow.safetensors", revision=vocoder_revision
    )
    Worker(model_dir, None, None).load()
    log("prefetched")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, help="the local model directory")
    parser.add_argument("--backend", default="", help="kittenml backend: torch (default) or mlx")
    parser.add_argument("--weights", default="", help="kittenml weights variant (emb4: smaller)")
    parser.add_argument("--prefetch", action="store_true")
    parser.add_argument("--revision", default="")
    parser.add_argument("--vocoder-revision", default="")
    args = parser.parse_args()
    if args.prefetch:
        prefetch(args.model, args.revision, args.vocoder_revision)
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
