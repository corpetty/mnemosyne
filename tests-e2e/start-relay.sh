#!/usr/bin/env bash
# Plain-HTTP iroh relay for the guest-notes browser tests (ws://127.0.0.1 needs no TLS trust).
# The binary comes from `cargo build --features dev-relay --manifest-path link/Cargo.toml`.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
exec "${MNEMOSYNE_LINK_BIN:-$root/link/target/debug/mnemosyne-link}" dev-relay --listen 127.0.0.1:8038
