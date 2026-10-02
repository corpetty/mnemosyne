# API Reference

The Mnemosyne backend runs a FastAPI server on `http://127.0.0.1:8008`. All REST endpoints return JSON.
Interactive docs are served at `/docs` (Swagger) and `/openapi.json` while the backend runs.

Long-running work (transcription) never runs inside a request. Endpoints that start such work return a
**Job** immediately; progress and results arrive over the WebSocket, or by polling `/api/jobs/{id}`.

## Authentication (server mode)

Off by default. When the `api_token` setting is set, every `/api/*` request and the WebSocket must
carry it as `Authorization: Bearer <token>`, or `?token=<token>` for `<audio>` elements and `/ws`.
Missing or wrong tokens get `401` (`WWW-Authenticate: Bearer`); the WebSocket is closed with code
4401. `/health`, `/docs`, `/openapi.json`, the phone page `/m` and `POST /api/pairing/redeem` stay
open. Run the backend with `--host 0.0.0.0` to serve a LAN; there is no TLS, so keep it on trusted
networks, or reach it through Tailscale ([remote-access.md](remote-access.md)).

A paired phone has its own token (below), which opens only `POST /api/audio/import` and
`GET /api/pairing/me`; every other path answers it with `401`.


### Recording from a phone
`GET /m` (not under `/api`, always open) is a small mobile page. In a secure context (HTTPS or
localhost) it records in the page with MediaRecorder; otherwise, and always as a second option, it
uses the phone's own recorder through a file input with `capture`. Either way it uploads to
`/api/audio/import`, so the meeting is transcribed like any import. In server mode the page is opened
once as `/m?pair=<code>`: it redeems the code, keeps the device token in `localStorage` and removes the
code from the address. `/m?token=<api token>` from older links still works.

`GET /api/server/phone` → `{reachable, pairing, host, port, urls, note}`: whether a phone can reach
the page (the backend listens on a non-loopback address, from `mnemosyne-backend --host`, or the
`phone_url` setting is set), whether phones pair (an API token is set), and candidate addresses of the
page, `phone_url` first. The addresses never carry a token. Settings → Server mode shows them with a
QR code.

### Pairing phones
Paired devices live in `<data_dir>/paired_devices.json` (0600) as SHA-256 hashes of their tokens,
outside the database, so they are checked even while encrypted meetings are locked.

- `POST /api/pairing/codes` `{kind: "phone" | "desktop"}` (body optional, default phone) →
  `{code, kind, expires_at, urls, invite}`: a one-time code, valid for 10 minutes. For a phone, `urls`
  are the phone page addresses carrying it (`…/m?pair=<code>`); for a computer, `invite` is
  `<iroh ticket>#<code>`, to paste on the other machine (`409` unless remote access is running).
  `409` when no API token is set.
- `POST /api/pairing/redeem` `{code, name, endpoint_id?}` → `{device, token}`. No token needed; the
  code works once. A desktop code needs `endpoint_id`, which the link sidecar sets from the
  connection's verified iroh key; a phone code must not have one. `403` when the code is unknown,
  used, expired or of the other kind (a refusal of the wrong kind does not use it up).
- `GET /api/pairing/devices` → `[{id, name, kind, created_at, last_seen_at}]`. A phone's token opens
  only the phone page's paths; a computer's opens the whole API.
- `DELETE /api/pairing/devices/{id}`: the device's token stops working at once.
- `GET /api/pairing/me` → the device making the request (with its own token), else `404`.
- `GET /api/pairing/remote` → `{enabled, running, endpoint_id, error}`: remote access. With the
  `remote_access` setting on, the backend runs `mnemosyne-link home` (the `link/` crate, found via
  `MNEMOSYNE_LINK_BIN`, `PATH`, or a build in `link/target`), restarting it if it dies, and again
  when `remote_relays` changes (comma-separated iroh-relay URLs, passed as `--relay`; blank means
  n0's public relays; see deploy/relay/). It accepts
  iroh connections from paired computers only (re-reading `paired_devices.json`, so removal takes
  effect at once) and forwards each stream to this backend on `127.0.0.1`.

## Health

### `GET /health`

```json
{ "status": "ok", "version": "0.9.2", "host": "gpu-box", "auth_required": false, "team_mode": false, "cloud_models": true, "consent_required": false, "pid": 4242, "recording": false }
```

`pid` and `recording` are for the desktop shell, which may find a backend already running when it
starts (see below). `team_mode`: a team server (docs/team-server.md), where everyone signs
in. `cloud_models` false: no OpenAI or Anthropic provider exists, whatever else is set.
`consent_required`: recording needs a word on consent first (`require_consent`).

### People on a team server
With `team_mode` on, every `/api` request needs a person's token (or `api_token`, which acts as an
admin). The request then runs as that person (`access.py`): a **member** sees only meetings they
own (`Session.owner_id`, set when they create one) or that are shared with them, a **reviewer** reads all and changes only their
own, an **admin** does everything. Someone else's meeting answers 404 to a member and 403 to a
reviewer who tries to change it. Lists, search, Ask, tasks, people, topics, digests, jobs
(`Job.owner_id`) and the WebSocket stream are filtered the same way; saved questions and digests are
personal. Opening, playing and exporting a meeting adds `viewed` / `played` / `exported` to its
history (at most once per person per half hour).

| Method | Path | |
|---|---|---|
| GET | `/api/users/me` | who this token is (`id` "" for the desktop app or `api_token`), `role`, `team_mode`, `cloud_models`, `supervision` |
| GET | `/api/users` | everyone's name and role; emails and devices for admins |
| POST | `/api/users` | admin: `{name, email, role}` → the person and a one-time invite `code` |
| PATCH | `/api/users/{id}` | admin: `name`, `email`, `role`, `disabled` (the last enabled admin stays) |
| POST | `/api/users/{id}/invite` | admin: a new invite code (valid 7 days, once) |
| POST | `/api/users/{id}/signout` | admin: drop all their tokens |
| POST | `/api/users/redeem` | no token needed: `{code, device}` → `{token, user}` |
| POST | `/api/users/me/signout` | forget this token |

The web app turns `<address>/?invite=<code>` into a token for that browser.

**Sharing** (`services/sharing.py`): a meeting's owner or an admin shares it with people or with
everyone (`session_shares`, `"*"`). Sharing gives read access (and ticking action items done);
the meeting stays its owner's to change. Each change goes into its history (`shared`,
`unshared`) and a `{type: "shares"}` event (no id) tells clients to reload their lists. With
`share_with_invitees` (on by default), a meeting named from the calendar is shared with team
members whose email is among its invitees.

| Method | Path | |
|---|---|---|
| GET | `/api/sessions/{id}/shares` | `{team, people: [{id, name}], can_change}` |
| PUT | `/api/sessions/{id}/shares` | owner or admin: `{team, user_ids}` → the same; exactly these from now on |

**Preferences** (`services/prefs.py`): on a team server each person has their own
`summary_style`, `summary_instructions`, `mention_keywords`, `calendar_ics_url`,
`hubspot_owner_email`, `share_new_meetings` (`"team"` shares everything they create with everyone),
`digest_weekday` and `digest_hour`; unset (null) means the server's setting. Work on a meeting uses
its owner's (summaries, mention alerts, the HubSpot owner of automatic pushes), and the mic channel
of their recordings carries their name; `/api/calendar` and recording-start naming use the
caller's calendar.

| Method | Path | |
|---|---|---|
| GET | `/api/users/me/prefs` | your preferences (400 on the desktop app: it has settings) |
| PUT | `/api/users/me/prefs` | replace them (null for the server's) |
`mnemosyne-backend users add|invite|list` does the same from the server's shell (for the first admin).
Admin-only elsewhere: `PUT /api/settings`, storage report and cleanup, backups, turning encryption
on or off, rebuilding the search index, pairing devices, editing voice profiles, diagnostics.

### Consent
With `require_consent` (always in team mode; `/health` says `consent_required`), both
`/api/audio/start` and `/api/audio/start-browser` need `consent`: `all_parties` (everyone was told
and agreed), `in_person` (everyone in the room was told) or `one_party`; without it they answer
400. It is logged as a `consent` event in the meeting's history with who started the recording.
`consent_script` is the text the app offers to read out.

### Recording from a browser
`POST /api/audio/start-browser` `{sources: ["mic", "system"], sample_rate, labels?, session_id?}`
starts a recording like `/api/audio/start` (a new meeting or a new part), whose audio the browser
sends itself: one WebSocket per source, `/api/record/{recording_id}/{source}` (token as `?token=`),
binary messages of 16-bit little-endian mono PCM at `sample_rate`, appended to that source's WAV.
Stop with `POST /api/audio/stop/{session_id}` after the sockets have sent what they hold. A
reconnect carries on in the same file and replaces the previous connection (closed with 4409);
an unknown recording or source closes with 4404, someone else's meeting with 4403, a stopped
recording with 1000. Sources appear as devices `-1` (mic) and `-2` (system) in `levels` and
`capture_health` events; `/api/audio/restart` answers 400 for them. A browser recording that has
received nothing for 10 minutes is stopped (`recording_stopped`, reason `browser_gone`).

### Records
`services/records.py`. Versions: a replaced transcript or summary is kept (`session_versions`;
reasons `edited`, `speaker X renamed`, `transcribed again`, `summarized again`). Seals: after
transcription, summary and edits, a SHA-256 of the content (transcript lines and summary) and of each
audio file as stored, chained (`session_seals`). `records_retention_years`: within it, deleting a
meeting (`DELETE /api/sessions/{id}?reason=`) or its audio (`DELETE /api/sessions/{id}/audio?reason=`)
needs an admin and a `reason` (400 without, 403 for others), and audio retention skips the meeting.
A legal hold refuses both to everyone (403) and refuses combining. Every deletion is logged in
`deletions`.

| Method | Path | |
|---|---|---|
| GET | `/api/sessions/{id}/records` | `kept_until`, `legal_hold`, `versions`, `verification` (`ok`, `seals`, `chain_head`, `problems`) |
| GET | `/api/sessions/{id}/versions/{version_id}` | an earlier version's transcript lines and summary |
| PUT | `/api/sessions/{id}/legal-hold` | reviewer or admin: `{reason}` holds, `""` lifts (logged in history) |
| GET | `/api/records/deletions?start=&end=` | reviewer or admin: the deletion log |
| POST | `/api/records/export` | `{session_ids}` or `{start, end}` → a `records_export` job; its result has `export_id` |
| GET | `/api/records/exports/{export_id}` | the zip, for whoever started it (or an admin), once |

### Supervision
`services/supervision.py`, on when `review_phrases` has any. Lines of the final transcript with a
phrase from `review_phrases` are flagged (`supervision_flags`). A new transcription replaces a
meeting's flags; edits, speaker renames and combining only add, so editing a line never removes its
flag. Changing the phrases (or turning supervision on) starts a `supervision_scan` job over every
meeting. A meeting is `reviewed` when its last review is newer than its newest flag. All of these are
for reviewers and admins (403 for members); `/api/users/me` has `supervision`.

| Method | Path | |
|---|---|---|
| GET | `/api/supervision` | flagged meetings, unreviewed first: `flags`, `phrases`, `owner`, `reviewed`, last review |
| GET | `/api/sessions/{id}/supervision` | `flags` (`idx`, `start`, `speaker`, `phrase`, `text`), `reviews`, `reviewed` |
| POST | `/api/sessions/{id}/supervision/review` | `{note}`: mark reviewed (history `supervision_reviewed`) |
| POST | `/api/supervision/scan` | admin: a `supervision_scan` job over every meeting (adds flags only) |

### The web app (`web_dir`)
With `web_dir` set to the output of `pnpm build`, the backend serves the web app at every path
outside `/api/`, `/ws`, `/health`, `/m` and the docs, without a token (the data behind `/api/` still
needs one). Its `index.html` carries `<meta name="mnemosyne-backend" content="same-origin">`, so the
app talks to the server it was loaded from.

### Sharing this computer: `GET|PUT /api/team`
The desktop app can make its own computer the team's server (`services/team_host.py`). `PUT
/api/team {enabled: true, name}` makes `name` (default: the system account's) the first admin
(`team_owner_id`, kept when turned on again), gives them the meetings without an owner, turns on
`team_mode` and `share_on_network`, and opens a second listener on all interfaces at `team_port`
(8443) with HTTPS from a self-signed certificate for this machine's names and addresses
(`<data_dir>/tls`, made again when they change). That listener is the same app: it serves the web
app (`web_dir`, bundled with the desktop app as `resources/web`) and needs sign-in like any team
server. On the main port, requests from 127.0.0.1 or ::1 without a token run as the owner, so the
desktop app keeps working without signing in; not when a proxy on this machine passed them on
(another `Host`, or `Forwarded`/`X-Forwarded-*`/`X-Real-IP`, as `tailscale serve` and Caddy send). `{enabled: false}` closes the listener and team mode;
people and meetings stay. Changing it needs an admin on 127.0.0.1 on the main port (else 403); a
port in use answers 409. `GET` returns `enabled`, `running`, `port`, `addresses` (the likeliest
first), `owner`, `firewall_hint` (a firewall-cmd or ufw command, "" when neither is installed),
`error` and `can_change`.

### Backend lifetime (desktop app)
The shell passes its pid in `MNEMOSYNE_APP_PID`; the backend watches it (`api/app_watch.py`). When
the app is gone the backend shuts down, after finishing running jobs. During a recording it keeps
recording for 15 minutes and says so in a desktop notification; an app started in that time takes
the backend over and shows the recording, otherwise the recording is stopped and saved like Stop
(not transcribed) and the backend shuts down. Without `MNEMOSYNE_APP_PID` (server mode) nothing is
watched. A backend binds its port before it starts anything and exits with code 3 when the port is
taken, so a second one never touches the first one's recording.

### `POST /api/system/attach`
`{pid}` → `{watching}`. A relaunched app takes over the backend its predecessor left running: the
backend now ends with that app. `watching` is false for a backend not started by the app, which
stays independent. On start the shell calls `/health`: a backend of its own version is taken over,
an older one is stopped (SIGTERM; its recording is recovered by the new backend), anything else on
the port is an error.

---


### `GET /api/system`
What this machine can do, for the setup wizard: `{gpu_driver, gpu_stack, parakeet, pipewire, ffmpeg,
hf_token, onnx_diarizer, transcriber_in_use, diarizer_in_use, platform}`. `gpu_driver` is an NVIDIA
driver (nvidia-smi or a loadable libcuda); `gpu_stack` means torch and WhisperX are installed;
`parakeet` means onnx-asr is; `onnx_diarizer` means sherpa-onnx is. `*_in_use` is what runs for the
current settings (`transcription/registry.py`): `transcriber = "auto"` (the default) is WhisperX
with a working CUDA GPU, else Parakeet; `diarizer = "auto"` is Nemotron with a GPU, else pyannote
when it is installed and `hf_token` is set, else `onnx` (pyannote segmentation + TitaNet-small on
the CPU via sherpa-onnx, ~50 MB downloaded into `<data_dir>/models/diarization` on first use), else
none. A saved engine whose packages are not installed falls back the same way. The setting
`setup_complete` records that the first-run wizard was finished or skipped; the app opens the wizard
when it is false and there are no meetings yet.

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

Stops capture at once and answers (the session is `encoding`); the slow part runs as a `finish`
job so the UI never waits for it (about 18 s for a 30-minute two-source meeting): it lets the live
transcript make its last pass, encodes each source to OGG/Opus (in parallel), records each as a
`Recording` on the session (`source` is `mic` for input devices and `system` for output monitors),
mixes all sources into a single `audio_file` (off the event loop), encrypts them when encryption is
on, sets the session to `created` and, when `will_transcribe`, queues the transcription job (its id
is in the finish job's `result.transcribe_job_id`). A recording with no audio fails the job and
leaves the session in `error`.

```json
{
  "session": { "...full SessionDetail, status encoding..." },
  "job_id": "j1k2l3m4",
  "will_transcribe": true,
  "message": "Recording stopped; saving it"
}
```

Recording again into a session that already has audio (a pause, a crash, a restart) adds a
**part** instead of replacing anything: each `Recording` carries its `part` and `offset` (where that
part starts on the meeting's timeline), the session's `audio_file` becomes all parts joined end to
end, and only the new part is transcribed (when the earlier ones already are). Its lines are shifted
by the offset and appended, and its speakers are matched to the earlier parts' by voice
(`speaker_match_threshold`), so labels and names carry over; the summary is then redone over the
whole meeting. Crash recovery adds a recovered recording the same way. `POST
/api/sessions/{id}/transcribe` transcribes every part.

`POST /api/audio/start` looks up the calendar meeting to name the session from the cached feed
(refreshed in the background when stale), so starting never waits for a download.

### `GET /api/audio/file/{session_id}?recording=`

Streams the session's mixed audio (or one recording by id) with the right media type and HTTP range
support, so an `<audio>` element can seek. `404` when the session has no audio.

### `POST /api/audio/import`

Multipart form: `file` (audio or video; wav, ogg, opus, mp3, m4a, flac, webm, mp4, mkv, aac, wma),
optional `name` (defaults to the file stem) and `transcribe` (default true). Creates a session,
stores the upload as a `Recording` with `source: import`, transcodes it to the mixed Opus file, and
queues transcription. Response is the same shape as stop-recording (`session`, `job_id`, `message`).
`400` for unsupported, empty, or undecodable files.

### `POST /api/audio/sample`
Imports the bundled sample (70 seconds of AMI meeting ES2004c, CC BY 4.0, credited in the meeting's
notes) as "Sample meeting (AMI corpus)", transcribes it, and summarizes it when the default provider
answers with a model. Same response shape as import.

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
{ "supported": true, "reason": null, "active": true, "enabled": true, "source_node_id": 146,
  "mic": "alsa_input.usb-RODE_Microphones_RODE_NT-USB-00.analog-stereo",
  "mic_description": "RODE NT-USB Analog Stereo", "pending_mic": null }
```

`POST` body `{ "enabled": true|false, "mic": "<node.name>" }` loads/unloads it and persists
`echo_cancel` (and `echo_cancel_mic`) in settings, so it comes back on the next start. `mic` pins the
module's capture side to that microphone (`target.object`, `node.dont-reconnect`); `""` means the
default source, and leaving it out keeps the saved one. Choosing another mic restarts the module,
except while recording: then `pending_mic` is set and the restart happens when the next recording
starts (device ids naming the echo-cancelled source are updated to its new node id). `400` with a
reason when the module or `pw-cli` is missing, or the mic is the echo-cancelled source itself.

### `GET /api/encryption` · `POST /api/encryption/enable|disable|unlock`

Encryption at rest (opt-in). `GET` → `{enabled, locked}`. `POST …/enable` (no body; `409` while
recording or with jobs running, `400` without a system keyring) creates a random 256-bit master
key, keeps it in the Secret Service keyring (one entry per data directory), rewrites the database
with SQLCipher and encrypts every recording, mix and clip to `<name>.enc` (AES-256-GCM in 64 KiB
chunks, keys derived with HKDF). It returns `{recovery_code, files, errors}`; the recovery code (the
master key in base32) is shown only then. From then on new audio is encrypted when a recording stops
or a file is imported; transcription reads private plaintext copies in `$XDG_RUNTIME_DIR` that are
removed afterwards; playback decrypts the requested byte range. Not encrypted: WAVs while recording,
Obsidian notes, `config.toml`, the log. `POST …/disable` decrypts everything and removes the key.
When the keyring does not have the key at startup the backend is *locked*: every other `/api`
route answers `423` until `POST …/unlock {"recovery_code": "…"}` (checked against a fingerprint in
the settings) opens the database and puts the key back in the keyring. `encrypt_at_rest` cannot be
changed through `PUT /api/settings`.

### `GET /api/audio/apps`
Other apps with an open recording stream right now (`Stream/Input/Audio` nodes in `pw-dump`), as of
the last 5-second poll: `[{app, binary, node_id}]`, with friendly names for common meeting apps and
browsers. Our own recorders (`application.name=Mnemosyne`) and `auto_record_ignore_apps` are left
out; empty while `auto_record` is `off`. Changes are published as `meeting_app` events.

### `GET /api/audio/active`
Recordings in progress, so a UI that (re)connects shows them: `[{session_id, started_at (unix
time), device_ids, part, live, live_segments: [{source, segment}]}]`. `live_segments` is the live
transcript so far. `problems` names sources no longer being captured (see below).

### Capture health: `capture_health` events · `POST /api/audio/restart/{session_id}`
While recording, each source is watched (`audio/health.py`): `stopped` when its recorder exits,
`stalled` after 10 s without new audio (silence is audio: EasyEffects writes digital silence),
`ok` when a stalled source delivers again. Each change is a
`{type: "capture_health", session_id, device_id, state, message}` event. `restart` saves what
was recorded (a `finish` job, not transcribed) and goes on recording into the same meeting as its
next part, from the same devices found again by PipeWire node name (restarting the echo canceller
if it went away); it answers like `start`. The copilot's notes carry over.

### `GET /api/audio/status/{session_id}`

```json
{ "session_id": "a1b2c3d4", "is_recording": true, "exists": true, "device_count": 2 }
```

---

## Backups

### `GET /api/backup` · `POST /api/backup` · `POST /api/backup/restore`
A backup (`services/backup.py`) is one tar in the backup folder (`backup_dir`, default
`~/Documents/Mnemosyne backups`): `manifest.json`, `settings.json` (no secrets), a consistent copy
of the database (encrypted when encryption at rest is on; the key is not in it, so elsewhere the
recovery code opens it) and `recordings/` (audio and clips; not WAVs of a capture in progress).
`GET` → `{dir, backups: [{name, created_at, size, sessions, encrypted, app_version}], restore_pending,
last_restore: {name, at, kept_in, error}}`. `POST` starts a `backup` job (409 while recording).
`backup_interval_days` (0 = off) makes one on its own when due, keeping `backup_keep`.
`restore` takes `{name}` (a backup in the folder, never a path) and answers
`{backup, restart_required: true}`: the backend restores on its next start, before opening the
database. What was there is moved to `<data_dir>/pre-restore-<time>/`, never deleted; a failed
restore puts it back. Settings come from the backup except secrets and the backup settings; audio
paths are moved to this machine's recordings folder once the database is open.

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

### `GET /api/sessions/{session_id}/copilot` · `POST /api/sessions/{session_id}/copilot/ask`
Live copilot. While a session records with the live transcript on and the `copilot` setting on, a
`copilot` job keeps running notes `{session_id, summary, decisions, action_items: [{text, owner}],
open_questions, lines, updated_at}`: the first as soon as there is enough speech, then at most every
`copilot_interval_seconds` (default 180), each call folding only the lines since the last update into
the previous notes. Updates arrive as `copilot_notes` events and are saved with the session
(`Session.copilot_notes`, cleared when a new recording of it starts); GET returns the latest (or
`null`). Summarizing passes the notes to the model as a hint, and copilot to-dos with no matching
summary item are appended to `action_items` with `live: true`.
POST `{"question": "..."}` queues a `copilot_ask` job answering from the transcript so far (the most
recent ~14k characters and the notes), result `{question, answer, asked_at}`. Local-only meetings get
no copilot when the default model is a cloud one.

### `GET /api/sessions/{session_id}/stats`
Talk time from the transcript: `{duration_seconds, speech_seconds, silence_seconds, turns,
speakers: [{speaker, talk_seconds, share, turns, longest_turn_seconds, words, words_per_minute}],
timeline: [{speaker, start, end, first_idx}]}`. A turn is a run of consecutive lines by the same
speaker; `speech_seconds` counts overlapping speech once.

### `PUT /api/sessions/{session_id}/local-only`
Body `{"local_only": true}`. A local-only meeting is never sent to a cloud LLM provider (`openai`,
`anthropic`; Ollama and vLLM count as local): summarize and follow-up return 400 for a cloud
provider (and their jobs refuse too), glossary LLM correction is skipped, and Ask, digests and topic
threads leave it out when they use a cloud provider. `SessionSummary.local_only` shows it in lists.

The MCP server (`mnemosyne-mcp`) never returns local-only meetings: they are left out of its list,
search and action-item tools, `get_meeting` answers with a short note, and its Ask requests set
`exclude_local_only` (below), since its answers go to an assistant.

With the `cloud_redaction` setting, every prompt to a cloud provider has emails, phone numbers and
the names of known people (participants, invitees, owners, saved voices) replaced by `[EMAIL_n]`,
`[PHONE_n]`, `[PERSON_n]`, and the reply is restored before it is parsed. Full multi-word names match
in any case; single-word names and the parts of full names match only as written ("Will" but not
"will"). Phone numbers are 9 to 15 digits that are not dates or thousands-grouped amounts.
Identifiers become `[SSN_n]`, `[ACCOUNT_n]`, `[ROUTING_n]`, `[CARD_n]`, `[DOB_n]` first.

Identifiers (summarization/privacy.py): Social Security numbers (123-45-6789, or 9 digits
after "social"/"SSN"), account numbers (6 to 17 digits that are not money, dates, years, times or
phone numbers; "account ending 1234"), routing numbers (9 digits passing the ABA checksum), card
numbers (13 to 19 digits passing Luhn) and dates of birth introduced as such ("born on", "DOB").
Spoken digits ("four five six, seventy-eight, double nine") count after a cue word, which may be at
the end of the previous transcript line. With `redact_exports` (on by default) the Markdown export,
Obsidian notes, daily note entries and person notes show them as `[SSN]`, `[account ••1234]`,
`[routing number]`, `[card ••4242]`, `[date of birth]`; with `redact_stored_transcripts` (off by
default) the transcript is saved that way (its words too) when a meeting is transcribed, so
summaries, search and everything after only see the markers. The audio is never changed.

### `PATCH /api/sessions/{session_id}`

`{ "name": "New Name" }` → `SessionDetail`.

### `DELETE /api/sessions/{session_id}`

Deletes the session, its transcript, and its recordings directory. → `{ "message": "Session deleted" }`

### `POST /api/sessions/{session_id}/notes`

`{ "notes": "..." }` → `SessionDetail`.

### Bookmarks: `POST /api/sessions/{session_id}/bookmarks` · `PATCH|DELETE …/bookmarks/{id}`
`{at?, note}` → `{id, at, note, created_at}`. Without `at`, while recording, it marks now (the Mark
button, Ctrl+M, the tray's "Mark this moment", `mnemosyne --mark`). Kept as (part, seconds), so
`at` is always on the meeting's timeline, also after meetings are combined. A session lists its
`bookmarks`; the summary is told to cover those moments; the Obsidian note lists them.
Changes publish `{type: "bookmarks", session_id}`.

### History: `GET /api/sessions/{session_id}/history` · `POST …/recover`
What a meeting is made of and what happened to it (services/history.py): `parts` (each with its
`offset`, `seconds`, wall-clock `started_at`/`ended_at` (`approximate` when worked out from when it
was saved), `gap_before` (seconds not recorded), `how` (recorded, recovered, imported, combined,
recording) and `files` with sizes), `recorded_seconds`, `gap_seconds`, `pending` (audio recorded
but not in the meeting yet: an interrupted recording or a failed save, `waiting` or `saving`),
`missing` (files it names that are gone), `orphans` (audio in its folder it does not use) and
`events` (`{at, kind, part, detail}`: created, recording_started, recording_stopped with a
`reason` of stop/capture_restart/app_gone/shutdown, part_saved, save_failed, interrupted,
recovered, recover_empty, recover_failed, capture_problem, capture_ok, imported, combined,
transcribed, transcribe_failed, summarized, summarize_failed, audio_deleted). New events publish
`{type: "history", session_id}`. `recover` saves the pending audio as the next part (a `recover`
job); 404 when nothing is waiting, 409 while the meeting is recording or busy.

### Agenda: `PUT /api/sessions/{session_id}/agenda`
`{items: [{text, covered}]}` → the items. Also taken from the calendar event's description at
recording start (the list under "Agenda", else its list items) when the meeting has none. While
recording, the copilot's notes carry `agenda_covered` (1-based) and the session's items are marked
covered. The summary lists points not discussed under open questions; the note has a checklist.

### Meeting types: `PUT /api/sessions/{session_id}/meeting-type`
`settings.meeting_types`: `[{name, match, summary_style, instructions, obsidian_folder,
local_only, auto_record}]`, `match` being comma-separated words found in a title. A session gets
`meeting_type` when it is named (created with a name, renamed, from the calendar or its summary)
unless one was chosen; `{name}` chooses one (`"none"` for none). The type sets the summary style
and adds its instructions, chooses the Obsidian folder, makes the meeting local-only, and
`auto_record` makes the UI record such a calendar meeting when it starts.
`GET /api/settings` lists every type a meeting can have in `meeting_type_names`.

### Glossary from corrections: `POST /api/glossary/suggest` · `POST /api/glossary/corrections`
`suggest {before, after}` → `[{heard, correct}]`: short phrases a transcript edit replaced with one
that has a capital letter. `corrections {heard, correct, session_id?}` adds `heard -> correct` to
the glossary and fixes that meeting's other lines → `{glossary, fixed_lines}`.

### Resources: `/api/assets` · `/api/sessions/{session_id}/assets`
A library of links and files shared by meetings (`services/assets.py`). `GET /api/assets?q=` →
`[{asset, used}]`; `POST /api/assets/link {url, title?, session_id?}` (one entry per address);
`POST /api/assets/file` (multipart `file`, `title?`, `session_id?`; text read from text, Markdown,
HTML, Word and PDF); `GET /api/assets/{id}/file`; `PATCH /api/assets/{id} {title}`;
`DELETE /api/assets/{id}` (from every meeting). `POST /api/sessions/{id}/assets {asset_id}`
attaches, `DELETE …/assets/{asset_id}` detaches. A session lists its `assets`. Links in the
calendar invite are attached at recording start (not the call's join link). The summary gets
titles and text excerpts; the note links them and copies files to `attachments/`. Files are under
`<data_dir>/assets`, encrypted with encryption at rest, and in backups. Changes publish
`{type: "assets", session_id}`.

### Other assistants' notes: `POST /api/sessions/{session_id}/external-notes` (+ `/file`, `DELETE …/{id}`)
`{text, source?}` or a file → `{id, source, text, filename, added_at}`; `source` is recognized when
blank (Gemini, Zoom, Otter, Teams Copilot, Fireflies, Fathom...). The summary uses them as context
(the transcript wins); a meeting with notes and no transcript is summarized from the notes
(no item times).

### `POST /api/sessions/{session_id}/combine`
`{other_id}` → a `combine` job (`services/combine.py`): every part of both meetings in the order
it was recorded, one joined audio and transcript; the other meeting's speakers matched to this
one's by voice (a name given there and not matched stays). Its recordings and clips move into
this meeting's folder, its notes are appended, the stricter `local_only` wins, and it is deleted.
A summary is made again when either had one. 400 without audio, 409 while recording or busy.
`POST /api/audio/import` with a `session_id` form field adds a file to that meeting as its next
part instead of making a new meeting (only that part is transcribed).

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

### `POST /api/sessions/{session_id}/speakers/reviewed`

`{ "reviewed": true }` (the default). Sets `Session.speakers_reviewed`, which hides the Transcript
tab's "Who is who?" card (one row per `SPEAKER_nn` / `Speaker n` label with a sample and a name
field). A new transcription of the session resets it; meetings from before the flag existed count as
reviewed. **Response:** `SessionDetail`.

### `POST /api/sessions/{session_id}/clip` · `GET …/clips/{clip_id}` · `POST …/clips/{clip_id}/save`

Share a quote. `POST …/clip` `{ "first_idx": 1, "last_idx": 3, "audio": true }` returns
`{ text, clip }`: `text` is a markdown blockquote (`> **Ana** [12:31]: …` per line, then the meeting
name and date); with `audio` and a mixed recording, `clip` is `{id, filename, seconds}` for an Opus
cut of those lines (0.3 s padding, at most 10 minutes) saved in `recordings/<id>/clips/`, so it
counts in the storage report and goes with the session's audio. `GET …/clips/{id}` downloads it
(named after the meeting and where the quote starts); `POST …/clips/{id}/save` `{ "path": … }`
copies it to a path chosen in the desktop app's save dialog (loopback clients only, `403`
otherwise). `400` for lines outside the transcript.

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

### `GET /api/search?q=...&limit=20&mode=hybrid|keyword`
Keyword search (FTS5) over transcript lines and session name/summary/notes, plus, in the default
`hybrid` mode, meetings that match by meaning. Results are fused by reciprocal rank. Each hit has
`match: "keyword" | "semantic" | "both"`; segment hits found by meaning have `semantic: true` and a
plain snippet (keyword snippets mark matches with `[[ ]]`).

In the keyword part every term is required; the last term matches as a prefix so results appear while typing. Matches are
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

### `GET /api/search/index` · `POST /api/search/index/rebuild`
Semantic index status: `{enabled, model, ready, indexed_sessions, total_sessions, pending, error}`.
Rebuild drops all vectors and re-embeds every meeting in the background. Meetings are split into
windows of up to 8 lines / 600 characters plus one chunk for the summary (with decisions, items,
questions and chapter titles), embedded with `embedding_model` (model2vec, default
`minishlab/potion-retrieval-32M`, downloaded once) and re-indexed a few seconds after any `session`
event. With `semantic_search` off, or when the model cannot load, search and Ask use keywords only.

## Ask across meetings

### `POST /api/ask`

```json
{ "question": "When does the Waku migration ship?", "provider": "", "model": "", "exclude_local_only": false }
```

`exclude_local_only` leaves local-only meetings out even with a local provider (they are always
left out for a cloud provider).

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
once it is due, unless that week already has one or has no summarized meetings. On a team server
each person gets their own, on their schedule (their `digest_weekday`/`digest_hour` preferences,
else the server's), from the meetings they can read, saved as theirs.

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

Where meetings come from is `calendar_source`: `off` (no calendar: `configured` is false and the UI
stops polling), `ics` (`calendar_ics_url`, the default) or `desktop`: the calendars of GNOME Online
Accounts and evolution-data-server, read over D-Bus (for work calendars that cannot publish an ICS
address; the account is added in GNOME Settings -> Online Accounts). `calendar_desktop_calendars`
is a comma-separated list of their ids, blank for all; `GET /api/calendar/desktop` lists them as
`[{uid, name, account}]` (empty without evolution-data-server). A refresh reads yesterday to two
weeks ahead. `auto_record_calendar` (default on) lets auto-record start when a calendar meeting
begins; off, only meeting apps opening the microphone start it.

With `calendar_auto_name` (default on), a session that is still untitled when it is created or when
recording starts is renamed to `current.title` and gets its `attendees`. Attendees are passed to the
summary prompt as context (without mapping them to speaker labels) and exported as `attendees:`
links in Obsidian frontmatter.

### `PUT /api/sessions/{session_id}/attendees`

`{ "attendees": ["Alice", "Bob"] }` → `SessionDetail` (trimmed, de-duplicated).

---

## Action items across meetings

### `GET /api/action-items?status=open|done|all&owner=&due=overdue|week&mine=`
Every action item of every summarized meeting the caller can read, newest meeting first (default
`status=open`; `owner` matches case-insensitively): `[{session_id, session_name, created_at, idx,
text, owner, done, issue_url, due, mine}]`. `mine`: the owner is the caller (their full name or
email, or their first name when no one else on the server has it; on the desktop app,
`local_speaker_name`); `mine=true` keeps only those. `idx` is the item's position in that session's `summary_data.action_items`.
`due` (`YYYY-MM-DD` or null) is a deadline named in the meeting: the summary prompt gives the model
the meeting's date and weekday so "by Friday" becomes a date; anything that is not an ISO date is
dropped. Open items with a deadline come first, soonest first; `due=overdue` keeps those past due,
`due=week` those due within seven days. GitHub issues get a "Due:" line, Linear `dueDate`, Jira
`duedate`, and the Obsidian note `📅 YYYY-MM-DD` (the Tasks plugin's format).

### `PATCH /api/sessions/{session_id}/action-items/{idx}`
Body `{"done": true}`. Returns the updated item and publishes a `session` event; 404 for an unknown
session or index. Anyone who can read the meeting may tick its items (someone it is shared with
ticks off theirs); when it is not the owner, `task_done` / `task_reopened` goes into its history. Re-summarizing a meeting keeps `done` and `issue_url` on items whose text matches
the previous summary's (same words, or at least 85% similar), and their `due` when the new summary
names none.

### `GET /api/brief?title=&attendees=&attendees=&exclude=`
What is still open from earlier meetings like this one: up to five, newest first, whose name
matches `title` (ignoring case, punctuation, numbers and dates; generic names such as "Untitled
Session" never match) or that share at least two `attendees` (or the same one or two people).
Returns `{meetings: [{id, name, created_at, match: "title"|"people"|"both"}], open_items: [TaskItem],
open_questions: [{session_id, session_name, text}], last_summary}`; questions and summary come from
the most recent matching meeting that has a summary. `organization`: the organization the meeting
is with (see Organizations), `{id, name, last_meeting, facts, earlier_facts}` with the client facts
of its last meeting that has any, or null. Used by the calendar banner and the Recording tab.

## People

### `GET /api/people`
Everyone seen as a named speaker (renamed from `SPEAKER_n`), a calendar invitee, an action item
owner or a saved voice, matched case-insensitively; generic labels (`SPEAKER_n`, `Speaker 2`,
`UNKNOWN`, `local_speaker_name`, `remote_speaker_name`) are left out. `[{name, meetings, last_seen,
open_tasks, has_voice}]`, most recently seen first.

### `GET /api/people/{name}`
`{name, has_voice, meetings: [{id, name, created_at, role: "speaker"|"invited"|"both", talk_seconds,
share}], total_talk_seconds, open_tasks: [TaskItem], done_tasks: [TaskItem], decisions: [{session_id,
session_name, created_at, text}]}`; decisions come from the last 10 meetings they spoke in. 404 for
an unknown or generic name.

With `obsidian_people_notes` on, exporting a meeting also writes `<subfolder>/people/<Name>.md` for
its people (meetings, open and done tasks). A person who already has a note of that name anywhere
in the vault is skipped, and notes without the `mnemosyne: person` frontmatter are never overwritten.

## Organizations

`services/organizations.py`: people grouped by who they are with (a client, a customer, a partner),
with the client facts (`client` summaries) of their meetings. A meeting is an organization's when a
member spoke in it or was invited
(name or email), when its title names a member by first and last name or names the organization, or
when it was pushed to the organization's HubSpot company. A person is in one organization at most.
Facts and meetings come only from meetings the caller may see.

| Method | Path | |
|---|---|---|
| GET | `/api/organizations` | `[{id, name, hubspot_company_id, members: [{name, email, source}], meetings, last_meeting, facts}]` |
| POST | `/api/organizations` | `{name, members: [{name, email?}]}` → the organization |
| GET | `/api/organizations/{id}` | the organization with `meetings`, `facts` (newest meeting first) and `open_tasks` |
| PUT | `/api/organizations/{id}` | `{name, members}` (replaces them) |
| DELETE | `/api/organizations/{id}` | the grouping only; people and meetings stay |
| POST | `/api/organizations/sync-hubspot` | admin: HubSpot companies and their contacts (each contact's primary company) become organizations, matched by company id; HubSpot members are replaced, hand-added ones kept. Reads only. `{created, updated, organizations}`; 502 on a HubSpot error |

## Topics

### `GET /api/topics?limit=30`
Topics from meeting summaries (`summary_data.topics`, normalized), most meetings first:
`[{topic, meetings, last_seen}]`.

### `GET /api/topics/thread?q=`
Meetings about `q`, oldest first (up to 12): those whose topics name it, keyword hits and
meaning hits, ranked by reciprocal rank fusion. Each has `{id, name, created_at, summary (first
three sentences), chapters, decisions, action_items: [TaskItem], open_questions}`, filtered to the
pieces that match the topic by words or by meaning.

### `POST /api/topics/thread/summary`
Body `{"q": "...", "provider": "", "model": ""}`. Queues a `thread` job: the LLM writes where the
topic stands (current state, how it got there, what is still open). Result `{text, query, meetings}`.

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


## Linear, Jira, Slack and Matrix

`POST /api/sessions/{session_id}/action-items/{tracker}` with `tracker` = `github` | `linear` | `jira`
and body `{"indices": [0, 2]}` creates one issue per item and stores its URL on the item (items that
already have an issue are skipped). Same `IssueResult` for all three. Linear uses a personal API key
and a team key (`linear_api_key`, `linear_team`); Jira Cloud uses `jira_url`, `jira_email`,
`jira_api_token`, `jira_project`, `jira_issue_type` (description in Atlassian Document Format).

`GET /api/integrations` → `{trackers, destinations}`: which have their settings filled in.
`GET /api/integrations/{linear|jira|slack|matrix}/check` → `{ok, message}` (Slack can only check the
webhook address without posting; Matrix checks the token and that the room is joined).

`POST /api/sessions/{session_id}/followup/send` with `{"destination": "slack"|"matrix", "text": ""}`
posts the text (default: the saved follow-up draft) to the Slack incoming webhook
(`slack_webhook_url`) or the Matrix room (`matrix_homeserver`, `matrix_access_token`,
`matrix_room_id`). 400 when not configured or there is nothing to send; 502 on a remote error.

## HubSpot

A meeting in the client's HubSpot record (services/hubspot.py, CRM API v3 on
`https://api.hubapi.com`). Settings: `hubspot_token` (a private app access token, secret; scopes
`crm.objects.contacts.read`/`.write`, `crm.objects.companies.read`, `crm.objects.owners.read`),
`hubspot_owner_email` (the HubSpot user tasks, the meeting and the note are assigned to) and
`hubspot_auto_push`. `GET /api/integrations` lists `crm: ["hubspot"]` when a token is set;
`GET /api/integrations/hubspot/check` → `{ok, message}` (reads contacts and looks up the owner).

`GET /api/sessions/{session_id}/crm/hubspot` → `HubSpotState` `{contacts, meeting_id, note_id,
task_ids, associated, pushed_at}`: what this meeting became in HubSpot (no network).

`GET /api/sessions/{session_id}/crm/hubspot/matches` → `{candidates, searched, state}`: the
contacts confirmed before, then calendar attendees found by email (the calendar's addresses are
kept per session when recording starts; an attendee written as an address counts too), then
attendees and named speakers found by first and last name. Each candidate has `name`, `email`,
`company` (the contact's primary company), `matched_by` (`confirmed` | `email` | `name`).

`POST /api/sessions/{session_id}/crm/hubspot` with `{"contact_ids": ["101"]}` → `{created,
message, state}`: a meeting engagement (title, start/end from the session and its transcript's
length, the summary as HTML), a note (client facts, decisions, open questions) and one task per
action item (due date, done → `COMPLETED`), each associated with the contacts and their primary
companies. The contacts and the created ids are stored with the session (`sessions.crm`); pushing
again PATCHes the same records (and associates contacts added since) instead of duplicating,
and re-creates a record deleted in HubSpot. 400 when not configured, not summarized or no
contacts; 502 with a readable message on a HubSpot error. Every push or failure is logged in the
meeting's history (`hubspot_pushed`, `hubspot_failed`). With `hubspot_auto_push`, each summary job
of a meeting that has confirmed contacts pushes again; a failure never fails the summary (the
job result's `hubspot` holds the outcome).

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
`local` (the built-in model, below) lists the models downloaded.

### The built-in model: `GET /api/local-model` · `POST /api/local-model/download` · `DELETE /api/local-model/{id}` · `POST /api/ollama/pull`
`services/local_llm.py`: provider `local` runs llama.cpp's `llama-server` (build b11323, Vulkan build
when libvulkan loads, else the CPU build) with a GGUF model, both downloaded into
`<data_dir>/models/llm` and checked by SHA-256. The server starts on 127.0.0.1 (a free port) on
the first request, fits the model into free GPU memory (the rest on the CPU), and stops after
`unload_models_after_minutes` idle and with the backend. Requests may take up to 30 minutes (a CPU)
and are not retried. Models: `qwen3-4b` (Qwen3-4B-Instruct-2507 Q4_K_M, 2.5 GB) and
`qwen3-30b-a3b` (Qwen3-30B-A3B-Instruct-2507 Q4_K_M, 18.6 GB, recommended from ~30 GB of RAM).

GET → `{ram_gb, vulkan, server_installed, running, models: [{id, label, size, downloaded,
recommended}], ollama: {reachable, models, suggested}}`. Admin: `download {model}` → a
`model_download` job (progress = share of bytes; the server too, the first time), `DELETE` frees the
model's disk, `ollama/pull {model}` → an `ollama_pull` job that pulls into the Ollama at
`ollama_url` with its progress (`suggested` is the same model in Ollama's library).

### `GET /api/summary-styles`

`[{ "id": "meeting", "description": "..." }, ...]` — `meeting`, `standup`, `interview`, `lecture`, `brainstorm`,
`client`.

### `POST /api/sessions/{session_id}/summarize`

```json
{ "provider": "ollama", "model": "llama3.1:latest", "style": "meeting", "instructions": null }
```

All fields optional: blanks fall back to `default_provider` and `default_model`, and the style and
instructions to the meeting type's, else the owner's preferences on a team server, else
`summary_style` and `summary_instructions` from settings. Queues a `summarize` **Job** and returns it immediately; the
LLM call runs in the background (up to two summaries at once). When it finishes the session's
`summary` (markdown) and `summary_data` are saved, a `session` event fires, and the job completes
with `result: { "provider", "model", "title" }`. Provider errors (unknown provider, no models,
network) fail the job with `error` set rather than failing the request.

The model is asked for JSON; the reply is parsed tolerantly (a non-JSON reply becomes the summary text
with empty structured fields). `summary_data` shape:

```json
{
  "title": "Release planning", "style": "meeting", "provider": "ollama", "model": "llama3.1:latest",
  "topics": ["release"], "decisions": ["Ship Friday"], "decision_at": [95.2],
  "action_items": [ { "text": "Update docs", "owner": "Alice", "at": 301.0, "done": false,
                      "live": false, "issue_url": null } ],
  "open_questions": ["Who reviews?"], "question_at": [null],
  "chapters": [ { "start": 0.0, "title": "Release status" }, { "start": 312.4, "title": "Docs" } ]
}
```

Transcripts longer than `summary_chunk_chars` (default 40000 formatted characters, about 10k
tokens; 0 never splits) are summarized in parts at line boundaries and the partial JSONs are then
merged by one more LLM call (job messages "Summarizing part 2 of 5", "Merging 5 parts"). If the merge
reply is not JSON, the parts are merged directly (deduplicated lists, chapters kept).

`chapters[].start` is snapped to the start of the nearest transcript line (the model is asked for
`MM:SS` timestamps as they appear in the prompt); chapters past the end are dropped. Likewise each
decision, action item and open question gets the time of the line where it came up:
`decision_at[i]` / `question_at[i]` (same order as `decisions` / `open_questions`) and
`action_items[].at`, snapped to a line start, or `null` when the model gave none or it was more
than 20 s from any line.

The `client` style (meetings with a client or customer) also fills `client_facts: [{kind, text,
at}]`, `kind` one of `goal`, `concern`, `preference`, `context`, `next_meeting`, `other` (anything
else the model writes becomes `other`), `at` resolved like `action_items[].at`. Its prompt tells
the model to record only what was said, attributed ("Client said ..."), with no advice of its own.
Other styles leave the list empty. The Obsidian and Markdown exports have a "Client Facts" section grouped by kind.

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

### `POST /api/sessions/{session_id}/followup`
Body `{"style": "email" | "chat", "provider": "", "model": ""}` (defaults: email, default
provider and model). Queues a `followup` job that drafts a follow-up message from the summary,
decisions, action items and open questions. The job result is `{followup, style, provider, model}`;
the text is also saved as `summary_data.followup`. 400 when the session has no summary.

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
  "obsidian_vault_exists": false,
  "meeting_type_names": []
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

On connect the server sends a snapshot of in-flight jobs and of the recordings recovered since the
backend started:

```json
{ "type": "hello", "jobs": [ { "...Job..." } ], "recovered": [ { "...RecoveredRecording..." } ] }
```

A recording is *interrupted* when the backend stops while a session is `recording` or `encoding`
(crash, freeze, forced restart). On the next start a `recover` job per such session stops any
`pw-record` still writing its files, repairs the WAV headers, encodes and mixes them like a normal
stop (sources come from `recording.json`, written next to the audio at start) and sets the session to
`created`, queuing transcription when `auto_transcribe` is on. With no usable audio the job fails and
the session becomes `error`. `RecoveredRecording` is `{session_id, name, seconds, transcribing}`.

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
| `live_relabel` | `session_id`, `old`, `new` | A live speaker was recognised as a saved voice or as a speaker of an earlier part of the meeting, or two live speakers were merged; relabel earlier live lines |
| `live_labels` | `session_id`, `source`, `labels: [{start, old, new}]` | Some live lines of `source` have a better speaker now: with Nemotron streaming (`live_diarizer`), lines shown before the stream had scored their end; with voice clustering, after re-diarizing the recording so far (every `live_rediarize_seconds`). Give the line starting at `start` and labelled `old` the speaker `new` |
| `mention` | `session_id`, `keyword`, `speaker`, `text`, `start` | A live line contained one of `mention_keywords` (whole words, any case; not from your own mic when it is recorded separately; each keyword at most once per 20 s of recording) |
| `meeting_app` | `status` (`started`/`stopped`), `app` | Another app started or stopped recording audio (auto-record) |
| `copilot_notes` | `session_id`, `notes` | The live copilot's running notes were updated |
| `shares` | — | A meeting was shared or unshared; reload the meeting list |
| `live_partial` | `session_id`, `source`, `speaker`, `text` | The still-changing tail for that source; replaces the previous partial (may be empty) |
| `recovered` | `RecoveredRecording` fields | An interrupted recording was recovered (also listed in the next `hello`) |
| `pong` | | Reply to `ping` |

Clients should filter `transcription`/`status`/`error`/`live_*` by `session_id` and use `job` events for
state. Live segments are provisional: discard them when the session's final `transcribe` job starts.
