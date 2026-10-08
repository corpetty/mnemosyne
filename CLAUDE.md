# Mnemosyne

Linux desktop app for recording meetings (mic + system audio via PipeWire), transcribing
and diarizing them locally, summarizing with an LLM, and exporting to Obsidian.

Three parts, one repo:

- `src/` SvelteKit + Svelte 5 runes + Tailwind 4 frontend (static SPA, no SSR).
- `src-tauri/` Tauri v2 Rust shell. Only spawns/supervises the backend and owns the window.
- `backend/` Python 3.13 FastAPI service on `127.0.0.1:8008`. All real logic lives here.

Read `docs/architecture.md` before changing data flow. `docs/api-reference.md` is the HTTP/WS contract.

## Commands

```bash
pnpm tauri dev                 # full app: Vite + Tauri window + backend (spawned by Rust)
pnpm check                     # svelte-check, must stay at 0 errors
pnpm test:backend              # backend pytest (no GPU or network needed)
pnpm test:e2e                  # Playwright browser tests against a demo-mode backend (port 8018)
pnpm lint:backend              # ruff check + format --check
cd backend && uv run mnemosyne-bench --synthetic   # WER / speaker accuracy / speed of the engine
cd backend && uv run pytest    # same as test:backend
cd backend && uv run ruff format .   # auto-format before committing
```

Backend setup: `cd backend && uv sync --extra gpu --extra onnx --group dev` (uv sync is exact: leaving an
extra out uninstalls it; `onnx` is Parakeet). The `gpu` extra pulls torch
(CUDA 12.8 index, pinned in pyproject) and WhisperX. `uv.lock` is committed; do not
`uv pip install` things by hand, add them to `pyproject.toml` and re-lock.

## Conventions

- The backend is an installable package, `backend/mnemosyne/` (hatchling, installed editable by
  `uv sync`). Import it as `mnemosyne...`; inside the package use relative imports.
  `backend/main.py` is only a shim for `uvicorn main:app`; the CLI is `mnemosyne-backend`.
- ML code is imported lazily inside functions so the API starts without torch. Keep it that way;
  tests rely on it (`tests/fakes.py` provides `FakeEngine` / `FakeProvider`).
- Transcription is two pluggable stages: `Transcriber` and `Diarizer` Protocols in
  `transcription/engine.py`, joined by `ComposedEngine`, built from settings in
  `transcription/registry.py`. LLM providers implement `summarization/provider.py`.
  New implementations go behind those Protocols, never into routes or the pipeline.
- Long work runs as a Job (`jobs.py`) submitted from a route; progress flows over the
  `EventBus` to the WebSocket. Routes never call ML code directly.
- Demo mode (`MNEMOSYNE_DEMO=1` + `transcriber`/`diarizer`/`default_provider` = "demo",
  `mnemosyne/demo.py`) gives canned, deterministic transcripts and LLM replies. The e2e tests
  (`tests-e2e/`, started by `tests-e2e/start-backend.sh`) depend on its exact strings.
- `MNEMOSYNE_DATA_DIR` overrides the data directory; tests set it to a temp dir in `conftest.py`.
- REST types in the frontend are generated: after changing a response/request model, run
  `pnpm gen:api` (writes `src/lib/api/schema.d.ts`; CI fails when it is stale).
  `src/lib/types/index.ts` only aliases generated types plus the hand-written WebSocket events.
  Response models subclass `mnemosyne.models.base.ApiModel` so defaulted fields are required.
- App behaviour spanning stores (connect, recording actions, shortcuts, tray) lives in
  `src/lib/app/controller.svelte.ts`; view state in `stores/ui.svelte.ts`; `+page.svelte` only composes.
- Frontend state is class-based rune stores in `src/lib/stores/*.svelte.ts`. Cross-store
  communication is via callbacks, not imports, to avoid cycles.
- Secrets and machine-specific config live in `backend/.env` (gitignored). Never commit it.

## Gotchas

- Wayland + WebKitGTK needs `WEBKIT_DISABLE_DMABUF_RENDERER=1` (already in `pnpm dev:app`).
- NVIDIA: WebKit's GPU compositing stops presenting frames for good after a workspace or monitor
  change (the AppImage runs under XWayland: its GTK hook forces `GDK_BACKEND=x11`). The page's JS
  keeps running, the window never repaints, even on resize. main.rs sets
  `WEBKIT_DISABLE_COMPOSITING_MODE=1` when /proc/driver/nvidia exists (2026-09-29). That was not
  enough (2026-10-05: frozen with clicks still handled, idle main loop; GTK 3 frame sync waiting
  for a _NET_WM_FRAME_DRAWN that never came). Since then display.rs sets `GDK_BACKEND=wayland` in a
  Wayland session, over the hook (`MNEMOSYNE_X11=1` keeps XWayland); a release start on Wayland
  that dies before its window has been up 10 s leaves `<config>/display-x11` and later starts use
  X11 (`MNEMOSYNE_WAYLAND=1` forgets it). Under X11 frames.rs turns
  frame sync off; tray "Redraw window" / `mnemosyne --redraw` give the window a new X window.
  Building the AppImage locally needs `patchelf` on PATH (the gstreamer plugin).
- System Python is 3.14; the backend pins 3.13 via `backend/.python-version`. WhisperX
  does not support 3.14 yet.
- Diarizer `auto` (the default) is Nemotron when NeMo is installed and CUDA works, else pyannote
  when it is installed and `HF_TOKEN` is set, else `onnx` (sherpa-onnx on the CPU, onnx extra),
  else none. Transcriber `auto` is WhisperX with working CUDA, else Parakeet. `phonon` (Phonon-2,
  English only, never auto) runs Fermion's `phonon serve` (transcribers/phonon.py): the
  fermion-research package is not a dependency, people install it; missing, Parakeet is used. pyannote needs
  `HF_TOKEN` with its model license accepted; Nemotron and onnx do not, but voice profiles and live
  speaker labels still use pyannote's embedding model, so they do. CI has no ML packages: tests
  that assume one monkeypatch `registry.installed`.
- NeMo (for Nemotron) is in the gpu extra as a pinned GitHub source tarball of NVIDIA-NeMo/Speech
  main (PyPI 3.0.0 cannot load the model; a tarball so installs need no git). It pins
  lightning<=2.4, omegaconf<=2.3 and packaging<25, which pyannote and WhisperX accept. When
  bumping it, re-run `uv lock` and the AMI check below.
- `src-tauri/target/`, `link/target/` and `backend/.venv/` are multi-GB build artifacts; `src-tauri/binaries/`
  and `src-tauri/resources/` are generated by `scripts/build-all.sh`. All gitignored. Do not
  read or grep them.
- Release builds do not bundle Python or torch. The Rust shell runs the bundled `uv`
  sidecar to `uv sync` the backend into `~/.local/share/com.corpetty.mnemosyne/venv` on
  first launch. Keep `backend/uv.lock` committed and in sync with `pyproject.toml`.
- WhisperX's default pyannote VAD can report "no speech" on quiet recordings; the
  `whisper_vad` setting defaults to `silero` for that reason.
- Parakeet defaults to the int8 model: the fp32 one uses an external-data file that ONNX
  Runtime refuses to follow through the HuggingFace cache symlink.
- When testing a bundle with a fake `HOME`, put it on real disk (e.g. `~/.cache/...`), never
  under `/tmp`: `/tmp` is tmpfs here, uv cannot hardlink across filesystems and copies the
  7 GB venv into RAM, which has frozen the machine.
- Fedora's uv RPM ships `/etc/uv/uv.toml` (python-downloads = "manual", python-preference =
  "system"), which the bundled uv also reads; lib.rs overrides both via `UV_PYTHON_*` env vars.
  Smoke tests with a fake HOME do not avoid /etc, so they catch this.
- AppImage: build with `NO_STRIP=true` (linuxdeploy's strip chokes on `.relr.dyn`). Tauri's
  bundler makes `.DirIcon` an absolute symlink into the CI build dir; the Release workflow runs
  `scripts/fix-appimage.sh`, which makes it relative, repacks and signs the AppImage again. The
  AppRun exports `PYTHONHOME`, `LD_LIBRARY_PATH` etc. for the GUI; `lib.rs` scrubs them
  before spawning uv/Python or the backend dies with "No module named encodings".
- Capturing a sink: `pw-record -P '{ stream.capture.sink=true }' --target <sink node.name>`.
  Never `--target <sink>.monitor`: that is a PulseAudio name PipeWire does not resolve, and
  pw-record silently records the default microphone instead (this was a real bug until
  2026-09-24). Verify with `pw-link -l` that pw-record's inputs come from `<sink>:monitor_*`.
- Never play test audio on this machine (pw-play, self-test tones included) without asking:
  `pw-play --target <x>` falls back to the default output when `<x>` cannot run (a null sink
  made with `pw-cli create-node` has no driver), and it played a synthetic voice into Corey's
  podcast (2026-09-25). Test live features offline: transcribe a file and feed the segments in.
- This machine runs EasyEffects: recording streams from the mic get rerouted to
  `easyeffects_source`, whose RNNoise outputs exact digital silence when nobody speaks. A −90 dB
  mic level with the mic unmuted is that, not a capture bug.
- Updates: the Tauri updater (AppImage, deb and rpm) reads `latest.json` from the latest
  GitHub release; `scripts/updater-manifest.py` writes it in the Release workflow. Bundles are
  signed with `~/.tauri/mnemosyne.key`, password in `~/.tauri/mnemosyne.key.password` (repo
  secrets `TAURI_SIGNING_PRIVATE_KEY` and `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`; public key in
  tauri.conf.json). A key without a password cannot sign non-interactively (tauri-cli 2.10). Losing the key means shipped apps can never update again.
  `createUpdaterArtifacts` is only turned on in CI (`--config`), so local builds need no key.
- The Release workflow runs the AppImage smoke test (reusable `smoke.yml`) before publishing. To
  re-test an existing build without rebuilding: `gh workflow run "Smoke test" -f run_id=<Release
  run id>`. The script refuses to run outside CI (it starts PulseAudio with a null sink).
- Meetings can have several parts (services/parts.py): `Recording.part`/`offset`, the session's
  `audio_file` is all parts joined, transcript times are on that joined timeline. Never replace a
  session's audio or transcript wholesale when recording into it again; go through `add_part()`.
- Backups (services/backup.py): restores are staged and applied at the next start in
  `AppContext.build`, before the database opens; never unpack over a running backend. Tests and
  the e2e demo backend set `backup_dir` to a temp folder: never let them write ~/Documents.
- Meeting history (services/history.py, `session_events`): code that starts, stops, saves,
  recovers, imports, combines, transcribes or deletes a meeting's audio logs it with
  `history.log()`, which never raises. The Recording tab's "Parts & history" card shows it.
- Encryption at rest: code that reads audio must go through `storage.crypto.plaintext()` (files
  may be `<name>.enc`), and new audio must be sealed with `seal_session_audio()`. Tests and demo
  mode never use the real keyring (MemoryKeyStore / FileKeyStore); keep it that way.
- Backend lifetime: release backends get `MNEMOSYNE_APP_PID` and end with the app, except that a
  recording outlives a crashed app by 15 minutes so a relaunch can take it over
  (api/app_watch.py, `/api/system/attach`, `existing_backend` in lib.rs). The CLI binds the port
  before startup (cli.py `bind`): uvicorn binds after the lifespan, and a second backend's startup
  used to stop the first one's recorders. Recovery leaves recorders whose parent is a live Python.
  A backend that vanishes under a running app is started again by lib.rs `watch_backend` (only
  once the port is free; not while `BackendState.watched` is off: starting, restarting, exiting).
- The Rust shell never has the API token (`api_token`, set for pairing and remote access), so its
  `backend_request` calls to /api get 401 when one is set. A path the shell needs goes in auth.py
  `LOCAL_PATHS` (open to loopback, not the team port, no proxy headers). Attach was missing it
  until 0.15.0: relaunched apps never took their recording over (2026-10-05).
- LLM calls that need no reasoning (glossary pass, copilot notes) use `complete(..., think=False)`
  (provider.py `think_kwargs`): vLLM/built-in get `enable_thinking: false`, Ollama `think: false`.
  On Corey's vLLM Qwen a 40-line batch took 20-150 s thinking, 1-3 s without; `/no_think` in the
  prompt made it slower.
- Nemotron streaming (live speakers) is a second model instance on purpose: streaming settings
  live on the model, so sharing the offline diarizer's would let a final job change them under a
  recording. Its attention runs through SDPA (`sdpa_attention`): NeMo's compiled FlexAttention asks
  Triton for 80 KB of shared memory on the streaming shapes, more than Turing (RTX 20xx) has.
  `MNEMOSYNE_GPU_TESTS=1 uv run pytest tests/test_nemotron_stream.py -s` checks it against NeMo's own
  chunked pass (needs the AMI files); re-run it when bumping NeMo.
- The AppImage runs the backend, web app and link sidecar from a copy in
  `<app data>/runtime/<version>` (lib.rs `runtime_copy`): its own mount vanishes when it exits, and
  the backend may outlive it. Edit the bundled sources, never that copy. The copy is reused while
  the version is the same, so installing a local build over the same version needs
  `rm -rf <app data>/runtime/<version>` (app closed) or its backend changes do not run.
- Tests never touch the machine: conftest stubs the sleep inhibitor (`awake._spawn`), Tailscale
  (`team_host.tailscale_name`) and points `HF_HUB_CACHE` at a temp dir. Keep it that way.
- This machine has little memory to spare (a mining node, ~7 of 31 GB free): the full backend
  pytest suite OOM-killed the desktop app (2026-10-05), and CPU-heavy runs have frozen it. Locally
  run single test files, `pnpm check`, `cargo check`, ruff; leave the full suite, full e2e and
  builds to CI (push and watch the run, or a Release workflow_dispatch), or ask Corey first.
- Never `pgrep`/`pkill` with a pattern that appears in your own command line; use the
  `pgre[p]` bracket trick or `fuser -k <port>/tcp`. A `uv run uvicorn` child survives
  killing the `uv` wrapper; kill by port.

## Roadmap (agreed 2026-09-22)

0. Done: lockfile, GPU extra, ruff, pytest with fakes, this file.
1. Done: job runner + event stream, SQLite sessions, persisted config, per-source audio,
   UI never passes file paths.
2. Done: Transcriber/Diarizer protocols; whisperx, parakeet (onnx), remote transcribers;
   pyannote community-1; per-source speaker labelling.
3. Done: live provisional transcript while recording (transcription/live.py, Parakeet on
   CPU by default), replaced by the final job after stop.
4. Done: no PyInstaller; backend source + uv.lock + uv sidecar in the bundle, installed
   into a per-user venv on first launch (gpu extra only when nvidia-smi exists).

Added after the phases (all on main, 2026-09-22): echo dedup between mic and system
transcripts (transcription/dedup.py); voice profiles with auto-labelling
(services/speaker_service.py, `speakers` tables); transcript editing (routes/segments.py);
FTS5 search (routes/search.py); structured summaries with styles and instructions
(summarization/prompts.py, `summary_data`); Obsidian export with [[people]] links and tasks;
audio playback with range requests and file import (routes/audio.py); CI + tag releases
(.github/workflows).

2026-09-23: PipeWire echo cancellation on demand (audio/echo_cancel.py) and server mode with
bearer tokens (api/auth.py, frontend stores/connection.svelte.ts). Quick-wins sprint
(docs/plans/2026-09-23-quick-wins.md): summarization is a `summarize` job with
`auto_summarize`; untitled sessions are named from `summary_data.title`
(`auto_name_sessions`); summaries and notes render as markdown (components/Markdown.svelte);
released as v0.2.1.

Ask across meetings (routes/ask.py, services/ask_service.py, components/AskPanel.svelte).

Storage report, per-session audio deletion and audio retention (services/storage_service.py).

Tray + `mnemosyne --toggle|--start|--stop` via single-instance (src-tauri/src/lib.rs); device
selection remembered by node name.

Calendar via ICS feed (services/calendar_service.py): session naming, attendees, start banner.

Live speaker labels (transcription/live_speakers.py): pyannote embedding + online clustering +
voice-profile naming, `live_relabel` events.

Added 2026-09-24 (docs/plans/2026-09-24-next-ten.md, released as 0.5.0): installable backend
package + generated API types; level meters and capture self-test (audio/levels.py); stage
progress with ETA; glossary (transcription/glossary.py); MCP server (mcp_server.py); action items
to GitHub issues (services/github_service.py); weekly digest (services/digest_service.py);
Playwright e2e on a demo-mode backend; Tauri updater; Flatpak (flatpak/).

Neat-ideas batch (docs/plans/2026-09-24-neat-ideas.md, released as 0.6.0): talk time
(services/stats.py), chapters (`summary_data.chapters`), tasks across meetings
(services/tasks.py, `ActionItem.done`, carried over on re-summarize), follow-up drafts
(services/followup.py), pre-meeting brief (services/brief.py), mention alerts
(transcription/mentions.py, `mention` events, tauri-plugin-notification).

Next batch (docs/plans/2026-09-24-next-batch.md, released as 0.7.0): map-reduce summaries, semantic search
(search/), people (services/people.py), topics (services/topics.py), local-only + redaction
(summarization/privacy.py), auto-record (audio/streams.py + controller), Linear/Jira/Slack/Matrix
(services/trackers.py, chat_post.py), phone page (/m), two-phase install + Flatpak GPU (lib.rs),
mnemosyne-bench (bench.py).

After 0.7.0 (2026-09-25): review fixes, one `uiState.view` + grouped header, Settings tabs,
first-run setup wizard (components/SetupWizard.svelte, `/api/system`, `setup_complete`), live
copilot (services/copilot.py, `copilot_notes` events, components/CopilotPanel.svelte).
Road to 0.8 (docs/plans/2026-09-25-road-to-0.8.md, unreleased): recovery of interrupted
recordings on startup (services/recovery.py, `recording.json` per recording); AppImage smoke test
in CI (.github/workflows/smoke.yml, scripts/smoke-appimage.sh, `MNEMOSYNE_SMOKE` in lib.rs and
src/lib/app/smoke.ts); adaptive live interval (`live_adaptive`, /proc/pressure/cpu); backend log
file `<data_dir>/logs/backend.log` (mnemosyne/logs.py) and Copy diagnostics
(services/diagnostics.py); copilot notes saved with the session and fed to the summary; summary
items with transcript times (`decision_at`, `question_at`, `ActionItem.at`); Ctrl+K palette
(components/CommandPalette.svelte).
After 0.8.0 (docs/plans/2026-09-28-after-0.8.md, unreleased): GPU-extra install check in CI
(gpu-extra.yml, scripts/check-gpu-extra.py, a Release prerequisite); live labels corrected by
re-running Nemotron every 30 s (transcription/live_rediarize.py, `live_labels` events); per-word
speaker labels (transcription/assign.py); echo canceller pinned to the chosen mic
(`echo_cancel_mic`); "Who is who?" card (components/SpeakerNamingCard.svelte,
`speakers_reviewed`); quotes with audio clips (services/clips.py, `recordings/<id>/clips/`); due
dates on action items; Report a problem (clipboard + GitHub new-issue link); offline AppImage
(scripts/build-offline-appimage.sh, `offline/` in the bundle, `install_offline` in lib.rs);
encryption at rest (storage/crypto.py, services/encryption.py: SQLCipher + AES-GCM `.enc` audio,
key in the keyring, locked mode answers 423); MIT metadata and docs/flathub.md (not submitted).
Released as 0.9.0. After it (unreleased): recording screen (Stop pill in the header, live transcript
beside the copilot), calendar source off/ics/desktop (services/desktop_calendar.py reads GNOME
Online Accounts via evolution-data-server over D-Bus with jeepney), multi-part meetings.
Phone pairing (services/pairing.py, routes/pairing.py): one-time QR codes traded for per-device
tokens that only open the phone upload path; `phone_url` setting; Tailscale guide in
docs/remote-access.md. Remote access over iroh (link/ crate, `mnemosyne-link home` run by
services/link.py while `remote_access` is on): paired computers ("desktop" devices, bound to their
iroh endpoint id) tunnel TCP to 127.0.0.1:8008; tested with `cargo test` in link/. Desktop app
integration: src-tauri/src/remote.rs + Settings (RemoteConnect, RemoteAccessHome). Relays:
`remote_relays` (blank = n0's public ones); computers follow the relays in home's ticket; self-host
with deploy/relay/ (open iroh-relay, rate limits, no gating service). Local first (Corey's rule
for defaults): home announces itself by mDNS (iroh-mdns-address-lookup), computers only listen,
so same-network connections never touch a relay. Fallback relays: ours by default once they exist
(Corey, 2026-09-28), not n0's; until then blank `remote_relays` means n0's. Logos Messaging is later.

Nemotron diarizer (2026-09-25, transcription/diarizers/nemotron.py): NVIDIA Nemotron-3-Diarization
(Streaming Sortformer, max 8 speakers) via NeMo, default on NVIDIA through `diarizer = "auto"`;
`config_version` migrates saved "pyannote" to "auto". On AMI ES2004a-d with per-word labels it put
99.6% of words with the right speaker vs pyannote's 98.4%, ~10x faster (4 s vs 43 s per 38-min meeting).
Its DER is higher (22% vs 15%) only from missed speech, which the transcriber's words make moot.

For 0.10.0 (2026-09-28, unreleased): speech models unloaded when idle
(`unload_models_after_minutes`, ModelService.unload_if_idle); glossary entries from transcript
corrections (routes/glossary.py); Obsidian daily note (export/daily_note.py); bookmarks
(routes/bookmarks.py, `--mark`, stored as part + seconds); agenda followed by the copilot
(`agenda_covered`, calendar descriptions); meeting types (services/meeting_types.py,
`settings.meeting_types`); resources library (services/assets.py, `assets` tables,
<data_dir>/assets); other assistants' notes (services/external_notes.py; notes-only meetings are
summarized from them). Extra context reaches the summary as instructions (pipeline.py).

Live speakers from Nemotron streaming (2026-09-29, docs/plans/2026-09-29-nemotron-streaming.md,
unreleased): `live_diarizer` (auto = streaming on NVIDIA); transcription/diarizers/nemotron_stream.py
(own model instance, low-latency config, SDPA attention), transcription/live_streaming.py (timeline,
per-word split, `VoiceNamer` for earlier parts and voice profiles, `AppContext.live_voices`);
`mnemosyne-bench --live` (bench_live.py) and scripts/fetch-ami.py (AMI from the HF mirror into
~/.cache/mnemosyne-trials/ami). AMI ES2004a-d: 95.8-97.4% of live words on the right speaker vs
92.9-96.7% for clustering + 30 s re-diarization, at similar or lower GPU time (~20 ms per 0.72 s).
More than 8 voices in one stream get merged. Released as 0.11.0.

Team features (unreleased): built 2026-09-30 for a financial-advisory pilot the firm declined, then
made general (docs/plans/2026-10-01-teams.md; the old plan is docs/plans/2026-09-30-advisory-pilot.md).
Don't pitch it at a vertical. `team_mode`: sign-in with invite codes and roles (access.py,
services/users.py: member sees own meetings, reviewer reads all, admin; users.json;
`mnemosyne-backend users`), enforced in SessionRepository (`Hidden` = 404), someone else's meeting is
read-only in the UI (src/lib/app/access.ts `canChange`); the backend serves the web app (`web_dir`,
api/web.py); key from a TPM-sealed systemd credential (`CredentialKeyStore`); deploy/team/,
scripts/team-server-check.sh, docs/team-server.md. `cloud_models` off: no OpenAI/Anthropic provider
is created. Browser recorder (AudioWorklet PCM over `/api/record/{id}/{source}`, `BrowserProcess`,
devices -1/-2). Consent before each recording (`require_consent`). Identifier redaction
(summarization/privacy.py). HubSpot push and company sync (services/hubspot.py). `client` summary
style with `client_facts`. Records (services/records.py: versions, chained seals,
`records_retention_years`, legal hold, deletions log, export zip). Flagged phrases
(services/supervision.py, `review_phrases`, `supervision_flags`/`supervision_reviews`, Review view;
a new transcription replaces flags, edits only add). Organizations (services/organizations.py,
People > Organizations, `organization` in the brief). Released as 0.12.0.
After it (docs/plans/2026-10-01-team-sharing.md, released as 0.13.0): sharing (services/sharing.py,
`session_shares`, "*" = everyone; read access plus ticking action items; SessionRepository `_SEES`;
`share_with_invitees`; ShareMenu.svelte), per-person preferences (services/prefs.py, `user_prefs`,
`effective`/`for_meeting`: a meeting's work uses its owner's; `/api/users/me/prefs`,
MyPreferences.svelte; `calendar_for`), your tasks (`TaskItem.mine`, `mine_matcher`) and a weekly
digest per person on a team server (`maybe_schedule_digest`).

Start anywhere (docs/plans/2026-10-01-start-anywhere.md, released as 0.14.0): engines that fit the machine
(`resolve_transcriber`/`resolve_diarizer` in transcription/registry.py), a CPU diarizer
(transcription/diarizers/onnx.py, models fetched with pinned SHA-256s by services/downloads.py),
a sample meeting (`POST /api/audio/sample`, backend/mnemosyne/samples/, AMI CC BY 4.0); a built-in
summary model (services/local_llm.py: pinned llama.cpp `llama-server`, Vulkan or CPU, pinned Qwen3
GGUFs; provider "local", not retried, stopped when idle) and Ollama pulls (routes/local_model.py);
"Share this computer with my team" (services/team_host.py, routes/team.py: a second HTTPS listener
on `team_port` with a self-signed cert in <data_dir>/tls, the owner is `team_owner_id` and the
desktop's tokenless loopback requests run as them, the web app is bundled as resources/web).
After it (docs/plans/2026-10-02-shared-desktop-and-first-run.md, released as 0.15.0): close to the tray
(src-tauri/src/shell_prefs.rs, `<app config>/shell.json`); `keep_sharing_after_quit` (`/health`
`outlives_app`, app_watch keeps serving); sleep inhibitor while recording (services/awake.py,
`keep_awake_while_sharing`); `tailscale cert` served by SNI (`team_tailscale_cert`) and the
self-signed cert reloaded in place when addresses change; download progress and a setup
`prepare_models` job (services/model_downloads.py, `POST /api/system/prepare`); GPU support a setup
choice (`gpu_support`, Tauri `start_gpu_install`); the offline AppImage carries the CPU models
(`mnemosyne-backend prefetch`). Windows/macOS: planned only, docs/plans/2026-10-02-windows-and-macos.md.
Also in 0.15.0, from a crash mid-meeting (2026-10-05): attach without the API token (auth.py
`LOCAL_PATHS`), `watch_backend` in lib.rs and a "Backend not answering" watchdog in the UI, quit
without a backend, the glossary pass without thinking (four batches at a time, "(3 of 12)"), a
transcript saved when the job stops during that pass, the view following a running transcription
after looking at another meeting.
0.15.1 (2026-10-07, docs/plans/2026-10-07-for-0.15.1.md): the built-in model summarizes in parts of
`LocalProvider.max_chars` (20k characters); copilot notes without thinking (44 s → 9 s an update on
Corey's vLLM Qwen); the frozen-window fix (native Wayland by default for the AppImage with a
fallback to X11, display.rs; X11 frame sync off, tray "Redraw window" / `--redraw`, frames.rs), not
yet confirmed in daily use; summaries editable by hand (services/summary_edit.py, `PUT …/summary`
and `…/followup`, components/SummaryEditor.svelte; `edited_at` makes re-summarize ask first);
earlier summaries restored whole (`session_versions.summary_data`, SummaryVersions.svelte); topics
renamed or merged across meetings (`settings.topic_aliases`); tasks edited from the Tasks view
(`TaskItem.can_edit`); revise with an instruction (`revise` on summarize); Transcribe offered once a
stopped recording is saved (`finish` job completion reloads the meeting); no PyTorch compile worker
pool and memory handed back after the idle unload; a Wayland smoke test (headless Weston).

Candidates next: offline installer (pre-seeded uv cache). Flathub is on hold (Corey, 2026-09-29).

Frontend package manager: pnpm is pinned via `packageManager` (corepack). If `pnpm` complains
about an unexpected store location, run `pnpm install --config.confirmModulesPurge=false`.
