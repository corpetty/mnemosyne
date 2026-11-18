"""Phonon-2 through `phonon serve` (transcription/transcribers/phonon.py), with a stand-in
`phonon` program: the real one is Fermion's package, not a dependency."""

import hashlib
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
    import json, os, sys
    from pathlib import Path
    from http.server import BaseHTTPRequestHandler, HTTPServer

    import argparse
    # As strict as the real `phonon serve`: unknown arguments are refused.
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["serve"])
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--api-key")
    p.add_argument("--threads", type=int)
    port = p.parse_args().port
    log = open(sys.argv[0] + ".log", "a")
    log.write("ARGS " + " ".join(sys.argv[1:]) + "\\n")
    device = os.environ.get("FERMION_DEVICE", "")
    log.write("DEVICE " + device + "\\n"); log.flush()
    # Fermion refusing an engine before any download, as gate.require() does.
    if device and device == os.environ.get("FAKE_REFUSE_EARLY"):
        sys.exit("`phonon serve` is unavailable: the speech runtime needs mlx")
    # Like the real one: the first start puts the model into the cache folder it is given.
    cache = os.environ.get("FERMION_CACHE_DIR")
    if cache:
        model = Path(cache) / "models--x" / "snapshots" / "abc" / "model.bin"
        if not model.exists():
            if os.environ.get("FAKE_DOWNLOAD_FAIL"):
                sys.exit("could not download the model")
            model.parent.mkdir(parents=True, exist_ok=True)
            model.write_bytes(os.environ.get("FAKE_MODEL", "the model").encode())
    # An engine that does not load, after the download.
    if device and device == os.environ.get("FAKE_REFUSE"):
        print("[fermion] decode backend: " + device, file=sys.stderr)
        sys.exit("RuntimeError: the engine did not load")
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


@pytest.fixture(autouse=True)
def own_config(tmp_path, monkeypatch):
    """Settings() reads the config file: never the real one."""
    monkeypatch.setenv("MNEMOSYNE_CONFIG_FILE", str(tmp_path / "config.toml"))


@pytest.fixture
def stub(tmp_path):
    path = tmp_path / "phonon"
    path.write_text(STUB.format(python=sys.executable))
    path.chmod(0o755)
    yield str(path)
    for server in phonon._servers.values():
        server.stop()
    phonon._servers.clear()
    phonon._cpu_fallback.clear()


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
    assert not phonon._servers[(stub, None, None)].running()  # the last user stopped it


@pytest.mark.anyio
async def test_final_and_live_share_one_server(stub, wav):
    final, live = phonon.PhononTranscriber(command=stub), phonon.PhononTranscriber(command=stub)
    await final.load()
    await live.load()
    server = phonon._servers[(stub, None, None)]
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
    server = phonon._servers[(stub, None, None)]
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


def _listening(port: int) -> bool:
    import socket

    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.mark.parametrize(
    "guarded",
    [
        pytest.param(False, marks=pytest.mark.linux_only("PR_SET_PDEATHSIG")),
        True,  # the Mac's way (phonon_guard.py), which also works here
    ],
)
def test_the_server_dies_with_the_backend(stub, tmp_path, guarded):
    """A backend that is killed must not leave a 1.3 GB server behind."""
    script = tmp_path / "parent.py"
    script.write_text(
        "import asyncio, sys\n"
        "from mnemosyne.transcription.transcribers import phonon\n"
        f"phonon.GUARDED = {guarded}\n"
        f"s = phonon.server_for({stub!r})\n"
        "asyncio.run(s.acquire())\n"
        "print(s._proc.pid, s.port, flush=True)\n"
        "import time; time.sleep(60)\n"
    )
    import subprocess

    parent = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True)
    child, port = map(int, parent.stdout.readline().split())
    assert _listening(port)
    parent.kill()
    parent.wait()
    deadline = time.monotonic() + 8  # the guard looks once a second
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            if not _listening(port):
                return
        time.sleep(0.1)
    if guarded:
        os.killpg(child, 9)
    else:
        os.kill(child, 9)
    pytest.fail("phonon serve outlived its backend")


@pytest.mark.anyio
async def test_the_guarded_server_stops_with_it(stub, wav, monkeypatch):
    """On the Mac stop() terminates the guard, which passes it on to the server."""
    monkeypatch.setattr(phonon, "GUARDED", True)
    t = phonon.PhononTranscriber(command=stub)
    assert [s.text for s in await t.transcribe(wav)][0] == "hello there"
    server = t._server
    assert server is not None and server._group and _listening(server.port)
    guard = server._proc
    await t.unload()
    assert guard.poll() is not None and not _listening(server.port)


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
    assert (stub, 2, None) in phonon._servers
    await live.unload()


FAKE_UV = textwrap.dedent(
    """\
    #!{python}
    # Stands in for uv: `venv` makes the folder, `pip install` logs and, for the requirements
    # file, puts the stand-in phonon in place. FAKE_UV_FAIL=1 fails like a broken network.
    import os, shutil, sys
    from pathlib import Path
    open(sys.argv[0] + ".log", "a").write(" ".join(sys.argv[1:]) + "\\n")
    if os.environ.get("FAKE_UV_FAIL"):
        print("error: Failed to fetch: https://download.pytorch.org/whl/cpu"); sys.exit(2)
    if sys.argv[1] == "venv":
        bin_ = Path(sys.argv[-1]) / "bin"; bin_.mkdir(parents=True, exist_ok=True)
        (bin_ / "python").symlink_to(sys.executable)
    elif sys.argv[1:3] == ["pip", "install"] and "-r" in sys.argv:
        venv = Path(sys.argv[sys.argv.index("--python") + 1]).parent.parent
        shutil.copy(os.environ["FAKE_PHONON"], venv / "bin" / "phonon")
        print("Installed 40 packages")
    """
)


@pytest.fixture
def fake_uv(tmp_path, stub, monkeypatch):
    path = tmp_path / "uv"
    path.write_text(FAKE_UV.format(python=sys.executable))
    path.chmod(0o755)
    monkeypatch.setenv("FAKE_PHONON", stub)
    monkeypatch.setenv("MNEMOSYNE_UV_BIN", str(path))
    # The stand-in's model, in place of the real file's SHA-256.
    monkeypatch.setattr(
        phonon,
        "MODEL_FILES",
        {"models--x/snapshots/*/model.bin": hashlib.sha256(b"the model").hexdigest()},
    )
    return str(path)


@pytest.mark.anyio
@pytest.mark.parametrize("mac", [False, True])
async def test_install_into_its_own_folder_then_offline(fake_uv, tmp_path, wav, monkeypatch, mac):
    from mnemosyne.services import phonon_install

    monkeypatch.setattr(phonon_install, "on_mac", lambda: mac)
    monkeypatch.setattr(phonon, "MAC", mac)
    data = tmp_path / "data"
    seen = []
    await phonon_install.install(data, lambda m, f: seen.append((m, f)))
    assert phonon_install.installed(data) and phonon_install.size_bytes(data) > 0
    calls = open(fake_uv + ".log").read().splitlines()
    assert calls[0].startswith("venv --python")
    if mac:
        # PyPI's torch is the CPU build there: one step, all from PyPI, every hash required.
        assert len(calls) == 2 and "--index-url" not in calls[1]
        assert calls[1].endswith("phonon-requirements-macos.txt")
        assert "--require-hashes" in calls[1] and "--no-deps" not in calls[1]
        assert not (data / "phonon" / "torch-requirements.txt").exists()
    else:
        # CPU torch first, alone, and everything with its hash required.
        assert "--index-url https://download.pytorch.org/whl/cpu" in calls[1]
        assert "--require-hashes" in calls[1] and "--no-deps" in calls[1]
        torch_file = data / "phonon" / "torch-requirements.txt"
        assert calls[1].endswith(str(torch_file))
        torch_pin = torch_file.read_text()
        assert torch_pin.startswith("torch==") and "--hash=sha256:" in torch_pin
        assert "-r" in calls[2] and calls[2].endswith("phonon-requirements.txt")
        assert "--require-hashes" in calls[2] and "--no-deps" not in calls[2]
    assert seen[-1] == ("Phonon-2 is installed", 1.0)

    # The transcriber finds our install without a path, and its server runs offline, keyed.
    home = data / "phonon"
    assert phonon.find_phonon("", home) == str(phonon.managed_program(home))
    monkeypatch.setenv("FERMION_NATIVE_BIN", "/tmp/evil")
    monkeypatch.setenv("PHONON2_CPU_LIBRARY", "/tmp/evil.so")
    env = phonon.server_env(home)
    assert env["HF_HUB_OFFLINE"] == "1" and env["FERMION_CACHE_DIR"] == str(home / "cache")
    assert "FERMION_NATIVE_BIN" not in env and "PHONON2_CPU_LIBRARY" not in env
    # The Mac runs Fermion's MLX engine, by name; Linux leaves the choice to Fermion (CPU).
    assert env.get("FERMION_DEVICE") == ("mlx" if mac else None)
    t = phonon.PhononTranscriber(home=home)
    assert [s.text for s in await t.transcribe(wav)][0] == "hello there"
    assert "--api-key" in open(str(phonon.managed_program(home)) + ".log").read()
    await t.unload()

    phonon_install.remove(data)
    assert not home.exists() and not phonon_install.installed(data)


def _devices(home):
    """FERMION_DEVICE of each start of our install's (stand-in) server, in order."""
    log = str(phonon.managed_program(home)) + ".log"
    return [line[7:] for line in open(log).read().splitlines() if line.startswith("DEVICE ")]


@pytest.fixture
def on_a_mac(monkeypatch):
    from mnemosyne.services import phonon_install

    monkeypatch.setattr(phonon_install, "on_mac", lambda: True)
    monkeypatch.setattr(phonon, "MAC", True)


def test_the_engine_by_platform(tmp_path, monkeypatch):
    """Our own install: MLX on the Mac (CPU once it fell back, or when forced), nothing on Linux
    (Fermion's CPU engine). Someone else's `phonon`: Fermion chooses, everywhere."""
    home = tmp_path / "phonon"
    monkeypatch.setenv("FERMION_DEVICE", "cuda")  # never passed on
    monkeypatch.setattr(phonon, "MAC", False)
    assert "FERMION_DEVICE" not in phonon.server_env(home)
    assert "FERMION_DEVICE" not in phonon.server_env(None)
    monkeypatch.setattr(phonon, "MAC", True)
    assert phonon.server_env(home)["FERMION_DEVICE"] == "mlx"
    assert phonon.server_env(home, "cpu")["FERMION_DEVICE"] == "cpu"
    assert "FERMION_DEVICE" not in phonon.server_env(None)
    phonon._cpu_fallback.add(home)
    try:
        assert phonon.server_env(home)["FERMION_DEVICE"] == "cpu"
    finally:
        phonon._cpu_fallback.clear()


@pytest.mark.anyio
async def test_mlx_that_does_not_start_falls_back_to_the_cpu(
    fake_uv, on_a_mac, tmp_path, wav, monkeypatch
):
    from mnemosyne.services import phonon_install

    monkeypatch.setenv("FAKE_REFUSE", "mlx")  # downloads the model, then the engine fails
    data = tmp_path / "data"
    home = data / "phonon"
    log = data / "logs" / "phonon.log"
    await phonon_install.install(data, lambda m, f: None)  # the first start, inside the install
    assert phonon_install.installed(data)
    assert _devices(home) == ["mlx", "cpu"]  # once more, with the CPU engine
    assert home in phonon._cpu_fallback
    assert "[mnemosyne] Phonon-2 did not start with Fermion's MLX engine" in log.read_text()

    # From then on its servers start with the CPU engine straight away.
    t = phonon.PhononTranscriber(home=home, log_path=log)
    assert [s.text for s in await t.transcribe(wav)][0] == "hello there"
    assert t._server.engine == "cpu"
    await t.unload()
    assert _devices(home) == ["mlx", "cpu", "cpu"]

    # Installing again tries MLX again.
    monkeypatch.delenv("FAKE_REFUSE")
    await phonon_install.install(data, lambda m, f: None)
    assert home not in phonon._cpu_fallback
    assert _devices(home)[-1] == "mlx"


@pytest.mark.anyio
async def test_mlx_refused_before_the_download_falls_back_too(
    fake_uv, on_a_mac, tmp_path, monkeypatch
):
    from mnemosyne.services import phonon_install

    monkeypatch.setenv("FAKE_REFUSE_EARLY", "mlx")
    data = tmp_path / "data"
    await phonon_install.install(data, lambda m, f: None)
    assert phonon_install.installed(data)
    assert _devices(data / "phonon") == ["mlx", "cpu"]


@pytest.mark.anyio
async def test_a_failed_download_is_not_retried_with_the_cpu(
    fake_uv, on_a_mac, tmp_path, monkeypatch
):
    from mnemosyne.services import phonon_install

    monkeypatch.setenv("FAKE_DOWNLOAD_FAIL", "1")
    data = tmp_path / "data"
    with pytest.raises(RuntimeError, match="stopped while starting"):
        await phonon_install.install(data, lambda m, f: None)
    assert _devices(data / "phonon") == ["mlx"]  # the CPU engine would fetch the same file
    assert not phonon._cpu_fallback and not phonon_install.installed(data)


@pytest.mark.anyio
async def test_a_forced_engine_has_no_fallback_and_its_own_server(
    fake_uv, on_a_mac, tmp_path, wav, monkeypatch
):
    from mnemosyne.services import phonon_install

    data = tmp_path / "data"
    home = data / "phonon"
    log = data / "logs" / "phonon.log"
    await phonon_install.install(data, lambda m, f: None)
    default = phonon.PhononTranscriber(home=home, log_path=log)
    cpu = phonon.PhononTranscriber(home=home, log_path=log, device="cpu")
    await default.load()
    await cpu.load()
    assert default._server is not cpu._server
    assert (default._server.engine, cpu._server.engine) == ("mlx", "cpu")
    await default.unload()
    await cpu.unload()

    monkeypatch.setenv("FAKE_REFUSE", "mlx")
    forced = phonon.PhononTranscriber(home=home, log_path=log, device="mlx")
    with pytest.raises(RuntimeError, match="stopped while starting"):
        await forced.load()
    assert _devices(home)[-1] == "mlx" and home not in phonon._cpu_fallback


@pytest.mark.anyio
async def test_a_failed_install_is_not_used(fake_uv, tmp_path, monkeypatch):
    from mnemosyne.services import phonon_install

    monkeypatch.setenv("FAKE_UV_FAIL", "1")
    with pytest.raises(RuntimeError, match="Failed to fetch"):
        await phonon_install.install(tmp_path / "data", lambda m, f: None)
    assert not phonon_install.installed(tmp_path / "data")


def test_auto_picks_phonon_for_english_once_installed(tmp_path, monkeypatch):
    home = tmp_path / "phonon"
    english = Settings(data_dir=tmp_path, language="en")
    monkeypatch.setattr(registry, "installed", lambda *m: set(m) <= {"onnx_asr"})
    assert registry.resolve_transcriber(english) == "parakeet"  # not installed yet
    phonon.managed_program(home).parent.mkdir(parents=True)
    phonon.managed_program(home).write_text("#!/bin/sh\n")
    phonon.managed_program(home).chmod(0o755)
    (home / "installed").write_text("x\n")
    assert registry.resolve_transcriber(english) == "phonon"
    assert registry.resolve_transcriber(Settings(data_dir=tmp_path)) == "parakeet"  # language?
    assert registry.resolve_transcriber(Settings(data_dir=tmp_path, language="de")) == "parakeet"


def test_install_route(client, ctx, fake_uv):
    from tests.conftest import drain_until_job

    info = client.get("/api/system").json()
    assert info["phonon"] is False and info["phonon_installable"] is True
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        job = client.post("/api/system/phonon").json()
        drain_until_job(ws, job["id"])
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "completed"
    assert client.get("/api/system").json()["phonon"] is True
    assert client.delete("/api/system/phonon").status_code == 200
    assert client.get("/api/system").json()["phonon"] is False


def _entries(file) -> dict[str, tuple[str, list[str]]]:
    """name -> (version, hash lines) of a requirements file, checking its shape."""
    entries: dict[str, tuple[str, list[str]]] = {}
    name = ""
    for line in file.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        if line[:1].isspace():
            entries[name][1].append(line.strip().removesuffix("\\").strip())
        else:
            assert "==" in line and ";" not in line, line  # no markers: one platform per file
            name, version = line.removesuffix("\\").strip().split("==")
            entries[name] = (version, [])
    return entries


@pytest.mark.parametrize("mac", [False, True])
def test_every_pin_has_hashes(mac):
    """The real files: each entry is name==version with at least one sha256 and nothing else."""
    from mnemosyne.services import phonon_install

    file = phonon_install.MAC_REQUIREMENTS if mac else phonon_install.LINUX_REQUIREMENTS
    entries = _entries(file)
    assert {"torch", "fermion-research", "transformers", "numpy"} <= set(entries)
    for name, (_, hashes) in entries.items():
        assert hashes and all(h.startswith("--hash=sha256:") for h in hashes), name
        assert all(len(h) == len("--hash=sha256:") + 64 for h in hashes), name
    torch = phonon_install._pin("torch", file)
    # Linux: the CPU build from PyTorch's index. Mac: PyPI's, which is the CPU build there.
    assert torch.startswith("torch==") and ("+cpu" in torch.splitlines()[0]) != mac
    assert len(torch.splitlines()) == len(entries["torch"][1]) + 1
    assert "annotated-doc" not in torch  # just torch's entry
    assert not any(n.startswith(("nvidia-", "triton")) for n in entries)


# Fermion's `mlx` extra on the Mac (its MLX engine) and what those pull in, reviewed 2026-11-17.
# A re-pin that drags in anything else fails below until it is reviewed and named here.
MLX_EXTRA = {
    "mlx",
    "mlx-metal",
    "mlx-audio",
    "mlx-lm",
    "miniaudio",
    "protobuf",
    "sentencepiece",
    "sounddevice",
}


def test_mac_and_linux_pin_the_same_versions():
    """Every Linux package is on the Mac at the same version (torch without +cpu); the Mac adds
    only the MLX extra's packages."""
    from mnemosyne.services import phonon_install

    linux = {
        n: v.removesuffix("+cpu")
        for n, (v, _) in _entries(phonon_install.LINUX_REQUIREMENTS).items()
    }
    mac = {n: v for n, (v, _) in _entries(phonon_install.MAC_REQUIREMENTS).items()}
    assert {n: mac.get(n) for n in linux} == linux
    assert set(mac) - set(linux) == MLX_EXTRA


def test_the_platform_picks_its_pins(monkeypatch):
    from mnemosyne.services import phonon_install

    monkeypatch.setattr(phonon_install, "on_mac", lambda: True)
    mac_hash = phonon_install.requirements_hash()
    assert phonon_install.requirements() == phonon_install.MAC_REQUIREMENTS
    monkeypatch.setattr(phonon_install, "on_mac", lambda: False)
    assert phonon_install.requirements() == phonon_install.LINUX_REQUIREMENTS
    assert phonon_install.requirements_hash() != mac_hash  # an install is for its own platform


def _model_file(data):
    return data / "phonon" / "cache" / "models--x" / "snapshots" / "abc" / "model.bin"


@pytest.mark.anyio
async def test_a_wrong_model_fails_the_install_and_empties_the_cache(
    fake_uv, tmp_path, monkeypatch
):
    from mnemosyne.services import phonon_install

    data = tmp_path / "data"
    monkeypatch.setenv("FAKE_MODEL", "something else")
    with pytest.raises(phonon.ModelMismatch, match="does not match its pinned SHA-256"):
        await phonon_install.install(data, lambda m, f: None)
    assert not phonon_install.installed(data)
    assert not (data / "phonon" / "cache").exists()  # a retry downloads it again
    assert "pinned SHA-256" in (data / "logs" / "phonon.log").read_text()


@pytest.mark.anyio
async def test_a_model_replaced_after_the_install_is_not_started(fake_uv, tmp_path, wav):
    from mnemosyne.services import phonon_install

    data = tmp_path / "data"
    home = data / "phonon"
    await phonon_install.install(data, lambda m, f: None)
    program = str(phonon.managed_program(home))
    log = data / "logs" / "phonon.log"

    # The pinned file: it starts (and the transcription works).
    ok = phonon.PhononTranscriber(home=home, log_path=log)
    assert [s.text for s in await ok.transcribe(wav)][0] == "hello there"
    await ok.unload()
    started = open(program + ".log").read().count("ARGS")

    # Another file in its place: refused before the server starts, in the log and the error,
    # and from then on `phonon` is not on offer (Parakeet takes over, see the registry).
    _model_file(data).write_bytes(b"tampered")
    bad = phonon.PhononTranscriber(home=home, log_path=log)
    with pytest.raises(phonon.ModelMismatch, match="does not match its pinned SHA-256"):
        await bad.transcribe(wav)
    assert open(program + ".log").read().count("ARGS") == started  # not started again
    assert "does not match its pinned SHA-256" in log.read_text()
    assert phonon.find_phonon("", home) is None
    with pytest.raises(phonon.ModelMismatch):  # the same clear error, not "not installed"
        await phonon.PhononTranscriber(home=home, log_path=log).load()

    # Installing again brings the right file back and lifts the refusal.
    await phonon_install.install(data, lambda m, f: None)
    assert phonon.find_phonon("", home) == program
    again = phonon.PhononTranscriber(home=home, log_path=log)
    assert [s.text for s in await again.transcribe(wav)][0] == "hello there"
    await again.unload()


def test_a_missing_model_file_is_refused(fake_uv, tmp_path):
    home = tmp_path / "ph-home"
    with pytest.raises(phonon.ModelMismatch, match="missing"):
        phonon.verify_model(home)
    assert home in phonon._refused
    phonon._refused.clear()


def test_a_file_is_hashed_once_while_it_is_unchanged(fake_uv, tmp_path, monkeypatch):
    home = tmp_path / "ph-home"
    model = home / "cache" / "models--x" / "snapshots" / "abc" / "model.bin"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"the model")
    calls = []
    real = hashlib.file_digest
    monkeypatch.setattr(hashlib, "file_digest", lambda f, d: calls.append(f.name) or real(f, d))
    phonon.verify_model(home)
    phonon.verify_model(home)
    assert len(calls) == 1
    model.write_bytes(b"the model, changed")  # size and mtime change: hashed again, and refused
    with pytest.raises(phonon.ModelMismatch):
        phonon.verify_model(home)
    assert len(calls) == 2
    phonon._refused.clear()


def test_live_auto_follows_phonon_for_english_and_never_picks_whisperx(tmp_path, monkeypatch):
    # Both packages present: the final "auto" may choose WhisperX, live (CPU) never does.
    monkeypatch.setattr(registry, "installed", lambda *m: True)
    monkeypatch.setattr(registry, "cuda_works", lambda: True)
    english = Settings(data_dir=tmp_path, language="en")
    assert english.live_transcriber == "auto"  # the default
    assert registry.resolve_live_transcriber(english) == "parakeet"  # not installed
    home = tmp_path / "phonon"
    phonon.managed_program(home).parent.mkdir(parents=True)
    phonon.managed_program(home).write_text("#!/bin/sh\n")
    phonon.managed_program(home).chmod(0o755)
    (home / "installed").write_text("x\n")
    assert registry.resolve_live_transcriber(english) == "phonon"
    assert registry.build_live_transcriber(english).name == "phonon"
    assert (
        registry.resolve_live_transcriber(Settings(data_dir=tmp_path, language="en-GB")) == "phonon"
    )
    assert (
        registry.resolve_live_transcriber(Settings(data_dir=tmp_path, language="de")) == "parakeet"
    )
    assert registry.resolve_live_transcriber(Settings(data_dir=tmp_path)) == "parakeet"
    # A saved explicit choice is kept.
    kept = Settings(data_dir=tmp_path, language="en", live_transcriber="parakeet")
    assert registry.resolve_live_transcriber(kept) == "parakeet"


def test_system_reports_the_live_engine(client, ctx, monkeypatch):
    monkeypatch.setattr(registry, "installed", lambda *m: set(m) <= {"onnx_asr"})
    info = client.get("/api/system").json()
    assert info["live_transcriber_in_use"] == "parakeet"
    monkeypatch.setattr(registry, "phonon_available", lambda s: True)
    ctx.settings.language = "en"
    assert client.get("/api/system").json()["live_transcriber_in_use"] == "phonon"
