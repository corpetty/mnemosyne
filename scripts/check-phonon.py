#!/usr/bin/env python3
"""Check that Phonon-2 really installs and transcribes, as the app does it.

Run from backend/ after `uv sync --locked`:
    uv run --no-sync python ../scripts/check-phonon.py

Does the real install (services/phonon_install.py: real uv, the pins in
phonon-requirements.txt, ~1 GB, the model download) into a temporary data dir, transcribes the
first minute of the bundled sample meeting with PhononTranscriber and asserts that segments with
word times came back, and that the server refuses a request without its API key (401).
0.16.0 shipped an installer that never worked because the tests' stand-in `phonon` accepted any
arguments; this is the test with the real one. Needs ffmpeg. Never run it on a machine you use.
"""

import asyncio
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SAMPLE = Path(__file__).resolve().parent.parent / "backend/mnemosyne/samples/sample-meeting.ogg"
CUT_SECONDS = 60


def fail(message: str) -> int:
    print(f"FAILED: {message}", flush=True)
    return 1


async def check(data_dir: Path, sample: Path) -> int:
    import httpx

    from mnemosyne.services import phonon_install
    from mnemosyne.transcription.transcribers import phonon

    started = time.monotonic()

    def report(message: str, fraction: float) -> None:
        print(f"[{time.monotonic() - started:6.0f}s {fraction:4.0%}] {message}", flush=True)

    try:
        await phonon_install.install(data_dir, report)
    except Exception as e:  # noqa: BLE001
        return fail(f"install: {e.__class__.__name__}: {e}")
    if not phonon_install.installed(data_dir):
        return fail("install returned, but the install is not marked complete")
    print(f"installed: {phonon_install.size_bytes(data_dir) / 1e6:.0f} MB", flush=True)

    transcriber = phonon.PhononTranscriber(
        log_path=data_dir / "logs" / "phonon.log", home=phonon_install.home(data_dir)
    )
    try:
        try:
            segments = await transcriber.transcribe(str(sample), language="en")
        except Exception as e:  # noqa: BLE001
            return fail(f"transcribe: {e.__class__.__name__}: {e}")
        if not segments:
            return fail("no segments came back")
        words = [w for s in segments for w in (s.words or [])]
        if not words:
            return fail(f"{len(segments)} segments, none with word times")
        if any(w.end < w.start for w in words) or any(s.end < s.start for s in segments):
            return fail("a segment or word ends before it starts")
        if words[-1].start < 5:
            return fail(f"word times look wrong (the last word starts at {words[-1].start:.1f} s)")
        text = " ".join(s.text for s in segments)
        print(f"{len(segments)} segments, {len(words)} words: {text[:200]}", flush=True)

        # The server must refuse a request without its API key.
        server = transcriber._server
        if server is None or not server.running():
            return fail("the server is not running after the transcription")
        async with httpx.AsyncClient(timeout=30) as client:
            url = f"{server.url}/v1/audio/transcriptions"
            for label, headers in (
                ("without a key", {}),
                ("with a wrong key", {"Authorization": "Bearer wrong"}),
            ):
                r = await client.post(
                    url, headers=headers, files={"file": ("a.wav", b"RIFF")}, data={"model": "x"}
                )
                if r.status_code != 401:
                    return fail(f"a request {label} got {r.status_code}, not 401")
                print(f"request {label}: 401", flush=True)
    finally:
        await transcriber.unload()
    print("phonon OK")
    return 0


def main() -> int:
    if not SAMPLE.is_file():
        return fail(f"{SAMPLE} is missing")
    with tempfile.TemporaryDirectory(prefix="check-phonon-") as tmp:
        cut = Path(tmp) / "sample.wav"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(SAMPLE), "-t", str(CUT_SECONDS)]
            + ["-ac", "1", "-ar", "16000", str(cut)],
            check=True,
        )
        data_dir = Path(tmp) / "data"
        return asyncio.run(check(data_dir, cut))


if __name__ == "__main__":
    sys.exit(main())
