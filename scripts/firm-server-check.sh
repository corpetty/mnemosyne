#!/bin/bash
# Check a firm's Mnemosyne server before advisors use it (docs/firm-server.md).
#   scripts/firm-server-check.sh https://mnemosyne.example-firm.com
# Runs on the server itself. Prints one line per check; exits 1 if any failed.
set -uo pipefail
URL="${1:?usage: $0 https://<the address advisors open>}"
DATA="${MNEMOSYNE_DATA_DIR:-/srv/mnemosyne/data}"
MODEL="${DEFAULT_MODEL:-qwen3:14b}"
failed=0
ok() { printf '  ok    %s\n' "$1"; }
bad() { printf '  FAIL  %s\n' "$1"; failed=1; }
check() { local what="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$what"; else bad "$what"; fi; }

echo "Mnemosyne firm server check"
check "NVIDIA GPU visible (nvidia-smi)" nvidia-smi
check "mnemosyne service running" systemctl is-active --quiet mnemosyne
health=$(curl -fsS --max-time 5 http://127.0.0.1:8008/health 2>/dev/null)
if [ -n "$health" ]; then ok "backend answers on 127.0.0.1:8008"; else bad "backend answers on 127.0.0.1:8008"; fi
if grep -q '"firm_mode":true' <<<"$health"; then ok "firm mode on (no cloud models)"; else bad "firm mode on (FIRM_MODE=true in the unit)"; fi
# No -k: the certificate has to be one the advisors' browsers accept.
check "HTTPS with a valid certificate at $URL" curl -fsS --max-time 10 "$URL/health"
check "the web app loads at $URL" bash -c "curl -fsS --max-time 10 '$URL/' | grep -q 'mnemosyne-backend'"
tags=$(curl -fsS --max-time 5 http://127.0.0.1:11434/api/tags 2>/dev/null)
if [ -n "$tags" ]; then ok "Ollama answers"; else bad "Ollama answers (systemctl status ollama)"; fi
if grep -q "\"$MODEL\"" <<<"$tags"; then ok "model $MODEL is pulled"; else bad "model $MODEL is pulled (ollama pull $MODEL)"; fi
check "data folder $DATA is writable by mnemosyne" sudo -u mnemosyne test -w "$DATA"
free_gb=$(df -BG --output=avail "$DATA" 2>/dev/null | tail -1 | tr -dc 0-9)
if [ "${free_gb:-0}" -ge 100 ]; then ok "$free_gb GB free for recordings"; else bad "at least 100 GB free for recordings (${free_gb:-?} GB)"; fi
# Meeting records carry times: the clock must be right.
check "clock synchronised (NTP)" bash -c '[ "$(timedatectl show -p NTPSynchronized --value)" = yes ]'
check "encryption key credential installed" test -f /etc/credstore.encrypted/mnemosyne-key
echo
if [ "$failed" = 0 ]; then echo "All good."; else echo "Fix the FAIL lines above (docs/firm-server.md)."; fi
exit "$failed"
