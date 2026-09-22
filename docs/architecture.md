# Architecture

Mnemosyne is a desktop application for real-time audio transcription, speaker diarization, and LLM-powered summarization. It runs as a native Linux window with a Python backend for ML inference.

## System Overview

```
┌─────────────────────────────────────────────────────┐
│  Tauri v2 Shell (Rust)                              │
│  - Native window management (1200x800 default)      │
│  - Backend lifecycle (spawn, health poll, cleanup)  │
│  - File dialogs (tauri-plugin-dialog)               │
│  - Shell commands (tauri-plugin-shell)              │
│  - Logging (tauri-plugin-log)                       │
├─────────────────────────────────────────────────────┤
│  SvelteKit Frontend (Svelte 5 + Vite)               │
│  - Single-page app with adapter-static              │
│  - Real-time transcript view via WebSocket          │
│  - Session management UI with tabbed interface      │
│  - Settings, Obsidian export                        │
│  - Tailwind CSS 4 (dark theme)                      │
├─────────────────────────────────────────────────────┤
│  Python FastAPI Backend (sidecar on :8008)           │
│  - Audio capture via PipeWire (pw-record, pw-dump)  │
│  - Transcription via WhisperX (faster-whisper)      │
│  - Diarization via pyannote 3.1                     │
│  - Summarization (pluggable LLM providers)          │
│  - Obsidian export (direct file writes)             │
│  - Session persistence (JSON files on disk)         │
├─────────────────────────────────────────────────────┤
│  LAN LLM Infrastructure                             │
│  - Ollama (default, LAN-first)                      │
│  - vLLM (OpenAI-compatible API)                     │
│  - Cloud: OpenAI / Anthropic (optional)             │
└─────────────────────────────────────────────────────┘
```

## Component Communication

```
Frontend ──HTTP REST──> FastAPI Backend (all commands, incl. "start transcription")
Frontend <─WebSocket─── FastAPI Backend (event stream: jobs, sessions, segments)
Backend  ──PipeWire───> System Audio (pw-record, pw-dump)
Backend  ──HTTP───────> Ollama/vLLM/OpenAI/Anthropic
Backend  ──SQLite─────> data/mnemosyne.db (sessions, segments, recordings)
Backend  ──File I/O───> data/recordings/ (Opus audio), ~/.config/mnemosyne/config.toml
Backend  ──File I/O───> Obsidian vault (markdown export)
```

### REST API

The frontend communicates with the backend over HTTP on `127.0.0.1:8008`. All endpoints are prefixed with `/api/` except `/health` and `/ws`.

### WebSocket

A single connection at `ws://127.0.0.1:8008/ws` streams backend events to the UI. The client never
starts work over the socket; it only sends `ping`. Every connection gets its own bounded queue on the
in-process `EventBus`, so a slow client drops events instead of stalling the backend.

Event types: `hello` (snapshot of active jobs on connect), `job`, `session`, `transcription`, `status`,
`error`, `pong`. See `docs/api-reference.md`.

### Jobs

Anything that takes more than a moment runs as a **Job** (`jobs.py`). A route submits a runner
coroutine to the `JobManager`, gets a `Job` record back immediately, and the manager runs it as an
asyncio task. The runner reports progress through a `JobContext`, which publishes `job` events.
Per-kind concurrency limits serialize GPU work: only one `transcribe` job runs at a time.

Pipeline stages live in `services/pipeline.py` and are the only place ML work is invoked.

## Data Flow

### Recording Flow

1. User selects audio devices (microphone, system audio monitors)
2. Frontend calls `POST /api/audio/start` with device IDs and session ID
3. Backend spawns `pw-record` processes (one per device); session status `recording`
4. `POST /api/audio/stop/{id}`:
   - status `encoding`; each WAV is converted to OGG/Opus via ffmpeg
   - each source is stored on the session as a `Recording` with `source: mic | system`, so later
     stages can attribute the mic channel to the local user
   - all sources are mixed into a single mono `audio_file` (what the current engine consumes)
   - unless disabled, a `transcribe` job is queued and its id returned

### Transcription Flow

1. A `transcribe` job starts (from stop-recording, or `POST /api/sessions/{id}/transcribe`)
2. Session status `transcribing`; `status` events mark stages
3. `ModelService` lazily loads WhisperX (first use only)
4. Pipeline: transcribe (faster-whisper) → align (wav2vec2) → diarize (pyannote) → assign speakers
5. Each segment is published as a `transcription` event as the engine yields it
6. Transcript and participants are saved; session status `completed`; job `completed`
7. On failure: session status `error`, an `error` event, job `failed` with the message

### Summarization Flow

1. User selects provider and model in the Summary tab
2. Frontend calls `POST /api/sessions/{id}/summarize`
3. Backend formats transcript text and sends to LLM provider
4. Summary is saved to the session and returned

## State Management (Frontend)

The frontend uses Svelte 5 runes (`$state`, `$effect`) in class-based stores:

| Store | File | Responsibility |
|-------|------|---------------|
| `audioState` | `stores/audio.svelte.ts` | Device list, selection, recording status, duration |
| `sessionState` | `stores/session.svelte.ts` | Session CRUD, active session, session list |
| `transcriptState` | `stores/transcript.svelte.ts` | Transcript for the shown session; follows its `transcribe` job via WS events |
| `wsState` | `stores/websocket.svelte.ts` | WebSocket connection, auto-reconnect, message dispatch |
| `toastState` | `stores/toast.svelte.ts` | Toast notifications |

`transcriptState` holds a `sessionId` and ignores events for other sessions, so a job for one session
never leaks into another's view. Cross-store communication uses callbacks and the page wiring
(`+page.svelte`), not store-to-store imports. `session` events trigger a sidebar refresh.

## Persistence

### Database

Sessions live in `data/mnemosyne.db` (SQLite, WAL mode, stdlib `sqlite3`), via
`storage/sqlite.py::SessionRepository`:

- `sessions`: metadata, summary, notes, participants, mixed `audio_file`
- `segments`: one row per transcript segment (word timings as JSON), cascade-deleted
- `recordings`: one row per captured source (`mic` / `system`), cascade-deleted

Listing sessions never reads `segments`. On startup, any legacy `data/sessions/*.json` files from v2 are
imported once (files are left in place).

### Audio Files

Recordings are stored in `data/recordings/{session_id}/`:
- Individual sources: `{recording_id}_device_{device_id}.ogg`
- Mixed: `{recording_id}_mixed.ogg`

Audio is captured as WAV, then converted to OGG/Opus (~12x smaller) via ffmpeg.

### Configuration

`config.py::Settings` (pydantic-settings). Precedence: environment (incl. `backend/.env`) → user config
file (`$XDG_CONFIG_HOME/mnemosyne/config.toml`) → defaults. The Settings UI writes the file; the API
reports which fields the environment is overriding. `MNEMOSYNE_DATA_DIR` and `MNEMOSYNE_CONFIG_FILE`
relocate the data directory and config file (tests use both).

## ML Model Management

Heavy ML models (WhisperX, pyannote) are loaded lazily on first use:

1. `services/model_service.py` imports the engine inside a function, so the API starts without torch
2. Models are loaded into GPU memory when the first transcription job runs
3. Models remain in memory for subsequent transcriptions
4. `unload()` frees GPU memory; changing whisper settings via the API unloads so the next job reloads

This allows the backend to start instantly without waiting for multi-gigabyte model downloads.

## Backend Lifecycle

The Tauri Rust shell manages the Python backend as a child process:

```
App Start
  └─ setup() callback
       ├─ Spawn backend process (in new process group via setsid)
       │    ├─ Dev:     uv run uvicorn main:app --reload  (from backend/ dir)
       │    └─ Release: mnemosyne-backend --host --port   (from resource dir)
       ├─ Store Child in Mutex<Option<Child>>
       └─ Spawn async health poll task
            └─ TcpStream::connect("127.0.0.1:8008") every 500ms, 30s timeout
                 └─ Emit "backend-ready" event to frontend

App Exit (window closed)
  └─ RunEvent::Exit handler
       └─ kill_process_tree()
            ├─ kill(-pid, SIGTERM)   ← kills entire process group
            ├─ sleep 500ms
            ├─ kill(-pid, SIGKILL)   ← force kill stragglers
            └─ wait()                ← reap zombie
```

### Why Process Groups?

In dev mode, `uv run uvicorn` spawns `uvicorn` as a child process. Killing only the `uv` process leaves `uvicorn` orphaned. By spawning in a new process group (`setsid`) and killing the group (`kill(-pid, ...)`), both processes are terminated cleanly.

### Packaging Architecture

The app does **not** use Tauri's `externalBin` sidecar mechanism. PyInstaller `--onedir` produces a directory (binary + `_internal/` with shared libs), not a single file. Instead:

- The PyInstaller output directory is bundled as a Tauri **resource**
- Rust spawns it via `std::process::Command` with `current_dir` set to the resource directory
- This ensures the binary finds its `_internal/` folder at runtime

## Security Model

- Backend listens only on `127.0.0.1:8008` (localhost only)
- CORS is permissive (same-machine communication)
- No authentication (single-user desktop app)
- HuggingFace token stored in `backend/.env` (gitignored)
- API keys for cloud providers stored in `backend/.env` (gitignored)
