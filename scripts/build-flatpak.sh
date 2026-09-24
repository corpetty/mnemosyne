#!/usr/bin/env bash
# Build a Flatpak bundle (mnemosyne.flatpak) from a release .deb.
#   bash scripts/build-flatpak.sh src-tauri/target/release/bundle/deb/Mnemosyne_0.5.0_amd64.deb
# Uses flatpak-builder if installed, else the org.flatpak.Builder app from Flathub.
# The build dir goes under ~/.cache (real disk), never /tmp.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
deb="$(realpath "${1:?usage: build-flatpak.sh <path/to/.deb>}")"
work="${FLATPAK_WORK:-$HOME/.cache/mnemosyne-flatpak}"
out="${2:-$root/mnemosyne.flatpak}"

git -C "$root" submodule update --init flatpak/shared-modules
cp "$deb" "$root/flatpak/mnemosyne.deb"
trap 'rm -f "$root/flatpak/mnemosyne.deb"' EXIT

if command -v flatpak-builder >/dev/null; then
  builder=(flatpak-builder)
else
  builder=(flatpak run --user org.flatpak.Builder)
fi
mkdir -p "$work"
"${builder[@]}" --user --install-deps-from=flathub --force-clean --disable-rofiles-fuse \
  --state-dir="$work/state" --repo="$work/repo" "$work/build" \
  "$root/flatpak/com.corpetty.mnemosyne.yml"
flatpak build-bundle --runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo \
  "$work/repo" "$out" com.corpetty.mnemosyne
echo "Built $out"
echo "Install with: flatpak install --user $out"
