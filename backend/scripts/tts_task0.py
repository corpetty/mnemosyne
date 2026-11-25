"""Task 0 of docs/plans/2026-11-20-narration-voices.md: a narration engine through our worker
(mnemosyne/synthesis/tts_worker.py), scored with the models Mnemosyne itself uses: Parakeet TDT
0.6B v3 int8 (onnx-asr) for the round-trip word error rate, WeSpeaker ResNet34-LM (sherpa-onnx)
for speaker similarity, AudioSeal's detector for the watermark. Files only; nothing is played.

Run it with the Python of an installed engine (<data_dir>/tts/<engine>/venv) after adding the two
scorers to that venv (`uv pip install sherpa-onnx onnx-asr[cpu,hub]`), never the backend's:

    <venv>/bin/python -I backend/scripts/tts_task0.py --engine qwen3|kitten
        --repo <the engine's model repository> --worker backend/mnemosyne/synthesis/tts_worker.py
        --model <data_dir>/tts/<engine>/model --cache <data_dir>/tts/<engine>/cache
        --out /tmp/task0 --refs <reference voice WAVs, 10-30 s each>

Use recordings of people who agreed to it (yourself): the 2026-11-20 run used KittenTTS's own
sample voices. Results go to <out>/results.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import soundfile as sf

WESPEAKER_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
    "wespeaker_en_voxceleb_resnet34_LM.onnx"
)
WESPEAKER_SHA = "e9848563da86f263117134dfd7ad63c92355b37de492b55e325400c9d9c39012"

# What Mnemosyne narrates: summary and digest prose, a list read as sentences, names, a question.
PARAGRAPHS = [
    "Weekly sync. The team agreed to ship the second version on Friday, after one more review of "
    "the release notes.",
    "Decisions. Move the budget document to the shared drive, and keep the old one read only until "
    "the end of the month.",
    "Action items. Ada will send the interview notes to Grace by Monday. Corey will book the room "
    "for the planning session.",
    "Open questions. Should the pilot start in June or slip into July, and who owns the training "
    "plan if it slips?",
    "Overview of the week. Most meetings were about the launch. The main risk is timing, not cost, "
    "and the team wants a decision on the vendor before the next call.",
    "Mnemosyne records the meeting, transcribes it on your own computer, and writes a summary with "
    "decisions, action items and open questions.",
]


def log(*parts):
    print(*parts, flush=True)


def normalize(text: str) -> list[str]:
    text = text.lower().replace("-", " ")
    return re.findall(r"[a-z0-9']+", text)


def wer(ref: str, hyp: str) -> float:
    r, h = normalize(ref), normalize(hyp)
    d = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        prev, d[0] = d[0], i
        for j, hw in enumerate(h, 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (rw != hw))
            prev, d[j] = d[j], cur
    return d[len(h)] / max(1, len(r))


def cosine(a, b) -> float:
    a, b = np.asarray(a, np.float64), np.asarray(b, np.float64)
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))


class Worker:
    """synthesis/workers.py's side of the protocol, synchronously."""

    def __init__(
        self,
        python: str,
        worker: Path,
        model: Path,
        cache: Path,
        log_path: Path,
        engine: str,
        repo: str,
    ):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "VIRTUAL_ENV"))}
        # As synthesis/workers.py worker_env sets it.
        env.update(
            HF_HUB_CACHE=str(cache),
            AUDIOSEAL_CACHE_DIR=str(cache / "audioseal"),
            HF_HOME=str(cache / "hf-home"),
            HF_HUB_OFFLINE="1",
            HF_HUB_DISABLE_TELEMETRY="1",
        )
        self.proc = subprocess.Popen(
            [python, "-I", str(worker), "--engine", engine, "--model", str(model), "--repo", repo],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=open(log_path, "ab"),  # noqa: SIM115
            env=env,
            text=True,
        )

    def ask(self, req: dict) -> dict:
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("worker stopped")
        answer = json.loads(line)
        if not answer.get("ok"):
            raise RuntimeError(answer.get("error"))
        return answer

    def close(self):
        try:
            self.proc.stdin.write('{"op": "quit"}\n')
            self.proc.stdin.flush()
            self.proc.wait(30)
        except Exception:
            self.proc.kill()


def wespeaker(models: Path):
    import sherpa_onnx

    path = models / "wespeaker_en_voxceleb_resnet34_LM.onnx"
    if not path.exists():
        models.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(WESPEAKER_URL, path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != WESPEAKER_SHA:
        path.unlink()
        raise SystemExit("WeSpeaker model checksum mismatch")
    config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(path), num_threads=4)
    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)

    def embed(wav: Path):
        audio, rate = sf.read(str(wav), dtype="float32", always_2d=True)
        stream = extractor.create_stream()
        stream.accept_waveform(rate, audio.mean(axis=1))
        stream.input_finished()
        return np.asarray(extractor.compute(stream))

    return embed


def parakeet():
    import onnx_asr

    model = onnx_asr.load_model("nemo-parakeet-tdt-0.6b-v3", quantization="int8")

    def transcribe(wav: Path) -> str:
        audio, rate = sf.read(str(wav), dtype="float32", always_2d=True)
        mono = audio.mean(axis=1)
        if rate != 16000:
            n = int(round(mono.size * 16000 / rate))
            mono = np.interp(np.linspace(0, mono.size - 1, n), np.arange(mono.size), mono)
        return str(model.recognize(mono.astype(np.float32), sample_rate=16000))

    return transcribe


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="kitten", choices=["kitten", "qwen3"])
    ap.add_argument("--repo", default="")
    ap.add_argument("--worker", type=Path, required=True)
    ap.add_argument("--model", type=Path, required=True)
    ap.add_argument("--cache", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--refs", nargs="+", required=True, help="reference voice WAVs")
    ap.add_argument("--paragraphs", type=int, default=len(PARAGRAPHS))
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    refs = [Path(r) for r in args.refs]

    log("loading the scorers (Parakeet int8, WeSpeaker)")
    transcribe = parakeet()
    embed = wespeaker(args.out.parent / "models")

    t0 = time.perf_counter()
    worker = Worker(
        sys.executable,
        args.worker,
        args.model,
        args.cache,
        args.out / "worker.log",
        args.engine,
        args.repo,
    )
    rate = worker.ask({"op": "load"})["sample_rate"]
    load_s = time.perf_counter() - t0
    log(f"worker loaded in {load_s:.1f} s (sample rate {rate})")

    ref_vecs = {r.stem: embed(r) for r in refs}
    ref_texts = {}
    for r in refs:
        ref_texts[r.stem] = transcribe(r)
        info = sf.info(str(r))
        log(f"reference {r.stem}: {info.duration:.1f} s, heard: {ref_texts[r.stem][:90]}")

    rows = []
    for r in refs:
        for i, text in enumerate(PARAGRAPHS[: args.paragraphs]):
            out = args.out / f"{r.stem}-{i}.wav"
            t = time.perf_counter()
            answer = worker.ask(
                {
                    "op": "speak",
                    "text": text,
                    "reference": str(r),
                    "reference_text": ref_texts[r.stem],
                    "out": str(out),
                }
            )
            gen_s = time.perf_counter() - t
            seconds = sf.info(str(out)).duration
            heard = transcribe(out)
            vec = embed(out)
            own = cosine(vec, ref_vecs[r.stem])
            others = [cosine(vec, v) for k, v in ref_vecs.items() if k != r.stem]
            mark = worker.ask({"op": "detect", "path": str(out)})["score"]
            row = {
                "voice": r.stem,
                "paragraph": i,
                "seconds": round(seconds, 2),
                "gen_seconds": round(gen_s, 2),
                "rtf": round(gen_s / max(seconds, 1e-6), 2),
                "wer": round(wer(text, heard), 3),
                "similarity": round(own, 3),
                "max_other": round(max(others), 3) if others else None,
                "watermarked": answer.get("watermarked"),
                "detect": round(mark, 3),
                "heard": heard,
            }
            rows.append(row)
            log(
                f"{r.stem} #{i}: {seconds:.1f} s audio in {gen_s:.1f} s (RTF {row['rtf']}), "
                f"WER {row['wer']:.2f}, sim {own:.2f} (others max {row['max_other']}), "
                f"mark {mark:.2f}"
            )
    controls = {
        r.stem: round(worker.ask({"op": "detect", "path": str(r)})["score"], 3) for r in refs
    }
    worker.close()

    def mean(key, subset=rows):
        vals = [x[key] for x in subset if x[key] is not None]
        return round(float(np.mean(vals)), 3) if vals else None

    summary = {
        "load_seconds": round(load_s, 1),
        "chunks": len(rows),
        "mean_rtf": mean("rtf"),
        "mean_wer": mean("wer"),
        "median_wer": round(float(np.median([x["wer"] for x in rows])), 3),
        "chunks_over_10pct_wer": sum(x["wer"] > 0.10 for x in rows),
        "mean_similarity": mean("similarity"),
        "mean_max_other": mean("max_other"),
        "min_similarity": min(x["similarity"] for x in rows),
        "detect_min_on_output": min(x["detect"] for x in rows),
        "detect_on_references": controls,
        "per_voice": {
            r.stem: {
                "similarity": mean("similarity", [x for x in rows if x["voice"] == r.stem]),
                "wer": mean("wer", [x for x in rows if x["voice"] == r.stem]),
            }
            for r in refs
        },
    }
    (args.out / "results.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=1))
    log(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
