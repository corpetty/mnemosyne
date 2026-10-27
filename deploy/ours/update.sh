#!/usr/bin/env bash
# Fetch the guest page and viewer of the latest Mnemosyne release into ./guest and ./viewer.
# Run by ours-update.timer every hour; safe to run by hand. No restart is needed: the folders
# are mounted read-only into the publish container and replaced atomically.
#
# Needs: bash, curl, GNU tar, rsync, and write access to this folder.
set -euo pipefail

REPO="${MNEMOSYNE_REPO:-corpetty/mnemosyne}"
cd "$(dirname "$(readlink -f "$0")")"
STATE=".update-tag"
GUEST_PATH="backend/mnemosyne/api/static/guest"
VIEWER_PATH="backend/mnemosyne/api/static/viewer"

tag=$(curl -fsS --max-time 30 -H 'Accept: application/vnd.github+json' \
        "https://api.github.com/repos/${REPO}/releases/latest" \
      | sed -n 's/^[[:space:]]*"tag_name":[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1)
case "$tag" in
  v[0-9]*) ;;
  *) echo "update.sh: no usable release tag from GitHub (got '${tag}')" >&2; exit 1 ;;
esac

if [ -f "$STATE" ] && [ "$(cat "$STATE")" = "$tag" ] && [ -f guest/index.html ] && [ -f viewer/index.html ]; then
  echo "update.sh: already at $tag"
  exit 0
fi

tmp=$(mktemp -d "${PWD}/.update.XXXXXX")
trap 'rm -rf "$tmp"' EXIT

curl -fsSL --max-time 300 -o "$tmp/src.tar.gz" "https://github.com/${REPO}/archive/refs/tags/${tag}.tar.gz"
# The archive has one top-level directory (mnemosyne-<tag without v>); take the two folders only.
tar -xzf "$tmp/src.tar.gz" -C "$tmp" --strip-components=1 \
    --wildcards "*/${GUEST_PATH}" "*/${VIEWER_PATH}"

for src in "$tmp/$GUEST_PATH" "$tmp/$VIEWER_PATH"; do
  [ -f "$src/index.html" ] || { echo "update.sh: $src/index.html missing in $tag" >&2; exit 1; }
done

# Replace the contents in place, not the directories: a bind-mounted directory that is renamed
# away keeps showing the old inode to the running container. --delay-updates puts all new files
# in place at the end of the transfer (each by an atomic rename) and --delete-delay removes old
# ones after that, so a reader sees old files or new ones, never half a file.
replace() {
  local src="$1" dest="$2"
  mkdir -p "$dest"
  rsync -a --delay-updates --delete-delay "$src"/ "$dest"/
}
replace "$tmp/$GUEST_PATH" guest
replace "$tmp/$VIEWER_PATH" viewer

echo "$tag" > "$STATE.tmp" && mv "$STATE.tmp" "$STATE"
echo "update.sh: now at $tag"
