# Troubleshooting

## Backend Won't Start

### `ModuleNotFoundError: No module named 'torch'`

WhisperX and PyTorch are heavy dependencies not in the base `pyproject.toml`. Install them manually:

```bash
cd backend
uv pip install torch torchaudio whisperx
```

### `ModuleNotFoundError: No module named 'whisperx'`

Same as above. WhisperX depends on torch, torchaudio, and several other packages:

```bash
cd backend
uv pip install whisperx
```

### Backend starts but models fail to load

Ensure your HuggingFace token is set and has accepted the required model licenses:

```bash
grep HF_TOKEN backend/.env   # Should show your token
```

Visit and accept licenses:
- https://huggingface.co/pyannote/speaker-diarization-3.1
- https://huggingface.co/pyannote/segmentation-3.0

---

## Audio Issues

### No devices shown in Device Selector

PipeWire must be running. Check:

```bash
pw-dump | head -5   # Should output JSON
```

If PipeWire isn't running:
```bash
systemctl --user start pipewire pipewire-pulse
```

### Recording produces empty files

Ensure the selected device is active. For system audio capture (output devices), there must be audio playing through that output during recording.

### `ffmpeg: command not found`

Install ffmpeg:
```bash
sudo dnf install ffmpeg        # Fedora
sudo apt install ffmpeg         # Ubuntu/Debian
```

---

## Transcription Issues

### `AttributeError: module 'whisperx' has no attribute 'DiarizationPipeline'`

WhisperX 3.8.1 moved the class. The code uses `whisperx.diarize.DiarizationPipeline` which is correct for 3.8.1+. If you have an older version:

```bash
cd backend
uv pip install --upgrade whisperx
```

### `OSError: libavutil.so.60: cannot open shared object file`

PyAV version mismatch with system FFmpeg. Downgrade PyAV:

```bash
cd backend
uv pip install 'av<14'
```

### CUDA out of memory

The WhisperX pipeline loads up to three models simultaneously (whisper + wav2vec2 alignment + pyannote diarization). With `large-v2`, peak VRAM can reach 9-11 GB, causing OOM on GPUs with 11 GB or less.

The default `medium.en` with batch size 8 is safe for 8+ GB GPUs. To further reduce VRAM:

```
WHISPER_MODEL_SIZE=small
WHISPER_COMPUTE_TYPE=int8
WHISPER_BATCH_SIZE=4
```

For GPUs with 24+ GB VRAM, you can use larger models:
```
WHISPER_MODEL_SIZE=large-v2
WHISPER_BATCH_SIZE=16
```

### Diarization shows only SPEAKER_00

This happens when:
- The audio has only one dominant speaker (diarization is correct)
- The audio quality makes speakers indistinguishable

The engine uses `min_speakers=1, max_speakers=10` and `fill_nearest=True` for best results. Test with audio that has clear speaker turns.

### torchcodec warnings in backend logs

Harmless warnings from pyannote. They're suppressed in `backend/main.py` but may still appear briefly. They don't affect functionality.

---

## Frontend Issues

### Tauri window is blank or shows errors

**GBM buffer error (`Failed to create GBM buffer of size ...`):**
```bash
WEBKIT_DISABLE_DMABUF_RENDERER=1 pnpm tauri dev
```

The `pnpm dev:app` script sets this automatically.

**Wayland protocol error (`Error 71 dispatching to Wayland display`):**
Same fix as above. The `WEBKIT_DISABLE_DMABUF_RENDERER=1` env var resolves most WebKitGTK display issues on Wayland.

### Frontend freezes or becomes unresponsive

This was caused by infinite `$effect` loops in Svelte 5 reactivity. If you're modifying stores, ensure:

1. `$effect` blocks don't mutate state that triggers the same `$effect`
2. Use ID-based guards for session switching (check `lastLoadedSessionId` in `+page.svelte`)
3. Use the callback pattern (`transcriptState.onComplete()`) instead of importing stores circularly

### WebSocket not connecting

Check that the backend is running on port 8008:

```bash
curl http://127.0.0.1:8008/health
```

The WebSocket auto-reconnects every 3 seconds. The status bar shows "WS" with a blue dot when connected.

### Summary tab shows "Record and transcribe audio first"

The session needs a transcript before summarization. Ensure:
1. You've recorded audio and stopped recording
2. Transcription completed (check the Transcript tab)
3. The session was refreshed after transcription (happens automatically via the `onComplete` callback)

---

## Summarization Issues

### `400 Bad Request` from Ollama

Likely caused by selecting an embedding-only model. The app filters these out, but if you see this error:

1. Check which model was selected in the Summary tab
2. Models with "embed" in the name or from the BERT family are chat-incompatible
3. The filtering is in `backend/src/mnemosyne/summarization/ollama.py`

### No models shown in provider list

- **Ollama:** Ensure the Ollama server is running and reachable: `curl http://your-ollama-url:11434/api/tags`
- **vLLM:** Check URL and that the server is running: `curl http://your-vllm-url:8000/v1/models`
- **OpenAI/Anthropic:** Set API keys in `backend/.env`

Click "Refresh Models" in the Settings panel after configuration changes.

### Summarization is slow

LLM inference time depends on:
- Model size (larger = slower but better)
- Transcript length
- Network latency to LAN server
- Server GPU (Ollama/vLLM)

For faster results, use smaller models or the compact prompt (auto-selected for short transcripts).

---

## Obsidian Export Issues

### "Obsidian vault path not configured"

Set the vault path either:
- In the Export tab using the Browse button (Tauri only) or manual path input
- In `backend/.env`: `OBSIDIAN_VAULT_PATH=/path/to/vault`

### "Vault not found"

The path must point to an existing directory. Verify:
```bash
ls /path/to/your/vault/.obsidian   # Should exist if it's a vault
```

### Export succeeds but file doesn't appear in Obsidian

Obsidian needs to detect the new file. Try:
1. Click in the Obsidian vault folder pane to refresh
2. Check the export path matches your vault root
3. Look in the subfolder (default: `meetings/mnemosyne/`)

---

## Build & Packaging Issues

### PyInstaller build fails with missing hidden imports

PyInstaller may miss dynamic imports from torch, whisperx, or uvicorn. Add the missing module to `scripts/build-backend.sh`:

```bash
--hidden-import "missing.module.name"
```

Common ones already included: `torch._C`, `torch._C._jit`, `uvicorn.logging`, `uvicorn.loops.auto`, etc.

### PyInstaller output is very large (~7 GB)

This is expected. PyTorch CUDA libraries alone are ~2 GB. The `--collect-all torch` flag includes all backends. To reduce size:
- Use `--exclude-module` for unused torch backends (e.g., `torch.distributed`)
- Consider building without CUDA for CPU-only deployment (not recommended for transcription performance)

### Standalone backend binary can't find `_internal/`

The PyInstaller `--onedir` binary must be run from the directory containing it. The Tauri shell sets `current_dir` to the binary's parent directory. If running manually:

```bash
cd src-tauri/binaries/mnemosyne-backend-dir
./mnemosyne-backend --port 8008
```

### Backend doesn't start when launched from Tauri

Check the Tauri logs for spawn errors:
1. In dev mode, ensure `uv` is on PATH
2. In release mode, ensure the resource directory contains the PyInstaller output
3. Check if port 8008 is already in use: `lsof -i :8008`

### Orphan backend processes after crash

If the app is killed with SIGKILL (or crashes), the `RunEvent::Exit` handler doesn't run and the backend may be orphaned. To clean up:

```bash
pkill -f mnemosyne-backend    # Kill PyInstaller binary
pkill -f "uvicorn main:app"   # Kill dev mode backend
```

### `resource path 'binaries/mnemosyne-backend-dir' doesn't exist` during Tauri build

Tauri validates resource paths at compile time. Run `scripts/build-backend.sh` first, or ensure the placeholder directory exists:

```bash
mkdir -p src-tauri/binaries/mnemosyne-backend-dir
touch src-tauri/binaries/mnemosyne-backend-dir/.gitkeep
```
