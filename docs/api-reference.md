# API Reference

The Mnemosyne backend runs a FastAPI server on `http://127.0.0.1:8008`. All REST endpoints return JSON.

## Health Check

### `GET /health`

Returns backend status.

**Response:**
```json
{ "status": "ok" }
```

---

## Devices

### `GET /api/devices`

List available PipeWire audio devices.

**Response:** `AudioDevice[]`
```json
[
  {
    "id": 76,
    "name": "alsa_input.pci-0000_00_1f.3.analog-stereo",
    "description": "Built-in Audio Analog Stereo",
    "media_class": "Audio/Source",
    "is_input": true,
    "is_output": false,
    "is_monitor": false
  },
  {
    "id": 42,
    "name": "alsa_output.pci-0000_00_1f.3.analog-stereo",
    "description": "Built-in Audio Analog Stereo",
    "media_class": "Audio/Sink",
    "is_input": false,
    "is_output": true,
    "is_monitor": false
  }
]
```

**Notes:**
- Input devices (`Audio/Source`) are microphones and other capture devices
- Output devices (`Audio/Sink`) can be selected to capture system audio via their monitor source
- Monitor sources are auto-detected and flagged with `is_monitor: true`

---

## Audio Recording

### `POST /api/audio/start`

Start recording from selected audio devices.

**Request body:**
```json
{
  "device_ids": [76, 42],
  "session_id": "a1b2c3d4"  // optional, creates new session if omitted
}
```

**Response:**
```json
{
  "session_id": "a1b2c3d4",
  "recording_id": "e5f6g7h8",
  "message": "Recording started from 2 device(s)"
}
```

**Behavior:**
- Spawns `pw-record` processes for each device
- For sink (output) devices, automatically uses the monitor source
- Records as 48kHz, mono, 16-bit PCM WAV
- Sets session status to `recording`

### `POST /api/audio/stop/{session_id}`

Stop recording and process audio.

**Response:**
```json
{
  "session_id": "a1b2c3d4",
  "recording_id": "e5f6g7h8",
  "output_file": "/path/to/data/recordings/a1b2c3d4/e5f6g7h8_mixed.ogg",
  "individual_files": [
    "/path/to/data/recordings/a1b2c3d4/e5f6g7h8_device_76.ogg",
    "/path/to/data/recordings/a1b2c3d4/e5f6g7h8_device_42.ogg"
  ],
  "message": "Recording stopped. 2 file(s) captured."
}
```

**Behavior:**
- Terminates all `pw-record` processes
- Converts WAV files to OGG/Opus via ffmpeg (64k bitrate)
- Mixes multiple sources into a single file via numpy
- Sets session status to `processing`

### `GET /api/audio/status/{session_id}`

Check recording status.

**Response:**
```json
{
  "session_id": "a1b2c3d4",
  "is_recording": true,
  "exists": true,
  "device_count": 2
}
```

---

## Sessions

### `GET /api/sessions`

List all sessions (summary view).

**Response:** `SessionSummary[]`
```json
[
  {
    "id": "a1b2c3d4",
    "name": "Team Standup",
    "status": "completed",
    "created_at": "2026-02-18T10:30:00",
    "updated_at": "2026-02-18T11:00:00",
    "has_transcript": true,
    "has_summary": true,
    "participant_count": 3
  }
]
```

### `POST /api/sessions`

Create a new session.

**Request body:**
```json
{
  "name": "Team Standup"  // optional, defaults to "Untitled Session"
}
```

**Response:** `SessionDetail` (see GET by ID below)

### `GET /api/sessions/{session_id}`

Get full session details including transcript.

**Response:** `SessionDetail`
```json
{
  "id": "a1b2c3d4",
  "name": "Team Standup",
  "status": "completed",
  "created_at": "2026-02-18T10:30:00",
  "updated_at": "2026-02-18T11:00:00",
  "audio_file": "/path/to/recording.ogg",
  "transcript": [
    {
      "text": "Good morning everyone.",
      "speaker": "SPEAKER_00",
      "start": 0.5,
      "end": 2.1,
      "words": [
        { "word": "Good", "start": 0.5, "end": 0.8, "score": 0.95 },
        { "word": "morning", "start": 0.82, "end": 1.3, "score": 0.97 },
        { "word": "everyone.", "start": 1.35, "end": 2.1, "score": 0.92 }
      ]
    }
  ],
  "summary": "## Meeting Summary\n...",
  "notes": "User notes here",
  "participants": ["SPEAKER_00", "SPEAKER_01"]
}
```

### `PATCH /api/sessions/{session_id}`

Rename a session.

**Request body:**
```json
{ "name": "New Name" }
```

**Response:** `SessionDetail`

### `DELETE /api/sessions/{session_id}`

Delete a session and its data.

**Response:**
```json
{ "message": "Session deleted" }
```

### `POST /api/sessions/{session_id}/notes`

Update session notes.

**Request body:**
```json
{ "notes": "Updated notes content" }
```

**Response:** `SessionDetail`

---

## Models & Summarization

### `GET /api/models`

List available LLM models from all configured providers.

**Response:** `ProviderModels[]`
```json
[
  {
    "provider": "ollama",
    "models": ["llama3.1:latest", "mistral:latest", "qwen3:32b"]
  },
  {
    "provider": "openai",
    "models": ["gpt-4o", "gpt-4o-mini"]
  }
]
```

**Notes:**
- Ollama models are filtered to exclude embedding-only models (BERT family, models with "embed" in name)
- Providers with no API key configured return empty model lists
- vLLM uses OpenAI-compatible `/v1/models` endpoint

### `POST /api/sessions/{session_id}/summarize`

Generate a summary for a session's transcript.

**Request body:**
```json
{
  "provider": "ollama",   // "ollama", "vllm", "openai", "anthropic"
  "model": "llama3.1:latest"  // optional, uses first available if empty
}
```

**Response:**
```json
{
  "summary": "## Meeting Summary\n\n### Key Points\n...",
  "provider": "ollama",
  "model": "llama3.1:latest"
}
```

**Behavior:**
- Formats transcript into text with speaker labels and timestamps
- Selects system prompt based on transcript length (compact for short, detailed for long)
- Saves summary to session on success

---

## Obsidian Export

### `POST /api/sessions/{session_id}/export/obsidian`

Export a session as a markdown file to the configured Obsidian vault.

**Response:**
```json
{
  "path": "/home/user/vault/meetings/mnemosyne/2026-02-18-Team-Standup.md",
  "message": "Exported successfully"
}
```

**Errors:**
- `400` if vault path not configured
- `400` if vault path doesn't exist
- `404` if session not found

### `GET /api/settings/obsidian`

Get current Obsidian vault configuration.

**Response:**
```json
{
  "vault_path": "/home/user/vault",
  "subfolder": "meetings/mnemosyne",
  "exists": true
}
```

### `POST /api/settings/obsidian`

Update Obsidian vault configuration (runtime only, not persisted to .env).

**Request body:**
```json
{
  "vault_path": "/home/user/vault",
  "subfolder": "meetings/mnemosyne"
}
```

**Response:** Same as GET.

---

## WebSocket

### `ws://127.0.0.1:8008/ws`

Real-time transcription streaming endpoint.

#### Client Messages

**Start transcription:**
```json
{
  "type": "transcribe",
  "audio_path": "/path/to/recording.ogg",
  "session_id": "a1b2c3d4"
}
```

**Ping (keepalive):**
```json
{ "type": "ping" }
```

#### Server Messages

**Status updates:**
```json
{
  "type": "status",
  "session_id": "a1b2c3d4",
  "message": "Loading models..."  // or "Transcribing...", "Transcription complete"
}
```

**Transcript segments (streamed one at a time):**
```json
{
  "type": "transcription",
  "session_id": "a1b2c3d4",
  "segment": {
    "text": "Good morning everyone.",
    "speaker": "SPEAKER_00",
    "start": 0.5,
    "end": 2.1,
    "words": [...]
  }
}
```

**Errors:**
```json
{
  "type": "error",
  "session_id": "a1b2c3d4",
  "message": "Transcription failed: ..."
}
```

**Pong:**
```json
{ "type": "pong" }
```
