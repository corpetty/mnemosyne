# API Reference

The Mnemosyne backend runs a FastAPI server on `http://127.0.0.1:8008`. All REST endpoints return JSON.
Interactive docs are served at `/docs` (Swagger) and `/openapi.json` while the backend runs.

Long-running work (transcription) never runs inside a request. Endpoints that start such work return a
**Job** immediately; progress and results arrive over the WebSocket, or by polling `/api/jobs/{id}`.

## Authentication (server mode)

Off by default. When the `api_token` setting is set, every `/api/*` request and the WebSocket must
carry it as `Authorization: Bearer <token>`, or `?token=<token>` for `<audio>` elements and `/ws`.
Missing or wrong tokens get `401` (`WWW-Authenticate: Bearer`); the WebSocket is closed with code
4401. `/health`, `/docs` and `/openapi.json` stay open. Run the backend with `--host 0.0.0.0` to
serve a LAN; there is no TLS, so keep it on trusted networks or behind a reverse proxy.

## Health

### `GET /health`

```json
{ "status": "ok", "version": "0.4.1", "host": "gpu-box", "auth_required": false }
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
(48 kHz mono 16-bit WAV), targeting nodes by name. Output devices are captured from their monitor
ports (`stream.capture.sink=true` on the sink node). Session status becomes `recording`.

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

### `GET /api/audio/file/{session_id}?recording=`

Streams the session's mixed audio (or one recording by id) with the right media type and HTTP range
support, so an `<audio>` element can seek. `404` when the session has no audio.

### `POST /api/audio/import`

Multipart form: `file` (audio or video; wav, ogg, opus, mp3, m4a, flac, webm, mp4, mkv, aac, wma),
optional `name` (defaults to the file stem) and `transcribe` (default true). Creates a session,
stores the upload as a `Recording` with `source: import`, transcodes it to the mixed Opus file, and
queues transcription. Response is the same shape as stop-recording (`session`, `job_id`, `message`).
`400` for unsupported, empty, or undecodable files.

### `GET /api/audio/level/{device_id}?seconds=1`

Records briefly from a device (0.2 to 5 s) and returns `{ "rms_db": -38.2, "peak_db": -21.0 }` (dBFS,
floor -90). Used by the "check" button next to input devices.

### `POST /api/audio/self-test`

`{ "device_id": 69 }` for an output device. Records it exactly as a recording would, plays a short,
quiet 1 kHz tone through it, and reports:

```json
{
  "passed": true, "tone_detected": true, "tone_snr_db": 60.0,
  "level": { "rms_db": -41.0, "peak_db": -20.5 },
  "linked_from": ["alsa_output...:monitor_FL", "alsa_output...:monitor_FR"],
  "captures_monitor": true,
  "message": "System audio capture works: the test tone was recorded from this output."
}
```

`captures_monitor` is false when PipeWire connected the recorder to anything other than this
output's monitor ports (for example a microphone); the message names what it connected to instead.
`400` for input devices, `409` while recording.

While recording, the WebSocket also carries `levels` events every 250 ms:
`{ "type": "levels", "session_id": "...", "levels": { "<device_id>": { "rms_db": ..., "peak_db": ... } } }`.

### `GET /api/audio/echo-cancel` · `POST /api/audio/echo-cancel`

PipeWire WebRTC echo cancellation. The backend loads `libpipewire-module-echo-cancel` in monitor mode
into a long-lived `pw-cli` child (no config files, no daemon restart), which exposes a virtual
`Audio/Source` named "Mnemosyne: mic (echo cancelled)" (`is_echo_cancelled: true` in `/api/devices`).
It lives as long as the backend or until turned off.

```json
{ "supported": true, "reason": null, "active": true, "enabled": true, "source_node_id": 146 }
```

`POST` body `{ "enabled": true|false }` loads/unloads it and persists `echo_cancel` in settings (so
it comes back on the next start). `400` with a reason when the module or `pw-cli` is missing.

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

### `GET /api/sessions/{session_id}/stats`
Talk time from the transcript: `{duration_seconds, speech_seconds, silence_seconds, turns,
speakers: [{speaker, talk_seconds, share, turns, longest_turn_seconds, words, words_per_minute}],
timeline: [{speaker, start, end, first_idx}]}`. A turn is a run of consecutive lines by the same
speaker; `speech_seconds` counts overlapping speech once.

### `PATCH /api/sessions/{session_id}`

`{ "name": "New Name" }` → `SessionDetail`.

### `DELETE /api/sessions/{session_id}`

Deletes the session, its transcript, and its recordings directory. → `{ "message": "Session deleted" }`

### `POST /api/sessions/{session_id}/notes`

`{ "notes": "..." }` → `SessionDetail`.

### `POST /api/sessions/{session_id}/transcribe`

Queue a transcription job for the session's `audio_file`. **Response:** `Job`.

**Errors:** `400` no audio, `404` unknown session, `409` a job is already active for the session.

### `GET /api/sessions/{session_id}/speakers`

Speaker labels in the session and whether a voice embedding is stored for each (only diarized
speakers have one; the local mic channel does not).

```json
[ { "label": "SPEAKER_00", "has_voice": true }, { "label": "Me", "has_voice": false } ]
```

### `POST /api/sessions/{session_id}/speakers/rename`

```json
{ "label": "SPEAKER_00", "name": "Alice", "enroll": true }
```

Renames the label everywhere in this session (segments, participants). With `enroll` (default) and a
stored embedding, the voice is added to the `Alice` profile (running mean), so future sessions label
her automatically. Renaming to an existing participant's name merges them. **Response:** `SessionDetail`.

### Transcript editing

All return the updated `SessionDetail`, recompute `participants`, and emit a `session` event.

- `PATCH /api/sessions/{id}/segments/{idx}` `{ "text"?: "...", "speaker"?: "..." }`. Changing text
  drops that segment's word timings. `400` on empty values, `404` on a bad index.
- `DELETE /api/sessions/{id}/segments/{idx}`
- `POST /api/sessions/{id}/segments/{idx}/merge` merges segment `idx` into the previous one (keeps the
  earlier speaker; `400` for the first segment).
- `POST /api/sessions/{id}/segments/{idx}/split` `{ "offset": 42 }` splits at a character offset; the
  boundary time is the last word before the split when word timings exist, else proportional.

---

## Search

### `GET /api/search?q=...&limit=20`

Full-text search (SQLite FTS5) over transcript segments and session name, summary and notes. Every
term is required; the last term matches as a prefix so results appear while typing. Matches are
wrapped in `[[ ]]` inside snippets.

```json
[
  {
    "session_id": "a1b2c3d4", "session_name": "Planning sync", "created_at": "...", "score": 3.1,
    "session_snippet": null,
    "segments": [ { "idx": 12, "speaker": "Alice", "start": 61.2, "snippet": "…ship the [[release]] on…" } ]
  }
]
```

---

## Ask across meetings

### `POST /api/ask`

```json
{ "question": "When does the Waku migration ship?", "provider": "", "model": "" }
```

Queues an `ask` job (at most two at once) and returns it. The runner retrieves passages with an
any-term FTS query over the question's meaningful words (stopwords dropped, longer words
prefix-matched), widens each matching line by two lines either side, merges overlapping windows (at
most six per session), adds matching session summaries, and sends up to ~16k characters of numbered
excerpts to the LLM with instructions to answer only from them and cite `[n]`. With no matches it
answers without calling the model. The job `result` is the saved `Ask`:

```json
{
  "id": "a1b2c3d4", "question": "...", "answer": "Ships in the second week of October [2] ...",
  "citations": [
    { "n": 2, "session_id": "...", "session_name": "Infra weekly", "created_at": "...",
      "idx": 3, "start": 36.0, "excerpt": "Me: Then let's slip the migration by one week ..." }
  ],
  "provider": "vllm", "model": "qwen3.8-27b", "created_at": "..."
}
```

A citation's `idx`/`start` point at the best-matching line in its excerpt (`null` for a summary).
`400` for an empty or over-2000-character question; provider errors fail the job.

- `GET /api/asks?limit=50` — saved questions and answers, newest first.
- `DELETE /api/asks/{id}`
- `GET /api/ask/passages?q=...` — what retrieval would send for a question (debugging).

---

## Digests

A digest covers the meetings in a date range (normally Monday to Sunday). The LLM writes
the overview, themes and watch list from each meeting's summary; the meeting list,
decisions and action items are copied verbatim from the stored summaries. Only summarized
meetings are included; transcribed but unsummarized ones are listed at the end. With a
vault configured it is also written to `<vault>/<subfolder>/digests/<label>.md`.

### `POST /api/digests`
Body `{"start": "2026-09-21", "end": "2026-09-27", "provider": "", "model": ""}`, all
optional (default: the current week, default provider and model). Returns a `digest` Job
whose result is the saved Digest. 400 if `end` is before `start` or the range exceeds 93
days. The job fails with "No summarized meetings between ..." when there is nothing to
digest. Regenerating a range replaces the earlier digest with the same `label`.

### `GET /api/digests?limit=50` · `GET /api/digests/{id}` · `DELETE /api/digests/{id}`
Digest: `{id, label, start, end, markdown, session_ids, provider, model, path, created_at}`.
`label` is `2026-W39` for an ISO week, otherwise `2026-09-01 to 2026-09-10`.

Scheduled digests: settings `digest_weekday` (0 = Monday .. 6 = Sunday, -1 = off) and
`digest_hour`. The backend checks every 15 minutes and queues the current week's digest
once it is due, unless that week already has one or has no summarized meetings.

## Storage

Only audio is ever removed; transcripts, summaries and notes are kept.

- `GET /api/storage` → `{ data_dir, recordings_bytes, database_bytes, sessions_with_audio,
  retention_days, largest: [{ session_id, name, created_at, audio_bytes, has_transcript }] }`
- `DELETE /api/sessions/{id}/audio` — delete that session's recordings folder and clear
  `audio_file`/`recordings`. Returns the updated `SessionDetail`; `409` while it is recording or has
  an active job. Emits a `session` event with status `audio_deleted`.
- `POST /api/storage/cleanup?dry_run=true&days=` — apply the retention rule (transcribed sessions
  older than `days`, default the `audio_retention_days` setting) and report `{ dry_run, sessions,
  freed_bytes }`. Dry run by default; `400` when retention is off.

With `audio_retention_days` > 0 the backend runs the same clean-up at startup and every 6 hours,
skipping sessions that are recording or have active jobs. Session summaries in `GET /api/sessions`
carry `has_audio`.

---

## Calendar

Configured with the `calendar_ics_url` setting (secret): any provider's private ICS address,
`webcal://` links, or a local `.ics` path. The feed is cached for 10 minutes; on a fetch error the last
good copy keeps being used. Recurring events are expanded; all-day, cancelled, and longer-than-8-hour
events are ignored. Attendee names come from `CN`, falling back to the email's local part; rooms and
resources are skipped.

### `GET /api/calendar?hours=12&refresh=false`

```json
{
  "configured": true, "error": null,
  "current": { "uid": "...", "title": "Daily standup", "start": "...", "end": "...",
               "location": "", "attendees": ["Corey Petty", "Alice Smith"] },
  "upcoming": [ ... ]
}
```

`current` is the meeting in progress (the most recently started one wins when meetings overlap),
otherwise one starting within 10 minutes. `refresh=true` refetches the feed.

With `calendar_auto_name` (default on), a session that is still untitled when it is created or when
recording starts is renamed to `current.title` and gets its `attendees`. Attendees are passed to the
summary prompt as context (without mapping them to speaker labels) and exported as `attendees:`
links in Obsidian frontmatter.

### `PUT /api/sessions/{session_id}/attendees`

`{ "attendees": ["Alice", "Bob"] }` → `SessionDetail` (trimmed, de-duplicated).

---

## GitHub issues from action items

Settings: `github_repo` (owner/name), `github_token` (secret; a fine-grained token with
"Issues: read and write"), `github_labels` (comma-separated, default `meeting-action`).

- `GET /api/integrations/github/check` → `{ ok, repo, can_create_issues, message }`.
- `POST /api/sessions/{id}/action-items/github` `{ "indices": [0, 2] }` → `{ created: [{index, url}],
  skipped: [...], errors: [...] }`. One issue per selected action item (title = the item; body names
  the meeting, date, owner and the meeting's decisions). Each created issue's URL is stored on the
  item (`summary_data.action_items[i].issue_url`), so it is skipped next time. If the repository
  rejects the labels, the issue is created without them. `400` when not configured or no action items.

Obsidian export links created issues next to their task. `MNEMOSYNE_GITHUB_API` overrides the API base
URL (for testing against a stand-in).

---

## Speaker profiles

Known voices, built from renames. Vectors are never returned.

- `GET /api/speakers` → `[{ "id", "name", "sample_count", "created_at", "updated_at" }]`
- `PATCH /api/speakers/{id}` `{ "name": "..." }` (`409` if the name is taken)
- `DELETE /api/speakers/{id}` (existing transcripts keep the name)

Auto-labelling happens at the end of each transcription job when `auto_label_speakers` is on: each
diarized speaker's embedding is compared (cosine) against all profiles and assigned greedily above
`speaker_match_threshold`, one person per label. A `status` event `Recognized Alice, Bob` is emitted.

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

`progress` (0..1) and `message` (current stage, e.g. "Identifying speakers in system audio...") update while a `transcribe` job runs.
`kind`: `transcribe` (final pipeline), `summarize` (LLM summary), `ask` (question across meetings; `session_id` is null), or `live` (provisional text while recording).
`status`: `queued`, `running`, `completed`, `failed`, `cancelled`. Only one `transcribe` job and at most two
`summarize` jobs run at a time; others wait in `queued`.

- `GET /api/jobs?session_id=&active_only=` list jobs. A completed `transcribe` job's `result` is `{ "segments", "sources", "echo_dropped" }`.
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

### `GET /api/summary-styles`

`[{ "id": "meeting", "description": "..." }, ...]` — `meeting`, `standup`, `interview`, `lecture`, `brainstorm`.

### `POST /api/sessions/{session_id}/summarize`

```json
{ "provider": "ollama", "model": "llama3.1:latest", "style": "meeting", "instructions": null }
```

All fields optional: blanks fall back to `default_provider`, `default_model`, `summary_style` and
`summary_instructions` from settings. Queues a `summarize` **Job** and returns it immediately; the
LLM call runs in the background (up to two summaries at once). When it finishes the session's
`summary` (markdown) and `summary_data` are saved, a `session` event fires, and the job completes
with `result: { "provider", "model", "title" }`. Provider errors (unknown provider, no models,
network) fail the job with `error` set rather than failing the request.

The model is asked for JSON; the reply is parsed tolerantly (a non-JSON reply becomes the summary text
with empty structured fields). `summary_data` shape:

```json
{
  "title": "Release planning", "style": "meeting", "provider": "ollama", "model": "llama3.1:latest",
  "topics": ["release"], "decisions": ["Ship Friday"],
  "action_items": [ { "text": "Update docs", "owner": "Alice" } ],
  "open_questions": ["Who reviews?"]
}
```

**Errors:** `400` no transcript or unknown style, `404` session, `409` a summary is already running for
the session.

`summary_data.source_hash` fingerprints the transcript (speakers + text) the summary was made from;
`SessionDetail.summary_stale` is true once the transcript has changed since. With
`obsidian_auto_export` on and a vault configured, the note is exported when the summary lands (the job
result's `exported` is the file path, or `null`; export errors never fail the job).

With the `auto_summarize` setting on, a summarize job is queued automatically after every successful
transcription, using the defaults.

With `auto_name_sessions` on (default), a session still called "Untitled Session" is renamed to
`summary_data.title` when the summary lands; custom names are never overwritten.

---

## Export

### `GET /api/sessions/{session_id}/export/markdown`

`{ "markdown": "..." }` — the note exactly as export would write it (preview, clipboard).

### `POST /api/sessions/{session_id}/export/obsidian`

Writes `<vault>/<subfolder>/YYYY-MM-DD-<name>.md` using the configured vault. The note has YAML
frontmatter (`participants`, `people` as `[[links]]` for named speakers when `obsidian_link_people`,
`topics`, `tags` from `obsidian_tags`, `duration_minutes`), then Summary, Decisions, Action Items as
`- [ ]` tasks with owners, Open Questions, Notes, and the Transcript unless
`obsidian_include_transcript` is off.

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
| `live_segment` | `session_id`, `source` (`mic`/`system`/`mixed`), `segment` | A provisional segment committed by the live transcriber (absolute times, no words); `speaker` is a saved voice's name, `Speaker N`, or the channel label |
| `live_relabel` | `session_id`, `old`, `new` | A live speaker was recognised as a saved voice, or two live speakers were merged; relabel earlier live lines |
| `live_partial` | `session_id`, `source`, `speaker`, `text` | The still-changing tail for that source; replaces the previous partial (may be empty) |
| `pong` | | Reply to `ping` |

Clients should filter `transcription`/`status`/`error`/`live_*` by `session_id` and use `job` events for
state. Live segments are provisional: discard them when the session's final `transcribe` job starts.
