#!/usr/bin/env bash
# Build notes-wasm (hashseq's browser bindings) into src/lib/notes/pkg/.
#
#   scripts/build-notes-wasm.sh
#
# Needs the wasm32-unknown-unknown target (rustup target add wasm32-unknown-unknown) and the
# wasm-bindgen CLI at exactly the version notes-wasm/Cargo.lock pins: the CLI and the crate
# must match or the generated JS does not fit the module. Use the prebuilt release binary
# (https://github.com/wasm-bindgen/wasm-bindgen/releases, x86_64-unknown-linux-musl), on PATH,
# in ~/.cache/mnemosyne-tools/bin, or named by WASM_BINDGEN (CI sets it). No wasm-pack, no wasm-opt: the
# output should depend only on the toolchain versions.
#
# Paths are remapped (CARGO_HOME -> /cargo, the repo -> /mnemosyne, rust-src -> /rustc/<commit>)
# so panic locations in the module do not carry this machine's home directory. Byte-identical
# output across machines also needs the same rustc (and cargo: its registry directory name).
set -euo pipefail

WANT_BINDGEN="0.2.129"
# The compiler the committed src/lib/notes/pkg/ was built with (CI pins the same one).
WANT_RUSTC="1.93.1"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CRATE="$ROOT/notes-wasm"
OUT="$ROOT/src/lib/notes/pkg"
TARGET=wasm32-unknown-unknown

die() { echo "build-notes-wasm: $*" >&2; exit 1; }

# --- tools ---
command -v cargo >/dev/null || die "cargo not found"

bindgen="${WASM_BINDGEN:-}"
if [[ -z "$bindgen" ]]; then
	if command -v wasm-bindgen >/dev/null; then
		bindgen="$(command -v wasm-bindgen)"
	elif [[ -x "$HOME/.cache/mnemosyne-tools/bin/wasm-bindgen" ]]; then
		bindgen="$HOME/.cache/mnemosyne-tools/bin/wasm-bindgen"
	else
		die "wasm-bindgen not found. Install the prebuilt $WANT_BINDGEN CLI from
  https://github.com/wasm-bindgen/wasm-bindgen/releases/tag/$WANT_BINDGEN
  (wasm-bindgen-$WANT_BINDGEN-x86_64-unknown-linux-musl.tar.gz) into ~/.cache/mnemosyne-tools/bin,
  or set WASM_BINDGEN=/path/to/wasm-bindgen."
	fi
fi
have_bindgen="$("$bindgen" --version 2>/dev/null | awk '{print $2}')"
[[ "$have_bindgen" == "$WANT_BINDGEN" ]] ||
	die "$bindgen is version '${have_bindgen:-unknown}', need exactly $WANT_BINDGEN"

lock_bindgen="$(awk '/^name = "wasm-bindgen"$/ { getline; gsub(/"/, "", $3); print $3; exit }' "$CRATE/Cargo.lock")"
[[ "$lock_bindgen" == "$WANT_BINDGEN" ]] ||
	die "notes-wasm/Cargo.lock has wasm-bindgen '${lock_bindgen:-missing}', the CLI is $WANT_BINDGEN; keep them equal"

# Run cargo/rustc from the crate so a notes-wasm/rust-toolchain.toml, if one is added, applies.
cd "$CRATE"
sysroot="$(rustc --print sysroot)"
[[ -d "$sysroot/lib/rustlib/$TARGET" ]] ||
	die "Rust target $TARGET is not installed for $(rustc --version) (rustup target add $TARGET)"

have_rustc="$(rustc --version | awk '{print $2}')"
echo "rustc:        $(rustc --version)"
if [[ "$have_rustc" != "$WANT_RUSTC" ]]; then
	echo "build-notes-wasm: WARNING: rustc $have_rustc, the committed module was built with $WANT_RUSTC;" >&2
	echo "  the output will probably differ byte for byte (CI will call the committed module stale)." >&2
fi
echo "wasm-bindgen: $have_bindgen ($bindgen)"

# --- build ---
cargo_home="${CARGO_HOME:-$HOME/.cargo}"
target_dir="${CARGO_TARGET_DIR:-$CRATE/target}"
# With the rust-src component installed, std's paths point into the sysroot: map them back to
# the /rustc/<commit> form a toolchain without rust-src uses.
rust_commit="$(rustc -vV | awk '/^commit-hash:/ {print $2}')"
export RUSTFLAGS="--remap-path-prefix=$cargo_home=/cargo --remap-path-prefix=$target_dir=/target --remap-path-prefix=$ROOT=/mnemosyne --remap-path-prefix=$sysroot/lib/rustlib/src/rust=/rustc/$rust_commit"
cargo build --locked --release --target "$TARGET"

wasm="$target_dir/$TARGET/release/notes_wasm.wasm"
rm -rf "$OUT"
mkdir -p "$OUT"
"$bindgen" --target web --out-dir "$OUT" "$wasm"

# --- report ---
if grep -qa "$HOME" "$OUT/notes_wasm_bg.wasm"; then
	die "the module still contains $HOME; path remapping failed"
fi
for f in "$OUT"/*; do
	printf '%-24s %8d bytes  %8d gzip  %s\n' "$(basename "$f")" "$(stat -c%s "$f")" \
		"$(gzip -9cn "$f" | wc -c)" "$(sha256sum "$f" | cut -c1-16)"
done
