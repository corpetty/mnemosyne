# API Reference

The Mnemosyne backend runs a FastAPI server on `http://127.0.0.1:8008`. All REST endpoints return JSON.
Interactive docs are served at `/docs` (Swagger) and `/openapi.json` while the backend runs.

Long-running work (transcription) never runs inside a request. Endpoints that start such work return a
**Job** immediately; progress and results arrive over the WebSocket, or by polling `/api/jobs/{id}`.

## Health

### `GET /health`

```json
{ "status": "ok", "version": "0.2.0" }
```

---

## Devices

### `GET /api/devices`

List available PipeWire audio devices.

```json
[
  { "id": 76, "name": "alsa_input....", "description": "Built-in Audio Analog Stereo",
    "media_class": "Audio/Source", "is_input": true, "is_output": false, "is_monitor": false },
  { "id": 42, "name": "alsa_output....", "description": "Built-in Audio Analog Stereo",
    "media_class": "Audio/Sink", "is_input": false, "is_output": true, "is_monitor": false }
]
```

Input devices are microphones. Output devices are selected to capture system audio via their monitor source.

---

## Audio Recording

### `POST /api/audio/start`

```json
{ "device_ids": [76, 42], "session_id": "a1b2c3d4" }
```

`session_id` is optional; a new session is created if omitted. Spawns one `pw-record` per device
(48 kHz mono 16-bit WAV). Session status becomes `recording`.

**Response:** `{ "session_id": "...", "recording_id": "...", "live_job_id": "..." | null, "message": "..." }`

When `live_transcription` is enabled a `live` job starts alongside the recording and streams
provisional text (see WebSocket `live_*` events). It is cancelled by stop.

**Errors:** `400` no devices, `404` unknown session, `409` session already recording.

### `POST /api/audio/stop/{session_id}`

Optional body: `{ "transcribe": true | false }`. Omit to follow the `auto_transcribe` setting.

Terminates capture, encodes each source to OGG/Opus, records each as a `Recording` on the session
(`source` is `mic` for input devices and `system` for output monitors), mixes all sources into a single
`audio_file`, and by default queues a transcription job.

```json
{
  "session": { "...full SessionDetail..." },
  "job_id": "j1k2l3m4",
  "message": "Recording stopped. 2 source(s) captured."
}
```

`job_id` is `null` when transcription was not queued.

### `GET /api/audio/status/{session_id}`

```json
{ "session_id": "a1b2c3d4", "is_recording": true, "exists": true, "device_count": 2 }
```

---

## Sessions

Session `status` is one of: `created`, `recording`, `encoding`, `transcribing`, `completed`, `error`.

### `GET /api/sessions`

Summary list, newest first. Never includes transcripts.

```json
[
  { "id": "a1b2c3d4", "name": "Team Standup", "status": "completed",
    "created_at": "2026-02-18T10:30:00", "updated_at": "2026-02-18T11:00:00",
    "has_transcript": true, "has_summary": true, "participant_count": 3 }
]
```

### `POST /api/sessions`

`{ "name": "Team Standup" }` (optional). **Response:** `SessionDetail`.

### `GET /api/sessions/{session_id}`

```json
{
  "id": "a1b2c3d4",
  "name": "Team Standup",
  "status": "completed",
  "created_at": "2026-02-18T10:30:00",
  "updated_at": "2026-02-18T11:00:00",
  "audio_file": "/path/to/data/recordings/a1b2c3d4/e5f6g7h8_mixed.ogg",
  "recordings": [
    { "id": "r1", "source": "mic", "device_id": 76, "device_name": "Built-in Mic",
      "path": "/path/.../e5f6g7h8_device_76.ogg", "created_at": "..." },
    { "id": "r2", "source": "system", "device_id": 42, "device_name": "Speakers",
      "path": "/path/.../e5f6g7h8_device_42.ogg", "created_at": "..." }
  ],
  "transcript": [
    { "text": "Good morning everyone.", "speaker": "SPEAKER_00", "start": 0.5, "end": 2.1,
      "words": [ { "word": "Good", "start": 0.5, "end": 0.8, "score": 0.95 } ] }
  ],
  "summary": "## Meeting Summary\n...",
  "notes": "User notes here",
  "participants": ["SPEAKER_00", "SPEAKER_01"]
}
```

### `PATCH /api/sessions/{session_id}`

`{ "name": "New Name" }` → `SessionDetail`.

### `DELETE /api/sessions/{session_id}`

Deletes the session, its transcript, and its recordings directory. → `{ "message": "Session deleted" }`

### `POST /api/sessions/{session_id}/notes`

`{ "notes": "..." }` → `SessionDetail`.

### `POST /api/sessions/{session_id}/transcribe`

Queue a transcription job for the session's `audio_file`. **Response:** `Job`.

**Errors:** `400` no audio, `404` unknown session, `409` a job is already active for the session.

---

## Jobs

```json
{
  "id": "j1k2l3m4",
  "kind": "transcribe",
  "session_id": "a1b2c3d4",
  "status": "running",
  "message": "Transcribing...",
  "progress": null,
  "error": null,
  "result": null,
  "created_at": "...", "started_at": "...", "finished_at": null
}
```

`kind`: `transcribe` (final pipeline) or `live` (provisional text while recording).
`status`: `queued`, `running`, `completed`, `failed`, `cancelled`. Only one `transcribe` job runs at a time;
others wait in `queued`.

- `GET /api/jobs?session_id=&active_only=` list jobs
- `GET /api/jobs/{job_id}`
- `POST /api/jobs/{job_id}/cancel` (`409` if not running)

---

## Models & Summarization

### `GET /api/models`

```json
[
  { "provider": "ollama", "models": ["llama3.1:latest", "qwen3:32b"] },
  { "provider": "vllm", "models": [] }
]
```

Cloud providers appear only when their API key is set. Embedding-only Ollama models are filtered out.

### `POST /api/sessions/{session_id}/summarize`

```json
{ "provider": "ollama", "model": "llama3.1:latest" }
```

Both fields optional; blank values fall back to `default_provider` / `default_model` from settings, and a
blank model uses the provider's first listed model. Runs synchronously and saves the summary to the session.

**Response:** `{ "summary": "...", "provider": "ollama", "model": "llama3.1:latest" }`

---

## Export

### `POST /api/sessions/{session_id}/export/obsidian`

Writes `<vault>/<subfolder>/YYYY-MM-DD-<name>.md` using the configured vault.

**Response:** `{ "path": "...", "message": "Exported successfully" }`
**Errors:** `400` vault not configured or missing, `404` session.

---

## Settings

Settings are loaded from environment variables (highest precedence, including `backend/.env`), then the
user config file (`$XDG_CONFIG_HOME/mnemosyne/config.toml`, or `MNEMOSYNE_CONFIG_FILE`), then defaults.

### `GET /api/settings`

```json
{
  "values": {
    "data_dir": "/home/me/mnemosyne/data",
    "hf_token": "",
    "whisper_model_size": "medium.en",
    "whisper_compute_type": "float16",
    "whisper_batch_size": 8,
    "auto_transcribe": true,
    "ollama_url": "http://localhost:11434",
    "vllm_url": "http://localhost:8000",
    "openai_api_key": "",
    "anthropic_api_key": "",
    "default_provider": "ollama",
    "default_model": "",
    "obsidian_vault_path": "",
    "obsidian_subfolder": "meetings/mnemosyne"
  },
  "secrets_set": { "hf_token": true, "openai_api_key": false, "anthropic_api_key": false },
  "env_overrides": ["hf_token"],
  "config_file": "/home/me/.config/mnemosyne/config.toml",
  "obsidian_vault_exists": false
}
```

Secret values are never returned; `secrets_set` says whether each is configured. `env_overrides` lists
fields currently forced by the environment (saving them has no effect until the variable is removed).

### `PUT /api/settings`

Partial update; only provided fields change. For secret fields, `""` keeps the current value and `null`
clears it. Persists to the config file (mode 0600) and applies immediately: providers are rebuilt, and the
transcription engine is unloaded if its configuration changed.

**Response:** same shape as `GET`. **Errors:** `422` invalid value.

---

## WebSocket

### `ws://127.0.0.1:8008/ws`

A read-mostly event stream. The only client message is `{ "type": "ping" }` (answered with `pong`).
Work is started over HTTP.

On connect the server sends a snapshot of in-flight jobs:

```json
{ "type": "hello", "jobs": [ { "...Job..." } ] }
```

Then every backend event, in order:

| `type` | Payload | When |
|---|---|---|
| `job` | `job: Job` | Any job state or message change |
| `session` | `session_id`, `status` (session status or `"deleted"`) | Session created, status changed, deleted |
| `transcription` | `session_id`, `segment: TranscriptSegment` | Each segment as the engine yields it |
| `status` | `session_id`, `message` | Human-readable stage text (`Loading models...`, `Transcribing...`, `Transcription complete`) |
| `error` | `session_id`, `message` | A stage failed |
| `live_status` | `session_id`, `message` | Live transcriber state (`Loading live transcriber...`, `Live`) |
| `live_segment` | `session_id`, `source` (`mic`/`system`/`mixed`), `segment` | A provisional segment committed by the live transcriber (absolute times, no words) |
| `live_partial` | `session_id`, `source`, `speaker`, `text` | The still-changing tail for that source; replaces the previous partial (may be empty) |
| `pong` | | Reply to `ping` |

Clients should filter `transcription`/`status`/`error`/`live_*` by `session_id` and use `job` events for
state. Live segments are provisional: discard them when the session's final `transcribe` job starts.
