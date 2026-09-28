#!/usr/bin/env bash
# Demo backend with the seeded meetings (seed.py), for the landing page's screenshots.
# Port 8048 and its own folder, so a running app and the e2e backend are left alone.
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
data="$root/site/shots/.data"
rm -rf "$data"
mkdir -p "$data"
cat > "$data/config.toml" <<TOML
transcriber = "demo"
diarizer = "demo"
default_provider = "demo"
live_transcription = false
auto_summarize = false
setup_complete = true
backup_dir = "$data/backups"
calendar_source = "off"
TOML
export MNEMOSYNE_DEMO=1 MNEMOSYNE_DATA_DIR="$data/db" MNEMOSYNE_CONFIG_FILE="$data/config.toml"
cd "$root/backend"
uv run --quiet python "$root/site/shots/seed.py"
exec uv run --quiet mnemosyne-backend --host 127.0.0.1 --port 8048
