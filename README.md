# Mnemosyne

A real-time audio transcription, diarization, and summarization desktop app for Linux. Built with Tauri v2, SvelteKit, and Python.

## Name

> Mnemosyne is the personification of memory in Greek mythology. She was a Titaness, the daughter of Uranus and Gaia, and the mother of the nine Muses by Zeus. The name derives from the Greek word "mneme," meaning "memory" or "remembrance."

## Features

- **Real-time transcription** using WhisperX (faster-whisper + word-level alignment)
- **Speaker diarization** using pyannote.audio 3.1
- **Pluggable summarization** via Ollama (LAN default), vLLM, OpenAI, or Anthropic
- **Multiple audio sources** — capture system audio and microphone simultaneously via PipeWire
- **Session management** — create, rename, delete sessions with full persistence to disk
- **Obsidian integration** — export sessions as markdown with YAML frontmatter directly to your vault
- **Desktop app** — native Linux window via Tauri v2 (not a browser tab)
- **Storage-friendly** — audio saved as OGG/Opus (~12x smaller than WAV)

## Architecture

```
Tauri v2 (Rust)          — Window management, native dialogs
SvelteKit (Svelte 5)     — Frontend UI, reactive state via runes
Python FastAPI (sidecar)  — Audio capture, ML inference, API server on :8008
```

The Python backend runs as a sidecar process managed by Tauri. The frontend communicates with it over HTTP (REST) and WebSocket (real-time transcription streaming).

### LLM Infrastructure

Summarization is LAN-first by default:

- **Ollama** (configurable URL, default `localhost:11434`)
- **vLLM** (configurable URL, OpenAI-compatible API)
- **OpenAI** and **Anthropic** available when API keys are set

## Prerequisites

- Linux with PipeWire audio (Fedora 43+, Ubuntu 22.04+, etc.)
- NVIDIA GPU with CUDA support (for WhisperX inference)
- [Rust](https://rustup.rs/) (1.77+)
- [Node.js](https://nodejs.org/) 22.x (pinned via `.nvmrc`)
- [pnpm](https://pnpm.io/) (10+)
- [uv](https://docs.astral.sh/uv/) (Python project manager)
- [ffmpeg](https://ffmpeg.org/) (for audio format conversion)
- Tauri system dependencies:
  ```bash
  # Fedora
  sudo dnf install webkit2gtk4.1-devel openssl-devel curl wget file \
    libappindicator-gtk3-devel librsvg2-devel pango-devel
  ```
- HuggingFace account with accepted model licenses:
  - [pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1)
  - [pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0)

## Setup

1. **Clone and install frontend dependencies:**
   ```bash
   git clone <repo-url> && cd mnemosyne
   nvm use  # uses Node 22 from .nvmrc
   pnpm install
   ```

2. **Set up the Python backend:**
   ```bash
   cd backend
   uv sync  # creates venv with Python 3.13, installs deps
   ```

   For GPU transcription, also install WhisperX and PyTorch (these are heavy and may already be in your venv):
   ```bash
   uv pip install torch torchaudio whisperx
   ```

3. **Configure environment:**
   ```bash
   cp backend/.env.example backend/.env
   # Edit backend/.env and set:
   #   HF_TOKEN=your_huggingface_token
   #   OLLAMA_URL=http://your-ollama-server:11434
   #   OBSIDIAN_VAULT_PATH=/path/to/your/vault  (optional)
   ```

4. **Run in development mode (single terminal):**
   ```bash
   pnpm tauri dev
   ```

   Tauri automatically spawns the Python backend (via `uv run uvicorn`) and manages its lifecycle. The backend starts on `127.0.0.1:8008` with hot-reload enabled. When you close the window, the backend is automatically shut down.

## Usage

1. **Create a session** using the sidebar
2. **Select audio devices** — check the microphone and/or system audio monitors you want to capture
3. **Record** — click Record or press `Ctrl+R`; press `Ctrl+S` to stop and transcribe
4. **View transcript** — diarized segments appear in the Transcript tab with speaker labels and timestamps
5. **Summarize** — switch to the Summary tab, pick a provider/model, and click Summarize
6. **Take notes** — use the Notes tab for freeform markdown notes per session
7. **Export to Obsidian** — configure your vault path in the Export tab and click Export

### Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `Ctrl+R` | Start recording |
| `Ctrl+S` | Stop recording & transcribe |
| `Ctrl+E` | Export to Obsidian |
| `Ctrl+B` | Toggle sidebar |

## Project Structure

```
mnemosyne/
├── src/                          # SvelteKit frontend
│   ├── lib/
│   │   ├── components/           # Svelte 5 components
│   │   ├── stores/               # Rune-based reactive stores
│   │   ├── api/                  # Backend HTTP/WS client
│   │   └── types/                # TypeScript types
│   └── routes/                   # SPA page
├── src-tauri/                    # Tauri v2 Rust shell
│   ├── tauri.conf.json
│   └── src/lib.rs
├── backend/                      # Python FastAPI backend
│   ├── pyproject.toml
│   ├── main.py
│   └── src/mnemosyne/
│       ├── api/                  # FastAPI routes + WebSocket
│       ├── audio/                # PipeWire capture + Opus encoding
│       ├── transcription/        # WhisperX engine
│       ├── summarization/        # Ollama, vLLM, OpenAI, Anthropic providers
│       ├── export/               # Obsidian markdown exporter
│       ├── models/               # Pydantic data models
│       ├── services/             # Session + model lifecycle
│       └── config.py             # Environment configuration
├── scripts/                      # Build and dev scripts
│   ├── build-backend.sh          # PyInstaller backend build
│   ├── build-all.sh              # Frontend + backend build orchestrator
│   └── package.sh                # Full packaging (AppImage/deb)
└── data/                         # Runtime data (gitignored)
    ├── sessions/                 # JSON session files
    └── recordings/               # Audio files (OGG/Opus)
```

## Configuration

All configuration is via environment variables in `backend/.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `HF_TOKEN` | (required) | HuggingFace token for pyannote diarization models |
| `WHISPER_MODEL_SIZE` | `medium.en` | WhisperX model size (`base`, `small`, `medium`, `large-v2`, `large-v3`) |
| `WHISPER_COMPUTE_TYPE` | `float16` | Compute type (`float16`, `int8`) |
| `WHISPER_BATCH_SIZE` | `8` | Batch size for transcription (lower = less VRAM) |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama API endpoint |
| `VLLM_URL` | `http://localhost:8000` | vLLM API endpoint |
| `OPENAI_API_KEY` | (optional) | Enables OpenAI provider |
| `ANTHROPIC_API_KEY` | (optional) | Enables Anthropic provider |
| `OBSIDIAN_VAULT_PATH` | (optional) | Path to Obsidian vault for export |
| `OBSIDIAN_SUBFOLDER` | `meetings/mnemosyne` | Subfolder within vault |

## Building & Packaging

### Development

```bash
pnpm tauri dev    # Starts Tauri + Vite + Python backend (all in one)
```

### Build the Backend Sidecar

The Python backend is bundled as a self-contained binary using PyInstaller:

```bash
bash scripts/build-backend.sh
```

This produces `src-tauri/binaries/mnemosyne-backend-dir/` (~7 GB, includes PyTorch CUDA libraries).

### Build Distributable

```bash
bash scripts/package.sh    # Builds backend + frontend + Tauri → AppImage/deb
```

Artifacts are placed in `src-tauri/target/release/bundle/`.

### System Requirements (Target Machine)

The packaged app bundles the Python backend and all ML libraries, but requires:

- **PipeWire** (`pw-record`, `pw-dump`) — system audio stack
- **ffmpeg** — audio format conversion
- **NVIDIA drivers + CUDA runtime** — GPU transcription
- ML models (~3-5 GB) are downloaded from HuggingFace on first transcription

## Obsidian Export Format

Exported sessions produce markdown files with YAML frontmatter:

```yaml
---
title: "Session Name"
date: 2026-02-18
type: meeting-note
source: mnemosyne
participants: ["SPEAKER_00", "SPEAKER_01"]
tags: [meeting, mnemosyne]
---
```

Followed by sections for Summary, Participants, Notes, and the full Transcript with timestamps.

## Tech Stack

- **Desktop shell:** [Tauri v2](https://v2.tauri.app/) (Rust)
- **Frontend:** [SvelteKit](https://svelte.dev/) (Svelte 5 with runes), [Tailwind CSS 4](https://tailwindcss.com/)
- **Backend:** [FastAPI](https://fastapi.tiangolo.com/) (Python 3.13 via uv)
- **Transcription:** [WhisperX](https://github.com/m-bain/whisperX) (faster-whisper + pyannote 3.1)
- **Summarization:** [Ollama](https://ollama.com/), [vLLM](https://docs.vllm.ai/), OpenAI, Anthropic
- **Audio:** PipeWire (`pw-record`, `pw-dump`), ffmpeg (Opus encoding)

## Documentation

- [Architecture](docs/architecture.md) — System overview, data flows, state management design
- [API Reference](docs/api-reference.md) — Complete REST and WebSocket API documentation
- [Development Guide](docs/development.md) — Setup, project structure, adding providers, building
- [Troubleshooting](docs/troubleshooting.md) — Common issues and solutions

## Status

The app is feature-complete with packaging support (Phases 0-8). The Tauri shell manages the Python backend lifecycle automatically — single-command dev workflow and distributable AppImage/deb builds.

## License

MIT
