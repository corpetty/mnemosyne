"""Phonon-2 through `phonon serve` (transcription/transcribers/phonon.py), with a stand-in
`phonon` program: the real one is Fermion's package, not a dependency."""

import os
import sys
import textwrap
import time

import pytest

from mnemosyne.config import Settings
from mnemosyne.transcription import registry
from mnemosyne.transcription.transcribers import phonon

STUB = textwrap.dedent(
    """\
    #!{python}
    # Answers like `phonon serve`: /health, and /v1/audio/transcriptions in verbose_json.
    import json, sys
    from http.server import BaseHTTPRequestHandler, HTTPServer

    port = int(sys.argv[sys.argv.index("--port") + 1])
    log = open(sys.argv[0] + ".log", "a")
    log.write("ARGS " + " ".join(sys.argv[1:]) + "\\n"); log.flush()
    BODY = {{
        "text": "hello there general kenobi",
        "segments": [{{"start": 0.0, "end": 1.0, "text": "hello there"}},
                     {{"start": 1.5, "end": 3.0, "text": "general kenobi"}}],
        "words": [{{"word": "hello", "start": 0.0, "end": 0.4}},
                  {{"word": "there", "start": 0.5, "end": 1.0}},
                  {{"word": "general", "start": 1.5, "end": 2.1}},
                  {{"word": "kenobi", "start": 2.2, "end": 3.0}}],
    }}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _send(self, body):
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_GET(self):
            self._send({{"status": "ok"}})
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            log.write("POST\\n"); log.flush()
            self._send(BODY)

    HTTPServer(("127.0.0.1", port), H).serve_forever()
    """
)


@pytest.fixture
def stub(tmp_path):
    path = tmp_path / "phonon"
    path.write_text(STUB.format(python=sys.executable))
    path.chmod(0o755)
    yield str(path)
    for server in phonon._servers.values():
        server.stop()
    phonon._servers.clear()


def _audio(path, parts):
    """A 16 kHz WAV of (seconds, loud) parts: a tone, or digital silence."""
    import numpy as np

    pcm = np.concatenate(
        [
            (0.3 * np.sin(np.arange(int(sec * 16000)) * 2 * np.pi * 440 / 16000))
            if loud
            else np.zeros(int(sec * 16000))
            for sec, loud in parts
        ]
    )
    phonon._write_wav(path, pcm.astype(np.float32))
    return str(path)


@pytest.fixture
def wav(tmp_path):
    return _audio(tmp_path / "a.wav", [(3, True)])


def _requests(stub):
    return [line for line in open(stub + ".log").read().splitlines() if line == "POST"]


@pytest.mark.anyio
async def test_phonon_serve_transcribes_with_word_times(stub, wav, tmp_path):
    t = phonon.PhononTranscriber(command=stub, log_path=tmp_path / "logs" / "phonon.log")
    segments = await t.transcribe(wav)
    assert [s.text for s in segments] == ["hello there", "general kenobi"]
    assert [w.word for w in segments[1].words] == ["general", "kenobi"]
    assert t.is_loaded()
    await t.unload()
    assert not phonon._servers[(stub, None)].running()  # the last user stopped it


@pytest.mark.anyio
async def test_final_and_live_share_one_server(stub, wav):
    final, live = phonon.PhononTranscriber(command=stub), phonon.PhononTranscriber(command=stub)
    await final.load()
    await live.load()
    server = phonon._servers[(stub, None)]
    pid = server._proc.pid
    await live.transcribe(wav)
    assert server._proc.pid == pid  # not started twice
    await live.unload()
    assert server.running()  # the final transcriber still uses it
    await final.unload()
    assert not server.running()


@pytest.mark.anyio
async def test_a_dead_server_is_started_again(stub, wav):
    t = phonon.PhononTranscriber(command=stub)
    await t.load()
    server = phonon._servers[(stub, None)]
    server._proc.kill()
    server._proc.wait()
    assert not t.is_loaded()
    assert [s.text for s in await t.transcribe(wav)][0] == "hello there"
    assert server._users == 1
    await t.unload()
    assert not server.running()


@pytest.mark.anyio
async def test_english_only_and_missing_program(stub, wav, tmp_path):
    t = phonon.PhononTranscriber(command=stub)
    with pytest.raises(ValueError, match="English only"):
        await t.transcribe(wav, language="de")
    assert not t.is_loaded()  # refused before starting anything
    missing = phonon.PhononTranscriber(command=str(tmp_path / "nope"))
    with pytest.raises(RuntimeError, match="pip install fermion-research"):
        await missing.load()


def test_registry_builds_it_and_falls_back_when_it_is_gone(stub, tmp_path, monkeypatch):
    s = Settings(data_dir=tmp_path, transcriber="phonon", phonon_command=stub)
    assert registry.resolve_transcriber(s) == "phonon"
    built = registry.build_transcriber(s)
    assert built.name == "phonon" and built.log_path == tmp_path / "logs" / "phonon.log"
    # Parakeet installed, nothing else (CI has no torch: auto must not look for a GPU there).
    monkeypatch.setattr(registry, "installed", lambda *m: set(m) <= {"onnx_asr"})
    gone = Settings(data_dir=tmp_path, transcriber="phonon", phonon_command=str(tmp_path / "x"))
    assert registry.resolve_transcriber(gone) == "parakeet"
    assert registry.resolve_transcriber(Settings(data_dir=tmp_path)) != "phonon"  # not auto


def test_the_server_dies_with_the_backend(stub, tmp_path):
    """A backend that is killed must not leave a 1.3 GB server behind (PR_SET_PDEATHSIG)."""
    script = tmp_path / "parent.py"
    script.write_text(
        "import asyncio, sys\n"
        "from mnemosyne.transcription.transcribers.phonon import server_for\n"
        f"s = server_for({stub!r})\n"
        "asyncio.run(s.acquire())\n"
        "print(s._proc.pid, flush=True)\n"
        "import time; time.sleep(60)\n"
    )
    import subprocess

    parent = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True)
    child = int(parent.stdout.readline())
    parent.kill()
    parent.wait()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    os.kill(child, 9)
    pytest.fail("phonon serve outlived its backend")


def test_cut_points_fall_in_quiet_moments():
    import numpy as np

    rate = 16000
    pcm = np.full(rate * 700, 0.3, dtype=np.float32)  # 11 min 40 s of sound
    pcm[rate * 290 : rate * 291] = 0.0  # a pause ten seconds before the first boundary
    cuts = phonon.cut_points(pcm)
    assert cuts[0] == 0 and cuts[-1] == pcm.size and len(cuts) == 4
    assert rate * 290 <= cuts[1] <= rate * 291
    assert all(b - a <= 300 * rate for a, b in zip(cuts, cuts[1:], strict=False))
    assert phonon.cut_points(pcm[: rate * 60]) == [0, rate * 60]


@pytest.mark.anyio
async def test_long_audio_goes_in_pieces_and_silence_is_not_sent(stub, tmp_path):
    # 6 min of speech, then 6 min of silence: two pieces with sound at most, none of the last.
    path = _audio(tmp_path / "long.wav", [(360, True), (360, False)])
    t = phonon.PhononTranscriber(command=stub)
    seen = []
    segments = await t.transcribe(path, progress=seen.append)
    assert len(_requests(stub)) == 2  # the silent piece was not sent
    assert seen[-1] == 1.0 and len(seen) == 3
    # The second piece's lines come back at its own place in the meeting.
    second = [s for s in segments if s.start > 250]
    assert second and second[0].start == pytest.approx(300.0, abs=15)
    assert second[0].words[0].start == second[0].start
    await t.unload()


@pytest.mark.anyio
async def test_live_gets_its_thread_budget(stub, wav):
    live = phonon.PhononTranscriber(command=stub, threads=2)
    await live.transcribe(wav)
    assert "--threads 2" in open(stub + ".log").read()
    assert (stub, 2) in phonon._servers
    await live.unload()
