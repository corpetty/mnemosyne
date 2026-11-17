#!/usr/bin/env python3
"""Check that Phonon-2 really installs and transcribes, as the app does it.

Run from backend/ after `uv sync --locked`:
    uv run --no-sync python ../scripts/check-phonon.py

Does the real install (services/phonon_install.py: real uv, this platform's pins in
phonon-requirements.txt or phonon-requirements-macos.txt, ~1 GB, the model download) into a
temporary data dir whose path has a space in it (the Mac's is under `Application Support`, and
uv writes other launchers for such paths), transcribes the
first minute of the bundled sample meeting with PhononTranscriber and asserts that segments with
word times came back, and that the server refuses a request without its API key (401). On a Mac
the first transcription must have run Fermion's MLX engine (its "decode backend: mlx" line in
phonon.log, no fallback to the CPU), and the same cut is then transcribed again with the CPU engine
forced (`device="cpu"`), printing both timings.
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
MAC = sys.platform == "darwin"


def fail(message: str) -> int:
    print(f"FAILED: {message}", flush=True)
    return 1


def log_size(log: Path) -> int:
    return log.stat().st_size if log.exists() else 0


def ran_engine(transcriber, log: Path, mark: int, engine: str) -> str | None:
    """Why `transcriber`'s server did not run Fermion's `engine`, from what it wrote to phonon.log
    after `mark` (Fermion prints "decode backend: <engine>" as it loads one); None: it did."""
    from mnemosyne.transcription.transcribers import phonon

    server = transcriber._server
    if server is None or server.engine != engine:
        return f"the server ran with FERMION_DEVICE={getattr(server, 'engine', None)}, not {engine}"
    if transcriber.home in phonon._cpu_fallback:
        return "the install fell back to the CPU engine (see phonon.log)"
    with log.open("rb") as f:
        f.seek(mark)
        written = f.read().decode(errors="replace")
    if f"decode backend: {engine}" not in written:
        return (
            f'phonon.log has no "decode backend: {engine}" line from this start:\n{written[-2000:]}'
        )
    return None


async def warm(transcriber, sample: Path) -> float:
    """Seconds for a second transcription of `sample`, the server already up (the first one
    includes the server's start and, for MLX, compiling its Metal kernels)."""
    begun = time.monotonic()
    await transcriber.transcribe(str(sample), language="en")
    return time.monotonic() - begun


async def cpu_check(data_dir: Path, sample: Path, mlx_took: tuple[float, float]) -> int:
    """The Mac: the same cut with Fermion's CPU engine forced, to compare with the MLX run."""
    from mnemosyne.services import phonon_install
    from mnemosyne.transcription.transcribers import phonon

    log = data_dir / "logs" / "phonon.log"
    transcriber = phonon.PhononTranscriber(
        log_path=log, home=phonon_install.home(data_dir), device="cpu"
    )
    mark = log_size(log)
    try:
        try:
            begun = time.monotonic()
            segments = await transcriber.transcribe(str(sample), language="en")
            took = time.monotonic() - begun
        except Exception as e:  # noqa: BLE001
            return fail(f"transcribe with the CPU engine: {e.__class__.__name__}: {e}")
        if not [w for s in segments for w in (s.words or [])]:
            return fail("the CPU engine returned no words")
        if why := ran_engine(transcriber, log, mark, "cpu"):
            return fail(why)
        again = await warm(transcriber, sample)
    finally:
        await transcriber.unload()
    print(
        f"{CUT_SECONDS} s with the MLX engine: {mlx_took[0]:.1f} s first, {mlx_took[1]:.1f} s "
        f"warm; with the CPU engine: {took:.1f} s first, {again:.1f} s warm (first = server "
        "start included)",
        flush=True,
    )
    print(f"CPU engine: {' '.join(s.text for s in segments)[:200]}", flush=True)
    return 0


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

    log = data_dir / "logs" / "phonon.log"
    transcriber = phonon.PhononTranscriber(log_path=log, home=phonon_install.home(data_dir))
    mark = log_size(log)
    try:
        try:
            begun = time.monotonic()
            segments = await transcriber.transcribe(str(sample), language="en")
            took = time.monotonic() - begun
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
        print(f"transcribed {CUT_SECONDS} s in {took:.1f} s (server start included)", flush=True)
        print(f"{len(segments)} segments, {len(words)} words: {text[:200]}", flush=True)
        if MAC:  # Fermion's MLX engine is the Mac's default, the CPU engine only a fallback
            if why := ran_engine(transcriber, log, mark, "mlx"):
                return fail(why)
            print("engine: mlx", flush=True)
            mlx_warm = await warm(transcriber, sample)

        # The server must refuse a request without its API key.
        server = transcriber._server
        if server is None or not server.running():
            return fail("the server is not running after the transcription")
        if sys.platform == "darwin" and not server._group:
            return fail("on a Mac the server must run under its guard (phonon_guard.py)")
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
    if MAC and await cpu_check(data_dir, sample, (took, mlx_warm)):
        return 1
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
        data_dir = Path(tmp) / "Application Support" / "data"
        return asyncio.run(check(data_dir, cut))


if __name__ == "__main__":
    sys.exit(main())
