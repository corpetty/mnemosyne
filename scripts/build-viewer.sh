#!/usr/bin/env bash
# Build the publication viewer (viewer/) into backend/mnemosyne/api/static/viewer/:
#
#   scripts/build-viewer.sh          (or: pnpm build:viewer)
#
#   index.html         the hosted page, served at /p/<id> by the backend and the publish host;
#                      its JS and CSS are /p/assets/index-<hash>.{js,css} (absolute paths, so
#                      they resolve under /p/<id>; both servers serve /p/assets/*).
#   assets/            those two files.
#   single.html        the single-file template: the same JS and CSS inlined, a CSP <meta> with
#                      their sha256 hashes, and <script type="application/json" id="payload">
#                      __PAYLOAD__</script> for the file export to fill (viewer/single.ts).
#
# The output is committed and must be reproducible: CI rebuilds it and fails when it differs.
# Vite, Svelte and Tailwind are pinned by pnpm-lock.yaml; the build reads only viewer/ and the
# shared Markdown component, so it does not depend on where the checkout lives.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/backend/mnemosyne/api/static/viewer"
cd "$ROOT"

pnpm exec vite build --config viewer/vite.config.ts --logLevel warn

for f in "$OUT"/index.html "$OUT"/single.html "$OUT"/assets/*; do
	printf '%-28s %8d bytes  %8d gzip  %s\n' "${f#"$OUT"/}" "$(stat -c%s "$f")" \
		"$(gzip -9cn "$f" | wc -c)" "$(sha256sum "$f" | cut -c1-16)"
done
