#!/usr/bin/env bash
# Brings deploy/ours up on this machine and checks it works, without Let's Encrypt or public DNS.
# Run by the CI job "Our relay and host (deploy/ours)"; see compose.test.yaml for what differs.
#
#   - a throwaway CA signs a certificate for each name; relay and Caddy are given those;
#   - /etc/hosts points relay.petty.services at 127.0.0.2 and notes.petty.services at 127.0.0.1;
#   - the production compose.yaml, iroh-relay.toml and Caddyfile are used, with only the TLS lines
#     changed (by sed, so a change to the real files is what gets tested).
#
# Needs: docker with compose >= 2.24 (the override uses !override), openssl, curl, sudo, and a
# built mnemosyne-link (MNEMOSYNE_LINK_BIN, default link/target/debug/mnemosyne-link) that reads
# MNEMOSYNE_RELAY_CA (it ignores the system store and SSL_CERT_FILE: iroh trusts only its
# built-in web roots plus what the code adds).
#
# It edits /etc/hosts and starts containers, so it refuses to run outside CI unless
# OURS_SMOKE_FORCE=1.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OURS="$(dirname "$HERE")"
REPO="$(cd "$OURS/../.." && pwd)"
WORK="$HERE/work"
LINK_BIN="${MNEMOSYNE_LINK_BIN:-$REPO/link/target/debug/mnemosyne-link}"

RELAY_NAME=relay.petty.services
NOTES_NAME=notes.petty.services
export RELAY_ADDR=127.0.0.2
export HOST_ADDR=127.0.0.1
export PUBLISH_DOMAIN=$NOTES_NAME
# An open host, as in production; do not let a stray environment close it.
export PUBLISH_UPLOAD_TOKEN=

if [ "${CI:-}" != "true" ] && [ "${OURS_SMOKE_FORCE:-}" != "1" ]; then
  echo "smoke.sh: refusing to edit /etc/hosts outside CI (set OURS_SMOKE_FORCE=1 to override)" >&2
  exit 2
fi
[ -x "$LINK_BIN" ] || { echo "smoke.sh: $LINK_BIN not built (cargo build --manifest-path link/Cargo.toml)" >&2; exit 2; }

SUDO=""
[ "$(id -u)" = 0 ] || SUDO="sudo -n"
MARK="# ours-smoke"

dc() {
  docker compose --project-name ours-smoke --project-directory "$OURS" \
    -f "$OURS/compose.yaml" -f "$HERE/compose.test.yaml" "$@"
}

step() { printf '\n== %s\n' "$*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

cleanup() {
  status=$?
  set +e
  if [ "$status" != 0 ]; then
    echo; echo "smoke.sh failed (exit $status); container state and logs follow" >&2
    dc ps -a 2>&1
    for s in relay publish caddy; do
      echo "---- logs: $s"; dc logs --no-color --tail 200 "$s" 2>&1
    done
  fi
  dc down -v --remove-orphans >/dev/null 2>&1
  $SUDO sed -i "\|$MARK\$|d" /etc/hosts 2>/dev/null
  exit "$status"
}
trap cleanup EXIT

# ---- certificates -------------------------------------------------------------------------------

step "test CA and certificates"
rm -rf "$WORK"
mkdir -p "$WORK/certs/relay" "$WORK/certs/notes" "$WORK/viewer" "$WORK/guest"
CA="$WORK/certs/ca.crt"
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 2 \
  -subj "/CN=Mnemosyne ours-smoke test CA" \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -keyout "$WORK/certs/ca.key" -out "$CA" 2>/dev/null

leaf() { # leaf <dir> <name>: <dir>/{cert.crt,cert.key}, signed by the CA
  local dir=$1 name=$2
  openssl req -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -subj "/CN=$name" \
    -keyout "$dir/$name.key" -out "$dir/$name.csr" 2>/dev/null
  printf 'subjectAltName=DNS:%s\nbasicConstraints=critical,CA:FALSE\nextendedKeyUsage=serverAuth\nkeyUsage=critical,digitalSignature\n' \
    "$name" > "$dir/$name.ext"
  openssl x509 -req -in "$dir/$name.csr" -CA "$CA" -CAkey "$WORK/certs/ca.key" -CAcreateserial \
    -days 2 -extfile "$dir/$name.ext" -out "$dir/$name.crt" 2>/dev/null
  # The containers run as other users; these are throwaway keys.
  chmod 644 "$dir/$name.key" "$dir/$name.crt"
}
leaf "$WORK/certs/relay" "$RELAY_NAME"
leaf "$WORK/certs/notes" "$NOTES_NAME"

# ---- configuration: production files with only the TLS lines changed ---------------------------

step "test configuration from the production files"
# Relay: Manual certificates from the test CA, no Let's Encrypt contact.
sed -e 's|^cert_mode = .*|cert_mode = "Manual"\nmanual_cert_path = "/certs/'"$RELAY_NAME"'.crt"\nmanual_key_path = "/certs/'"$RELAY_NAME"'.key"|' \
    -e '/^contact = /d' "$OURS/iroh-relay.toml" > "$WORK/iroh-relay.toml"
grep -q '^cert_mode = "Manual"' "$WORK/iroh-relay.toml" || fail "iroh-relay.toml: cert_mode line not found; update smoke.sh"
grep -q LetsEncrypt "$WORK/iroh-relay.toml" && fail "iroh-relay.toml still mentions LetsEncrypt"
# Caddy: the site block gets a certificate from the test CA (so no ACME).
sed -e 's|^{\$PUBLISH_DOMAIN} {$|&\n\ttls /certs/'"$NOTES_NAME"'.crt /certs/'"$NOTES_NAME"'.key|' \
    "$OURS/Caddyfile" > "$WORK/Caddyfile"
grep -q '^	tls /certs/' "$WORK/Caddyfile" || fail "Caddyfile: site block line not found; update smoke.sh"
# The viewer and guest page of this checkout (what update.sh fetches in production).
cp -r "$REPO/backend/mnemosyne/api/static/viewer/." "$WORK/viewer/"
cp -r "$REPO/backend/mnemosyne/api/static/guest/." "$WORK/guest/"
[ -f "$WORK/viewer/index.html" ] && [ -f "$WORK/guest/index.html" ] || fail "built viewer or guest page missing"

# ---- names ---------------------------------------------------------------------------------------

step "/etc/hosts"
# Free 80 and 443 on the loopback addresses (the runner image has apache2 and nginx installed).
$SUDO systemctl stop apache2 nginx 2>/dev/null || true
printf '%s %s %s\n%s %s %s\n' "$RELAY_ADDR" "$RELAY_NAME" "$MARK" "$HOST_ADDR" "$NOTES_NAME" "$MARK" \
  | $SUDO tee -a /etc/hosts >/dev/null
getent hosts "$RELAY_NAME" "$NOTES_NAME"

# ---- up --------------------------------------------------------------------------------------------

step "docker compose config"
dc config >/dev/null

step "docker compose up --build"
dc up -d --build

curl_ca() { curl -sS --max-time 20 --cacert "$CA" "$@"; }

wait_for() { # wait_for <what> <url>
  local what=$1 url=$2 i
  for i in $(seq 1 60); do
    if curl_ca -f -o /dev/null "$url" 2>/dev/null; then echo "$what answers"; return 0; fi
    sleep 2
  done
  curl_ca -v -o /dev/null "$url" || true
  fail "$what did not answer at $url within 120 s"
}
step "waiting for the services"
wait_for "relay" "https://$RELAY_NAME/healthz"
wait_for "host" "https://$NOTES_NAME/health"

# ---- checks ----------------------------------------------------------------------------------------

BODY="$WORK/body"
status_of() { curl_ca -o "$BODY" -w '%{http_code}' "$@"; }
expect() { # expect <code> <curl args...>
  local want=$1; shift
  local got; got=$(status_of "$@") || true
  [ "$got" = "$want" ] || { echo "--- body:"; head -c 600 "$BODY"; echo; fail "$* -> HTTP $got, wanted $want"; }
}

step "relay and host answer over the test certificates"
expect 200 "https://$RELAY_NAME/healthz"
expect 200 "https://$NOTES_NAME/health"
grep -qx 'ok' "$BODY" || fail "/health did not say ok"

step "privacy notice"
expect 200 "https://$NOTES_NAME/privacy"
grep -q 'petty.services' "$BODY" || fail "/privacy does not mention petty.services"

step "guest page and viewer"
# The room id must be 16 lowercase hex characters (valid_id); /n/x would be 404.
expect 200 "https://$NOTES_NAME/n/"
grep -qi '<html' "$BODY" || fail "/n/ is not an HTML page"
expect 200 "https://$NOTES_NAME/n/0123456789abcdef"
grep -qi '<html' "$BODY" || fail "/n/<room> is not an HTML page"
expect 200 "https://$NOTES_NAME/p/0123456789abcdef"
grep -qi '<html' "$BODY" || fail "/p/<id> is not an HTML page"
# Our relay is allowed in the guest page's Content-Security-Policy (PUBLISH_RELAY_ORIGINS).
curl_ca -sD - -o /dev/null "https://$NOTES_NAME/n/" | grep -i '^content-security-policy:' | grep -q "$RELAY_NAME" \
  || fail "the guest page's Content-Security-Policy does not allow $RELAY_NAME"

step "publication round trip through Caddy"
ID=$(openssl rand -hex 8)
TOKEN=$(openssl rand -hex 16)
printf 'blob %s\n' "$(openssl rand -hex 32)" > "$WORK/blob.bin"
printf '{"padded":"%s"}\n' "$(openssl rand -hex 32)" > "$WORK/manifest.bin"
SHA=$(openssl dgst -sha256 -r "$WORK/blob.bin" | cut -d' ' -f1)
EXPIRES=$(( $(date +%s) + 3600 ))
PUB="https://$NOTES_NAME/pub/$ID"
auth=(-H "Authorization: Bearer $TOKEN")

expect 401 -X PUT --data-binary @"$WORK/blob.bin" "$PUB/$SHA"
expect 201 -X PUT "${auth[@]}" --data-binary @"$WORK/blob.bin" "$PUB/$SHA"
expect 200 -X PUT "${auth[@]}" -H 'X-Pub-Version: 1' -H "X-Pub-Expires: $EXPIRES" -H "X-Pub-Blobs: $SHA" \
  --data-binary @"$WORK/manifest.bin" "$PUB"
expect 403 -X PUT -H "Authorization: Bearer someone-else" -H 'X-Pub-Version: 2' -H "X-Pub-Expires: $EXPIRES" \
  --data-binary @"$WORK/manifest.bin" "$PUB"
curl_ca -f -D "$WORK/headers" -o "$WORK/got-manifest" "$PUB"
cmp "$WORK/manifest.bin" "$WORK/got-manifest" || fail "manifest read back differs"
grep -iq '^x-pub-version: 1' "$WORK/headers" || fail "X-Pub-Version missing on GET"
curl_ca -f -o "$WORK/got-blob" "$PUB/$SHA"
cmp "$WORK/blob.bin" "$WORK/got-blob" || fail "blob read back differs"
expect 204 -X DELETE "${auth[@]}" "$PUB"
expect 410 "$PUB"
echo "publication $ID: put, get, forbid, delete, gone"

step "mnemosyne-link relay-check through relay.petty.services"
if ! grep -aq MNEMOSYNE_RELAY_CA "$LINK_BIN"; then
  fail "$LINK_BIN does not read MNEMOSYNE_RELAY_CA: relay-check would not trust the test CA (iroh ignores the system store). Add the env var to link/src/main.rs (RelayCheck)."
fi
MNEMOSYNE_RELAY_CA="$CA" "$LINK_BIN" relay-check --relay "https://$RELAY_NAME" --timeout-secs 30 | tee "$WORK/relay-check.json"
grep -q '"ok":true' "$WORK/relay-check.json" || grep -q '"ok": true' "$WORK/relay-check.json" || fail "relay-check did not report ok"

step "all checks passed"
