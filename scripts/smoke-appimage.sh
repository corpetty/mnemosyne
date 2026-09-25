#!/usr/bin/env bash
# Release smoke test for the AppImage, run in CI (Release workflow, `smoke` job).
#
# 1. The bundle carries a GStreamer that can play audio (scripts/check-appimage-gstreamer.py).
# 2. The real AppImage starts on a virtual display, installs its backend on first launch (uv),
#    opens a meeting and plays its audio, muted, into a PulseAudio null sink, without WebKit's
#    web process dying (src/lib/app/smoke.ts, MNEMOSYNE_SMOKE in lib.rs).
#
# CI only: it starts PulseAudio with a null sink and writes to the user's data dir.
# Usage: scripts/smoke-appimage.sh path/to/Mnemosyne.AppImage
set -euo pipefail

if [ -z "${CI:-}" ]; then
  echo "smoke-appimage.sh changes the audio setup and the app's data; it only runs in CI" >&2
  exit 2
fi

appimage="$(realpath "$1")"
here="$(cd "$(dirname "$0")" && pwd)"
work="$(mktemp -d)"
chmod +x "$appimage"

echo "== Bundled GStreamer"
(cd "$work" && "$appimage" --appimage-extract >/dev/null)
python3 "$here/check-appimage-gstreamer.py" "$work/squashfs-root"
rm -rf "$work/squashfs-root"

echo "== Sound server (null sink)"
pulseaudio --start --exit-idle-time=-1 2>/dev/null || true
pactl load-module module-null-sink sink_name=smoke >/dev/null
pactl set-default-sink smoke

echo "== Launch"
python3 - "$work/meeting.wav" <<'PY'
import math, struct, sys, wave
with wave.open(sys.argv[1], "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
    w.writeframes(b"".join(
        struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / 16000)))
        for i in range(16000 * 20)
    ))
PY
export MNEMOSYNE_SMOKE=1 WEBKIT_DISABLE_COMPOSITING_MODE=1 LIBGL_ALWAYS_SOFTWARE=1
xvfb-run -a "$appimage" >"$work/app.log" 2>&1 &
app=$!

cleanup() {
  kill "$app" 2>/dev/null || true
  fuser -k 8008/tcp 2>/dev/null || true
}
trap cleanup EXIT

fail() {
  echo "SMOKE FAIL: $1"
  echo "---- app log (last 150 lines)"
  tail -n 150 "$work/app.log"
  exit 1
}

echo "Waiting for the backend (first launch installs it)..."
for _ in $(seq 1200); do
  curl -sf http://127.0.0.1:8008/health >/dev/null && break
  kill -0 "$app" 2>/dev/null || fail "the app exited before its backend came up"
  sleep 1
done
curl -sf http://127.0.0.1:8008/health >/dev/null || fail "backend never became healthy"

status=$(curl -s -o "$work/import.json" -w '%{http_code}' -F "file=@$work/meeting.wav" \
  -F name="Smoke test" -F transcribe=false http://127.0.0.1:8008/api/audio/import)
[ "$status" = 200 ] || fail "import returned $status: $(cat "$work/import.json")"

echo "Waiting for the app to report..."
for _ in $(seq 180); do
  kill -0 "$app" 2>/dev/null || break
  sleep 1
done
kill -0 "$app" 2>/dev/null && fail "no result within 3 minutes"
set +e
wait "$app"
code=$?
set -e
grep '^SMOKE' "$work/app.log" || true
[ "$code" -eq 0 ] || fail "the app exited with $code"
grep -q '^SMOKE OK' "$work/app.log" || fail "no SMOKE OK line"
echo "Smoke test passed"
