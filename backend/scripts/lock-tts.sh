#!/bin/sh
# Regenerate the hashed requirement files of the narration engines
# (mnemosyne/synthesis/requirements/<engine>-<platform>.txt, installed by services/tts_install.py).
# Resolves only; installs nothing. Bump the versions here, then run from backend/.
#   linux       PyTorch's CPU build (from its CPU index), the rest from PyPI
#   linux-cuda  PyTorch for CUDA 12.8 (the backend's gpu extra uses the same baseline)
#   macos       Apple silicon, everything from PyPI
set -eu
KITTENML=0.9.8
QWEN_TTS=0.1.1
AUDIOSEAL=0.2.1
TORCH_CUDA=2.11.0  # the newest torch with cu128 wheels for Linux and Python 3.13
DIR=mnemosyne/synthesis/requirements
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$DIR"

lock() {  # engine platform requirements...
    engine=$1 platform=$2
    shift 2
    printf '%s\n' "$@" > "$TMP/$engine-$platform.in"
    case $platform in
    linux)
        set -- --python-platform x86_64-manylinux_2_28 --index-url https://pypi.org/simple \
            --extra-index-url https://download.pytorch.org/whl/cpu --index-strategy unsafe-best-match ;;
    linux-cuda)
        set -- --python-platform x86_64-manylinux_2_28 --index-url https://pypi.org/simple \
            --extra-index-url https://download.pytorch.org/whl/cu128 --index-strategy unsafe-best-match ;;
    macos)
        set -- --python-platform aarch64-apple-darwin ;;
    esac
    uv pip compile "$TMP/$engine-$platform.in" --python-version 3.13 "$@" --generate-hashes \
        --no-header -q -o "$TMP/$engine-$platform.txt"
    {
        printf '# Narration engine %s for %s (services/tts_install.py): installed into\n' "$engine" "$platform"
        printf '# <data_dir>/tts/%s/venv with uv --require-hashes, never into the backend lock.\n' "$engine"
        printf '# Regenerate with backend/scripts/lock-tts.sh.\n'
        cat "$TMP/$engine-$platform.txt"
    } > "$DIR/$engine-$platform.txt"
    echo "wrote $DIR/$engine-$platform.txt"
}

lock kitten linux "kittenml==$KITTENML" soundfile "audioseal==$AUDIOSEAL"
lock kitten linux-cuda "kittenml==$KITTENML" soundfile "audioseal==$AUDIOSEAL" \
    "torch==$TORCH_CUDA+cu128" "torchaudio==$TORCH_CUDA+cu128"
lock kitten macos "kittenml[mlx]==$KITTENML" soundfile "audioseal==$AUDIOSEAL"
lock qwen3 linux "qwen-tts==$QWEN_TTS" soundfile "audioseal==$AUDIOSEAL" \
    "torch==$TORCH_CUDA+cpu" "torchaudio==$TORCH_CUDA+cpu"
lock qwen3 linux-cuda "qwen-tts==$QWEN_TTS" soundfile "audioseal==$AUDIOSEAL" \
    "torch==$TORCH_CUDA+cu128" "torchaudio==$TORCH_CUDA+cu128"
lock qwen3 macos "qwen-tts==$QWEN_TTS" soundfile "audioseal==$AUDIOSEAL" \
    "torch==$TORCH_CUDA" "torchaudio==$TORCH_CUDA"
