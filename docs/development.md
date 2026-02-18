# Development Guide

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Rust | 1.77+ | `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \| sh` |
| Node.js | 22.x | `nvm install` (uses `.nvmrc`) |
| pnpm | 10+ | `npm install -g pnpm` |
| uv | latest | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| ffmpeg | system | `sudo dnf install ffmpeg` (Fedora) |
| PipeWire | system | Pre-installed on Fedora 43+, Ubuntu 22.04+ |
| NVIDIA GPU | CUDA-capable | Required for WhisperX inference |

### Tauri System Dependencies (Fedora)

```bash
sudo dnf install webkit2gtk4.1-devel openssl-devel curl wget file \
  libappindicator-gtk3-devel librsvg2-devel pango-devel
```

### HuggingFace Model Access

Speaker diarization requires accepting model licenses:

1. Create account at [huggingface.co](https://huggingface.co)
2. Accept licenses for:
   - [pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1)
   - [pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0)
3. Generate a token at [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens)

## Project Setup

```bash
# Clone
git clone <repo-url> && cd mnemosyne

# Frontend dependencies
nvm use
pnpm install

# Backend setup
cd backend
uv sync                  # Creates venv with Python 3.13
uv pip install torch torchaudio whisperx  # GPU dependencies
cp .env.example .env     # Configure environment
cd ..
```

## Running in Development

Single terminal — Tauri manages the backend automatically:

```bash
pnpm tauri dev
```

This starts:
1. **Vite dev server** (frontend hot-reload)
2. **Tauri window** (native desktop shell)
3. **Python backend** (`uv run uvicorn main:app --host 127.0.0.1 --port 8008 --reload`)

The Rust shell spawns the backend as a child process, polls TCP port 8008 for readiness, and emits a `backend-ready` event to the frontend. When the window is closed, the backend process tree is killed automatically (using process groups via `setsid`).

To run the backend standalone (e.g., for debugging):
```bash
cd backend
uv run uvicorn main:app --host 127.0.0.1 --port 8008 --reload
```

## Project Structure

```
mnemosyne/
├── package.json              # Root scripts, frontend deps
├── svelte.config.js          # SvelteKit config (adapter-static)
├── vite.config.ts            # Vite config
├── tailwind.config.ts        # Tailwind CSS 4
├── tsconfig.json             # TypeScript config
├── .nvmrc                    # Node version pin (22)
│
├── src/                      # SvelteKit frontend
│   ├── app.html              # HTML template
│   ├── app.css               # Global styles (Tailwind imports)
│   ├── lib/
│   │   ├── api/
│   │   │   └── backend.ts    # HTTP client for all API calls
│   │   ├── components/       # Svelte 5 components
│   │   │   ├── AudioControls.svelte
│   │   │   ├── DeviceSelector.svelte
│   │   │   ├── NotesEditor.svelte
│   │   │   ├── ObsidianExport.svelte
│   │   │   ├── SessionList.svelte
│   │   │   ├── SettingsPanel.svelte
│   │   │   ├── SummaryView.svelte
│   │   │   ├── ToastContainer.svelte
│   │   │   └── TranscriptView.svelte
│   │   ├── stores/           # Svelte 5 rune-based state
│   │   │   ├── audio.svelte.ts
│   │   │   ├── session.svelte.ts
│   │   │   ├── toast.svelte.ts
│   │   │   ├── transcript.svelte.ts
│   │   │   └── websocket.svelte.ts
│   │   └── types/
│   │       └── index.ts      # TypeScript interfaces
│   └── routes/
│       ├── +layout.svelte    # Root layout (imports app.css)
│       ├── +layout.ts        # Prerender config
│       └── +page.svelte      # Main SPA page
│
├── src-tauri/                # Tauri v2 Rust shell
│   ├── Cargo.toml            # Rust dependencies
│   ├── tauri.conf.json       # Window, build, bundle config
│   ├── binaries/             # PyInstaller output (build artifact, gitignored)
│   ├── capabilities/
│   │   └── default.json      # Tauri permissions
│   └── src/
│       ├── main.rs           # Entry point
│       └── lib.rs            # Backend lifecycle (spawn, health poll, cleanup)
│
├── backend/                  # Python FastAPI backend
│   ├── pyproject.toml        # Python deps (uv project)
│   ├── .python-version       # Python 3.13 pin
│   ├── main.py               # Uvicorn entry point
│   ├── .env.example          # Configuration template
│   └── src/mnemosyne/
│       ├── config.py         # Environment variable loading
│       ├── api/
│       │   ├── app.py        # FastAPI app factory
│       │   ├── routes/
│       │   │   ├── audio.py      # Recording start/stop
│       │   │   ├── devices.py    # PipeWire device listing
│       │   │   ├── export.py     # Obsidian export
│       │   │   ├── models.py     # LLM model listing, summarization
│       │   │   └── sessions.py   # Session CRUD
│       │   └── websocket.py      # WebSocket transcription streaming
│       ├── audio/
│       │   ├── capture.py    # PipeWire recording + Opus conversion
│       │   └── mixer.py      # Multi-source audio mixing
│       ├── transcription/
│       │   ├── engine.py     # TranscriptionEngine Protocol
│       │   └── whisperx_engine.py  # WhisperX implementation
│       ├── summarization/
│       │   ├── provider.py           # SummarizationProvider Protocol
│       │   ├── prompts.py            # LLM prompt templates
│       │   ├── ollama.py             # Ollama provider
│       │   ├── vllm.py               # vLLM provider
│       │   ├── openai_provider.py    # OpenAI provider
│       │   └── anthropic_provider.py # Anthropic provider
│       ├── export/
│       │   ├── obsidian.py   # Obsidian vault exporter
│       │   └── templates.py  # Markdown templates
│       ├── models/
│       │   ├── session.py    # Session Pydantic model
│       │   └── transcript.py # Transcript segment models
│       └── services/
│           ├── model_service.py          # ML model singleton
│           ├── session_service.py        # Session CRUD service
│           └── summarization_service.py  # Summarization orchestrator
│
├── scripts/
│   ├── build-backend.sh      # PyInstaller backend build
│   ├── build-all.sh          # Frontend + backend build orchestrator
│   └── package.sh            # Full packaging (AppImage/deb)
│
└── data/                     # Runtime data (gitignored)
    ├── sessions/             # JSON session files
    └── recordings/           # OGG/Opus audio files
```

## Environment Configuration

Copy `backend/.env.example` to `backend/.env` and configure:

```bash
# Required
HF_TOKEN=hf_your_token_here

# WhisperX settings
WHISPER_MODEL_SIZE=medium.en    # safe default; use large-v2 with 24GB+ VRAM
WHISPER_COMPUTE_TYPE=float16    # or int8 for lower VRAM
WHISPER_BATCH_SIZE=8            # lower for less VRAM usage

# LLM providers (at least one recommended)
OLLAMA_URL=http://localhost:11434
VLLM_URL=http://localhost:8000
OPENAI_API_KEY=                 # optional
ANTHROPIC_API_KEY=              # optional

# Obsidian (optional)
OBSIDIAN_VAULT_PATH=/path/to/vault
OBSIDIAN_SUBFOLDER=meetings/mnemosyne
```

## Known Platform Issues

### Wayland/WebKitGTK Display Errors

On Wayland with HiDPI displays, WebKitGTK may fail with GBM buffer errors:

```
Failed to create GBM buffer of size 2400x1600: Invalid argument
```

**Fix:** The `pnpm dev:app` script sets `WEBKIT_DISABLE_DMABUF_RENDERER=1` automatically. If running Tauri manually:

```bash
WEBKIT_DISABLE_DMABUF_RENDERER=1 pnpm tauri dev
```

### PyAV / FFmpeg Version Mismatch

If you see `libavutil.so.60: cannot open shared object file`, PyAV's bundled FFmpeg version doesn't match the system FFmpeg:

```bash
cd backend
uv pip install 'av<14'   # Forces PyAV 13.x with FFmpeg 7 (libavutil.so.59)
```

### Python 3.14 Compatibility

WhisperX requires Python <3.14. The backend pins Python 3.13 via `backend/.python-version`. If `uv sync` tries to use system Python 3.14, ensure uv respects the pin:

```bash
cd backend
uv python install 3.13
uv sync
```

### VRAM Usage

The WhisperX pipeline loads up to three models simultaneously at peak: the Whisper model, wav2vec2 alignment model, and pyannote diarization model.

| Model | Whisper VRAM | Peak Pipeline VRAM (with diarization) |
|-------|-------------|--------------------------------------|
| `base` | ~1 GB | ~3 GB |
| `small` | ~2 GB | ~4 GB |
| `medium` / `medium.en` | ~3 GB | ~5 GB |
| `large-v2` / `large-v3` | ~5 GB | ~9-11 GB |

The default `medium.en` is safe for GPUs with 8+ GB VRAM. Using `large-v2` on an RTX 2080 Ti (11 GB) may cause OOM during the alignment step when all three models are loaded. Users with 24+ GB VRAM can override to `large-v2` / batch 16 in their `.env`.

To reduce VRAM usage:
- Use a smaller model: `WHISPER_MODEL_SIZE=small`
- Use int8 quantization: `WHISPER_COMPUTE_TYPE=int8`
- Reduce batch size: `WHISPER_BATCH_SIZE=4`

## Adding a New Summarization Provider

1. Create `backend/src/mnemosyne/summarization/my_provider.py`:

```python
class MyProvider:
    name = "my_provider"

    async def list_models(self) -> list[str]:
        return ["model-a", "model-b"]

    async def summarize(self, transcript: str, model: str, system_prompt: str) -> str:
        # Call your LLM API and return markdown summary
        ...
```

2. Register in `backend/src/mnemosyne/services/summarization_service.py`:

```python
from ..summarization.my_provider import MyProvider

class SummarizationService:
    def __init__(self):
        self.providers = {
            # ... existing providers ...
            "my_provider": MyProvider(),
        }
```

3. The new provider will automatically appear in the frontend's provider dropdown via `GET /api/models`.

## Building for Production

### Backend Sidecar (PyInstaller)

The Python backend is bundled as a self-contained binary using PyInstaller `--onedir` mode:

```bash
bash scripts/build-backend.sh
```

This:
1. Syncs dependencies with `uv sync`
2. Runs PyInstaller with `--collect-all` for torch, whisperx, pyannote, etc.
3. Copies the output directory to `src-tauri/binaries/mnemosyne-backend-dir/`

The output is ~7 GB due to PyTorch CUDA libraries. The binary accepts `--host` and `--port` CLI arguments.

### Full Package

```bash
bash scripts/package.sh
```

This builds the backend sidecar, then runs `pnpm tauri build` which:
1. Builds the SvelteKit frontend (`pnpm build`)
2. Compiles the Rust shell
3. Bundles everything into AppImage and/or deb packages

Artifacts are placed in `src-tauri/target/release/bundle/`.

### How Packaging Works

The Tauri shell does **not** use the formal `externalBin` sidecar mechanism. Instead:

- PyInstaller produces a **directory** (`mnemosyne-backend` binary + `_internal/` with shared libs)
- The entire directory is bundled as a Tauri **resource** (configured in `tauri.conf.json`)
- At runtime, Rust spawns the binary via `std::process::Command` with `current_dir` set to the resource directory (so `_internal/` is found correctly)
- Process groups (`setsid`) ensure the backend and all its children are killed on app exit

### System Requirements (Target Machine)

The packaged app bundles Python, PyTorch, and all ML libraries, but requires:

- **PipeWire** (`pw-record`, `pw-dump`) — system audio stack, cannot be meaningfully bundled
- **ffmpeg** — audio format conversion
- **NVIDIA drivers + CUDA runtime** — for GPU transcription
- ML models (~3-5 GB) — downloaded from HuggingFace on first transcription
