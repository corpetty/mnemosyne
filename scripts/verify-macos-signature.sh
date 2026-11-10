#!/usr/bin/env bash
# Usage: verify-macos-signature.sh <Mnemosyne.app> "<identity>"
# Checks the whole bundle and every Mach-O in Contents/MacOS were signed by the identity.
set -euo pipefail
app="$1"; identity="$2"; bad=0
codesign --verify --deep --strict --verbose=2 "$app"
for f in "$app" "$app"/Contents/MacOS/*; do
  [ -f "$f" ] || [ "$f" = "$app" ] || continue
  out="$(codesign -dv --verbose=4 "$f" 2>&1)"
  if echo "$out" | grep -q "^Authority=$identity\$"; then
    echo "ok   $(basename "$f"): Authority=$identity"
  else
    echo "FAIL $(basename "$f"):"; echo "$out" | sed 's/^/    /'; bad=1
  fi
done
exit $bad
