# Architecture

Mnemosyne is a desktop application for real-time audio transcription, speaker diarization, and LLM-powered summarization. It runs as a native Linux window with a Python backend for ML inference.

## System Overview

```
┌─────────────────────────────────────────────────────┐
│  Tauri v2 Shell (Rust)                              │
│  - Native window management (1200x800 default)      │
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
Frontend ──HTTP REST──> FastAPI Backend
Frontend ──WebSocket──> FastAPI Backend (real-time transcription)
Backend  ──PipeWire───> System Audio (pw-record, pw-dump)
Backend  ──HTTP───────> Ollama/vLLM/OpenAI/Anthropic
Backend  ──File I/O───> data/ (sessions, recordings)
Backend  ──File I/O───> Obsidian vault (markdown export)
```

### REST API

The frontend communicates with the backend over HTTP on `127.0.0.1:8008`. All endpoints are prefixed with `/api/` except `/health` and `/ws`.

### WebSocket

A single WebSocket connection at `ws://127.0.0.1:8008/ws` handles real-time transcription streaming. The frontend sends a `transcribe` message with an audio file path, and the backend streams `transcription` messages back as segments are processed.

Message types:
- **Client -> Server:** `{ type: "transcribe", audio_path: "...", session_id: "..." }`
- **Client -> Server:** `{ type: "ping" }`
- **Server -> Client:** `{ type: "transcription", segment: {...}, session_id: "..." }`
- **Server -> Client:** `{ type: "status", message: "...", session_id: "..." }`
- **Server -> Client:** `{ type: "error", message: "...", session_id: "..." }`

## Data Flow

### Recording Flow

1. User selects audio devices (microphone, system audio monitors)
2. Frontend calls `POST /api/audio/start` with device IDs and session ID
3. Backend spawns `pw-record` processes (one per device)
4. When stopped via `POST /api/audio/stop/{id}`:
   - Each WAV recording is converted to OGG/Opus via ffmpeg
   - Multiple sources are mixed into a single file via numpy
   - Session status moves to "processing"

### Transcription Flow

1. After recording stops, frontend sends WebSocket message: `{ type: "transcribe", audio_path: "...", session_id: "..." }`
2. Backend loads WhisperX model (lazy, first use only)
3. Pipeline runs in order:
   - **Transcribe** with faster-whisper (batch size 16)
   - **Align** timestamps with wav2vec2
   - **Diarize** with pyannote speaker-diarization-3.1
   - **Assign speakers** to word-level segments (fill_nearest=True)
4. Each segment is broadcast via WebSocket as it's ready
5. Full transcript is saved to the session JSON file

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
| `transcriptState` | `stores/transcript.svelte.ts` | Transcript segments, speaker colors, processing status |
| `wsState` | `stores/websocket.svelte.ts` | WebSocket connection, auto-reconnect, message dispatch |
| `toastState` | `stores/toast.svelte.ts` | Toast notifications (info, success, error) |

### Reactivity Design

Cross-store communication uses a callback pattern to avoid circular imports:

```
transcriptState.onComplete(() => {
    sessionState.refreshActive();    // reload session from backend
    sessionState.loadSessions();     // refresh sidebar list
});
```

Session switching uses an ID-based guard to prevent infinite `$effect` loops:

```typescript
let lastLoadedSessionId = $state<string | null>(null);
$effect(() => {
    const session = sessionState.activeSession;
    if (session && session.id !== lastLoadedSessionId) {
        lastLoadedSessionId = session.id;
        transcriptState.loadFromSession(session.transcript);
    }
});
```

## Persistence

### Sessions

Sessions are persisted as JSON files in `data/sessions/{id}.json`. Each session contains:
- Metadata (id, name, status, timestamps)
- Audio file path
- Full transcript (array of segments)
- Summary text
- User notes
- Participant list

### Audio Files

Recordings are stored in `data/recordings/{session_id}/`:
- Individual device recordings: `{recording_id}_device_{device_id}.ogg`
- Mixed output: `{recording_id}_mixed.ogg`

Audio is captured as WAV, then converted to OGG/Opus (~12x smaller) via ffmpeg.

## ML Model Management

Heavy ML models (WhisperX, pyannote) are loaded lazily on first use:

1. `model_service.py` uses `TYPE_CHECKING` for compile-time type safety without import-time loading
2. Models are loaded into GPU memory when the first transcription is requested
3. Models remain in memory for subsequent transcriptions
4. The `unload()` method frees GPU memory and clears CUDA cache

This allows the backend to start instantly without waiting for multi-gigabyte model downloads.

## Security Model

- Backend listens only on `127.0.0.1:8008` (localhost only)
- CORS is permissive (same-machine communication)
- No authentication (single-user desktop app)
- HuggingFace token stored in `backend/.env` (gitignored)
- API keys for cloud providers stored in `backend/.env` (gitignored)
