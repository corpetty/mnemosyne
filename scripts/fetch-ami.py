#!/usr/bin/env python3
"""Fetch AMI meetings (headset mix) with their speaker turns, for diarization benchmarks.

    cd backend && uv run python ../scripts/fetch-ami.py            # ES2004a-d
    cd backend && uv run python ../scripts/fetch-ami.py IS1009b --out /some/dir

Reads the Hugging Face mirror diarizers-community/ami (config ihm; the Edinburgh server stalls),
which stores whole meetings in three test-split parquet files of ~300 MB each: every file that holds
a wanted meeting is read once, and only the wanted meetings are written. Per meeting:
<id>.wav (16 kHz mono) and <id>.turns.json, a list of {speaker, start, end}. Default output:
~/.cache/mnemosyne-trials/ami. The AMI corpus is CC BY 4.0.
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
from pathlib import Path

REPO = "datasets/diarizers-community/ami"
SPLITS = [f"ihm/test-{i:05d}-of-00003.parquet" for i in range(3)]


def main() -> None:
    import pyarrow.parquet as pq
    from huggingface_hub import HfFileSystem

    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("meetings", nargs="*", default=["ES2004a", "ES2004b", "ES2004c", "ES2004d"])
    p.add_argument("--out", type=Path, default=Path.home() / ".cache/mnemosyne-trials/ami")
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    wanted = {m for m in args.meetings if not (args.out / f"{m}.wav").exists()}
    fs = HfFileSystem()
    for split in SPLITS:
        if not wanted:
            break
        f = pq.ParquetFile(fs.open(f"{REPO}/{split}"))
        names = [r["audio"]["path"] for r in f.read(columns=["audio.path"]).to_pylist()]
        here = [n.split(".")[0] for n in names]
        if not wanted & set(here):
            continue
        print(f"reading {split}")
        for row in f.read().to_pylist():
            meeting = row["audio"]["path"].split(".")[0]
            if meeting not in wanted:
                continue
            wav = args.out / f"{meeting}.wav"
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", "pipe:0", "-ac", "1", "-ar", "16000",
                 "-c:a", "pcm_s16le", str(wav)],
                input=io.BytesIO(row["audio"]["bytes"]).getvalue(),
                check=True,
            )
            turns = [
                {"speaker": s, "start": round(a, 3), "end": round(b, 3)}
                for s, a, b in zip(
                    row["speakers"], row["timestamps_start"], row["timestamps_end"], strict=True
                )
            ]
            turns.sort(key=lambda t: t["start"])
            (args.out / f"{meeting}.turns.json").write_text(json.dumps(turns, indent=0))
            wanted.discard(meeting)
            print(f"{meeting}: {len(turns)} turns, {len({t['speaker'] for t in turns})} speakers")
    if wanted:
        raise SystemExit(f"not found: {', '.join(sorted(wanted))}")


if __name__ == "__main__":
    main()
