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

### Search

`segments_fts` and `sessions_fts` (FTS5, unicode61 tokenizer) are kept in sync by triggers on
`segments` and `sessions`. Databases created before schema 3 are backfilled on open. User input is
turned into a safe query by quoting every term (`storage/sqlite.py::fts_query`).

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

## Transcription Engine

Transcription is two pluggable stages behind Protocols in `transcription/engine.py`:

| Stage | Protocol | Implementations |
|---|---|---|
| Speech to text | `Transcriber` | `whisperx` (faster-whisper + wav2vec2 alignment, GPU), `parakeet` (NVIDIA Parakeet TDT via onnx-asr, CPU or CUDA, no torch), `remote` (any OpenAI-compatible `/audio/transcriptions` server) |
| Who spoke | `Diarizer` | `pyannote` (community-1 by default, GPU), `none` |

`ComposedEngine` (`transcription/composed.py`) joins one of each. `transcription/registry.py` builds it
from settings (`transcriber`, `diarizer`, and per-implementation options). Heavy imports happen inside
the builders, so `remote` + `none` never imports torch.

### Per-source speaker attribution

The recorder keeps each captured source as its own file. `services/pipeline.py::sources_for_session`
turns them into `AudioSource`s:

- mic + system captured: the mic file is labelled `local_speaker_name` (default "Me") and not diarized;
  the system file is diarized. This gives a guaranteed "you" label and keeps diarization to the remote
  side, where it matters.
- mic only: it may contain a whole room, so it is diarized.
- otherwise: the mixed file is used.

Segments from all sources are merged by start time. `transcription/assign.py` maps diarization turns
onto segments by time overlap, using word timings for a majority vote when available.

### Live transcription

While recording, a `live` job (`transcription/live.py`) tails each source's WAV file as pw-record
writes it, keeps a buffer of not-yet-committed audio per source, and every `live_interval_seconds`
runs a separate `Transcriber` instance (`live_transcriber`, Parakeet on CPU by default) over the
buffer. Segments that end at least one second before the buffer edge are committed and streamed as
`live_segment` events with absolute times; the remainder is sent as a replaceable `live_partial` and
retried on the next tick, so words cut by a chunk boundary are not lost. With mic and system captured
separately the labels are `local_speaker_name` and `remote_speaker_name`; no diarization runs live.
Stop cancels the job (a final flush tick runs) before the final `transcribe` job replaces everything.

### Speaker bleed removal

Without headphones the mic hears the remote participants through the speakers, so the mic transcript
repeats the system transcript. `transcription/dedup.py::remove_echo` runs in `ComposedEngine` after
per-source transcription: a labelled (mic) segment is dropped when the diarized segments overlapping
it in time (±1 s) contain its words with similarity ≥ `echo_similarity`. Only genuinely local speech
survives on the mic channel. The job result reports `echo_dropped`.

### Voice profiles

pyannote returns one embedding per diarized speaker. The pipeline stores them per session
(`session_speakers`) and, when `auto_label_speakers` is on, `services/speaker_service.py` matches
them against known profiles (`speakers` table, cosine ≥ `speaker_match_threshold`, greedy one-to-one)
and relabels the transcript before it is saved. Renaming a speaker in the UI relabels the session and,
if that label has an embedding, enrolls it into the named profile as a running mean. Profiles are
managed in Settings.

### Model lifecycle

`services/model_service.py` builds the engine lazily on first job (so the API starts without torch),
keeps it loaded across jobs, and drops it when any engine-related setting changes so the next job
rebuilds with the new configuration. `unload()` frees GPU memory.

## Backend Lifecycle

The Tauri Rust shell (`src-tauri/src/lib.rs`) supervises the Python backend from a worker thread and
reports progress to the UI as `backend-status` events (`installing`, `starting`, `ready`, `error`):

```
App Start
  └─ setup() -> supervisor thread
       ├─ Dev:     uv run uvicorn main:app --reload      (from backend/)
       └─ Release:
            ├─ resolve layout: resources/backend (source + uv.lock), sidecar mnemosyne-uv,
            │                  ~/.local/share/com.corpetty.mnemosyne/{venv,data}
            ├─ if venv missing or .installed-uv.lock != uv.lock:
            │     uv sync --frozen --no-dev --extra onnx [--extra gpu if nvidia-smi on PATH]
            │     (stderr lines streamed as "installing" events; stamp written on success)
            └─ venv/bin/python main.py --host 127.0.0.1 --port 8008
                  env MNEMOSYNE_DATA_DIR=.../data, cwd=resources/backend
       ├─ Child stored in Mutex<Option<Child>> (spawned in its own process group via setsid)
       └─ TCP poll on :8008 (120 s) -> "ready" / "error", plus legacy "backend-ready"

App Exit
  └─ RunEvent::Exit -> kill(-pgid, SIGTERM) ... SIGKILL ... wait()
```

### Packaging

Nothing heavy is shipped. The bundle contains the frontend, the Rust shell, the backend *source*
and `uv.lock` as resources, and a pinned `uv` binary as a Tauri `externalBin` sidecar (installed
as `mnemosyne-uv` so it never shadows a user's own `uv`). Python itself comes from uv's managed
python-build-standalone download on first run; ML wheels come from the locked indexes; models come
from HuggingFace on first use.

Consequences:
- Bundles are tens of MB instead of 7 GB, and a release does not have to embed CUDA libraries.
- The same bundle works on CPU-only machines (Parakeet) and GPU machines (WhisperX + pyannote).
- Updating the app re-runs `uv sync` only when `uv.lock` changed.
- The install needs network once. An offline installer would pre-seed uv's cache; not done yet.

The AppImage runtime's AppRun exports `PYTHONHOME`, `PYTHONPATH`, `LD_LIBRARY_PATH` and GTK/GIO
paths for the GUI process. `lib.rs::scrub_runtime_env` removes them from the uv and Python child
commands (restoring `APPIMAGE_ORIGINAL_*` values) so the venv Python and the tools it spawns
(ffmpeg, pw-record) see the host environment.

`scripts/build-all.sh` (Tauri's `beforeBuildCommand`) builds the frontend, stages the backend into
`src-tauri/resources/backend/`, and fetches the sidecar into `src-tauri/binaries/`.

## Security Model

- Backend listens only on `127.0.0.1:8008` (localhost only)
- CORS is permissive (same-machine communication)
- No authentication (single-user desktop app)
- HuggingFace token stored in `backend/.env` (gitignored)
- API keys for cloud providers stored in `backend/.env` (gitignored)
