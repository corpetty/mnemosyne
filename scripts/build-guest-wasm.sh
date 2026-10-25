#!/usr/bin/env bash
# Build guest-wasm (the guest page's iroh client) into guest/pkg/.
#
#   scripts/build-guest-wasm.sh
#
# CI is the builder of record: the `guest-wasm` job runs this on every push and uploads guest/pkg/
# as the artifact `guest-wasm-pkg`. Corey's machine has no clang and no wasm32 target, so the
# loop for a change to guest-wasm/ is: push, wait for the `guest-wasm` job, then
#
#   gh run download <run id> -n guest-wasm-pkg -D guest/pkg
#
# and commit guest/pkg/ (CI then finds it current). Locally only `cargo check` / `cargo test` /
# `cargo clippy` for the native target work (the crate compiles natively; only the browser runs it).
#
# Needs, as scripts/build-notes-wasm.sh: the wasm32-unknown-unknown target and the wasm-bindgen
# CLI at exactly the version guest-wasm/Cargo.lock pins (on PATH, in ~/.cache/mnemosyne-tools/bin,
# or named by WASM_BINDGEN). No wasm-pack, no wasm-opt. In addition: a C compiler and archiver
# for wasm32, because ring (iroh's TLS crypto) compiles C: CC_wasm32_unknown_unknown (default
# clang-18) and AR_wasm32_unknown_unknown (default llvm-ar-18). The clang version is part of the
# output, as rustc's is: CI pins clang 18.
#
# Paths are remapped as for notes-wasm (CARGO_HOME -> /cargo, the sources -> /mnemosyne, rust-src
# -> /rustc/<commit>, and the same prefixes for ring's C), and the crate is built from a fixed
# directory (SRC below), so a checkout anywhere gives the same bytes. RUSTFLAGS replaces
# guest-wasm/.cargo/config.toml's rustflags, so getrandom's browser backend
# (`--cfg getrandom_backend="wasm_js"`) is repeated here.
set -euo pipefail

WANT_BINDGEN="0.2.129"
# The compiler the committed guest/pkg/ was built with (CI pins the same one), as for notes.
WANT_RUSTC="1.93.1"
WANT_CLANG_MAJOR="18"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CRATE="$ROOT/guest-wasm"
OUT="$ROOT/guest/pkg"
TARGET=wasm32-unknown-unknown

die() { echo "build-guest-wasm: $*" >&2; exit 1; }

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
	die "guest-wasm/Cargo.lock has wasm-bindgen '${lock_bindgen:-missing}', the CLI is $WANT_BINDGEN; keep them equal"

# ring's C for wasm32 (the cc crate reads these per-target variables).
export CC_wasm32_unknown_unknown="${CC_wasm32_unknown_unknown:-clang-$WANT_CLANG_MAJOR}"
export AR_wasm32_unknown_unknown="${AR_wasm32_unknown_unknown:-llvm-ar-$WANT_CLANG_MAJOR}"
command -v "$CC_wasm32_unknown_unknown" >/dev/null ||
	die "C compiler '$CC_wasm32_unknown_unknown' not found (ring compiles C for wasm32): install
  clang $WANT_CLANG_MAJOR or set CC_wasm32_unknown_unknown. CI builds this module; see the header."
command -v "$AR_wasm32_unknown_unknown" >/dev/null ||
	die "archiver '$AR_wasm32_unknown_unknown' not found: install llvm $WANT_CLANG_MAJOR or set AR_wasm32_unknown_unknown"
have_clang="$("$CC_wasm32_unknown_unknown" --version | head -n 1)"
have_clang_major="$(sed -nE 's/.*clang version ([0-9]+)\..*/\1/p' <<<"$have_clang")"

# Run cargo/rustc from the crate so a guest-wasm/rust-toolchain.toml, if one is added, applies.
cd "$CRATE"
sysroot="$(rustc --print sysroot)"
[[ -d "$sysroot/lib/rustlib/$TARGET" ]] ||
	die "Rust target $TARGET is not installed for $(rustc --version) (rustup target add $TARGET)"

have_rustc="$(rustc --version | awk '{print $2}')"
echo "rustc:        $(rustc --version)"
if [[ "$have_rustc" != "$WANT_RUSTC" ]]; then
	echo "build-guest-wasm: WARNING: rustc $have_rustc, the committed module was built with $WANT_RUSTC;" >&2
	echo "  the output will probably differ byte for byte (CI will call the committed module stale)." >&2
fi
echo "clang:        $have_clang ($CC_wasm32_unknown_unknown)"
if [[ "$have_clang_major" != "$WANT_CLANG_MAJOR" ]]; then
	echo "build-guest-wasm: WARNING: clang ${have_clang_major:-unknown}, the committed module was built with clang $WANT_CLANG_MAJOR;" >&2
	echo "  ring's C will probably differ byte for byte." >&2
fi
echo "wasm-bindgen: $have_bindgen ($bindgen)"

# --- build ---
cargo_home="${CARGO_HOME:-$HOME/.cargo}"
target_dir="${CARGO_TARGET_DIR:-$CRATE/target}"
# The same path on every machine (see the header); only the crate's sources, a few tens of KB.
SRC=/tmp/mnemosyne-guest-wasm-src
rm -rf "$SRC"
mkdir -p "$SRC/guest-wasm"
(cd "$CRATE" && git ls-files -z --cached --others --exclude-standard | xargs -0 cp --parents -t "$SRC/guest-wasm")
# With the rust-src component installed, std's paths point into the sysroot: map them back to
# the /rustc/<commit> form a toolchain without rust-src uses.
rust_commit="$(rustc -vV | awk '/^commit-hash:/ {print $2}')"
remap="--remap-path-prefix=$cargo_home=/cargo --remap-path-prefix=$target_dir=/target --remap-path-prefix=$SRC=/mnemosyne --remap-path-prefix=$sysroot/lib/rustlib/src/rust=/rustc/$rust_commit"
export RUSTFLAGS="$remap --cfg getrandom_backend=\"wasm_js\""
# The same prefixes for ring's C (it is in the cargo registry; its build output in the target dir).
export CFLAGS_wasm32_unknown_unknown="-ffile-prefix-map=$cargo_home=/cargo -ffile-prefix-map=$target_dir=/target"
(cd "$SRC/guest-wasm" && CARGO_TARGET_DIR="$target_dir" cargo build --locked --release --target "$TARGET")

wasm="$target_dir/$TARGET/release/guest_wasm.wasm"
rm -rf "$OUT"
mkdir -p "$OUT"
"$bindgen" --target web --out-dir "$OUT" "$wasm"

# --- report ---
if grep -qa "$HOME" "$OUT/guest_wasm_bg.wasm"; then
	die "the module still contains $HOME; path remapping failed"
fi
for f in "$OUT"/*; do
	printf '%-24s %8d bytes  %8d gzip  %s\n' "$(basename "$f")" "$(stat -c%s "$f")" \
		"$(gzip -9cn "$f" | wc -c)" "$(sha256sum "$f" | cut -c1-16)"
done
