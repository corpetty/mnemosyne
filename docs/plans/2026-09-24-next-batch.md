# Next batch (2026-09-24, after 0.6.0)

Ten items, built in this order (later ones reuse earlier ones). One commit per item on main,
each with backend tests; UI items also get an e2e check on the demo backend. Not released
until asked.

Checks after each: `ruff check`, `ruff format --check`, `pytest`, `scripts/gen-api-types.sh --check`,
`pnpm check`, `pnpm test:e2e`, and `cargo check` when Rust changes.

## 1. Long meetings (map-reduce summaries)
Today the whole transcript goes to the LLM in one prompt (no chunking), which overflows small
context windows on long meetings. Setting `summary_chunk_chars` (default 40000, about 10k tokens).
Over it, split the formatted transcript at line boundaries into chunks; summarize each with a
"partial notes" prompt that returns the same JSON (with chapters and timestamps); then a reduce
prompt merges the partial JSONs into the final JSON. Progress: "Summarizing part 2 of 5".
Chapters from partials are kept with their timestamps and snapped as today. Demo provider handles
both prompts.

## 2. Semantic search for Ask and search
Hybrid retrieval: keep FTS5/bm25 and add embeddings. `Embedder` protocol in
`search/embeddings.py`; the default is `model2vec` static embeddings
(`minishlab/potion-retrieval-32M`, CPU, no torch), loaded lazily; a fake in tests. Windows of
transcript lines and summaries are embedded into a `chunk_vectors` table (float32 blobs) by an
`index` job after transcription, summary and edits, and on startup for anything missing.
Retrieval merges bm25 and cosine ranks with reciprocal rank fusion. `/api/search` gains
`mode=hybrid|keyword`. Setting `semantic_search` (default on; falls back to keyword if the model
cannot load). Status and "rebuild index" in Settings.

## 3. People pages
`GET /api/people`: everyone seen as a named speaker, an attendee, an action item owner or a voice
profile (SPEAKER_n labels excluded), with meeting count and last seen. `GET /api/people/{name}`:
their meetings (as speaker or invitee), talk time per meeting, open and done tasks they own, and
decisions from meetings they spoke in. A People view in the header. Optional Obsidian person notes
(`obsidian_people_notes`): `<subfolder>/people/<Name>.md` with meetings and tasks, rewritten on
export.

## 4. Topic threads
`GET /api/topics`: frequent topics across meetings (from `summary_data.topics` and chapter titles,
normalized). `GET /api/topics/thread?q=`: meetings about a topic, newest first, via item 2 retrieval
over summaries and chapters, each with the matching chapters, decisions, action items and open
questions. Optional `thread` job: the LLM writes "where this stands" from those pieces. A Topics
view.

## 5. Local-only meetings and redaction
`Session.local_only`: never sent to a cloud provider (openai, anthropic). Summarize, follow-up and
glossary correction refuse with a clear error; Ask and digest leave those meetings out when the
provider is cloud. Setting `cloud_redaction`: before any cloud call, replace emails, phone numbers
and known names (participants, attendees, voice profiles) with placeholders like `[PERSON_1]`, then
restore them in the reply. Implemented as a provider wrapper so every call path is covered. A lock
toggle in the session header.

## 6. Auto-record
Backend polls `pw-dump` every 5 s for capture streams from other apps (browsers, Zoom, Teams,
Slack, Discord...) and emits `meeting_app` events when one starts or stops. Setting `auto_record`:
`off` | `ask` (banner: "Zoom is using the microphone. Record?") | `auto` (start recording with the
remembered devices). Auto-started recordings stop when the triggering app's stream has been gone
for 60 s, when the calendar meeting that started it ended 5 minutes ago, or after
`auto_stop_silence_minutes` (default 10) of silence on every source. Calendar-triggered auto-start
uses the existing "meeting is starting" logic.

## 7. Posting follow-ups and tasks elsewhere
Destinations beside GitHub: Slack (incoming webhook URL), Matrix (homeserver, access token, room
id) for follow-ups; Linear (API key, team) and Jira (site, email, API token, project) for action
items. Each is a small service with a mocked-transport test, a "Test" button in Settings, and
buttons in the Summary tab. Secrets stored like other secrets.

## 8. Recording from a phone
The backend serves a small mobile page at `/m` (token in the URL in server mode). It records with
MediaRecorder when the page is in a secure context (HTTPS or localhost) and otherwise uses the
phone's recorder via `<input type=file accept=audio/* capture>`; either way it uploads to
`/api/audio/import`. Settings → Server mode shows the LAN URL and a QR code.

## 9. Faster first launch and GPU in the Flatpak
Two-phase install in the shell: sync base + onnx, start the backend (Parakeet works), then sync
the GPU extra in the background and restart the backend when idle, with progress in the status
bar. GPU detection also accepts a loadable `libcuda.so.1` (the Flatpak's NVIDIA GL extension has
it but no `nvidia-smi`).

## 10. Quality benchmark
`mnemosyne-bench`: transcribe audio with the configured engine and score it against a reference:
word error rate, speaker attribution accuracy (best label mapping), runtime and real-time factor.
References can be a JSON transcript, or any session you have corrected in the app
(`--session <id>`: its edited transcript is the reference). `--synthetic` makes a two-voice
test clip with espeak-ng. Results as a table or JSON.
