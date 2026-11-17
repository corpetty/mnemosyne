"""Do pyannote's speaker embedder and the ONNX WeSpeaker ResNet34-LM give the same vectors?

A one-off check for docs/plans/2026-11-17-speakers-without-torch.md (task 0). If the two embed the same audio to
(nearly) the same vector, voice profiles made on a GPU machine work on a machine with only the ONNX embedder.
It is a tool for a developer, not part of the app: nothing imports it and the app never runs it.

Turns: the sample meeting has no reference turns, so this cuts fixed-length windows (default 4 s) at speech-dense
places: the recording is split into N equal slots and the loudest window of each slot is taken (by RMS energy).
Windows come from all over the meeting, so they hold several speakers.

Prints, per turn, the cosine between the pyannote vector and the ONNX vector of the same audio; the median and
minimum of those; and the Spearman correlation of each embedder's turn-to-turn cosine matrix (do both spaces rank
pairs of turns the same way). Pass bar: median same-audio cosine >= 0.95.

  --proxy   pyannote/wespeaker-voxceleb-resnet34-LM, not gated, via Model.from_pretrained + Inference("whole")
  default   the app's own PyannoteEmbedder on --pyannote-model (community-1; needs an HF token)

Run from backend/:  uv run --no-sync python scripts/compare-voice-spaces.py --proxy
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
import urllib.request
from pathlib import Path

import numpy as np

ONNX_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
    "wespeaker_en_voxceleb_resnet34_LM.onnx"
)
ONNX_SHA256 = "e9848563da86f263117134dfd7ad63c92355b37de492b55e325400c9d9c39012"
ONNX_SIZE = 26_530_550
RATE = 16000
SAMPLE = Path(__file__).resolve().parent.parent / "mnemosyne" / "samples" / "sample-meeting.ogg"


def pick_windows(pcm: np.ndarray, count: int, seconds: float) -> list[tuple[float, float]]:
    """The loudest `seconds` window in each of `count` equal slots of the recording."""
    win = int(seconds * RATE)
    hop = RATE // 2
    slot = max(len(pcm) // count, win)
    out = []
    for i in range(count):
        lo, hi = i * slot, min((i + 1) * slot, len(pcm))
        best, best_e = None, -1.0
        for s in range(lo, max(lo + 1, hi - win + 1), hop):
            chunk = pcm[s : s + win]
            if len(chunk) < win:
                break
            e = float(np.sqrt(np.mean(chunk.astype(np.float64) ** 2)))
            if e > best_e:
                best, best_e = s, e
        if best is not None:
            out.append((best / RATE, (best + win) / RATE))
    return out


def fetch_onnx(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "wespeaker_en_voxceleb_resnet34_LM.onnx"

    def ok() -> bool:
        return (
            path.is_file()
            and path.stat().st_size == ONNX_SIZE
            and hashlib.sha256(path.read_bytes()).hexdigest() == ONNX_SHA256
        )

    if not ok():
        tmp = path.with_suffix(".part")
        urllib.request.urlretrieve(ONNX_URL, tmp)  # noqa: S310 (pinned https URL)
        tmp.replace(path)
        if not ok():
            path.unlink(missing_ok=True)
            sys.exit("ONNX model failed the size/SHA-256 check")
    return path


def onnx_vectors(model: Path, clips: list[np.ndarray], threads: int, scale: float = 1.0) -> np.ndarray:
    import sherpa_onnx

    config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(model), num_threads=threads)
    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
    vecs = []
    for clip in clips:
        stream = extractor.create_stream()
        stream.accept_waveform(RATE, (clip * scale).astype(np.float32))
        stream.input_finished()
        vecs.append(np.asarray(extractor.compute(stream), dtype=np.float64))
    return np.stack(vecs)


def pyannote_vectors(args, clips: list[np.ndarray]) -> np.ndarray:
    import torch

    torch.set_num_threads(args.threads)
    if args.proxy:
        from pyannote.audio import Inference, Model

        model = Model.from_pretrained("pyannote/wespeaker-voxceleb-resnet34-LM")
        inf = Inference(model, window="whole", device=torch.device("cpu"))
        vecs = []
        for clip in clips:
            wav = torch.from_numpy(clip).unsqueeze(0)
            vecs.append(np.asarray(inf({"waveform": wav, "sample_rate": RATE}), dtype=np.float64).ravel())
        return np.stack(vecs)

    from mnemosyne.transcription.live_speakers import PyannoteEmbedder

    embedder = PyannoteEmbedder(args.pyannote_model, hf_token=args.hf_token, device="cpu")

    async def run():
        return [await embedder.embed(c, RATE) for c in clips]

    vecs = asyncio.run(run())
    if any(v is None for v in vecs):
        sys.exit("PyannoteEmbedder returned no vector for some clip")
    return np.stack([np.asarray(v, dtype=np.float64) for v in vecs])


def unit(m: np.ndarray) -> np.ndarray:
    return m / np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-12)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("audio", nargs="?", type=Path, default=SAMPLE, help="audio file (default: the sample meeting)")
    p.add_argument("--turns", type=int, default=30, help="number of windows to embed (default 30)")
    p.add_argument("--seconds", type=float, default=4.0, help="window length in seconds (default 4)")
    p.add_argument("--proxy", action="store_true", help="use the ungated pyannote/wespeaker-voxceleb-resnet34-LM")
    p.add_argument("--pyannote-model", default="pyannote/speaker-diarization-community-1")
    p.add_argument("--hf-token", default=os.environ.get("HF_TOKEN", ""), help="default: $HF_TOKEN")
    p.add_argument("--models-dir", type=Path, default=Path.home() / ".cache" / "mnemosyne-trials" / "voices")
    p.add_argument(
        "--onnx-scale",
        type=float,
        default=1.0,
        help="multiply samples before sherpa-onnx (default 1; 32768 tests the int16-range hypothesis)",
    )
    p.add_argument("--threads", type=int, default=2)
    args = p.parse_args()

    if not args.proxy and not args.hf_token:
        sys.exit("community-1 is gated: pass --hf-token / set HF_TOKEN, or use --proxy")

    from mnemosyne.audio.mixer import decode_audio

    pcm = decode_audio(args.audio, RATE).astype(np.float32)
    spans = pick_windows(pcm, args.turns, args.seconds)
    clips = [np.ascontiguousarray(pcm[int(a * RATE) : int(b * RATE)]) for a, b in spans]
    print(f"{len(clips)} windows of {args.seconds:g} s from {args.audio.name} (loudest window per equal slot)")

    onnx = onnx_vectors(fetch_onnx(args.models_dir), clips, args.threads, args.onnx_scale)
    py = pyannote_vectors(args, clips)
    print(f"dims: pyannote {py.shape[1]}, onnx {onnx.shape[1]}")
    if py.shape[1] != onnx.shape[1]:
        sys.exit("different dimensions: not the same space")

    pu, ou = unit(py), unit(onnx)
    same = np.sum(pu * ou, axis=1)
    for (a, b), c in zip(spans, same):
        print(f"  {a:7.1f}-{b:7.1f} s  cos(pyannote, onnx) = {c:.4f}")
    med = float(np.median(same))
    print(f"same audio: median {med:.4f}, min {same.min():.4f}, max {same.max():.4f}")

    iu = np.triu_indices(len(clips), 1)
    pm, om = (pu @ pu.T)[iu], (ou @ ou.T)[iu]
    from scipy.stats import spearmanr

    rho = float(spearmanr(pm, om).statistic)
    for name, v in (("pyannote", pm), ("onnx", om)):
        q = np.percentile(v, [5, 25, 50, 75, 95])
        print(f"turn-to-turn cosine, {name}: p5 {q[0]:.3f} p25 {q[1]:.3f} p50 {q[2]:.3f} p75 {q[3]:.3f} p95 {q[4]:.3f}")
    print(f"Spearman of the pairwise-cosine matrices: {rho:.4f}")
    print(f"verdict: {'SAME space' if med >= 0.95 else 'NOT the same space'} (bar: median >= 0.95)")


if __name__ == "__main__":
    main()
