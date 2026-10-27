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
Per-kind concurrency limits serialize GPU work: only one `transcribe` job runs at a time, and at most two `summarize` jobs (LLM calls to a remote server). With `auto_summarize`, the transcribe runner queues a summarize job when it finishes. The frontend tracks all jobs in `stores/jobs.svelte.ts` from `hello` and `job` events.

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

### Ask across meetings

`storage/sqlite.py::retrieve` ranks transcript windows and session summaries for a question
(any-term FTS5 + bm25, context lines merged, each window remembering its best-matching line);
`services/ask_service.py` numbers the passages into a budgeted prompt, calls the provider's generic
`complete()`, and maps `[n]` markers back to citations. Answers are stored in the `asks` table.
LLM providers expose `complete(system, user, model)`; `summarize` is a wrapper over it.

### Semantic search

`search/index.py::VectorIndex` embeds transcript windows and summaries (`search/embeddings.py`:
model2vec by default, a hashed embedder in tests and demo mode) into `chunk_vectors`, following
`session` events with a short debounce and skipping unchanged chunks by hash. `search/hybrid.py`
fuses those hits with the FTS5 results by reciprocal rank, for `/api/search` and for Ask's passages.
Each embedder has its own `min_score` noise floor (measured, not guessed).

### Auto-record

`AppContext._apps_loop` polls `pw-dump` every 5 s (only when `auto_record` is not `off`) and publishes
`meeting_app` events when another app opens or closes a recording stream (`audio/streams.py`; our
own pw-record streams are tagged `application.name=Mnemosyne`). The decisions live in the frontend
controller, because it owns starting and stopping recordings: offer (ask) or start (auto) on
`meeting_app started` or a starting calendar meeting, and stop an auto-started recording 60 s after
its app stops, 5 minutes after its calendar meeting ends, or after `auto_stop_silence_minutes`
without any source above -50 dB. Recordings started by hand are never stopped automatically.

### Digests

`services/digest_service.py` builds a digest of a date range from the stored summaries
(`repo.sessions_between`). The LLM writes only Overview / Themes / Watch; the meeting list,
decisions and action items are appended verbatim with `[[note|name]]` links whose targets match
`export/obsidian.py::note_stem`. The `digest` job (`pipeline.make_digest`) saves to the `digests`
table (one per label) and writes `<subfolder>/digests/<label>.md` when a vault is set.
`AppContext._digest_loop` checks the `digest_weekday`/`digest_hour` schedule every 15 minutes.

### MCP server

`mnemosyne/mcp_server.py` (`mnemosyne-mcp`, stdio, MCP Python SDK 2.x `MCPServer`) is a thin HTTP
client of the backend (`MNEMOSYNE_URL`, `MNEMOSYNE_TOKEN`), so it works against the local app or a
server-mode backend. Tools: `list_meetings`, `search_meetings`, `ask_meetings` (queues an `ask` job and
polls it), `get_meeting` (paged transcript), `get_action_items`. Output is plain text with session ids
for follow-up calls.

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
| Who spoke | `Diarizer` | `nemotron` (NVIDIA Nemotron-3-Diarization via NeMo, GPU), `pyannote` (community-1, GPU), `none`; `auto` (default) picks nemotron when NeMo and CUDA are available, else pyannote |

`ComposedEngine` (`transcription/composed.py`) joins one of each. `transcription/registry.py` builds it
from settings (`transcriber`, `diarizer`, and per-implementation options). Heavy imports happen inside
the builders, so `remote` + `none` never imports torch.

### Progress

`Transcriber.transcribe` and `Diarizer.diarize` take an optional `progress(fraction)` callback:
Parakeet reports how far through the audio its VAD has reached, WhisperX forwards its batch
`progress_callback` (transcription 85 %, alignment 15 %), pyannote's `hook` is mapped over its steps
(segmentation, speaker counting, embeddings). `ComposedEngine` weights sources by duration (ffprobe),
splits a diarized source 70/30 between transcription and speaker identification, and marshals the
callbacks from worker threads to the event loop. The pipeline throttles updates (stage change, 1 %,
or 1 s) into `Job.progress` and `Job.message`; the UI derives time remaining from `started_at`.

### Glossary

`transcription/glossary.py` parses the `glossary` setting into terms and `wrong -> right`
corrections. The transcribe runner applies corrections (whole words, case-insensitive) to every
final transcript; with `glossary_llm_correct` it then asks the default LLM, in batches of 40 lines,
to fix misrecognised glossary terms, accepting only edits that keep most of the line's words (so a
rewrite is rejected). WhisperX gets the terms as its `initial_prompt` (the model reloads when the
glossary changes); summaries and Ask get a "spell these exactly" instruction.

### Per-source speaker attribution

The recorder keeps each captured source as its own file. `services/pipeline.py::sources_for_session`
turns them into `AudioSource`s:

- mic + system captured: the mic file is labelled `local_speaker_name` (default "Me") and not diarized;
  the system file is diarized. This gives a guaranteed "you" label and keeps diarization to the remote
  side, where it matters.
- mic only: it may contain a whole room, so it is diarized.
- otherwise: the mixed file is used.

Segments from all sources are merged by start time. `transcription/assign.py` maps diarization turns
onto segments by time overlap. With word timings (all our transcribers give them) each word takes its
own speaker and a segment is split where the speaker changes: segments are cut at pauses, not speaker
changes, so one speaker per segment caps word speaker accuracy at 95% even with perfect diarization
(AMI ES2004a-d: 94% per segment, 98.4% per word with pyannote, 99.6% with Nemotron).

### Live transcription

While recording, a `live` job (`transcription/live.py`) tails each source's WAV file as pw-record
writes it, keeps a buffer of not-yet-committed audio per source, and every `live_interval_seconds`
runs a separate `Transcriber` instance (`live_transcriber`, Parakeet on CPU by default) over the
buffer. Segments that end at least one second before the buffer edge are committed and streamed as
`live_segment` events with absolute times; the remainder is sent as a replaceable `live_partial` and
retried on the next tick, so words cut by a chunk boundary are not lost. With mic and system captured
separately the mic is labelled `local_speaker_name` and the system channel is diarized live (see
Live speaker labels). Stop cancels the job (a final flush tick runs) before the final `transcribe`
job replaces everything.

### Echo cancellation at capture

`audio/echo_cancel.py` keeps a `pw-cli` child alive with `libpipewire-module-echo-cancel` loaded in
`monitor.mode`: the default sink's monitor is the AEC reference, so no app has to be rerouted through
a virtual sink. The module's virtual source shows up in the device list flagged `is_echo_cancelled`;
the UI selects it in place of raw mics when the user turns it on. Because the module belongs to our
child process, stopping the backend removes it; the `echo_cancel` setting re-enables it on start.

With `noise_suppression` a second `pw-cli` child loads `libpipewire-module-filter-chain` running
DeepFilterNet's LADSPA plugin (`audio/noise_suppression.py`, downloaded once into `models_dir`):
mic -> WebRTC AEC (mono, WebRTC's own noise suppression off, source `mnemosyne_aec_raw`) ->
DeepFilterNet -> `mnemosyne_aec_source`. Its capture side is passive, so PipeWire suspends the chain
while nobody records from the source. Frames it hears only noise in come out as digital silence.
The plugin aborts its process when it falls a second behind; in its own child that never touches
the canceller or a recording of `mnemosyne_aec_raw`, and a plain copy (filter-chain's builtin
`copy`) takes `mnemosyne_aec_source` over (`EchoCancelManager._watch`).
It is for other apps (calls): our own recordings of the source take `mnemosyne_aec_raw` instead
(`AppContext.recording_targets`, `start_recording(targets=)`) unless `noise_suppression_record` is
on, because denoising raised transcription errors on AMI (docs/plans/2026-10-27-noise-suppression.md).

### Server mode

The frontend keeps its backend URL and token in `localStorage` (`stores/connection.svelte.ts`) and
sends `Authorization: Bearer` on every request, `?token=` on the WebSocket and audio URLs. The backend
enforces the token in `api/auth.py` only when `api_token` is set. Phones pair instead of carrying
that token: a one-time code (`POST /api/pairing/codes`, shown as a QR code) is traded for a
per-device token (`services/pairing.py`, hashed in `<data_dir>/paired_devices.json`) that only opens
the phone page's upload path, and can be revoked on its own. Recording, devices and echo
cancellation act on the machine running the backend; a remote backend is for working with its
sessions, imports and summaries from elsewhere. The Tauri shell still starts a local backend, which is
simply unused while a remote URL is configured.

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

### Live speaker labels

Live sources that may hold several voices (the system channel, or the only source when there is
one) get speaker labels one of two ways, chosen by `live_diarizer` (`registry.resolve_live_diarizer`:
`auto` is streaming when NeMo and CUDA are available).

**Nemotron streaming** (`transcription/diarizers/nemotron_stream.py`, `transcription/live_streaming.py`).
Nemotron-3-Diarization is a Streaming Sortformer: an arrival-order speaker cache and a FIFO of recent
frames carry speaker identity from chunk to chunk, so speaker *k* is the same person for the whole
recording (up to 8 speakers), with no clustering.

```
WavTail.read_new() ──┬─> transcriber buffer ──> committed lines ──┐
   (every sample,    │                                            │ speaker over each line
    silence too)     └─> NemotronStream.push() ─> SpeakerTimeline ┘ (per word, split at changes)
                          0.72 s chunks + 0.32 s        │
                          look-ahead, 10 ms frames      └─> VoiceNamer: earlier parts, voice profiles
```

`NemotronStreamModel` is its own model instance, never the final engine's diarizer: the streaming
settings (the model card's low-latency configuration) live on the model, and a final job during a
recording would otherwise change them under the stream. Each diarized source gets a `NemotronStream`
that resamples to 16 kHz, computes each chunk's mel features from the audio around it (identical to
the whole file's; `tests/test_nemotron_stream.py` checks against NeMo's own chunked pass) and runs one
`forward_streaming_step` per chunk, keeping no growing prediction tensor. Attention goes through SDPA
in these threads (`sdpa_attention`): NeMo's compiled FlexAttention recompiles for every early step and
on Turing picks a kernel that needs more shared memory than the GPU has. On an RTX 2080 Ti a step
takes ~23 ms (about 3% of real time per source) and 590 MiB of VRAM, flat over a meeting.

`SpeakerTimeline` keeps one byte per 10 ms (the most active speaker, or nobody) plus overlap flags.
A committed line takes the speaker most active over it; with word times each word takes its own
and the line is split where the speaker changes (runs of at least two words). A line ending past
what the stream has scored (it runs about a second behind) is re-checked on the next tick and
corrected with `live_labels`. Labels are "Speaker n" per (source, index) (`LiveSpeakerNames`).
`VoiceNamer` embeds a speaker's longest clean turns with the pyannote embedder once they have
3 s of clean speech: a match with a speaker of an earlier part of the same meeting takes that
label back (each part is a new recording, so a new stream; voices are kept in memory in
`AppContext.live_voices` for an hour), and a voice-profile match (`speaker_match_threshold`,
never the local user's name on the system channel) renames it (`live_relabel`); unmatched
speakers are tried again after another minute of speech. More than eight people are merged into
eight live labels; the final transcript is unaffected. If the model cannot load or a step fails,
the source falls back to voice clustering and the live status says so.

**Voice clustering** (`transcription/live_speakers.py`). Each committed segment's audio is embedded
with the embedding model inside the configured pyannote pipeline (community-1's bundled model, 256-d,
~10 ms per segment on GPU), so vectors live in the same space as saved voice profiles.
`OnlineClusterer` assigns segments to running centroids by cosine similarity
(`live_speaker_threshold`, default 0.45), merges clusters whose centroids later converge, and names a
cluster after a saved profile once it matches above `speaker_match_threshold` (one cluster per name;
the local user's profile is excluded when the mic is a separate channel). Renames are sent as
`live_relabel` events and applied to lines already on screen. Segments under 1 s inherit the previous
label. With `live_rediarize` (and Nemotron available) the recording so far is re-diarized every
`live_rediarize_seconds` and changed lines are corrected with `live_labels`
(`transcription/live_rediarize.py`). Without torch/pyannote (CPU-only installs) the live view keeps
channel labels.

`mnemosyne-bench --live` (`bench_live.py`) replays a recording through the live transcript and
scores both ways; with either, the final diarized transcript after stop remains authoritative.

### Model lifecycle

`services/model_service.py` builds the engine lazily on first job (so the API starts without torch),
keeps it loaded across jobs, and drops it when any engine-related setting changes so the next job
rebuilds with the new configuration. `unload()` frees GPU memory.

### Comments

`services/comments.py` stores threads per meeting (`comments` table, FTS-indexed). A comment is
anchored three ways: a time span on the meeting's joined timeline (transcript lines and moments), a
block id (summary decisions, questions and action items) or a text quote with prefix and suffix
(a selection of the rendered summary). Anchors are re-resolved at read time, so editing lines,
re-summarizing or rewording moves a comment to where it now belongs, or marks it detached while
keeping its quote. Comments travel with library sync (merged by id with the rest of a meeting), are
searchable, show in the meeting's history, and publish `comments` events. Tasks and decisions
show the threads of all their mentions.

### Sharing with people (phase 8)

Meetings can be shared with other people's computers (docs/plans/2026-10-23-phase-8-tasks.md).
Each library has one person key (Ed25519, `services/person.py`); two people pair with a contact
invite and trade signed cards (name and machine tickets) plus one shared random secret. Each
derives the CONTACT tokens from it (HMAC per direction), stored as CONTACT devices in
`services/pairing.py`, one per endpoint on the other's card; contacts live in the `contacts`
table and travel in library sync. The owner's computers are the hub: `peer_shares` says which
parts of a meeting a contact gets, and `GET /api/share/changes` builds a `SharedMeeting`
(`services/shares.py`) carrying only those parts, signed with the owner's key. The recipient's
`SharePuller` pulls these over the iroh tunnel (the link sidecar connects to the backend from
127.0.0.2, so auth can tell tunnelled calls from local ones), verifies the signature and writes
the copy with `apply_shared` (`sessions.shared_from`, read-only except comments, ticks and live
notes). What the recipient does goes back as signed contributions to `/api/share/contributions`
(`services/share_push.py`: a `share_outbox` row per comment write or tick, typed notes frames read
off the notes log past a per-meeting cursor; pushed shortly after a write and before each pull
round, idempotent by record id); the hub checks the signature, the contact's share and its parts before applying anything. Audio
never travels. Demo mode seeds a contact and a shared meeting (`services/share_demo.py`).

### Publishing

A curated, encrypted copy of a meeting that anyone with a link can read (phase 7; use in
[publishing.md](publishing.md)). Flow:

```
Publish dialog (PublishDialog.svelte, stores/publishing.svelte.ts)
  ├─ POST …/publication/preview   publishing.preview: the unsealed manifest, drawn by the viewer's own Meeting.svelte
  └─ PUT  …/publication           `publish` job (services/publishing.py)
        build_content (selection, names, redaction; never the transcript)
        → clips cut from the audio (reused when their span is unchanged)
        → seal (publication_format.py: manifest + clip blobs, AES-GCM)
        → host: PUT blobs then manifest to publish-host   |  here: <data_dir>/publications/<id>/
        → `publications` row, history line, records seal
  └─ POST …/publication/file      `publication_file` job: single.html with the sealed copy inlined

Reader: https://<host>/p/<id>#<key>
  → GET /p/<id>            viewer index.html (strict CSP; the backend or the publish host)
  → GET /p/assets/…        the viewer's JS and CSS
  → GET /pub/<id>          sealed manifest (ETag, X-Pub-Version, X-Pub-Expires)
  → GET /pub/<id>/<sha>    sealed clips, immutable
  → WebCrypto decrypts with the fragment's key, which no request carries; clips become blob: URLs
```

Format rules (services/publication_format.py; the reader is viewer/src/format.ts):

- One random 32-byte key per publication, shown only in the link fragment (base64url, 43 chars);
  never in `meta.json`, never in logs, never sent to a host. Ids are 16 random hex characters, not
  the session id. Versions reuse the key, so an update keeps the link.
- The manifest is canonical JSON padded to a multiple of 4 KiB (4-byte length, zero padding); a clip
  is Ogg Opus padded to 64 KiB. Blob = nonce(12) || AES-256-GCM ciphertext || tag. AAD =
  `mnemosyne/pub/1|<id>|<manifest|clip>|<version>`; a clip's version is the first one that uploaded
  it, so later versions can reuse it. A blob's address is the SHA-256 of the whole blob.
- Caps on padded size: manifest 1 MiB, a clip and all clips together 20 MiB.
- A password-protected file derives the key with PBKDF2-HMAC-SHA256 (random salt in the file).
- The manifest has no transcript field at all; quotes are the only transcript text
  (`tests/test_publishing.py` asserts it). Clips are not redacted.

Storage and serving:

- `<data_dir>/publications/<id>/{meta.json, manifest, blobs/<sha>}` is the local copy. `meta.json`
  is all `/pub` needs, so reading works while the library is locked; after an unpublish, expiry or
  deletion it stays as a `gone` tombstone for 30 days (410 instead of 404).
- The `publications` table holds the row (key and host token included; encrypted with the
  database when encryption at rest is on), one live row per meeting. It travels in `SyncSession.publications`
  (services/sync_publications.py; see the library sync rules, section 7).
- The backend serves `/p/` and `/pub/` as open routes (api/routes/publishing.py), also on the team
  listener, with the headers a publish host sends. The publish host (`publish-host/`, axum) is the
  same read interface plus authenticated writes, caps, rate limits and mandatory expiry; it only
  ever holds ciphertext. `deploy/publish/` runs it behind Caddy.
- The viewer (`viewer/`) is its own Vite entry (Svelte 5, Tailwind, no SvelteKit, no wasm) built by
  `pnpm build:viewer` into `backend/mnemosyne/api/static/viewer/` (`index.html`, `assets/`,
  `single.html`), committed and rebuilt in CI, which fails when the output differs.
- Where a link points: a host link uses `publish_host`; a "this computer" link uses the team
  listener, else `phone_url` (`local_reach`); with neither, only 127.0.0.1 can open it.

### Live shared notes

A meeting's notes are one hashseq document that everyone who has the meeting open edits live
(desktop webview and the team server's web app). Design and rationale: phase 6 of
docs/plans/2026-10-12-hashseq-collaboration.md.

```
client (wasm CRDT) <-- binary frames, JSON presence --> /ws/notes/{id} relay
                                                          |  append (author per frame)
                                                          v
                                       shared_notes_ops (log)   shared_notes (snapshot)
                                                          |  2 s after the last frame, 30 s, last leave,
                                                          |  before summarize / export / sync serve
                                       sidecar `notes doc fold`  --> snapshot + Session.notes (markdown)
```

- **Every client runs the CRDT.** `src/lib/notes/pkg` is notes-wasm (hashseq + the notes-core crate),
  committed and rebuilt by scripts/build-notes-wasm.sh. The editor (`src/lib/notes/editor`) edits the
  `WasmHashWeb` through commands (`commands.ts`, `ops.ts`, local undo); `doc.svelte.ts` owns the
  socket, `stores/notes.svelte.ts` shares one document per meeting between the Notes tab, the
  recording card and the Copilot's "Add to notes".
- **The backend is an opaque relay.** It cannot decode hashseq. A join sends a JSON hello, the folded
  snapshot, then the unfolded frames, read together so no frame is lost or doubled. A client frame
  (0xDE delta or 0xAF artifact, at most 2 MB) is appended with its author, relayed to the others
  and never echoed. Presence is JSON. A reconnecting client merges the hello into its live store
  and sets the upstream clock to the server's frontier (`applyFrontier`), so its offline edits
  go out and nothing else does.
- **Folding.** The stateless sidecar (`mnemosyne-link notes doc`, link/src/notes.rs over notes-core)
  folds snapshot + frames into a new snapshot and renders markdown. `Session.notes` is that
  markdown: search, Obsidian, MCP, combine and the summary read it as before, and the summary gets
  it as a `shared_notes_hint`. Text written around the document (the notes route, library sync's
  text merge) is caught up into it, as an `api` edit, before a join or fold. `set_text` diffs
  markdown into ops, empty for an unchanged document; a meeting with notes and no document is
  seeded this way, on its origin machine.
- **Library sync** carries the snapshot in `SyncSession.notes_doc`; two documents merge by union
  (`notes doc merge`, no base), one side only is adopted (open sockets get `reload`). A union that
  fails holds the sync cursor before that meeting and the round goes on.
- **Comments** on the notes are `comment:<id>` marks. The client sends the first and last element ids;
  the server puts the mark (`mark_comment`, frames logged and broadcast) so a read-only reader can
  comment too. Each fold reports every mark's range (`shared_notes.plain`/`comment_ranges`), read
  back as `notes_range`; a deleted range is detached.
- **Access.** Owner, admin and shared-with edit; other readers get a read-only editor and the relay
  refuses their frames (4403). Without the sidecar the hello is `unavailable` and
  LegacyNotesEditor (textarea, `POST /notes`) is used.

Document model (src/lib/notes/model.ts, notes-core/src/lib.rs): one flat seq of lines, each
ending in `\n`; the document always ends with one. Indexes are code points.

| Mark | On | Value | Anchors |
|---|---|---|---|
| `block` | a line's `\n` | `h1 h2 h3 bullet number task task_done quote code table table_head` | closed |
| `lang` | a code run's first `\n` | the fence's info string, raw | closed |
| `align` | a table head row's `\n` | one of `l c r -` per column, e.g. `"lc-r"` | closed |
| `bold`, `italic`, `strike` | characters | `"true"` | grow at the end only (wasm `markRange`) |
| `code` | characters | `"true"` | closed (`markRangeClosed`) |
| `link` | characters | the URL | closed |
| `comment:<id>` | characters | `"true"` | closed |

Table cells are separated by U+001F in the row's text; ragged rows are padded when rendered.
Concurrent values stay live: `task` with `task_done` is done, otherwise the first by mark id wins;
two link URLs render as plain text with "(link conflict)". Mark strings over 14 bytes travel as
0xAF artifact frames. The markdown renderer is canonical and the parser covers what it renders
(HTML and images stay literal), shared by the fold and the client's paste.

### Guest notes (phase 9)

A link that lets the people in a meeting type into its live notes from a browser (use in
[guest-notes.md](guest-notes.md)). The notes keep their home in the backend's relay; a room on a
publish host is only the meeting point.

```
guest page (guest/, WebCrypto, notes wasm)  <-- sealed frames -->  room on the host  <-->  Bridge (services/guest_notes.py)  <-->  /ws/notes relay
https://<host>/n/<room>#<key>                                      /live/{room} (WebSocket)                                       shared_notes.py
```

- **The bridge** (`Bridge`, one asyncio task per live room, outbound WebSocket so NAT is no
  problem). It is a hidden member of the notes room (`shared_notes.attach`); `_deliver` is where
  every local frame ends (owner typing, copilot, phase 8 contributions, the server's comment
  marks), so the bridge seals each one and sends it out with an empty gid and the owner's name.
  Frames coming in are opened and fed to `shared_notes.accept_frames(..., skip=member)` as author
  `guest:<gid>` / "<name> (guest)": logged, folded, relayed to editors, never echoed back. Frames
  carry no author on the way out, so guests see the owner's side as the owner's.
- **Comments.** The page mints a comment id and `notes_ids`; the bridge calls `comments.create`
  with an explicit actor `("guest:<gid>", "<name> (guest)")` (guests edit and delete only their
  own) and marks the text with `mark_comment`, so the mark belongs to the owner's server. After
  each `comments` event the notes' threads are sealed back to the room.
- **Snapshots and META.** The bridge folds the document (`fold_blocking` + `notes_catchup`) and
  seals canonical JSON `{format, notes, frames, threads, meta}` with `PUT /live/{room}/snapshot`
  and `X-Room-Since`, which prunes the host's log: at start, every 3 minutes while something
  changed, and at the end. A META envelope `{title, date, owner, agenda:[{text, covered}], ended,
  origin}` follows the `session` and `copilot_notes` events (`AgendaItem.covered`). The notes
  document's origin travels in SNAPSHOT and META because a browser cannot list a seq's objects.
- **The room** (`publish-host/src/rooms.rs`, routes and WebSocket in `live.rs`). The publisher
  token (plus the upload token on a closed host) creates, snapshots, ends and deletes a room over
  HTTP. A guest joins over WebSocket with `room_token`; the host stores only its hash and compares
  in constant time. After `welcome` it replays the snapshot and the log since `since`, then relays
  `0x01 || seq || envelope`, presence (`0x02`), snapshots (`0x03`), a connection `count` and
  `ended`. Caps: frame 2 MiB + 1 KiB, log 8 MiB, snapshot 8 MiB, 64 connections, rooms at most 7
  days, an ended room readable 24 h. The log is `seq u64 || len u32 || bytes` on disk. Acks come
  back through the relay, so the owner's `since` never skips a frame.
- **Format** (`services/guest_format.py`, mirrored by `guest/src/format.ts`). `frames_key =
  HKDF(key, "mnemosyne/guest/frames")`; `room_token = b64url(HKDF(key, "mnemosyne/guest/room"))`.
  Envelope `kind(1) || nonce(12) || AES-GCM(pad(plaintext))`, AAD `mnemosyne/guest/1|<room>|<kind>`;
  kinds notes=1, snapshot=2, comment=3, meta=4, presence=5. A notes plaintext is `u8 len gid ||
  gid || u8 len name || name || raw frame`. `backend/tests/vectors/guest_v1.json` is checked by
  Python, TypeScript and (the room token) Rust; a format change means bumping the version byte in
  the AAD.
- **The guest page** is its own Vite entry (`guest/`, Svelte 5, base `/n/`) built into
  `backend/mnemosyne/api/static/guest`, committed and diffed by CI like the viewer. Unlike the
  viewer it needs the notes wasm (committed bytes, emitted as an asset) and reuses
  `src/lib/notes` (`createEditorView`, `attachInput`, `UndoStack`, comments) with its own
  `guest/src/doc.ts` on `sync.ts` instead of the app's stores. The page connects before asking
  for a name so the consent line can name the owner, and keeps a mirror of what the room logged
  to resume after a reconnect. CSP: `script-src 'self' 'wasm-unsafe-eval'`, `connect-src 'self'`.
  Served at `/n/{room}` by the host (`PUBLISH_GUEST_DIR`) and the backend (`/n/` is an open
  path), both with `wasm -> application/wasm`.
- **Lifecycle** (`api/routes/guest_notes.py`, rows in `guest_rooms`). One room per meeting; start,
  end, New link and stop write history lines and seal records (`EVENT_SEALS`). Auto-end runs
  `guest_notes_end_hours` after `recording_stopped`, is cleared when a new part starts and never
  fires while recording. Deleting a meeting deletes its room. The host comes from `guest_host`,
  else `publish_host`, else `OUR_GUEST_HOST` (empty until our host runs); none gives 409. Team
  servers need `allow_guest_notes`.

**Phase 10: no host to run.** Guest notes work without a publish host (use and limits in
[guest-notes.md](guest-notes.md)). The rooms sidecar (`mnemosyne-link rooms`, run on demand by
`RoomsLink` in services/link.py, independent of remote access) embeds the publish host on a loopback
port and opens one guest iroh endpoint per room (own key in `<data_dir>/guest_rooms/keys`, never the
remote-access id; ALPN `mnemosyne/guest/1`). Each guest is one framed bidirectional stream
(`u32 length || tag || payload`, up to a full snapshot, 8 MiB + 64 KiB) proxied to the loopback
`/live/{room}` WebSocket, close codes becoming QUIC application closes. The Bridge is unchanged
against it: it speaks to the loopback host as to any publish host. With a configured publish host a
second Bridge mirrors the same room (same id and key) there. `guest_transport` (`plan` in
services/guest_notes.py) picks auto = iroh, else tunnel, else host. A tunnel (services/tunnel.py:
Tailscale Funnel, else a `cloudflared` quick tunnel) sits in front of the same loopback host, so it
needs no second room. In the browser, guest-wasm (iroh compiled to wasm, relay-only, built by CI and
committed as `guest/pkg`) gives `IrohSocket` behind the page's one socket interface, tried first
(about 8 s) before the host's wss, with a `since` per transport. Link format 2 is
`#2.<room>.<key>.<endpoint>.<relay host>`; format 1 links still work. The page is served from GitHub
Pages and keeps the last sealed snapshot in IndexedDB until the link expires.

**Phase 11: the notes editor, live** (use in [guest-notes.md](guest-notes.md); build order in
docs/plans/2026-10-25-phase-11-tasks.md). Presence is one path both ways, none of it stored. A client
sends `{"type":"presence","typing"?,"sel"?}` over `/ws/notes`; `shared_notes.py` keeps `typing` and `sel`
(anchor and focus as element ids, not offsets, so remote edits do not move a caret) on the in-memory `Member`
and sends `presence` (with `members`) and coalesced `caret` messages (one per 100 ms per member, the last
position always arrives). The bridge is a hidden member with `presence_tap`: it also hears `presence` and `caret`,
and seals each owner-side member to the room as PRESENCE gid `o:<member key>` beside the `""` owner heartbeat. A
guest's PRESENCE (`sel`, `gone`) comes back through `Bridge._on_presence` as `guests` (who is there or typing) and
`guest_caret` (only a selection moved) to the editors, and is forwarded byte for byte into the meeting's other
rooms, so guests on the iroh room and on a host mirror see each other (the publish host has its own presence
channel with a cap of 10 per second). The carets are drawn by `src/lib/notes/ui/CaretOverlay.svelte`;
`src/lib/app/notesPresence.ts` and `guest/src/presence.ts` are the two providers.

Chips are `link` marks, not a new mark kind: `[12:34](mnemosyne:t/<part>/<seconds>)`, `mnemosyne:person/<name>`,
`mnemosyne:meeting/<id>` (src/lib/notes/chips.ts `parseChip`; services/notes_chips.py is the strict Python twin).
Everything outside the editor reads text (`plain_notes`, `obsidian_notes`), and combining meetings retargets time
chips. The recording's place on the timeline is the `clock` (`{part, offset, started_at}`, services/parts.py
`recording_clock`) on the start and active-recording responses and in the guests' META.

Notes items (services/notes_items.py): a task line with a person chip is an action item. `Session.notes_items`
is derived at each place a fold result is stored (the sidecar answers with `tasks` for every request), so
nothing keeps a second copy in step; state that is not in the line (`follow`, `due`, `issue_url`, `task_id`) is
carried over by line id, or by text and owner when `set_text` gave a line a new id. They are task objects like
summary items. A tick made outside the editor is written into the document with the sidecar's `set_kind`, which
produces ordinary frames for the connected editors. The editor UI shared by the app and the guest page
(`src/lib/notes/ui/`: `NotesEditor`, the `/` menu, toolbars, touch bar) imports only `src/lib/notes`.

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

### Tray and remote control

`lib.rs` builds a tray (Start/Stop recording, Show, Quit) and registers the single-instance plugin
first: a second `mnemosyne --toggle|--start|--stop` launch hands its arguments to the running
instance, which emits a `tray-action` event; an action given to the first launch is stored and
fetched by the UI with `take_launch_action` once the backend is up. The UI owns recording (device
selection lives there, remembered by PipeWire node name in localStorage) and reports state back
with `set_recording_state`, which updates the tray label and tooltip. A missing tray host only logs a
warning.

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
- The install needs network once, except with the offline AppImage, which carries uv's cache, the
  managed Python and the CPU engines' models (`mnemosyne-backend prefetch`); the shell copies the
  models into the Hugging Face cache and `<data>/models` on first launch (`install_offline_models`).
- On NVIDIA machines the GPU extra waits for the setup wizard's answer (`gpu_support`,
  `start_gpu_install`); setup's last step downloads the chosen models (`POST /api/system/prepare`).
- The AppImage runs the backend from a copy in `<app data>/runtime/<version>`: its own files vanish
  when it exits, and the backend may outlive it (a recording after a crash, a shared computer with
  `keep_sharing_after_quit`).

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
