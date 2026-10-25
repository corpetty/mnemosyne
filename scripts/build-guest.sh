#!/usr/bin/env bash
# Build the guest notes page (guest/) into backend/mnemosyne/api/static/guest/:
#
#   scripts/build-guest.sh          (or: pnpm build:guest)
#
#   index.html         the page, served at /n/<room> by the backend and the publish host; its JS,
#                      CSS and the notes wasm are /n/assets/<name>-<hash>.* (absolute paths, so
#                      they resolve under /n/<room>; both servers serve /n/assets/*).
#   assets/            those files.
#
# The output is committed and must be reproducible: CI rebuilds it and fails when it differs.
# Vite, Svelte and Tailwind are pinned by pnpm-lock.yaml; the build reads only guest/ and
# src/lib/notes, so it does not depend on where the checkout lives.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/backend/mnemosyne/api/static/guest"
cd "$ROOT"

pnpm exec vite build --config guest/vite.config.ts --logLevel warn

for f in "$OUT"/index.html "$OUT"/assets/*; do
	printf '%-36s %8d bytes  %8d gzip  %s\n' "${f#"$OUT"/}" "$(stat -c%s "$f")" \
		"$(gzip -9cn "$f" | wc -c)" "$(sha256sum "$f" | cut -c1-16)"
done
