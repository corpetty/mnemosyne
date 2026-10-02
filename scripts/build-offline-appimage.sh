#!/usr/bin/env bash
# Build the offline flavour of the AppImage: the normal one plus what its first launch and first
# meeting would otherwise download: the base install (packages and Python) and the CPU engines'
# models (Parakeet, the speaker models, the search model; about 0.9 GB). The GPU extra is still
# downloaded on machines with an NVIDIA driver, and the built-in summary model when chosen.
#
# The shell (lib.rs) sees usr/lib/Mnemosyne/offline/ and installs from it with `uv sync
# --offline`, copying the cache and Python out of the read-only AppImage first, and the models
# into the Hugging Face cache and <data>/models.
#
# Usage: scripts/build-offline-appimage.sh dist/Mnemosyne_<v>_amd64.AppImage [out-dir]
# Needs the pinned uv sidecar (scripts/fetch-uv.sh) and appimagetool (downloaded if missing).
set -euo pipefail

appimage="$(realpath "$1")"
out_dir="$(realpath "${2:-$(dirname "$appimage")}")"
root="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d -p "${WORK_DIR:-$HOME/.cache}" offline-appimage.XXXX)" # real disk, not tmpfs
trap 'rm -rf "$work"' EXIT

uv="$(ls "$root"/src-tauri/binaries/mnemosyne-uv-* | head -1)" # same uv as the app: same cache format
echo "== Packages and Python with $("$uv" --version)"
UV_CACHE_DIR="$work/offline/uv-cache" \
UV_PYTHON_INSTALL_DIR="$work/offline/python" \
UV_PYTHON_DOWNLOADS=automatic UV_PYTHON_PREFERENCE=only-managed \
UV_PROJECT_ENVIRONMENT="$work/venv" UV_NO_PROGRESS=1 \
  "$uv" sync --frozen --no-dev --extra onnx --project "$root/backend"
echo "== Models"
"$work/venv/bin/mnemosyne-backend" prefetch "$work/offline/models"
rm -rf "$work/venv" "$work/offline/models/unused-config.toml"
du -sh "$work/offline/uv-cache" "$work/offline/python" "$work/offline/models"

echo "== Repack"
(cd "$work" && "$appimage" --appimage-extract >/dev/null)
app_dir="$work/squashfs-root"
lib="$(dirname "$(find "$app_dir/usr/lib" -maxdepth 2 -name backend -type d | head -1)")"
cp -a "$work/offline" "$lib/offline"

tool="${APPIMAGETOOL:-$work/appimagetool}"
if [ ! -x "$tool" ]; then
  curl -fsSL -o "$tool" \
    https://github.com/AppImage/appimagetool/releases/download/1.9.0/appimagetool-x86_64.AppImage
  chmod +x "$tool"
fi
name="$(basename "$appimage" .AppImage)-offline.AppImage"
ARCH=x86_64 "$tool" --appimage-extract-and-run --no-appstream "$app_dir" "$out_dir/$name" >/dev/null
ls -la "$out_dir/$name"
