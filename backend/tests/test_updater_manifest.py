"""scripts/updater-manifest.py on a fake dist directory (no network)."""

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent.parent / "scripts" / "updater-manifest.py"
BASE = "https://github.com/corpetty/mnemosyne/releases/download/v1.2.3"


def _bundle(dist: Path, name: str, sig: str | None = None) -> None:
    (dist / name).write_bytes(b"x")
    if sig is not None:
        (dist / f"{name}.sig").write_text(sig + "\n")


def _linux(dist: Path) -> None:
    _bundle(dist, "Mnemosyne_1.2.3_amd64.AppImage", "sig-appimage")
    _bundle(dist, "Mnemosyne_1.2.3_amd64-offline.AppImage", "sig-offline")
    _bundle(dist, "Mnemosyne_1.2.3_amd64.deb", "sig-deb")
    _bundle(dist, "Mnemosyne-1.2.3-1.x86_64.rpm", "sig-rpm")


def _run(dist: Path, notes: Path | None = None):
    args = [sys.executable, str(SCRIPT), "v1.2.3", str(dist)]
    if notes:
        args.append(str(notes))
    return subprocess.run(args, capture_output=True, text=True)


def test_linux_and_mac_platforms(tmp_path):
    _linux(tmp_path)
    _bundle(tmp_path, "Mnemosyne_1.2.3_aarch64.app.tar.gz", "sig-mac")
    _bundle(tmp_path, "Mnemosyne_1.2.3_aarch64.dmg")  # a download, not an update
    notes = tmp_path / "notes.md"
    notes.write_text("Hello\n")
    r = _run(tmp_path, notes)
    assert r.returncode == 0, r.stderr
    m = json.loads((tmp_path / "latest.json").read_text())
    assert m["version"] == "1.2.3"
    assert m["notes"] == "Hello\n"
    p = m["platforms"]
    assert list(p) == [
        "linux-x86_64-appimage",
        "linux-x86_64-deb",
        "linux-x86_64-rpm",
        "linux-x86_64",
        "darwin-aarch64",
    ]
    assert p["linux-x86_64-appimage"] == {
        "signature": "sig-appimage",
        "url": f"{BASE}/Mnemosyne_1.2.3_amd64.AppImage",
    }
    assert p["linux-x86_64"] == p["linux-x86_64-appimage"]
    assert p["linux-x86_64-deb"]["signature"] == "sig-deb"
    assert p["linux-x86_64-rpm"]["signature"] == "sig-rpm"
    assert p["darwin-aarch64"] == {
        "signature": "sig-mac",
        "url": f"{BASE}/Mnemosyne_1.2.3_aarch64.app.tar.gz",
    }


def test_linux_only_directory_still_works(tmp_path):
    _linux(tmp_path)
    assert _run(tmp_path).returncode == 0
    m = json.loads((tmp_path / "latest.json").read_text())
    assert "darwin-aarch64" not in m["platforms"]
    assert m["notes"] == ""


def test_mac_bundle_without_signature_fails(tmp_path):
    _linux(tmp_path)
    _bundle(tmp_path, "Mnemosyne_1.2.3_aarch64.app.tar.gz")
    r = _run(tmp_path)
    assert r.returncode == 1
    assert "missing signature" in r.stderr
    assert not (tmp_path / "latest.json").exists()


def test_no_appimage_fails(tmp_path):
    _bundle(tmp_path, "Mnemosyne_1.2.3_aarch64.app.tar.gz", "sig-mac")
    r = _run(tmp_path)
    assert r.returncode == 1
