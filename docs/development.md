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

You need two terminal processes:

**Terminal 1 — Backend:**
```bash
cd backend
uv run uvicorn main:app --host 127.0.0.1 --port 8008 --reload
```

**Terminal 2 — Frontend + Tauri:**
```bash
pnpm tauri dev
```

Or use convenience scripts:
```bash
pnpm dev:backend   # Terminal 1
pnpm tauri dev     # Terminal 2
```

The Tauri dev command starts both Vite (hot-reload) and the Tauri window. The backend runs separately because it manages heavy ML models.

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
│   ├── capabilities/
│   │   └── default.json      # Tauri permissions
│   └── src/
│       ├── main.rs           # Entry point
│       └── lib.rs            # Plugin setup (shell, dialog, log)
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
│   └── dev-backend.sh        # Backend dev script
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
WHISPER_MODEL_SIZE=large-v2     # or base, small, medium, large-v3
WHISPER_COMPUTE_TYPE=float16    # or int8 for lower VRAM
WHISPER_BATCH_SIZE=16           # lower for less VRAM usage

# LLM providers (at least one recommended)
OLLAMA_URL=http://bugger.ender.verse:11434
VLLM_URL=http://bugger.ender.verse:8000
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

The full WhisperX pipeline (transcription + alignment + diarization) requires significant GPU memory:

| Model | Approximate VRAM |
|-------|-----------------|
| `base` | ~2 GB |
| `small` | ~3 GB |
| `medium` | ~5 GB |
| `large-v2` | ~7 GB |
| `large-v3` | ~7 GB |

With diarization models loaded, add ~2 GB. An RTX 2080 Ti (11 GB) can run `large-v2` with diarization.

To reduce VRAM usage:
- Use a smaller model: `WHISPER_MODEL_SIZE=medium`
- Use int8 quantization: `WHISPER_COMPUTE_TYPE=int8`
- Reduce batch size: `WHISPER_BATCH_SIZE=8`

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

### Frontend Build

```bash
pnpm build   # Outputs static files to build/
```

### Tauri Build

```bash
pnpm tauri build   # Creates AppImage/deb in src-tauri/target/release/bundle/
```

**Note:** Packaging the Python backend as a sidecar binary (via PyInstaller) is deferred to Phase 8. For now, the backend must be started separately.
