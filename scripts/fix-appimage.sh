#!/usr/bin/env bash
# Fix what Tauri's AppImage bundler gets wrong, in place, and sign the result again.
#
# tauri-bundler (2.10) makes .DirIcon an absolute symlink into its build directory
# (…/target/release/bundle/appimage/Mnemosyne.AppDir/Mnemosyne.png), which does not exist on
# anyone's machine: file managers and AppImage tools show no icon. Every absolute symlink in the
# AppDir that points into it is made relative.
#
# Repacking changes the file, so the updater's .sig must be made again: with
# TAURI_SIGNING_PRIVATE_KEY (+ _PASSWORD) set, like `tauri build` in the Release workflow.
# Without a key the stale .sig is removed rather than left wrong.
#
# Usage: scripts/fix-appimage.sh dist/Mnemosyne_<v>_amd64.AppImage
# Needs appimagetool (APPIMAGETOOL, or downloaded).
set -euo pipefail

appimage="$(realpath "$1")"
root="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d -p "${WORK_DIR:-$HOME/.cache}" fix-appimage.XXXX)" # real disk, not tmpfs
trap 'rm -rf "$work"' EXIT

(cd "$work" && "$appimage" --appimage-extract >/dev/null)
app_dir="$work/squashfs-root"

fixed=0
while IFS= read -r -d '' link; do
  target="$(readlink "$link")"
  case "$target" in /*) ;; *) continue ;; esac
  # The build's AppDir path, whatever it was: keep what follows "<name>.AppDir/".
  inside="${target#*.AppDir/}"
  if [ "$inside" = "$target" ] || [ ! -e "$app_dir/$inside" ]; then
    echo "!! $link -> $target: not inside the AppDir, left alone" >&2
    continue
  fi
  ln -sfn "$(realpath --relative-to="$(dirname "$link")" "$app_dir/$inside")" "$link"
  echo "fixed ${link#"$app_dir"/} -> $(readlink "$link")"
  fixed=$((fixed + 1))
done < <(find "$app_dir" -type l -print0)
test -e "$app_dir/.DirIcon" || { echo "!! .DirIcon still does not resolve" >&2; exit 1; }
if [ "$fixed" -eq 0 ]; then
  echo "nothing to fix"
  exit 0
fi

tool="${APPIMAGETOOL:-$work/appimagetool}"
if [ ! -x "$tool" ]; then
  curl -fsSL -o "$tool" \
    https://github.com/AppImage/appimagetool/releases/download/1.9.0/appimagetool-x86_64.AppImage
  chmod +x "$tool"
fi
ARCH=x86_64 "$tool" --appimage-extract-and-run --no-appstream "$app_dir" "$work/fixed.AppImage" \
  >/dev/null
mv "$work/fixed.AppImage" "$appimage"
chmod +x "$appimage"

rm -f "$appimage.sig"
if [ -n "${TAURI_SIGNING_PRIVATE_KEY:-}" ]; then
  (cd "$root" && pnpm --silent tauri signer sign "$appimage" >/dev/null)
  test -s "$appimage.sig"
  echo "signed $(basename "$appimage").sig"
else
  echo "no TAURI_SIGNING_PRIVATE_KEY: not signed (the updater will not offer this file)"
fi
ls -la "$appimage"
