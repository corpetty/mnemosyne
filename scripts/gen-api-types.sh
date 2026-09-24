#!/bin/bash
# Regenerate src/lib/api/schema.d.ts from the backend's OpenAPI schema.
# CI runs this with --check and fails if the committed file is stale.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# create_app() opens a database; keep that out of the real data dir.
(cd "$ROOT/backend" && MNEMOSYNE_DATA_DIR="$TMP/data" MNEMOSYNE_CONFIG_FILE="$TMP/config.toml" \
  uv run --frozen python -c "import json; from mnemosyne.api.app import create_app; print(json.dumps(create_app().openapi(), indent=1, sort_keys=True))") \
  > "$TMP/openapi.json"

OUT="$ROOT/src/lib/api/schema.d.ts"
(cd "$ROOT" && pnpm exec openapi-typescript "$TMP/openapi.json" -o "$TMP/schema.d.ts" >/dev/null)

if [ "${1:-}" = "--check" ]; then
  if ! diff -q "$TMP/schema.d.ts" "$OUT" >/dev/null 2>&1; then
    echo "src/lib/api/schema.d.ts is stale: run scripts/gen-api-types.sh" >&2
    exit 1
  fi
  echo "API types up to date"
else
  cp "$TMP/schema.d.ts" "$OUT"
  echo "Wrote $OUT"
fi
