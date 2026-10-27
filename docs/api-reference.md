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
- Contacts (another person's computers, services/contacts.py; admin only, `409` on a team server,
  without an API token or without remote access): `POST /api/contacts/codes` → the same
  `{code, kind: "contact", expires_at, urls, invite}`; `POST /api/contacts/pair` `{invite}` →
  `ContactInfo` (the redeeming side; the other side's `POST /api/pairing/redeem` takes
  `card: {card, secret}` for a contact code and answers `{device, token, card}` with its own card);
  `GET /api/contacts` → `{me: {person, pid, name, fingerprint}, contacts: [{person, pid, name,
  card_name, fingerprint, machines, created_at, updated_at, pull}]}` (`pull`: `{state, at, error}`
  of the last pull of what they share, state idle|pulling|ok|unreachable|skew|error|gone, or null
  when not pulled since this start; `gone`: every machine of theirs refused us three rounds in a
  row, so their meetings were removed here); `PATCH /api/contacts/{person}` `{name}` (local name; empty = the
  card's); `DELETE /api/contacts/{person}`; `POST /api/contacts/{person}/pull` → `{state, at,
  error}` after pulling what that contact shares now (services/share_pull.py; `423` while locked).
  A contact's `POST /api/share/poke` here starts such a pull in the background.
- `GET /api/pairing/devices` → `[{id, name, kind, created_at, last_seen_at}]`. A phone's token opens
  only the phone page's paths; a computer's opens the whole API.
- `DELETE /api/pairing/devices/{id}`: the device's token stops working at once.
- `GET /api/pairing/me` → the device making the request (with its own token), else `404`.
- `GET /api/pairing/remote` → `{enabled, running, endpoint_id, error}`: remote access. With the
  `remote_access` setting on, the backend runs `mnemosyne-link home` (the `link/` crate, found via
  `MNEMOSYNE_LINK_BIN`, `PATH`, or a build in `link/target`), restarting it if it dies, and again
  when `remote_relays` changes (comma-separated iroh-relay URLs, passed as `--relay`; blank means
  Mnemosyne's relay while `use_our_servers` is on, else none, never n0's; see deploy/relay/, deploy/ours/). It accepts
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
| GET | `/api/sessions/{id}/versions` | earlier versions, oldest first (`has_summary`: can be restored as the summary); no seal check |
| GET | `/api/sessions/{id}/versions/{version_id}` | an earlier version's transcript lines and summary, with `summary_data` (topics, items...) for versions kept since 0.15.1 |
| POST | `/api/sessions/{id}/versions/{version_id}/restore-summary` | puts that summary back (text and, when kept, its data; done ticks, issue links and due dates carry over), keeping the current one as a version; history `summary_restored`; 404 without a summary, 403 for someone else's meeting |
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
`error`, `can_change`, `keep_sharing_after_quit`, `keep_awake_while_sharing`,
`keep_awake_available`, `tailscale_name` (this machine's tailnet name when Tailscale runs with
HTTPS certificates on, else ""), `tailscale_cert` and `tailscale_error`. `PUT` takes any of
`enabled`, `name`, `keep_sharing_after_quit`, `keep_awake_while_sharing` and `tailscale_cert`
(left out: unchanged).

`tailscale_cert` (setting `team_tailscale_cert`) fetches a real certificate for the tailnet name
with `tailscale cert` into `<data_dir>/tls/tailscale.{crt,key}` (409 with what to do when Tailscale
refuses: it needs root or `tailscale set --operator=<user>`), serves it to connections that ask for
that name (SNI), lists that address first, and renews it when under 30 days are left. Other
addresses keep the self-signed certificate. While shared, the addresses are checked every 30 s; when
they change, the self-signed certificate is made again and loaded into the running listener.

With `keep_sharing_after_quit`, `/health` says `outlives_app: true` while it is shared: the shell
then leaves the backend running when the app quits, and `api/app_watch.py` keeps serving instead of
shutting down (teammates' browser recordings go on; the desktop's own recording is still saved after
15 minutes). The AppImage runs the backend from a copy in `<app data>/runtime/<version>`, since its
own files vanish when it exits. While anything records, the backend holds a logind sleep inhibitor
(`services/awake.py`, `systemd-inhibit`); with `keep_awake_while_sharing`, all the time it is
shared.

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
when it is false and there are no meetings yet. The answer also carries `setup_complete` and
`gpu_support` (`auto` | `off`) for the desktop shell: on an NVIDIA machine it installs the gpu extra
(about 7 GB) in the background only after setup, and not when it was declined; setup and Settings →
Transcription start it with the Tauri command `start_gpu_install`.

### `POST /api/system/phonon` · `DELETE /api/system/phonon`
Admin. Phonon-2 for English (services/phonon_install.py): an `install_phonon` job that makes
`<data_dir>/phonon/venv` with uv (the app's bundled one, `MNEMOSYNE_UV_BIN`, else on PATH), installs
CPU PyTorch from download.pytorch.org and then `fermion-research` with every package pinned
(transcription/transcribers/phonon-requirements.txt), downloads the model once and writes
`installed`; messages are uv's lines, about 1 GB in all. 409 while one runs. DELETE removes the
folder (409 while recording or transcribing). `GET /api/system` says `phonon`, `phonon_bytes` and
`phonon_installable`. Transcriber `auto` uses it when `language` is English; servers it starts run
offline, on loopback, with a random API key and without Fermion's library-override variables.

### `POST /api/system/prepare`
Admin. A `prepare_models` job that downloads the models the current settings use: the transcriber
and diarizer (by loading the engine), the live transcript's model when it is another, and the search
model. Its messages say "Downloading …: N of about M MB" (services/model_downloads.py adds up how
much the Hugging Face cache and `<data_dir>/models` grew, against the expected size per engine);
409 while one runs. Setup starts it on its last step. A transcription that loads the engine for the
first time reports its download the same way.

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

**Response:** `{ "session_id": "...", "recording_id": "...", "live_job_id": "..." | null, "message": "...",
"clock": {part, offset, started_at} | null }`. `clock` is the recording's place on the meeting's timeline
(services/parts.py `recording_clock`): `part` it records into, `offset` where that part starts on the joined
timeline (seconds) and `started_at` when its capture started (UTC ISO, milliseconds). The notes editor's time chips
are `(part, seconds since started_at)`. `restart` answers with it too.

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
time), device_ids, part, live, live_segments: [{source, segment}], problems, clock}]`. `live_segments` is the live
transcript so far. `problems` names sources no longer being captured (see below). `clock` is the recording's
`{part, offset, started_at}` as in `start`.

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

`{ "notes": "..." }` → `SessionDetail`. With live shared notes (see "Live notes") the text goes
through the meeting's document: the sidecar's `set_text` diffs it in as author `api`, the frames
are broadcast to everyone who has the notes open, and `notes` comes back as the document renders
it (canonical markdown). Without (no `mnemosyne-link` with `notes doc`, or a meeting recorded on
another machine whose document has not arrived by library sync yet) it is stored as given. Who
may: as for live notes (owner, admin, anyone it is shared with); others get 403, a meeting they
cannot see 404.

### `PUT /api/sessions/{session_id}/summary`

A summary edited by hand, saved together (services/summary_edit.py): `{summary, topics: [str],
decisions: [{text, at}], open_questions: [{text, at}], action_items: [ActionItem], chapters:
[{start, title}], client_facts: [ClientFact]}` → `SessionDetail`. Each decision and question
carries its transcript time (`at`, seconds or null), stored back as `decision_at`/`question_at`.
Empty items are dropped, topics deduplicated, chapters sorted by time. Style, model, source
fingerprint and follow-up draft stay; `summary_data.edited_at`/`edited_by` are set (a new summary
clears them, and the UI asks before replacing edits). Works without a summary yet (style
`manual`). Keeps the previous version, logs `summary_edited` in the history, seals it, publishes
a `session` event (search reindexes), and re-exports to Obsidian when auto-export is on. Someone
else's meeting: 403.

### `PUT /api/sessions/{session_id}/followup`

`{ "text": "..." }` → `SessionDetail`: keeps the follow-up draft as edited, without marking the
summary edited. 404 without a summary.

### Bookmarks: `POST /api/sessions/{session_id}/bookmarks` · `PATCH|DELETE …/bookmarks/{id}`
`{at?, note}` → `{id, at, note, created_at}`. Without `at`, while recording, it marks now (the Mark
button, Ctrl+M, the tray's "Mark this moment", `mnemosyne --mark`). Kept as (part, seconds), so
`at` is always on the meeting's timeline, also after meetings are combined. A session lists its
`bookmarks`; the summary is told to cover those moments; the Obsidian note lists them.
Changes publish `{type: "bookmarks", session_id}`.

### Comments: `GET|POST /api/sessions/{session_id}/comments` · `PATCH|DELETE …/comments/{id}` · `POST …/{id}/resolve|reopen`
Threads on a meeting (services/comments.py). `GET` → `SessionComments` `{threads: [{root,
replies}], can_comment}`, oldest first. `POST` `CommentCreate` `{text, target, target_id?,
parent_id?, idx?, idx_to?, seconds?, exact?, prefix?, suffix?}` → `Comment`:

- `target: "segment"` with `idx` (and `idx_to` for several lines): stored as a time span
  `anchor {part, start, end}` (seconds into the part, like bookmarks), `quote` the lines' text.
  With `seconds` (meeting timeline, from the audio player) instead: a moment, `start == end`,
  `quote` the line playing then (`""` between lines).
- `target: "decision" | "question" | "item"` with `target_id`, the summary block's id
  (`decision_ids`, `question_ids`, `ActionItem.id`); `quote` its text.
- `target: "body"`: a selection of the rendered summary, `anchor {exact, prefix, suffix}`
  (`exact` must be in the summary's plain text; `""` = the whole summary), `quote = exact`.
- `target: "notes"`: the live shared notes. `exact` is the selected text (required, = `quote`);
  the server mints the id and stores `anchor {mark: "comment:<id>"}`, `target_id ""`. `notes_ids`
  `[first, last]` are the element ids (hex, the editor's `seqIdAt`) of the first and the last
  selected character: the SERVER then puts the mark `comment:<id>` on that range (the sidecar's
  `notes doc` op `mark_comment`), appends the frames to the notes' log as the commenter and sends
  them to everyone connected, so a reader, whose editor is read-only, can comment too. Without
  `notes_ids`, or when an id is unknown or deleted (or the sidecar fails), the comment is kept and
  shows as detached with its quote (still `200`). `409 "The notes are not shared on this server"` when there is no
  sidecar. Read: the fold reports each mark's range in the notes' plain text (code points, U+001F
  cells are tabs), kept in `shared_notes.plain`/`comment_ranges` (written by every fold and by
  library sync's union/adopt). `notes_range` `[start, end)` when the mark has text (`changed`
  when `plain[start:end]` is not the quote, ignoring case, punctuation and whitespace), else
  `null` and `detached` (also before the first fold that knows it). Resolve, reopen and delete
  leave the mark alone (the client hides or removes it).
- `target: ""`: the meeting as a whole. A reply names `parent_id` (a thread's first comment in the
  same meeting) and carries no target or anchor.

Comments are re-anchored when read: a transcript comment is on the line it overlaps most (a line
counts when the overlap is at least half the comment's span or half the line's own duration), so it
follows edits, splits, merges and a new transcription; `segment_idx` is that line and `at_seconds`
where the comment starts on the meeting's timeline. A moment is never detached while its part is
there: it is on the line containing it, else the nearest line whose start or end is within 5 s, else
on no line (`segment_idx` null, shown "at" `at_seconds`). A block comment follows its id (rewording
keeps it). A text comment is a selection of the rendered summary: it is matched against the
summary's plain text (no Markdown markers, emphasis or code ticks, links as their text, whitespace
collapsed), by its exact text (prefix/suffix choose among repeats), else by a fuzzy match (≥ 0.85)
over windows of sentences; the anchor is stored as sent. `changed`: the quoted words are no longer
there as written; `detached`: the place is gone (deleted line, removed item, rewritten text);
detached comments are kept, with their `quote`.

`PATCH` `{text}` (the author, or an admin) → `Comment`. `DELETE` (the author, the meeting's owner
or an admin) → `{deleted: true}`; a first comment with replies stays as an empty `deleted` one
until its last reply goes. `resolve` / `reopen` (threads only; the meeting's owner, an admin or the
thread's author) → `Comment` with `resolved_at` / `resolved_by`. Each comment carries `can_edit`,
`can_delete`, `can_resolve` for the caller. On a team server anyone who can read the meeting
comments; `author_id`/`author` are the signed-in person; `@Full Name` or `@First` (unique on the
server) become user ids in `mentions`. On the desktop `author_id` is `""`, `author` the
`local_speaker_name` (`"You"` when blank), everything is allowed and `mentions` is empty.

Errors: `404` for a meeting the caller may not see or a comment not in it, `403` not allowed,
`400` bad target, line, block, text or thread, `409` a notes comment without shared notes. History gets `commented` (`by`, `target`, `reply`,
`excerpt` ≤ 80 characters), `comment_resolved` and `comment_reopened`. Every change publishes
`{type: "comments", session_id, mentions}` (`mentions`: user ids newly named by this change), only
to those who may read the meeting.

### Comments on tasks and decisions: `GET|POST /api/tasks/{key}/comments` · `GET|POST /api/decisions/{key}/comments`
A task or decision is one object across meetings (services/task_links.py, decision_links.py), and
its threads are the comments on its mentions. `GET` → `ObjectComments` `{mentions: [{session_id,
session_name, created_at, item_id, threads}]}`, newest meeting first, only meetings the caller may
see. `POST` `{text, parent_id?}` → `Comment`: a new thread goes on the newest visible mention (as an
`item` / `decision` block comment there), a reply (`parent_id` = the thread's root id) on the
meeting of the thread it answers. Edit, resolve and delete use the meeting routes above. `404` for an
unknown key or thread, `400` for an empty text or an object without an id. Tasks and decisions carry
`open_comments` (open root threads over all their mentions).

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

### Publishing: `GET|PUT|DELETE /api/sessions/{session_id}/publication` · `POST …/publication/preview` · `GET …/publication/defaults` · `POST …/publication/file` · `GET /api/publications`

A curated, encrypted copy of a meeting (services/publishing.py, format in
services/publication_format.py). A `PublicationSelection` says what it shows: `title`, the
booleans `summary decisions questions action_items chapters topics notes`, `attendees` (names to
list; everyone else of the meeting becomes "Person N" everywhere), `names`
(`as_is|first|hidden`), `hide_identifiers` (also hides e-mail addresses and phone numbers), `quotes` (`{start, end, part?, with_clip}`: joined-
timeline seconds, or seconds into `part`; the transcript lines whose middle is inside), `where`
(`local|host`) and `expires_days` (`null` = never, only for `local`; at most 365 on a host).
`GET …/defaults` gives the live publication's selection, else the defaults (`publish_default_expiry_days`,
host when `publish_host` is set or `use_our_servers` is on: ours), plus `local_reach` (`team|phone_url|this_device`: who could open a
link to this computer) and `local_url` (the base such a link uses; `""` for `this_device`, where only
127.0.0.1 works). `POST …/preview` returns the unsealed manifest, a quote's clip as
`{preview_url, seconds, mime}` (`/api/audio/file/{id}#t=start,end`). `PUT` starts a `publish` job
(`result.publication`); an update keeps the id, key and link and bumps `version`. `DELETE`
unpublishes (`404` when not published). `Publication.url` (link with the key in the fragment) is
`null` for anyone but the owner or an admin; `behind` is true when publishing again would show
something different; `local_reach` and `published_by` say who could open it and who published. `PUT` answers
`400` for a selection that cannot be published (no host set, no expiry on a host, clips without audio) and `409`
"This meeting is being published already" while a `publish` job runs; `DELETE` answers `502` when the host
could not delete the copy. `403` when `allow_publishing` is off on a team server or the caller may not
change the meeting; `409` for clips of a meeting whose audio is on another computer ("The audio
is on <name>"), and `409` "Published from <name>" for `PUT`/`DELETE` of a `where: "local"`
publication whose files are on another of the person's computers (library sync): such a
publication has `served_by: <name>` and `url: null` here; a hosted one can be changed from either
computer. Readers (reviewers, members it is shared with) get the publication with `url: null`.

`POST …/publication/file` `{ selection, password? }` (password at least 8 characters, else `400`; `503` when the
viewer is not built in) starts a `publication_file` job: the publication as one self-contained HTML file,
not a publication (nothing to update or unpublish, never expires; logged and sealed like one). Its result is
`{token, filename, size}`; `GET …/publication/file/{token}` downloads it once (attachment, then it is deleted;
`404` when gone, files are kept a day) and `POST …/publication/file/{token}/save` `{ "path": … }` writes it to a
path chosen in the desktop app's save dialog instead (loopback clients only, `403` otherwise; `400` when the
folder does not exist; returns `{path}`).

Open (no sign-in: the key in the link is the permission), read-only, with the publish host's headers. Every
answer has `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`; errors are JSON
`{"error": "not_found"|"gone"}`.

- `GET /p/{id}`: the viewer's `index.html` for any id (it says "not found" itself), strict CSP
  (`default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; media-src blob:; img-src data: blob:;
  base-uri 'none'; form-action 'none'; frame-ancestors 'none'`), `Cache-Control: no-store`; `503` with a plain
  page when the viewer is not built into this Mnemosyne.
- `GET /p/{folder}/{path}`: the viewer build's files (`/p/assets/index-<hash>.js|css`), same CSP,
  `Cache-Control: no-cache`; `404` for dotfiles or anything outside the viewer folder.
- `GET /pub/{id}`: the sealed manifest (`application/octet-stream`) with `ETag` (its SHA-256),
  `X-Pub-Version`, `X-Pub-Expires` (unix seconds; absent when it never expires) and `Cache-Control: no-cache`;
  `304` on a matching `If-None-Match`; `404` unknown, `410` unpublished, expired or deleted (a tombstone for 30 days).
- `GET /pub/{id}/{sha256}`: a sealed clip, `Cache-Control: public, max-age=31536000, immutable`; `404` if the current
  version does not hold it.

### Guest notes: `GET|PUT|DELETE /api/sessions/{session_id}/guest-notes` · `POST …/guest-notes/end` · `POST …/guest-notes/new-link`

Opens the meeting's live notes to people who have no Mnemosyne: a link to a page in their browser, through a
room on a host that cannot read it (services/guest_notes.py, guest/). The host is `guest_host`, else
`publish_host`, else Mnemosyne's (`use_our_servers`); with none, starting answers `409 "No host for guest notes…"`. All five answer a `GuestRoom`:
`{ url, host_url, state: "live"|"ended"|"stopped", created_at, ended_at, expires_at, started_by, guests_present }`.

- `GET` the room (live or ended), or `null` with `200` when the meeting has none or it was stopped (it is not a
  `404`, as with `…/publication`). Anyone who can see the meeting may ask; `url` (`<host>/n/<room>#<key>`, the key is
  in the fragment and nothing else carries it) and `guests_present` (guests' names) are filled only for the
  meeting's owner and admins, readers get `url: null` and `[]`.
- `PUT` opens it: the live room if there is one, else a new one (an ended room is deleted from its host first: one
  room per meeting). Owner or admin only (`403`); on a team server also `allow_guest_notes` (off by default, `403`
  "turned off"). `409` when it cannot start (no host, the `mnemosyne-link` sidecar is missing, the meeting was
  shared with you, its notes have not arrived from sync); `502` when the host refuses or cannot be reached.
- `POST …/end` ends the live room: a final snapshot, nobody can type, guests may read until it expires
  (`guest_notes_end_hours` after the recording stopped ends it by itself; an ended room stays 24 h, at most the
  room's 7 days). `404` when there is no live room. Owner or admin.
- `POST …/new-link` stops the room and starts a new one: new id and key, the old link and its guests are gone. `404`
  when the meeting has no guest notes. Owner or admin.
- `DELETE` stops the room: deleted from its host at once, live or ended; returns it with `state: "stopped"` and
  `url: null`; `404` when there is none. Owner or admin, also when `allow_guest_notes` was turned off since. Deleting
  the meeting stops its room first (best effort).

Starting, ending and stopping are in the meeting's history (`guest_notes_started|ended|stopped`) and each seals a
record version (`EVENT_SEALS`); the records export has a `guest_notes.json` per such meeting (host, room id, who and
when; never the link, key or tokens).

Open pages, no sign-in (the key in the link is the permission), also on the team listener, with the publication
viewer's headers and a CSP that adds `'wasm-unsafe-eval'` to `script-src` (the notes document runs as WebAssembly):
`GET /n/{room}` is the guests' `index.html` for any room id (it reads room and key from its own address;
`503` with a plain page when not built in) and `GET /n/{folder}/{path}` its build files (`/n/assets/…`, the `.wasm` as
`application/wasm`).

The room protocol (guest page and the owner's backend both speak it to the host): HTTP with the owner's publisher
token to create (`PUT /live/{room}`), snapshot (`PUT …/snapshot`, `X-Room-Since` prunes the log), end
(`POST …/end`) and delete (`DELETE`); a WebSocket `GET /live/{room}` with the room token for everyone: `join`,
`welcome`, the snapshot, the log after `since`, then live relay (`0x01 || seq || envelope`), presence (`0x02`), `count`
and `ended`. Everything in an envelope is sealed (AES-256-GCM, `services/guest_format.py`; the host holds only the
room token). Two JSON kinds gained fields in phase 11 (older pages ignore them): PRESENCE `{gid, name, typing, sel?,
gone?}` where `sel` is `{a, f}` (element ids, 64 hex, or null) or null and `gone: true` says the page is closing (gid
"" is the owner's heartbeat, `o:<member key>` one of the owner-side editors, whose carets and typing the guests see);
META `clock?: {part, offset, started_at}` is the recording in progress (as in `POST /api/audio/start`) and is absent
while nothing records, so the guest page can insert time chips. A guest's PRESENCE heard in one of the meeting's rooms
is forwarded unchanged into the others. The exact messages, close codes and limits are in the header of `publish-host/src/live.rs`.

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
- `POST /api/asks/{id}/obsidian` → `{path, message}`. Writes the answer to
  `<vault>/<obsidian_subfolder>/asks/<YYYY-MM-DD> <question>.md` (frontmatter `mnemosyne: ask`): the
  question, the answer as written (links to vault notes when `obsidian_link_notes`, identifiers
  redacted when `redact_exports`), then `## Sources`: `- YYYY-MM-DD [[note|Name]] (mm:ss)` per
  meeting cited. Saving again rewrites the file; 409 when the vault has a note of the user's with
  that name, 400 without a vault, 404 for an unknown ask or another user's.
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
Items typed in a meeting's live notes (see "Action items from the notes") are listed too: `source: "notes"`, `idx`
`-(j+1)` for the j-th of `Session.notes_items`, which no idx-based writer accepts. `item_id` is the item's id (summary
items: `ActionItem.id`, "" on an old one without; notes items: 16 hex); `source` is `"summary"` or `"notes"`.
`due` (`YYYY-MM-DD` or null) is a deadline named in the meeting: the summary prompt gives the model
the meeting's date and weekday so "by Friday" becomes a date; anything that is not an ISO date is
dropped. Open items with a deadline come first, soonest first; `due=overdue` keeps those past due,
`due=week` those due within seven days. GitHub issues get a "Due:" line, Linear `dueDate`, Jira
`duedate`, and the Obsidian note `📅 YYYY-MM-DD` (the Tasks plugin's format).

### `PATCH /api/sessions/{session_id}/action-items/{idx}`
Body: any of `{done, text, owner, due}`; fields left out stay (`owner` "" or null and `due` null
clear them; empty `text` is 400). Returns the updated item (`can_edit`: whether the caller may
change text/owner/due) and publishes a `session` event; 404 for an unknown session or index.
Anyone who can read the meeting may tick its items (someone it is shared with ticks off theirs);
when it is not the owner, `task_done` / `task_reopened` goes into its history. Changing text, owner
or due needs write access (403 otherwise), marks the summary edited and logs `task_edited`. Re-summarizing a meeting keeps `done` and `issue_url` on items whose text matches
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

### `POST /api/topics/rename`
`{old, new}` → `{meetings, remembered}`. Renames a topic (matched like the list groups them:
words, case ignored) in every meeting the caller may change; when `new` is already a topic of a
meeting, the two merge there. Not a summary edit (no version, no `edited_at`). `remembered`: kept
in `settings.topic_aliases` so new and re-made summaries use the new name too (always on the
desktop; admins only on a team server). 400 for a blank name.

### `GET /api/topics/thread?q=`
Meetings about `q`, oldest first (up to 12): those whose topics name it, keyword hits and
meaning hits, ranked by reciprocal rank fusion. Each has `{id, name, created_at, summary (first
three sentences), chapters, decisions, action_items: [TaskItem], open_questions}`, filtered to the
pieces that match the topic by words or by meaning.

### `POST /api/topics/thread/summary`
Body `{"q": "...", "provider": "", "model": ""}`. Queues a `thread` job: the LLM writes where the
topic stands (current state, how it got there, what is still open). Result `{text, query, meetings}`.

### `POST /api/topics/obsidian`
Body `{"query": "...", "explanation": ""}` → `{path, message}`. Writes the topic thread to
`<vault>/<obsidian_subfolder>/topics/<Topic>.md` (frontmatter `mnemosyne: topic`): `explanation`
(the "Where it stands" text, no LLM call is made), then per meeting, oldest first,
`### YYYY-MM-DD [[note|Name]]` with its summary, decisions, action items (checkboxes by
`obsidian_task_scope`) and open questions; words that name vault notes are linked
(`obsidian_link_notes`). Only a file with our marker is rewritten: 409 when the user has a note of
that name (that file, or elsewhere in the vault). 400 without a vault, 404 without meetings.

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
`summary_style` and `summary_instructions` from settings. `revise` ("shorter", "focus on the
decisions"): the model also gets the current summary, hand edits included, and is asked to change
it as requested rather than start afresh; the history's `summarized` event then carries `revised`.
Topics go through `settings.topic_aliases`. Queues a `summarize` **Job** and returns it immediately; the
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
`obsidian_include_transcript` is off. With `obsidian_task_scope` `"mine"` (the default) only the
recorder's items (their name, "Me", or no owner) and followed items are `- [ ]` tasks; everyone
else's are plain bullets (`- Jonny Zerah: text`) under `### Others`. `"all"` writes every item as a
task. Person notes follow the same setting (open tasks as bullets, followed ones under "Waiting on").
With `obsidian_decisions_log` (default off) every export also rewrites `<subfolder>/Decisions.md`
(marker `mnemosyne: decisions`; a note of the user's with that name is left alone): the decisions of
all readable meetings, newest first, under `## YYYY-MM-DD [[note|Name]]`. The export-all job writes
it once at the end.

With `obsidian_link_notes` (default on) words in the Summary, Decisions, Action Items, Open
Questions, Chapters and the `topics` that are the exact name of a note already in the vault become
`[[links]]`, the first occurrence in each section. Case-sensitive, whole words, names of 3 or more
characters; never date-named or daily notes, notes in our own folders, single lowercase or very
common words, nor anything inside code, URLs or existing links. The transcript is never linked.

**Response:** `{ "path": "...", "message": "Exported successfully" }`
**Errors:** `400` vault not configured or missing, `404` session.

### `POST /api/export/obsidian/all`

Writes every summarized meeting the caller may change to the vault again (the notes, and the people
and daily notes when enabled), e.g. after changing `obsidian_task_scope`. Returns the `Job`
(`kind` `obsidian_export_all`); its message reads "Exporting <name> (n of N)" and its result is
`{ "exported": n, "failed": k, "total": N }`.

**Errors:** `400` vault not configured, `409` already running.

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

## Task and decision objects

A task object is every action item (across meetings) with the same `task_id`; an unlinked item is
its own object (key `<session_id>:<item id>`). Text, owner and due are the newest mention's;
done, follow and issue link are the object's (writes fan out to every mention). Decisions work the
same way through `summary_data.decision_links`. Only mentions in meetings the caller can see are
returned.

| Method | Path | |
|---|---|---|
| GET | `/api/tasks` | Task objects (filters as `/api/action-items`); each with `mentions` newest first, `linked`, `mine`, `can_edit` |
| PATCH | `/api/tasks/{key}` | `{done?, follow?}` for the whole task |
| POST | `/api/tasks/link` | `{a: {session_id, item_id}, b: {...}}`: one task (merges two) |
| DELETE | `/api/tasks/{key}/mentions/{session_id}/{item_id}` | Unlink one mention |
| GET | `/api/tasks/suggestions` | Close matches on existing data, not linked yet |
| POST | `/api/tasks/suggestions/apply`, `/dismiss` | `{a, b}` |
| GET | `/api/decisions` | Decision objects (`?linked=true`) |
| POST | `/api/decisions/link` | `{a: {session_id, decision_id}, b: {...}}`: the newer supersedes the older |
| DELETE | `/api/decisions/{key}/mentions/{session_id}/{decision_id}` | Unlink one mention |
| GET | `/api/sessions/{id}/links` | Per action item id and per decision id: `{key, earlier, later}` |

Settings: `task_linking` (`off` / `exact` / `related`, default `related`) and `task_linking_llm`
(default off).

## Sharing with people

Phase 8 (services/contacts.py, shares.py, share_pull.py; docs/plans/2026-10-23-phase-8-tasks.md).
Personal computers only: a team server answers `409`. Pairing details are in the contacts entry
under pairing above.

| Method | Path | What, and who |
|--------|------|---------------|
| GET | `/api/contacts` | `{me, contacts: [ContactInfo]}`, each with `pull` state. Admin / the desktop |
| POST | `/api/contacts/codes` | A one-time contact invite `{code, kind: "contact", expires_at, urls, invite}`. Admin; needs an API token and remote access |
| POST | `/api/contacts/pair` | `{invite}` → `ContactInfo`: redeem the other person's invite. Admin |
| PATCH | `/api/contacts/{person}` | `{name}`: rename locally (empty = the card's name). Admin |
| DELETE | `/api/contacts/{person}` | Remove: their CONTACT devices are revoked, their shares end. Admin |
| POST | `/api/contacts/{person}/pull` | Pull what that contact shares now → `{state, at, error}`. Admin; `423` while locked |
| GET | `/api/sessions/{id}/peer-shares` | Who has the meeting and which parts (`PeerShareInfo`). Its owner or an admin |
| PUT | `/api/sessions/{id}/peer-shares/{person}` | `ShareParts` body: share with a contact or change the parts. Owner or admin; `403` on a meeting shared with you |
| DELETE | `/api/sessions/{id}/peer-shares/{person}` | Stop sharing with that contact. Owner or admin |
| GET | `/api/sessions/{id}/share-status` | `ShareStatus` for a meeting shared with you: `owner {person, name}`, `waiting {comments, ticks, notes}` (what has not reached the owner's computer yet) and `rejected [{kind: comment\|item_flag\|notes, reason, excerpt (first 80 characters), at}]`. `404` for any other meeting. Admin / the desktop |
| GET | `/api/share/hello` | Who this is and the sharing protocol version. A contact's CONTACT token |
| GET | `/api/share/changes?since=&limit=` | Signed `SharedMeeting`s changed for the calling contact, plus tombstones. A contact |
| POST | `/api/share/card` | The contact's newer signed card (its machines changed). A contact |
| POST | `/api/share/poke` | The contact has news: pull from it now, in the background. A contact |
| POST | `/api/share/contributions` | `ContributionBatch`: signed comments, ticks and notes frames, checked at the hub (`413` over 200 records or 2 MB). A contact |

What you do on a meeting shared with you (comments, ticks and stars on action items, notes you type)
is signed and sent to the owner's computer by the outbox (services/share_push.py, table
`share_outbox`; typed notes are read off the notes log past `share_pushed_seq:<id>`): a second
after a local write, and at the start of every pull round. A transport failure leaves it waiting;
a refusal is final and shows in `share-status`. WebSocket event `{type: "share_outbox",
session_id, pending}` after each change. Until the owner has accepted them, pending comments and
ticks are shown again on the pulled copy.

A CONTACT token reaches only `/api/share/`; the admin token is refused there (`403`). In demo
mode (`MNEMOSYNE_DEMO=1`) a contact "Ada Lovelace" and one meeting she shared, "Shared: Waku
roadmap", are seeded at start (services/share_demo.py).

## Library sync

A person's computers keep one library (docs/plans/2026-10-12-hashseq-collaboration.md "Phase 3
design"; services/sync_peers.py, services/sync_service.py). Pairing is mutual, from a `sync`
pairing code: each side records the other as a DESKTOP device and as a sync peer in
`<data_dir>/sync_peers.json` (0600; it holds the token for the other machine and is not backed
up). Needs remote access and an API token; every route below answers `409` on a team server.

Each backend PULLS from its peers through the link sidecar's loopback listener for that peer,
with the token the peer gave it. The routes a peer calls know which peer asks from that token
(its device's endpoint); with the admin token nothing is remembered as served.

- `GET /api/sync/peers` → `{self: {id, name, person: {pid, fingerprint}}, peers: [{id, name,
  endpoint_id, created_at, last_sync_at, listen}]}`; never a token. `person` is the person key
  (services/person.py): synced computers show the same one. `POST /api/sync/pair` `{invite, name}`
  → `{peer, person_error}` pairs with the computer that showed the invite and shares the person
  key (below; `person_error` says why when pairing worked and sharing the key did not: the next
  pairing redoes it); `DELETE /api/sync/peers/{id}` stops syncing with it (what it
  synced stays).
- `GET /api/sync/hello` → `{protocol, schema_version, app_version, id, name, seq}`. Asked first in
  every round. `protocol` is 1; a peer speaking another one is not pulled from (state `skew`,
  "Update Mnemosyne on <name>"); `seq` is its change counter (a cursor past it means its change
  log started over, e.g. a restored backup: read from 0 again).
- `GET /api/sync/changes?since=0&limit=50&tombstones_since=` → `{sessions: [SyncSession],
  seqs: [int], tombstones: [{session_id, at, device, name}], next_since, more, total}`. `seqs`
  are the sessions' change numbers, in the same order. The meetings changed
  after `since` (this computer's change counter), whole, oldest change first, at most `limit`
  (1-200); the meetings deleted after `tombstones_since` (ISO time on this computer's clock; the
  puller asks from two hours before the last one it saw); `total`: meetings changed after
  `since`. Each document served is remembered for the calling peer (the last 3 per meeting), the
  base of a three-way merge when its later edits come back concurrent. A meeting with a live
  notes document carries it as `notes_doc` (base64 of the canonical snapshot, folded and caught up
  with text written around it just before serving; `null` without one); the kept base leaves it
  out. Two documents are merged by union, not by the three-way text merge (see "Live notes").
- `GET /api/sync/contacts` → `{person, has_people_data, contacts: [{person, name, my_name, card,
  secret, created_at, updated_at, removed_at}]}`: every contact, removed ones too, complete (the shared
  secret and card included: the caller is another of this person's computers), without the
  runtime state. One step of every round, after hello and before the changes; merged by person,
  the newer `updated_at` wins, a removal wins a tie (services/contacts.py `merge_rows`). An older
  peer's `404` is skipped. If the peer's `person` differs from the puller's (computers paired
  before the key was shared), the keys converge by the pairing rule, computed on both sides: both
  have contacts: no merge and `warning` "<name> belongs to another person: contacts are not
  shared" in `GET /api/sync/status` (meetings still sync); exactly one has: the other adopts its
  key (`GET /api/sync/person-key`) and merges in the same round; neither: the higher key adopts
  the lower. Only the adopting side acts.
  Contacts never travel inside a `SyncSession`.
- `GET /api/sync/person-key` → `{private}` and `PUT /api/sync/person-key` `{private}` → `{pid,
  fingerprint}`: how pairing for library sync gives both computers one key. The computer that
  showed the code answers the redeem with `sync.person = {pub, adopt}` (`adopt`: `mine` the
  redeemer takes this key, `yours` this computer takes the redeemer's, `same`); the redeemer sent
  `card.person = {pub, has_people_data}`. Neither has people data, or only the inviter: the
  redeemer adopts the inviter's key; only the redeemer: the inviter does (`PUT`). Both have
  contacts under different keys: the redeem answers `409` "These computers belong to different
  people" and nothing is paired. Both routes: only a computer synced with this one (not the admin
  token, a phone or a contact: `403`/`401`); `PUT` is `409` while this library has contacts under
  a different key.
- `POST /api/sync/poke` → `{scheduled}`: the calling peer has news; a pull from it follows in
  about 2 s (pokes in between make one pull). `false` when the caller is not a sync peer.
- `POST /api/sync/now` → `{started: [peer id]}`: pull from every peer now, in the background.
- `GET /api/sync/status` → `{self, peers: [{id, name, endpoint, listen, last_sync_at, state,
  error, done, total, received, updated, merged, deleted, waiting, clashes: [{session_id, name}],
  app_version, warning}]}`. `state`: `idle` (not tried since this start), `syncing`, `done`, `offline`
  (not reachable; tried again after 1, 2, 4 ... 10 minutes, at once on a poke or Sync now),
  `locked` (it answered `423`), `skew`, `error` (`error` says why). The counts are the current
  or last round's.

A round with a peer: hello, then pages of changes from the peer's cursor (kept after each page,
so an interrupted first sync resumes), then for each meeting: deleted here → ignored (never
resurrected); new here → written as it is (`origin` = the recording computer, no audio file,
`completed` with a transcript); its version vector dominates ours → taken as it is; ours
dominates or equals → ignored; concurrent → merged three ways (services/sync_merge.py) and logged
as `sync_merged` in the meeting's history; another meeting under the same id (started at another
time) → a clash, skipped and listed. A tombstone deletes our copy, with its audio when it was
recorded here, unless it is busy (recording, a job): then at the next round. Rounds run at
start, every 30 s, on a poke and after the sidecar dials the peers again; local changes poke the
peers. Meetings from before sync count their revisions under this computer's sync id from the
first start on.

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
| `notes` | `session_id` | The meeting's live shared notes were folded into `Session.notes` (see "Live notes"); reload it |
| `sync` | `peer`, `state`, `done`, `total` | Library sync with a peer: a round started, a page was taken in, or it ended (`state` as in `GET /api/sync/status`); meetings it changed also get a `session` event |
| `live_partial` | `session_id`, `source`, `speaker`, `text` | The still-changing tail for that source; replaces the previous partial (may be empty) |
| `recovered` | `RecoveredRecording` fields | An interrupted recording was recovered (also listed in the next `hello`) |
| `pong` | | Reply to `ping` |

Clients should filter `transcription`/`status`/`error`/`live_*` by `session_id` and use `job` events for
state. Live segments are provisional: discard them when the session's final `transcribe` job starts.

## Live notes

### `ws://127.0.0.1:8008/ws/notes/{session_id}`

The meeting's shared notes, edited live by everyone who has them open (phase 6 of
docs/plans/2026-10-12-hashseq-collaboration.md). Every client runs the CRDT (hashseq, wasm); the
backend cannot read it: it relays binary frames, logs them, and has the stateless sidecar
(`mnemosyne-link notes doc`) fold them into `Session.notes` (markdown), which search, Obsidian,
MCP, combine and the summary keep reading.

Auth as `/ws` (`?token=` when a token is required; 4401 before the handshake). A meeting the
caller may not see, or none: accepted, then closed with **4404**.

Who may edit: the owner, an admin, and anyone the meeting is shared with (alone or with everyone,
`*`); everyone else who can read it (a reviewer on someone else's meeting) is read-only. On the
desktop (no team) the user edits, as `local_speaker_name` (or "You").

**1. Hello** (JSON), first:

```json
{
  "type": "hello",
  "notes": "<64 hex: sha256(\"mnemosyne/notes/\" + session_id), the notes object's origin>",
  "you": { "id": "<user id, \"\" on the desktop>", "name": "Ann Lee", "key": "m3fa91c07" },
  "read_only": false,
  "present": ["Ann Lee", "Bob Stone"],
  "members": [{ "key": "m3fa91c07", "name": "Ann Lee", "who": "u-ann", "typing": false, "sel": null }],
  "last_edit": { "by": "Bob Stone", "at": "2026-10-22T10:15:00" },
  "snapshot": true,
  "backlog": 3
}
```

`present` is everyone connected, you included (distinct names). `members` is the same people one per
connection, you included: `key` (`m` + 8 hex, random at join; `you.key` is yours), `name`, `who` (the user id, else
the name: what a caret's colour is taken from, so one person's two windows share a colour), `typing` and `sel`
(below). `last_edit` is `null` before
anyone typed. `"waiting_for_sync": true` (with `read_only: true`): a meeting recorded on another
computer that has notes but whose document has not come by library sync yet; editing would make a
second document with the same text.

Without a sidecar the hello is only `{"type": "hello", "unavailable": true}` and the socket is
closed normally (1000): use `POST /api/sessions/{id}/notes` and the plain text, as before.

**2. Snapshot**: when `snapshot` is true, one binary message, the folded document.

**3. Backlog**: then `backlog` binary messages, the frames not folded yet, in log order.

**4. Live traffic**, both ways:

- Binary: a frame, `0xDE` (delta) or `0xAF` (artifact: mark strings over 14 bytes), at most 2 MB.
  The server appends it to the log with its author and relays it to the other connections (never
  back to the sender). Refused, and the socket closed: from a read-only client **4403**; another
  first byte **4400**; over 2 MB **1009**.
- Client JSON: `{"type": "presence", "typing"?: true|false, "sel"?: {"a", "f"} | null}` (`typing` is
  ignored from read-only clients, `sel` is taken from everyone), `{"type": "ping"}` (answered
  `{"type": "pong"}`). `sel` is the selection: `a` (anchor) and `f` (focus) are each null or the element id (64 hex)
  of the character before that end of the selection; anything else is ignored whole. It is kept in memory on the
  connection, never stored or logged.
- Server JSON: `{"type": "presence", "present": [names], "typing": [names], "members": [...]}` when someone joins,
  leaves or starts/stops typing (`members` as in the hello; a joiner gets the others' `sel` there);
  `{"type": "caret", "key": member key, "sel": {"a", "f"} | null}` when someone else's selection moved (never
  back to the mover; at most one per 100 ms for the same member, the newest position waits and goes out when the
  window ends); `{"type": "last_edit", "by": name, "at": iso}` when someone else
  typed (at most every 15 s for the same person).
- Guests (see docs/guest-notes.md) reach the editors as `{"type": "guests", "present": [names], "typing": [names],
  "members": [{"key": "g:<gid>", "name", "typing", "sel"}]}` when one joins, leaves or starts/stops typing (a joiner
  gets the last non-empty one), and `{"type": "guest_caret", "key": "g:<gid>", "sel": {"a", "f"} | null}` when only
  a guest's selection moved. A guest is `gone` at once when their page closes, else after 30 s without presence.

The notes route's writes (author `api`) arrive as ordinary frames. When the meeting had notes but
no document, the first connection (or write) seeds one from them (author `seed`), only on the
computer that recorded it.

**Folding**: 2 s after the last frame, every 30 s while frames keep coming, when the last
connection leaves, before a summary and before an Obsidian export. A fold stores the snapshot,
marks the frames folded (their rows and authors are kept) and, when the markdown changed, writes
`Session.notes` (a new version for library sync, the search index) and sends the `notes` event on
`/ws`.

**Catching up**: when `Session.notes` differs from the markdown of the last fold (it was written
while the sidecar was missing, or by library sync's text merge), the document takes that text
first, as an `api` edit against the folded state (broadcast like any frame), before a join is
served and before a fold; the fold then renders the newer text. A notes route write that fails on
a meeting with a document answers 503 and writes nothing.

**Library sync**: a meeting's document travels as `SyncSession.notes_doc`. A fold that changes
only the snapshot (comment marks, formatting, artifacts: the markdown is the same) is still a new
version of the meeting, or it would never be pulled; a fold that changes nothing is not.
Arriving at a computer that has a document too (and a different one) the two are merged by union
through the sidecar (`notes doc` `merge`, no base needed; the notes are not merged as text); the
merged snapshot becomes the stored fold, `Session.notes` its render, and what this computer's
snapshot lacked is appended to the log as author `sync (<computer name>)` (already folded) and
sent to the connected editors as ordinary binary frames. Where there was no document, the
arriving one is adopted (`waiting_for_sync` ends) and the open sockets of that meeting get
`{"type": "reload"}` and are closed normally (1000): a client rejoins and receives the stored
document. A computer with a document that receives a meeting without one merges the text as
text and the document catches up with it (above). Folding is blocked while a meeting is being
taken in. A union that cannot be made (sidecar missing or failing) leaves that meeting waiting:
the round goes on to the following pages, but the stored sync cursor stays just before the
earliest waiting meeting, so it is tried again next round (`waiting` in the peer's status).

### Chips and action items from the notes

**Chips.** A time, person or meeting reference in the notes is an ordinary markdown link whose URL starts with
`mnemosyne:` (the editor draws it as a pill; the model is a `link` mark, nothing new in the document format):

| Form | Chip |
|---|---|
| `[12:34](mnemosyne:t/<part>/<seconds>)` | a time in the recording: part number and whole seconds into that part |
| `[@Ada Lovelace](mnemosyne:person/Ada%20Lovelace)` | a person (the name is percent-encoded) |
| `[Planning, Oct 14](mnemosyne:meeting/<id>)` | a meeting (8 to 32 lowercase hex) |

A link that is not exactly one of these is an ordinary link. Everything that reads `Session.notes` outside the editor
sees text instead (services/notes_chips.py): the summary's hint, MCP and published notes get the chip's
text (`12:34`, `@Ada Lovelace`, the title), the Obsidian note `[[Name]]` for a person when `obsidian_link_people` is on
and `[[note|title]]` for a meeting that was exported; time chips are plain text there. The stored markdown keeps the
URLs (full-text search indexes them like any link). Combining meetings retargets time chips to the combined timeline.

**Action items from the notes** (services/notes_items.py). A task line (`- [ ]` / `- [x]`) with a person chip is an
action item of the meeting; one without stays a checklist. `Session.notes_items` (`[NotesItem]`, an `ActionItem` plus
`line`, `by`, `source: "notes"`) is derived again from the document at every fold: `owner` is the first person chip's
name, `text` the line without person and time chips, `at` the first time chip's position on the meeting's joined
timeline, `done` the tick in the notes. `id` is 16 hex taken from the line id; `follow`, `due`, `issue_url` and
`task_id` are kept across folds. They are task objects like summary items (linking, fan-out of done/follow), listed
in `GET /api/action-items` and `/api/tasks`, and exported to Obsidian as action items.

`PATCH /api/sessions/{session_id}/notes-items/{item_id}`: body any of `{done, follow, due}` (fields left out stay;
`due` null clears it). Returns the `TaskItem` (`source: "notes"`, `idx` negative). Text and owner change in the notes
only. `done` and `follow` need read access (`done` is written into the notes as the tick, so editors see it);
`due` needs write access. `400` for `PATCH …/action-items/{idx}` with a notes item's negative `idx`; `403 "Tick it in
the notes"` on a meeting shared with you; `404` unknown meeting or item; `409` when the notes could not take the tick.
Ticks and new items go into the meeting's history (`notes_task_added`, `task_done`, `task_reopened`).

**Sidecar** (`mnemosyne-link notes doc`, notes-core/src/api.rs). Every answer carries `tasks`: the document's task
lines in order, `[{line, done, text, links: [[start, end, url]], frame}]` (`line` the element id of the line's `\n`,
`links` the live link marks as code-point ranges of `text`, `frame` the index of the request frame that made the
line or its kind, else null). The op `set_kind` `{notes, snapshot, frames, line, kind: "task"|"task_done"}` makes
that task line open or done and answers like `mark_comment` (`frames` to broadcast, `[]` when it already shows
`kind`; an unknown, deleted or non-task line is an error). The backend uses it for a tick made outside the editor.
