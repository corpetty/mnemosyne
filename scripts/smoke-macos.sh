#!/usr/bin/env bash
# Smoke test for the built macOS app, run in CI (smoke-macos.yml).
#
# 1. The real Mnemosyne.app starts with a fake HOME, installs its backend on first launch (the
#    bundled uv), opens a meeting that has Ogg audio and plays it, muted, in WKWebView
#    (src/lib/app/smoke.ts, MNEMOSYNE_SMOKE in lib.rs). Ogg needs macOS 15.4+.
# 2. The app is started again (smoke mode ends the app and its backend with it) and the meeting
#    is transcribed on the CPU (Parakeet int8 + the onnx diarizer; about 700 MB of models):
#    segments and at least two speakers are expected.
#
# CI only: it writes to a fake HOME and kills whatever listens on port 8008.
# Usage: scripts/smoke-macos.sh path/to/Mnemosyne.app
set -euo pipefail

if [ -z "${CI:-}" ]; then
  echo "smoke-macos.sh starts the app and kills port 8008's listener; it only runs in CI" >&2
  exit 2
fi

app_bundle="$(cd "$1" && pwd)"
here="$(cd "$(dirname "$0")" && pwd)"
sample="$here/../backend/mnemosyne/samples/sample-meeting.ogg"
bin="$app_bundle/Contents/MacOS/mnemosyne"
[ -x "$bin" ] || { echo "no executable at $bin" >&2; exit 2; }
[ -f "$sample" ] || { echo "no sample at $sample" >&2; exit 2; }

# Real disk, never tmpfs-like places; the runner's temp dir is fine.
work="$(mktemp -d "${RUNNER_TEMP:-$HOME}/mnemosyne-smoke.XXXXXX")"
export HOME="$work/home"
mkdir -p "$HOME"
data="$HOME/Library/Application Support/com.corpetty.mnemosyne/data"
api=http://127.0.0.1:8008

dump_logs() {
  echo "---- app log (last 150 lines)"
  tail -n 150 "$work/app.log" 2>/dev/null || true
  echo "---- app log of the second start (last 100 lines)"
  tail -n 100 "$work/app2.log" 2>/dev/null || true
  echo "---- backend log (last 150 lines)"
  tail -n 150 "$data/logs/backend.log" 2>/dev/null || echo "(none at $data/logs/backend.log)"
}
fail() {
  echo "SMOKE FAIL: $1"
  dump_logs
  exit 1
}

app=""
stop_port() {
  local pids
  pids="$(lsof -ti tcp:8008 2>/dev/null || true)"
  [ -z "$pids" ] || kill $pids 2>/dev/null || true
}
cleanup() {
  [ -z "$app" ] || kill "$app" 2>/dev/null || true
  stop_port
}
trap cleanup EXIT

echo "== Launch 1: first start installs the backend, then plays a meeting's Ogg audio"
MNEMOSYNE_SMOKE=1 "$bin" >"$work/app.log" 2>&1 &
app=$!

echo "Waiting for the backend (first launch installs it)..."
for _ in $(seq 1500); do
  curl -sf $api/health >/dev/null && break
  kill -0 "$app" 2>/dev/null || fail "the app exited before its backend came up"
  sleep 1
done
curl -sf $api/health >/dev/null || fail "backend never became healthy"

status=$(curl -s -o "$work/import.json" -w '%{http_code}' -F "file=@$sample" \
  -F name="Smoke test" -F transcribe=false $api/api/audio/import)
[ "$status" = 200 ] || fail "import returned $status: $(cat "$work/import.json")"
sid="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["session"]["id"])' "$work/import.json")"

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
app=""
grep -o 'SMOKE .*' "$work/app.log" || true
[ "$code" -eq 0 ] || fail "the app exited with $code"
grep -q 'SMOKE OK' "$work/app.log" || fail "no SMOKE OK line"

echo "== Launch 2: transcribe the meeting on the CPU"
for _ in $(seq 30); do
  [ -z "$(lsof -ti tcp:8008 2>/dev/null || true)" ] && break
  sleep 1
done
stop_port
"$bin" >"$work/app2.log" 2>&1 &
app=$!
for _ in $(seq 300); do
  curl -sf $api/health >/dev/null && break
  kill -0 "$app" 2>/dev/null || fail "the app exited before its backend came up (second start)"
  sleep 1
done
curl -sf $api/health >/dev/null || fail "backend never became healthy (second start)"

curl -sf -X POST $api/api/sessions/"$sid"/transcribe -o "$work/job.json" \
  || fail "starting the transcription failed"
job="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$work/job.json")"
echo "Transcribing (job $job; the first run downloads the models)..."
state=""
for _ in $(seq 1500); do
  curl -sf $api/api/jobs/"$job" -o "$work/job.json" || fail "job $job not readable"
  state="$(python3 -c 'import json,sys; j=json.load(open(sys.argv[1])); print(j["status"], j.get("message",""), j.get("error") or "")' "$work/job.json")"
  case "$state" in completed*|failed*|cancelled*) break ;; esac
  sleep 2
done
echo "Job: $state"
case "$state" in completed*) ;; *) fail "the transcription did not complete: $state" ;; esac

curl -sf $api/api/sessions/"$sid" -o "$work/session.json" || fail "reading the meeting failed"
python3 - "$work/session.json" <<'PY' || fail "the transcript is not what was expected"
import json, sys
t = json.load(open(sys.argv[1]))["transcript"]
speakers = {s["speaker"] for s in t}
print(f"{len(t)} segments, speakers: {sorted(speakers)}")
if not t:
    sys.exit("no segments")
if len(speakers) < 2:
    sys.exit(f"expected at least 2 speakers, got {len(speakers)}")
PY

kill "$app" 2>/dev/null || true
wait "$app" 2>/dev/null || true
app=""
echo "Smoke test passed"
