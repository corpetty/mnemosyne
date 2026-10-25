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
backup_dir = "$data/backups"
guest_host = "http://127.0.0.1:8028"
# Phase 10: guest rooms over iroh through the plain-HTTP dev relay (start-relay.sh). The phase 9
# spec keeps the host transport; the iroh spec switches it per test through the settings API.
# guest_page_url: the page a non-mirrored iroh link opens (that host has no such room).
remote_relays = "http://127.0.0.1:8038"
guest_transport = "${E2E_GUEST_TRANSPORT:-host}"
guest_page_url = "http://127.0.0.1:8028/n/"
TOML
export MNEMOSYNE_DEMO=1 MNEMOSYNE_DATA_DIR="$data/db" MNEMOSYNE_CONFIG_FILE="$data/config.toml"
cd "$root/backend"
exec uv run --quiet mnemosyne-backend --host 127.0.0.1 --port "${E2E_BACKEND_PORT:-8018}"
