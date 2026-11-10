#!/usr/bin/env bash
# Puts the macOS test-signing identity into a temporary keychain for CI.
# Env: APPLE_CERTIFICATE (base64 .p12) and APPLE_CERTIFICATE_PASSWORD, both optional; without them a
# throwaway self-signed certificate with the same name is made (its grants do not survive updates).
# Env: IDENTITY (the certificate's CN). Writes KEYCHAIN_PATH to $GITHUB_ENV and a note to the job summary.
set -euo pipefail
IDENTITY="${IDENTITY:-Mnemosyne Test Signing}"
dir="$(mktemp -d)"
p12="$dir/signing.p12"
pw="${APPLE_CERTIFICATE_PASSWORD:-}"
if [ -n "${APPLE_CERTIFICATE:-}" ] && [ -n "$pw" ]; then
  echo "$APPLE_CERTIFICATE" | base64 --decode > "$p12"
  note="Signed with the stable certificate from the repository secrets."
else
  pw="$(openssl rand -hex 16)"
  openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=$IDENTITY" \
    -keyout "$dir/key.pem" -out "$dir/cert.pem" \
    -addext "keyUsage=critical,digitalSignature" \
    -addext "extendedKeyUsage=critical,codeSigning"
  openssl pkcs12 -export -inkey "$dir/key.pem" -in "$dir/cert.pem" -out "$p12" -passout "pass:$pw" \
    -certpbe PBE-SHA1-3DES -keypbe PBE-SHA1-3DES -macalg sha1
  note="Signed with a throwaway certificate: permissions will not survive updates."
fi
kc="$RUNNER_TEMP/mnemosyne-signing.keychain-db"
kcpw="$(openssl rand -hex 16)"
security create-keychain -p "$kcpw" "$kc"
security set-keychain-settings -lut 7200 "$kc"
security unlock-keychain -p "$kcpw" "$kc"
security import "$p12" -k "$kc" -P "$pw" -T /usr/bin/codesign -T /usr/bin/security
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$kcpw" "$kc" >/dev/null
# Search list: ours first, then the existing ones.
security list-keychains -d user -s "$kc" $(security list-keychains -d user | tr -d '"')
echo "--- identities (valid only)"; security find-identity -v -p codesigning "$kc" || true
echo "--- identities (all)"; security find-identity -p codesigning "$kc" || true
echo "KEYCHAIN_PATH=$kc" >> "$GITHUB_ENV"
echo "::notice::$note"
echo "$note" >> "$GITHUB_STEP_SUMMARY"
