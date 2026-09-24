# Mnemosyne

[![CI](https://github.com/corpetty/mnemosyne/actions/workflows/ci.yml/badge.svg)](https://github.com/corpetty/mnemosyne/actions/workflows/ci.yml)

A real-time audio transcription, diarization, and summarization desktop app for Linux. Built with Tauri v2, SvelteKit, and Python.

## Name

> Mnemosyne is the personification of memory in Greek mythology. She was a Titaness, the daughter of Uranus and Gaia, and the mother of the nine Muses by Zeus. The name derives from the Greek word "mneme," meaning "memory" or "remembrance."

## Features

- **Live transcript while recording** (a few seconds behind speech, Parakeet on CPU) with **live speaker labels**: voices are told apart as they speak and people with a saved voice are named on the spot; replaced by the full diarized transcript after stop
- **Pluggable transcription**: WhisperX (GPU), NVIDIA Parakeet TDT via ONNX (CPU, no torch), or any OpenAI-compatible speech server
- **Speaker diarization** using pyannote.audio (community-1), or none
- **Structured summaries**: decisions, action items with owners, open questions and topics as data, with styles (meeting, standup, interview, lecture, brainstorm) and your own standing instructions
- **Obsidian-native export**: `[[people]]` links, tags, topics in frontmatter, action items as tasks, optional transcript
- **Playback synced to the transcript**: click any timestamp to hear that moment; the line being played is highlighted
- **Import** existing audio or video files (button or drag-and-drop) through the same pipeline
- **Glossary for names and jargon**: corrections applied to every transcript, a spelling hint for WhisperX and summaries, and an optional LLM pass that fixes misheard names without rephrasing
- **Editable transcripts**: fix text, reassign a line to another speaker, merge or split segments in place
- **Search** across every transcript, summary and note, with jump-to-segment
- **Calendar-aware**: paste your calendar's private ICS link and recordings are named after the meeting in progress, invitees are offered as speaker names, and a banner offers to record when a meeting starts
- **Storage control**: see what audio takes space, delete audio but keep the transcript, or keep audio for N days only
- **Weekly digest**: one note per week with an overview and themes from the LLM plus every decision and action item, on demand or on a schedule, saved to your vault
- **Action items to GitHub issues**: tick items in a summary and create issues in your repository, linked back from the meeting
- **MCP server**: search, read and ask your meetings from Claude Code, Claude Desktop or any MCP client
- **Ask your meetings** (Ctrl+K): questions answered from your transcripts with numbered citations that jump to the exact moment; history is kept
- **Speakers that stick**: rename a speaker once and the app remembers the voice, labelling that person automatically in future meetings
- **Level meters and a capture self-test**: see every source's level while recording, check a mic before a call, and verify that system audio is really captured from your output (it plays a short tone and checks it arrives)
- **Echo cancellation** with one click: PipeWire's WebRTC canceller is loaded on demand and exposed as a virtual mic, so no headphones are needed
- **Server mode**: point the app at a backend on another machine (with a bearer token) and work with its sessions
- **Echo-safe**: mic segments that merely repeat what came out of the speakers are dropped, so no-headphones calls still attribute correctly
- **Per-source attribution**: mic and system audio are captured separately, so your own speech is labelled with your name and only the remote side is diarized
- **Pluggable summarization** via Ollama (LAN default), vLLM, OpenAI, or Anthropic
- **Multiple audio sources** — capture system audio and microphone simultaneously via PipeWire
- **Session management** — SQLite-backed sessions; transcription runs as background jobs with live progress
- **Settings UI** — engine, models, providers and keys are configured in-app and persisted to `~/.config/mnemosyne/config.toml`
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
   uv sync --extra gpu --group dev  # Python 3.13 venv, API deps, torch (CUDA 12.8) + WhisperX, test tools
   ```

   The `gpu` extra is several GB. Omit it to run the API without transcription (tests and UI work still function).

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

### Tray and a global record shortcut

Mnemosyne adds a tray icon with Start/Stop recording, Show and Quit (GNOME needs the AppIndicator
extension to show tray icons). Wayland does not let apps grab global keys, so bind a desktop shortcut
(GNOME Settings → Keyboard → Custom Shortcuts, or KDE's Shortcuts) to:

```bash
mnemosyne --toggle
```

`--start` and `--stop` also work. The running app receives the command; if it isn't running, it starts
and performs the action once ready. Recording from the tray or shortcut always starts a new session
with the devices you last selected (remembered across restarts); if none are selected yet, the
window opens on the Recording tab instead.

### Use your meetings from Claude (MCP)

Mnemosyne ships an MCP server, `mnemosyne-mcp`, with tools to list, search, read and ask across your
meetings and to collect action items. It talks to the running app's backend. With the app installed:

```bash
claude mcp add mnemosyne -- ~/.local/share/com.corpetty.mnemosyne/venv/bin/mnemosyne-mcp
```

From a source checkout, use `uv run --directory /path/to/mnemosyne/backend mnemosyne-mcp` as the command.
Set `MNEMOSYNE_URL` (default `http://127.0.0.1:8008`) and `MNEMOSYNE_TOKEN` for a remote backend in
server mode, e.g. `claude mcp add mnemosyne -e MNEMOSYNE_URL=http://gpu-box:8008 -e MNEMOSYNE_TOKEN=... -- ...`.
For Claude Desktop, add the same command under `mcpServers` in its config file.

### Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `Ctrl+R` | Start recording |
| `Ctrl+S` | Stop recording & transcribe |
| `Ctrl+E` | Export to Obsidian |
| `Ctrl+K` | Ask your meetings |
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
│   └── src/lib.rs                # Backend install (uv) + lifecycle
├── backend/                      # Python FastAPI backend
│   ├── pyproject.toml
│   ├── main.py
│   └── mnemosyne/
│       ├── api/                  # FastAPI routes + WebSocket
│       ├── audio/                # PipeWire capture + Opus encoding
│       ├── transcription/        # Transcriber/Diarizer protocols, whisperx, parakeet, remote, pyannote
│       ├── storage/              # SQLite session repository
│       ├── jobs.py, events.py    # Background jobs + event bus
│       ├── summarization/        # Ollama, vLLM, OpenAI, Anthropic providers
│       ├── export/               # Obsidian markdown exporter
│       ├── models/               # Pydantic data models
│       ├── services/             # Session + model lifecycle
│       └── config.py             # Environment configuration
├── scripts/                      # Build and dev scripts
│   ├── stage-backend.sh          # Copy backend source + lockfile into the bundle
│   ├── fetch-uv.sh               # Download the pinned uv sidecar
│   ├── build-all.sh              # Frontend + stage + fetch (Tauri beforeBuildCommand)
│   └── package.sh                # pnpm tauri build wrapper
└── data/                         # Runtime data (gitignored)
    ├── mnemosyne.db              # SQLite: sessions, segments, recordings
    └── recordings/               # Audio files (OGG/Opus), one per source + mixed
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
pnpm tauri dev      # Starts Tauri + Vite + Python backend (all in one)
pnpm check          # Frontend type check
pnpm test:backend   # Backend tests (no GPU needed; ML is faked)
pnpm lint:backend   # ruff
pnpm test:e2e       # Browser tests (Playwright) against a demo-mode backend
```

The browser tests need Chromium once: `pnpm exec playwright install chromium`. Demo mode
(`MNEMOSYNE_DEMO=1`, see `backend/mnemosyne/demo.py`) is also handy for trying the UI with
no models or LLM server: run `bash tests-e2e/start-backend.sh` and point the app at
`http://127.0.0.1:8018` in Settings → Connection.

### Releases

Pushing a `vX.Y.Z` tag builds the deb, rpm and AppImage in GitHub Actions and attaches them to a release
(see `docs/development.md`). Download from the Releases page instead of building locally.
From 0.5.0 on the app updates itself: it checks the latest release at start (and from
Settings → Updates) and installs signed updates in place; deb and rpm installs ask for your
password.

### Build Distributable

```bash
bash scripts/package.sh                 # AppImage + deb + rpm
bash scripts/package.sh --bundles deb   # just one
```

Tauri runs `scripts/build-all.sh` first, which builds the frontend, stages the backend source tree
and lockfile into `src-tauri/resources/backend/`, and downloads the pinned `uv` release as a sidecar.
Artifacts land in `src-tauri/target/release/bundle/`. Bundles are small (tens of MB): **no Python, no
torch, no models are shipped**.

### Flatpak

Each release also has a `.flatpak` bundle (GNOME 50 runtime, which Flatpak fetches from Flathub):

```bash
flatpak install --user Mnemosyne_0.5.0_x86_64.flatpak
flatpak run com.corpetty.mnemosyne
```

It records through the PipeWire socket and can read your home directory (for Obsidian vaults).
Data lives in `~/.var/app/com.corpetty.mnemosyne/`. GPU transcription is not set up inside the
sandbox; use Parakeet (CPU) or a remote transcriber. Build one yourself from a deb with
`bash scripts/build-flatpak.sh <deb>`. The Flatpak does not update itself; install the new bundle.

### First launch on a target machine

The app installs its own backend into `~/.local/share/com.corpetty.mnemosyne/`:

1. `uv sync` creates a venv with a managed Python 3.13 and the locked dependencies (`onnx` extra always;
   `gpu` extra when `nvidia-smi` is on the PATH). Progress is shown in the window. Needs internet once;
   later launches reuse it until an update changes `uv.lock`.
2. The backend starts from that venv. Session data lives in `.../data/`, settings in
   `~/.config/mnemosyne/config.toml`.
3. ML models download from HuggingFace on first use (Parakeet int8 ~0.6 GB; WhisperX + pyannote 3 to 5 GB).

### System Requirements (Target Machine)

- **PipeWire tools** (`pw-record`, `pw-dump`, `pw-cli`) and **ffmpeg**, declared as package dependencies (Fedora: `pipewire-utils`, `ffmpeg-free` or `ffmpeg`; Debian/Ubuntu: `pipewire-bin`, `ffmpeg`)
- **NVIDIA drivers** if you want the GPU engines; CPU-only machines run Parakeet
- Internet on first launch and first transcription

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

v3 (2026-09): background jobs and event stream, SQLite sessions, in-app settings, pluggable
transcription engines (WhisperX, Parakeet ONNX, remote), pyannote community-1, live transcript while
recording, and torch-free packaging (the app installs its backend with uv on first launch).

## License

MIT
