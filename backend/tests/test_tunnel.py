"""Tunnels (services/tunnel.py) against fake `cloudflared` and `tailscale` executables."""

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from mnemosyne.services import downloads, team_host, tunnel
from mnemosyne.services.tunnel import Funnel, QuickTunnel, TunnelError

# conftest stubs the module's open_tunnel: these tests call the real one
from mnemosyne.services.tunnel import open_tunnel as real_open_tunnel


def _script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


BANNER = """\
echo "2026-10-25T10:00:00Z INF Requesting new quick Tunnel on trycloudflare.com..." >&2
echo "2026-10-25T10:00:01Z INF |  https://quiet-river-1234.trycloudflare.com  |" >&2
exec sleep 60
"""


@pytest.fixture
def bin_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "bin"
    d.mkdir()
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    return d


def _fake_tailscale(
    bin_dir: Path, monkeypatch, state="Running", funnel_exit=0, funnel_err=""
) -> Path:
    status = json.dumps(
        {
            "BackendState": state,
            "CertDomains": ["box.tail1.ts.net"],
            "Self": {"DNSName": "box.tail1.ts.net."},
        }
    )
    # conftest stubs team_host.tailscale_name; this fake's status JSON is what the real one reads
    monkeypatch.setattr(team_host, "tailscale_name", lambda: "box.tail1.ts.net")
    log = bin_dir / "tailscale.log"
    return _script(
        bin_dir / "tailscale",
        f"""echo "$@" >> {log}
if [ "$1" = status ]; then echo '{status}'; exit 0; fi
if [ "$1" = funnel ] && [ "$2" != --https=443 ]; then
  echo "{funnel_err}" >&2; exit {funnel_exit}
fi
exit 0
""",
    )


@pytest.mark.anyio
async def test_quick_tunnel_url_from_stderr_and_stop(tmp_path):
    exe = _script(tmp_path / "cloudflared", BANNER)
    t = QuickTunnel(exe, 8008)
    assert await t.start() == "https://quiet-river-1234.trycloudflare.com"
    assert t.url.endswith(".trycloudflare.com") and t.alive()
    proc = t._proc
    await t.stop()
    assert not t.alive() and proc.returncode is not None


@pytest.mark.anyio
async def test_quick_tunnel_passes_flags(tmp_path):
    args = tmp_path / "args"
    exe = _script(tmp_path / "cloudflared", f'echo "$@" > {args}\n' + BANNER)
    t = QuickTunnel(exe, 4321)
    await t.start()
    await t.stop()
    line = args.read_text()
    assert "tunnel --url http://127.0.0.1:4321" in line and "--no-autoupdate" in line


@pytest.mark.anyio
async def test_quick_tunnel_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(tunnel, "START_TIMEOUT", 0.5)
    exe = _script(tmp_path / "cloudflared", 'echo "starting" >&2\nexec sleep 60\n')
    t = QuickTunnel(exe, 8008)
    with pytest.raises(TunnelError, match="did not come up"):
        await t.start()
    assert not t.alive()


@pytest.mark.anyio
async def test_quick_tunnel_exits_early(tmp_path):
    exe = _script(tmp_path / "cloudflared", 'echo "failed to dial edge" >&2\nexit 3\n')
    with pytest.raises(TunnelError, match="code 3\): failed to dial edge"):
        await QuickTunnel(exe, 8008).start()


@pytest.mark.anyio
async def test_quick_tunnel_missing_binary(tmp_path):
    with pytest.raises(TunnelError, match="Could not start cloudflared"):
        await QuickTunnel(tmp_path / "nope", 8008).start()


@pytest.mark.anyio
async def test_funnel_start_and_stop(bin_dir, monkeypatch):
    _fake_tailscale(bin_dir, monkeypatch)
    assert Funnel.possible()
    f = Funnel(8008)
    assert await f.start() == "https://box.tail1.ts.net"
    await f.stop()
    calls = (bin_dir / "tailscale.log").read_text().splitlines()
    assert "funnel --bg --https=443 http://127.0.0.1:8008" in calls
    assert "funnel --https=443 off" in calls


@pytest.mark.anyio
async def test_funnel_error_surfaced(bin_dir, monkeypatch):
    _fake_tailscale(
        bin_dir, monkeypatch, funnel_exit=1, funnel_err="Funnel not enabled for this tailnet"
    )
    with pytest.raises(TunnelError, match="Funnel not enabled for this tailnet"):
        await Funnel(8008).start()


def test_funnel_not_possible(bin_dir, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert not Funnel.possible()


@pytest.mark.anyio
async def test_open_tunnel_prefers_funnel(bin_dir, tmp_path, monkeypatch):
    _fake_tailscale(bin_dir, monkeypatch)
    t = await real_open_tunnel(SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path)), 8008)
    assert t.kind == "funnel" and t.url == "https://box.tail1.ts.net"
    await t.stop()


@pytest.mark.anyio
async def test_open_tunnel_falls_back_to_cloudflare(bin_dir, tmp_path, monkeypatch):
    _fake_tailscale(bin_dir, monkeypatch, state="Stopped")
    app = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(tunnel, "cloudflared_download", lambda: tunnel.CLOUDFLARED["amd64"])

    def fake_fetch(folder, item, progress=None):
        folder.mkdir(parents=True, exist_ok=True)
        return _script(folder / item.name, BANNER)

    monkeypatch.setattr(downloads, "fetch", fake_fetch)
    t = await real_open_tunnel(app, 8008)
    try:
        assert t.kind == "cloudflare" and t.url == "https://quiet-river-1234.trycloudflare.com"
    finally:
        await t.stop()


def test_ensure_cloudflared_reuses_verified_file(tmp_path, monkeypatch):
    item = tunnel.CLOUDFLARED["arm64"]
    monkeypatch.setattr(tunnel, "cloudflared_download", lambda: item)
    path = tmp_path / "tools" / item.name
    path.parent.mkdir()
    path.write_bytes(b"x")
    # downloads.fetch itself returns an existing file; emulate it, as the real one would
    monkeypatch.setattr(downloads, "fetch", lambda folder, it, progress=None: folder / it.name)
    app = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path))
    got = tunnel.ensure_cloudflared(app)
    assert got == path and got.stat().st_mode & 0o777 == 0o755


def test_unsupported_platform(tmp_path, monkeypatch):
    monkeypatch.setattr("platform.machine", lambda: "riscv64")
    app = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path))
    with pytest.raises(TunnelError, match="not available on this computer"):
        tunnel.ensure_cloudflared(app)


def test_pins_are_complete():
    assert set(tunnel.CLOUDFLARED) == {"amd64", "arm64"}
    for item in tunnel.CLOUDFLARED.values():
        assert len(item.sha256) == 64 and tunnel.CLOUDFLARED_VERSION in item.url
