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
pnpm test:unit                 # Vitest + jsdom: the notes editor's pure parts (src/lib/notes)
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
  else none. Transcriber `auto` is Phonon-2 when `language` is English and it is installed, else
  WhisperX with working CUDA, else Parakeet. Phonon (transcribers/phonon.py) runs Fermion's
  `phonon serve` from its own venv, `<data_dir>/phonon`, installed on demand
  (services/phonon_install.py, pins in phonon-requirements.txt; never in the backend's lock:
  it needs torch and transformers 5). AMI ES2004a-d (2026-10-08): 22.7 % WER vs Parakeet's
  27.4 % (fillers counted), 53x vs 13x real time, same speaker accuracy; Silero VAD tuning did
  not help Parakeet. Missing, Parakeet is used. pyannote needs
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
- Publishing: after changing `viewer/` or `publication_format.py`, run `pnpm build:viewer` and commit
  backend/mnemosyne/api/static/viewer/ (the `viewer` CI job fails on a stale build). The Python and TS formats must
  agree: backend/tests/vectors/publication_v1.json is checked by both; regenerate it with
  backend/scripts/make_publication_vectors.py only on purpose (a format change breaks old links: bump `FORMAT`).
- Guest notes: after changing `guest/` or `guest_format.py`, run `pnpm build:guest` and commit
  backend/mnemosyne/api/static/guest/ (the `viewer` CI job fails on a stale build). The Python and TS formats must
  agree via backend/tests/vectors/guest_v1.json; regenerate it with backend/scripts/make_guest_vectors.py only on purpose.
- Publish host "version already exists" (409 with `current` = ours) counts as success only when `GET /pub/<id>`
  returns an ETag equal to the SHA-256 of our manifest; else another computer published that version (`_host_upload`).
- A "this computer" publication link only reaches others through the team listener (or `phone_url`): `local_reach`.
  With neither, the dialog says so. `add_seal` used to dedup publish events away; `EVENT_SEALS` forces them.
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
- Tests never open a public tunnel: conftest's `no_tunnel` stubs services/tunnel.py (a test once opened a real
  Cloudflare quick tunnel). Keep it that way.
- Guest frames carry a full snapshot: 8 MiB + 64 KiB, in BOTH frame.rs copies (link/src/guest/frame.rs and
  guest-wasm/src/frame.rs). They must match.
- Never `pgrep`/`pkill` with a pattern that appears in your own command line; use the
  `pgre[p]` bracket trick or `fuser -k <port>/tcp`. A `uv run uvicorn` child survives
  killing the `uv` wrapper; kill by port.

- Live notes need the link sidecar. services/link.py prefers `link/target/release` over `debug`, so
  a stale local release build silently turns live notes off (hello `unavailable`): rebuild it or set
  `MNEMOSYNE_LINK_BIN`. The e2e CI job builds link, so the sidecar specs run there.
- After changing notes-core or bumping hashseq's rev, run scripts/build-notes-wasm.sh
  (`WASM_BINDGEN` at ~/.cache/mnemosyne-tools/bin/wasm-bindgen) and commit `src/lib/notes/pkg`;
  keep rustc and wasm-bindgen as the script pins them or the bytes differ.
- Notes clients must `applyFrontier(encodeFrontier())` after applying remote frames or opening a
  snapshot (hashseq QUEUE 39) or the next drain ships everything back; mark strings over 14 bytes
  (URLs, `comment:<id>`) travel as 0xAF artifact frames beside the 0xDE delta, never inside it.
- Indexes in the notes model and the sidecar are code points, not UTF-16 (src/lib/notes/model.ts
  `cpLen`/`cpIndex`/`utf16Index`); an astral character is 1 there and 2 in a JS string.
- Notes chips are `link` marks with `mnemosyne:` URLs; dom.ts gives an `href` only to http(s) and mailto, so a chip
  (or a peer's `javascript:` link) never navigates. Anything that is not the editor must read `plain_notes` /
  `obsidian_notes`, never the raw markdown.
- A notes item's state (done, follow, due, issue_url, task_id) is written only through `notes_items.write_fields`: it
  `_touch`es the meeting for library sync and appends the `set_kind` frame for done. The derivation at a fold
  (`notes_items.apply`, `set_notes_items`) never `_touch`es: each machine derives its own. `set_text` may give a line
  a new id, so a vanished line's state moves to the line with the same text and owner.
- `src/lib/notes/ui/` imports only from `src/lib/notes/` (never `$lib/stores`, `$lib/api`, toasts): the guest Vite
  entry bundles it. App-only behaviour comes in through the host and provider objects.

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
0.16.0 (2026-10-12): Phonon-2 for English (transcribers/phonon.py `phonon serve` in 5-min pieces,
services/phonon_install.py on-demand install into <data_dir>/phonon, `/api/system/phonon`,
components/PhononCard.svelte, setup offers it first for an English locale; auto picks it when the
language is English). The real install was only exercised with stand-in uv/phonon in tests.
0.16.1: `phonon serve` without a model argument (0.16.0's install failed at that step).
For 0.17 (docs/plans/2026-10-12-for-0.17.md, from Corey's 141 action items, ~26 his): Phonon proven
in CI (phonon.yml, scripts/check-phonon.py: real install + transcription, a Release prerequisite),
pinned by hash (`--require-hashes`, model SHA-256 checked before each start, `MODEL_FILES`),
`live_transcriber` auto (Phonon for English, else Parakeet) and "Use Phonon-2" on the card; tasks:
"me"/compound owners in `mine_matcher`, speaker renames follow into owners, `ActionItem.follow` +
Mine/Following/Everyone, summary told who recorded it (`recorder_names`), others' copilot to-dos
kept out of the summary; Obsidian: `obsidian_task_scope` (others' items as plain bullets so vault
task queries list only yours; export-all job), ticks read back from exported notes
(services/vault_sync.py, every 5 min and before each export; an untick only reopens when the note
is newer than the meeting), `obsidian_link_notes` (export/links.py, vault_index.py), topic notes
and `obsidian_decisions_log` (export/topic_notes.py), Ask answers to the vault (export/ask_notes.py).
Only notes carrying our frontmatter marker are ever rewritten.
Two-way meeting notes (unreleased, phase 1 of docs/plans/2026-10-12-hashseq-collaboration.md):
export merges the user's edits in the vault with the new rendering instead of overwriting
(services/vault_sync.py `write_note`, export/note_merge.py, `vault_notes` table). The merge is
`mnemosyne-link notes merge` (link/src/notes.rs, hashseq pinned by git rev): its history keeps
the app's own branch next to the combined one, else a re-render deletes edits it never had.
Checkbox status and done dates stay out of the text merge (`normalize`/`restore`). With a record,
a tick counts when it differs from what we wrote (the mtime rule is only for notes without one).
No sidecar or a failure: the note is overwritten as before. Tests: conftest points
MNEMOSYNE_LINK_BIN at nothing; tests/fake_link.py is the fake sidecar; real-sidecar tests skip
without a build.
Summary as a document (unreleased, phase 2; design in that plan): a re-summarize, revise or
Obsidian edit MERGES with hand edits instead of replacing them (services/summary_doc.py
`apply_llm` / `apply_human`, `summary_docs` table). One merge history per block (body, each
decision, question and action item) keyed by stable ids (`ActionItem.id`, `decision_ids`,
`question_ids`, services/summary_ids.py; Obsidian block ids `^m-<id>`). Touched items survive an
LLM that drops them; hand-deleted ones are tombstoned; a revise rebases first or echoed edits
double. summary_data stays the projection everything reads. Edits in the vault's summary sections
flow back (vault_sync `apply_vault_edits`, export/note_parse.py) and those sections are cut out of
phase 1's text merge. No sidecar: replace + carry_over as before (`SummaryDocUnavailable`).
Phantom mic lines (2026-10-12, meeting ec7cf909, ~/.cache/mnemosyne-trials/phantom): Phonon turned
keyboard clicks and near-silence into "Yeah." etc.; not bleed (echo cancellation was on).
transcription/speech_gate.py (Silero from the HF cache, onnx extra) drops mic lines with fewer than
8 speech frames around their words (34 of 34 phantoms, 0 of 40 real lines) and skips live mic
buffers with fewer than 3 (`mic_speech_gate`). Live lines can be marked "Not said" (`live_flags`,
dropped from the final transcript of their part). dedup.similarity works on words.
Library sync (unreleased, phase 3; design in the hashseq plan, rules in
docs/plans/2026-10-14-library-sync-rules.md): two of Corey's computers share one library. Each
`mnemosyne-link home` also dials its sync peers (stdin control channel: pair/dial/hangup) and the
backends PULL each other over HTTP through the tunnel (services/sync_service.py, routes/sync.py;
peers and their tokens in `<data_dir>/sync_peers.json`). A meeting travels whole as a
`SyncSession` (models/sync_session.py, services/sync_state.py); `sessions.origin` names the
recording machine, `sync_vv`/`sync_seq` + repo `_touch` (every travelling write) track changes,
`sync_served` keeps merge bases, `sync_tombstones` deletes. Concurrent edits merge three-way
against the state the peer last saw (services/sync_merge.py; texts via the sidecar's `base`
form). Audio never travels: the origin does transcription and automation (services/origin.py,
409 "The audio is on <name>"), playback streams from it. Refused in team mode. New meeting ids
are 12 hex. Tests: test_sync_*.py run two apps through httpx.ASGITransport.
Tasks and decisions as objects (unreleased, phase 4; design in the hashseq plan): the link lives on
the item (`ActionItem.task_id`, `SummaryData.decision_links`), objects are derived
(services/task_links.py, decision_links.py; head = newest mention). done/follow/issue_url fan out to
every mention (internal writer without access checks) and `reconcile` repairs after sync and at
startup. Linking only reaches OLDER meetings' open objects: exact text always, close text
(`same_item`, >= 20 chars) only in related meetings (`task_linking`); optional LLM hints
(`task_linking_llm`, never provider "local", `ActionItem._continues` private attr). Obsidian: only
the head mention has a checkbox, earlier ones `↩ continued in [[note]]`. One-off migration at
startup (services/task_migration.py, meta `task_links_migrated`).
Comments (unreleased, phase 5): threads on transcript lines, audio moments, the summary text,
decisions, questions and action items (services/comments.py, routes/comments.py, `comments` table).
Anchors are re-found at read time, not stored positions: transcript = time span (part, start,
end) like bookmarks; items = block id; summary text = quote with prefix/suffix matched against
the RENDERED markdown (`plain_markdown`). Changed/detached comments are kept and shown. Comments
travel inside SyncSession, are keyword-searchable (`comments_fts`, never Ask/semantic), not exported
to Obsidian. Readers may comment; resolve = meeting owner, admin or thread author. Task comments
attach to a mention and are aggregated on the task object.
Live shared notes (unreleased, phase 6; design in the hashseq plan): the meeting's notes are one
hashseq document edited live by everyone who has them open (desktop, team web app). The CRDT runs
in every client as wasm (notes-wasm/, `src/lib/notes/pkg`, committed, rebuilt by
scripts/build-notes-wasm.sh with rustc 1.93.1 + wasm-bindgen 0.2.129; the CI `notes` job fails when
it is stale). The backend is an opaque relay with an append log (`/ws/notes/{id}`, api/notes_ws.py,
services/shared_notes.py, tables `shared_notes_ops`/`shared_notes`; origin = sha256 of
"mnemosyne/notes/"+id, computed in Python) and folds with the stateless sidecar `mnemosyne-link
notes doc` (fold/set_text/merge/mark_comment, from notes-core, the crate the wasm also compiles).
`Session.notes` = the folded markdown, what search, Obsidian, MCP, combine and the summary still
read; text written around the doc (notes route, sync's text merge, no sidecar) is caught up into it
as an `api` edit before a join or fold. Model (src/lib/notes/model.ts, notes-core/src/lib.rs): one
flat seq of lines ending in `\n`; the line kind is a closed `block` mark on the `\n`
(h1..table_head); bold/italic/strike grow at the end only, code/link/`comment:<id>` closed; tables
are `table` lines with U+001F between cells. The editor is our own contenteditable
(src/lib/notes/editor, kb.js primitives ported by name; commands/ops/undo are local and
id-anchored; harness at /dev/notes-editor). Library sync merges two documents by union
(services/sync_notes.py), no base. Comments on notes are marked by the SERVER (`notes_ids` ->
`mark_comment`), so a read-only reader can comment. Edit rule: owner, admin, shared-with; other
readers read. No sidecar: hello says `unavailable` and LegacyNotesEditor (the textarea) is used.
Publishing (unreleased, phase 7; design in the hashseq plan, use in docs/publishing.md): a curated, encrypted
copy of a meeting behind a link `https://<host>/p/<id>#<key>`, never the record. One encrypted snapshot per version
(services/publication_format.py: padded JSON manifest + Ogg Opus clip blobs, AES-256-GCM, AAD
`mnemosyne/pub/1|id|kind|version`, one random key per publication that only the fragment carries), built by the
`publish` job (services/publishing.py: selection, names, `redact_identifiers` + `redact_contacts`, quotes with
optional clips). The manifest has no transcript field, only chosen quotes; a test asserts it. The viewer is a
separate Vite entry (`viewer/`, Svelte 5, WebCrypto, no wasm), committed as backend/mnemosyne/api/static/viewer/
and rebuilt reproducibly with `pnpm build:viewer`; CI (`viewer` job) diffs it. The backend serves it and the sealed
files on open routes `/p/` and `/pub/` (api/routes/publishing.py), also on the team listener; `publish-host/` (axum
crate, `cargo test` in CI) + deploy/publish/ is the same read side with authenticated writes, caps and mandatory
expiry. Also: a one-file export (services/publication_file.py, `single.html`, optional PBKDF2 password), the
`publications` table travelling in `SyncSession.publications` (services/sync_publications.py; hosted ones editable
from either computer, "this computer" ones only from the one serving them), and records (publish, update, unpublish
seal a version; `publications.json` in the export, never the key). Team servers: `allow_publishing`, owner or admin.
Logos storage/delivery is only a written trial (docs/plans/2026-10-23-logos-publishing-trial.md).
Sharing with people (released as 0.23.0, phase 8; design in the hashseq plan, build order and corrections in
docs/plans/2026-10-23-phase-8-tasks.md): meetings shared between different people's desktops with
the owner's machines as the hub. A person key (services/person.py, Ed25519, sealed with encryption
at rest, shared by one person's machines through library-sync pairing). Contacts
(services/contacts.py): signed cards listing a person's machines, a `contact` invite, CONTACT devices
whose token is derived from a per-pair secret and opens only `/api/share/*` as that contact's
Principal. Tunnelled TCP reaches the backend from 127.0.0.2 (link sidecar), so it always needs a
token and never counts as this machine. Owner: `peer_shares`, a signed `SharedMeeting` per share
with only the chosen parts (services/shares.py), `/api/share/changes` and signed contributions
(comments, done/follow, notes frames) checked before the normal write paths. Recipient:
`sessions.shared_from`, SharePuller (pull + verify + `apply_shared`), the outbox
(services/share_push.py), local rules in services/shared_with_me.py; such meetings never enter
library sync, task links, Ask, semantic search, the digest or Obsidian (`shared_in_*`). Demo mode
seeds Ada Lovelace's shared meeting only through `POST /api/demo/share` (the e2e spec calls it).


Guest notes (released as 0.24.0, phase 9; design in the hashseq plan, build order and corrections in
docs/plans/2026-10-24-phase-9-tasks.md, use in docs/guest-notes.md): a link `https://<host>/n/<room>#<key>` for the
people in a meeting to type into the live notes, comment on them and see the agenda; nothing else goes out. Sealed
frames (services/guest_format.py, AES-GCM under HKDF keys from the fragment key, AAD `mnemosyne/guest/1|room|kind`)
relayed by a room on a publish host (publish-host/src/rooms.rs + live.rs: WebSocket relay, log, snapshot, caps).
services/guest_notes.py is the bridge (hidden `shared_notes` member seals local frames out; guests come in through
`accept_frames` as `guest:<gid>`; comments via `comments.create` with an explicit actor, marked by the owner's server;
sealed snapshots and META with the agenda); api/routes/guest_notes.py is start/end/new link/stop. The page is `guest/`
(Vite entry reusing src/lib/notes and the wasm), built with `pnpm build:guest` into backend/mnemosyne/api/static/guest
and diffed by the CI viewer job; vectors backend/tests/vectors/guest_v1.json are shared by Python and TS; the e2e job
runs a local publish host on 8028 (tests-e2e/start-publish-host.sh). Settings: `guest_host` (else `publish_host`, else
ours, which does not run yet: 409 "No host for guest notes"), `guest_notes_end_hours` (12 after the recording stops),
`allow_guest_notes` (team servers, owner or admin). Rules: the host never sees the key; owner-side frames (typing,
copilot, phase 8) reach guests as the owner's; guest comments are ordinary comments (searchable, synced, in records);
one room per meeting; a deleted meeting's room is deleted; refused for shared_from meetings and without the sidecar.

Guest notes without a host (released as 0.25.0, phase 10; design in the hashseq plan, build order and corrections in
docs/plans/2026-10-25-phase-10-tasks.md, use in docs/guest-notes.md): guest notes need no publish host. The rooms
sidecar `mnemosyne-link rooms` (link/src/guest.rs; services/link.py `RoomsLink`, started on demand, NOT tied to remote
access) embeds the publish host on a loopback port and gives each room its own guest iroh endpoint (ALPN
`mnemosyne/guest/1`, one bi stream per guest, framed, proxied to the loopback WebSocket); room keys live in
`<data_dir>/guest_rooms/keys` so a link survives restarts. services/guest_notes.py `plan` (setting `guest_transport`):
auto = iroh, else tunnel (services/tunnel.py: Tailscale Funnel, else a Cloudflare quick tunnel, cloudflared fetched by
services/downloads.py), else host; a configured publish host is also a MIRROR (a second Bridge, same room id and key).
Link format 2: `https://<origin>/n/#2.<room>.<key>.<endpoint>.<relay host>`; the page tries iroh first, then the host's
wss, with a `since` per transport. The page is served from GitHub Pages (`guest_page_url`; pages.yml adds the meta CSP
to that copy only) and keeps a sealed copy in IndexedDB. guest-wasm/ (iroh in the browser, relay-only) is built ONLY by
CI (no clang locally): after changing it, push, take the `guest-wasm-pkg` artifact of the guest-wasm job (`gh run
download <run> -n guest-wasm-pkg -D guest/pkg`), commit guest/pkg, then `pnpm build:guest` and commit
backend/mnemosyne/api/static/guest. The guest e2e uses `mnemosyne-link dev-relay` on 8038 (feature `dev-relay`, never
in releases). The room stays up only while this computer is on (sleep inhibitor held while a room is live).

The notes editor, live (released as 0.25.0, phase 11; design in the hashseq plan, build order and corrections in
docs/plans/2026-10-25-phase-11-tasks.md): one editor for the desktop and the guest page, tools where the work is, live
carets, chips, and action items typed in the notes. `src/lib/notes/ui/` (`NotesEditor.svelte`, slim, selection and
touch toolbars, `/` `@` `[[` menu in slash.ts, `CaretOverlay.svelte`) is mounted by components/SharedNotes.svelte and
guest/src/Notes.svelte through a `NotesHost` plus `presence` / `inserts` providers (src/lib/app/notesPresence.ts,
notesInserts.ts, guest/src/presence.ts, inserts.ts); input.ts gained `command`, `setLineKind(kind, {toggle:false})`,
`onTyped` and `interceptKey`. Presence: `Member.key` (`m<8 hex>`), `sel` ({a, f} element ids) and `typing` in memory in
services/shared_notes.py, `presence.members` plus coalesced `caret` (100 ms) to the editors; the guest bridge is a
`presence_tap` member that seals owner-side carets to the room as PRESENCE `o:<key>` (guest_notes.py), hears guests'
PRESENCE `sel` / `gone` back as `guests` + `guest_caret`, and forwards a guest's envelope into the other rooms
(publish-host rooms.rs has a separate presence channel, 10/s). Chips are `link` marks with `mnemosyne:t/<part>/<s>`,
`person/<name>`, `meeting/<id>` URLs (src/lib/notes/chips.ts; services/notes_chips.py `plain_notes`, `obsidian_notes`,
`retarget_chips`, `task_line_parts`); time chips count from `clock` {part, offset, started_at} (services/parts.py
`recording_clock`, on start/active recording and the guests' META). Notes items (services/notes_items.py): a task line
with a person chip is an action item, `Session.notes_items` (`sessions.notes_items`, ids 16 hex from the line id)
derived at every fold-result site from the sidecar's new `tasks` list; they are task objects (`TaskItem.item_id`,
`source`, `idx` < 0; task_links, item_flags, Obsidian, sync's `SyncSession.notes_items`), changed through
`PATCH /api/sessions/{id}/notes-items/{item_id}`, and a tick goes into the document with the sidecar's `set_kind`
(notes-core/src/api.rs). Tests: backend test_notes_items / test_notes_chips, Vitest chips/slash/carets/placement,
tests-e2e/notes-items.spec.ts; harness at /dev/notes-editor.

Candidates next: offline installer (pre-seeded uv cache). Flathub is on hold (Corey, 2026-09-29).

Frontend package manager: pnpm is pinned via `packageManager` (corepack). If `pnpm` complains
about an unexpected store location, run `pnpm install --config.confirmModulesPurge=false`.
