#!/usr/bin/env bash
# Demo-mode backend for the browser tests: canned transcriber, diarizer and LLM,
# a fresh data dir and config on every start, port 8018 so a running app is untouched.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
data="$root/tests-e2e/.data${E2E_BACKEND_PORT:+-$E2E_BACKEND_PORT}"
rm -rf "$data"
mkdir -p "$data/vault"
cat > "$data/config.toml" <<TOML
transcriber = "demo"
diarizer = "demo"
default_provider = "demo"
live_transcription = false
auto_summarize = false
setup_complete = ${E2E_SETUP_COMPLETE:-true}
obsidian_vault_path = "$data/vault"
TOML
export MNEMOSYNE_DEMO=1 MNEMOSYNE_DATA_DIR="$data/db" MNEMOSYNE_CONFIG_FILE="$data/config.toml"
cd "$root/backend"
exec uv run --quiet mnemosyne-backend --host 127.0.0.1 --port "${E2E_BACKEND_PORT:-8018}"
