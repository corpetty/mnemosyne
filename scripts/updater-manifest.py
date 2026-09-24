#!/usr/bin/env python3
"""Write latest.json for the Tauri updater from the signed bundles in a directory.

usage: updater-manifest.py <tag> <dist-dir> [notes-file]

Each bundle (AppImage, deb, rpm) needs its .sig next to it (tauri build with
createUpdaterArtifacts and TAURI_SIGNING_PRIVATE_KEY set). The updater picks the
entry for its own bundle type (linux-x86_64-deb, ...); plain linux-x86_64 is the
AppImage, used by builds that do not know their bundle type.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = "corpetty/mnemosyne"
KINDS = {"appimage": "*.AppImage", "deb": "*.deb", "rpm": "*.rpm"}


def main() -> int:
    tag, dist = sys.argv[1], Path(sys.argv[2])
    notes = Path(sys.argv[3]).read_text() if len(sys.argv) > 3 and Path(sys.argv[3]).is_file() else ""
    platforms = {}
    for kind, pattern in KINDS.items():
        files = sorted(dist.glob(pattern))
        if not files:
            continue
        bundle = files[0]
        sig = bundle.with_name(bundle.name + ".sig")
        if not sig.is_file():
            print(f"missing signature for {bundle.name}", file=sys.stderr)
            return 1
        platforms[f"linux-x86_64-{kind}"] = {
            "signature": sig.read_text().strip(),
            "url": f"https://github.com/{REPO}/releases/download/{tag}/{bundle.name}",
        }
    if "linux-x86_64-appimage" not in platforms:
        print("no AppImage found", file=sys.stderr)
        return 1
    platforms["linux-x86_64"] = platforms["linux-x86_64-appimage"]
    manifest = {
        "version": tag.removeprefix("v"),
        "notes": notes,
        "pub_date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "platforms": platforms,
    }
    (dist / "latest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"latest.json: {', '.join(platforms)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
