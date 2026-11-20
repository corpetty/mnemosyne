#!/bin/sh
# Regenerate the hashed requirement files of the KittenTTS 2 narration engine
# (mnemosyne/synthesis/kitten-requirements*.txt, installed by services/tts_install.py).
# Resolves only; installs nothing. Bump KITTENML / AUDIOSEAL here, then run from backend/.
set -eu
KITTENML=0.9.8
AUDIOSEAL=0.2.1
DIR=mnemosyne/synthesis
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

header() {
    printf '# KittenTTS 2 for narration voices (services/tts_install.py), %s.\n' "$1"
    printf '# Installed into <data_dir>/tts/kitten/venv with uv --require-hashes, never into the backend lock.\n'
    printf '# Regenerate with backend/scripts/lock-kitten.sh; keep kittenml and audioseal versions in step\n'
    printf '# across both files.\n'
}

printf 'kittenml==%s\nsoundfile\naudioseal==%s\n' "$KITTENML" "$AUDIOSEAL" > "$TMP/linux.in"
uv pip compile "$TMP/linux.in" --python-version 3.13 --python-platform x86_64-manylinux_2_28 \
    --index-url https://pypi.org/simple --extra-index-url https://download.pytorch.org/whl/cpu \
    --index-strategy unsafe-best-match --generate-hashes --no-header -q -o "$TMP/linux.txt"
{ header "Linux, x86_64: torch from PyTorch's CPU index (the rest from PyPI)"; cat "$TMP/linux.txt"; } \
    > "$DIR/kitten-requirements.txt"

printf 'kittenml[mlx]==%s\nsoundfile\naudioseal==%s\n' "$KITTENML" "$AUDIOSEAL" > "$TMP/macos.in"
uv pip compile "$TMP/macos.in" --python-version 3.13 --python-platform aarch64-apple-darwin \
    --generate-hashes --no-header -q -o "$TMP/macos.txt"
{ header "macOS, Apple silicon: everything from PyPI, with kittenml's mlx extra"; cat "$TMP/macos.txt"; } \
    > "$DIR/kitten-requirements-macos.txt"
echo "Wrote $DIR/kitten-requirements.txt and $DIR/kitten-requirements-macos.txt"
