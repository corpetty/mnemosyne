#!/usr/bin/env bash
# Publish host for the guest-notes browser tests: a fresh data dir, open uploads, the committed
# viewer and guest pages, port 8028 (the backend's `guest_host`, see start-backend.sh).
# The binary comes from `cargo build --manifest-path publish-host/Cargo.toml` (CI builds it).
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
data="$root/tests-e2e/.publish"
rm -rf "$data"
mkdir -p "$data"
bin="${MNEMOSYNE_PUBLISH_HOST_BIN:-$root/publish-host/target/debug/mnemosyne-publish-host}"
export PUBLISH_LISTEN=127.0.0.1:8028
export PUBLISH_DATA_DIR="$data"
export PUBLISH_VIEWER_DIR="$root/backend/mnemosyne/api/static/viewer"
export PUBLISH_GUEST_DIR="$root/backend/mnemosyne/api/static/guest"
unset PUBLISH_UPLOAD_TOKEN
export PUBLISH_WRITE_BURST=100000 PUBLISH_WRITE_PER_MINUTE=100000
export PUBLISH_READ_BURST=100000 PUBLISH_READ_PER_MINUTE=100000
exec "$bin"
