#!/usr/bin/env bash
# Retake the landing page's screenshots and write them to site/assets/ as WebP.
set -euo pipefail
cd "$(dirname "$0")/../.."
rm -rf site/shots/out
pnpm exec playwright test --config site/shots/playwright.config.ts
(cd backend && uv run --quiet python - <<'PY'
from pathlib import Path
from PIL import Image
out = Path("../site/assets"); out.mkdir(exist_ok=True)
for png in sorted(Path("../site/shots/out").glob("*.png")):
    Image.open(png).save(out / f"{png.stem}.webp", "WEBP", quality=82, method=6)
    print(png.stem, (out / f"{png.stem}.webp").stat().st_size // 1024, "KB")
PY
)
node site/shots/og.mjs
