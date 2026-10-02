# Windows and macOS

Status: plan only (Corey, 2026-10-02: "plan it … we'll look at starting it later"). Written from a
survey of everything that ties the app to Linux; file references are as of 0.14.0.

## Goal

The desktop app on Windows 10/11 (x86_64) and macOS 14.2+ (Apple silicon): record the microphone and
the system audio, transcribe and label speakers locally, summarize, share the computer with a team.
Same backend, same UI, same release cadence as Linux.

Not in scope at first: Intel Macs (onnxruntime has no x86_64 macOS wheels for our Python), Windows on
ARM, Nemotron on either (NeMo does not support them in practice), echo cancellation, auto-record by
meeting-app detection, the desktop-calendar source (ICS works everywhere), the Flatpak-style
sandboxed installs.

Today a Windows or Mac user can already take part through someone's shared Linux computer or a
team server, in Chrome or Edge (src/lib/app/browser-capture.ts). That stays the fallback.

## What breaks today

- **The shell does not compile on Windows**: `libc::kill` in `stop_backend_pid` and `libc::dlopen`
  in `lib_loadable` (src-tauri/src/lib.rs) are not behind `cfg(unix)`.
- **The backend does not import on Windows**: `import pwd` at the top of services/team_host.py;
  `os.getuid` in storage/crypto.py `_scratch_root`.
- **The base install fails on both**: `sqlcipher3-binary` has manylinux x86_64 wheels only and no
  sdist, so `uv sync --frozen` cannot install it.
- **No tray**: it is gated on loading `libayatana-appindicator3.so.1`.
- **Every recording path is PipeWire's command-line tools**: `pw-dump` (devices, apps), `pw-record`
  (one recorder per device, appending to a growing WAV), `pw-link` and `pw-play` (levels,
  self-test), `pw-cli` (echo cancellation).
- **/proc and POSIX signals**: app_watch.py (`start_time` from /proc, SIGTERM to itself),
  services/recovery.py (finding recorders in /proc, SIGKILL), storage/crypto.py (`clean_scratch`
  checks /proc/<pid>), local_llm.py `ram_gb` (/proc/meminfo).
- **ffmpeg/ffprobe on PATH** (mixer, parts, clips, opus conversion): the deb/rpm declare it; Windows
  and macOS have none.

## Phases

### Phase 0: it builds, starts and transcribes a file (about a week)
Linux behaviour unchanged; each item behind `cfg`/`sys.platform` or a small abstraction.

Shell (src-tauri):
- `cfg(unix)` around `libc` uses; `libloading` for `lib_loadable` (`nvcuda.dll`, `vulkan-1.dll`).
- Tray built everywhere except Linux-without-appindicator.
- `.exe` names: `mnemosyne-uv.exe`, `mnemosyne-link.exe`, `venv\Scripts\python.exe`; `on_path` with
  PATHEXT.
- Windows process tree: a Job Object with KILL_ON_JOB_CLOSE instead of setsid/killpg;
  `CREATE_NO_WINDOW` for uv and Python; a graceful stop through a new `POST /api/system/shutdown`
  (loopback only) instead of SIGTERM, which Windows turns into a hard kill.
- Pass `MNEMOSYNE_CONFIG_FILE` under `app_config_dir` (config.py defaults to `~/.config` everywhere).
- Short venv path on Windows (MAX_PATH: torch and NeMo trees are deep), e.g. `%LOCALAPPDATA%\Mn\venv`.

Backend:
- `psutil` (new dependency) for process start times, finding recorders, RAM.
- app_watch `_exit` sets the server's `should_exit` instead of signalling itself.
- Notifications: D-Bus on Linux, a toast on Windows (`windows-toasts` or WinRT), `osascript` or
  UNUserNotificationCenter on macOS. Behind one `notify()`.
- `pwd`/`getuid` behind helpers (`getpass.getuser()`; display name from the OS).
- Atomic `replace()` while a reader has the file open fails on Windows: audit the call sites the
  survey found (crypto, mixer, parts, combine, downloads, encryption, config, log rotation).
- `jeepney` with a `sys_platform == "linux"` marker.
- ffmpeg and ffprobe as sidecars from pinned static builds (sha256 like uv), on PATH for the
  backend.
- `pdftotext` stays optional.

Dependencies: decide **sqlcipher3** (see Decisions). Until then, encryption at rest is Linux-only
behind a platform marker, and the setting is hidden elsewhere.

CI: `windows-latest` and `macos-14` jobs running the backend tests (Linux-only tests marked:
test_capture_command, test_levels, test_echo_cancel_and_auth's echo part, test_recovery's /proc
part, test_app_watch's subprocess part) and `cargo check` of the shell.

Done when: the app installs its backend, opens the wizard, and transcribes an imported file with
Parakeet and the onnx diarizer on both.

### Phase 1: recording (two to three weeks; macOS is most of it)
A capture sidecar, `mnemosyne-capture` (Rust, next to `mnemosyne-link`), keeping pw-record's
contract so everything downstream (levels, capture health, the live transcript's WavTail,
recovery's WAV repair) is unchanged:
- `mnemosyne-capture devices` prints JSON: id (stable: WASAPI endpoint id, Core Audio UID), name,
  kind (input | output), default. The UI saves selections by these ids, as by node name today.
- `mnemosyne-capture record --device <id> [--loopback] --rate 48000 --channels 1 out.wav` appends to
  a growing 16-bit WAV, rewrites the header every few seconds (so a hard stop leaves a valid file),
  and stops cleanly on stdin closing (portable; no signals).
- Windows: WASAPI shared mode; system audio by loopback on the render endpoint.
- macOS 14.2+: microphone through Core Audio / AVAudioEngine; system audio through a Core Audio
  process tap (an aggregate device on a tap of all processes). Needs `NSMicrophoneUsageDescription`
  and `NSAudioCaptureUsageDescription` in Info.plist and the `audio-input` hardened-runtime
  entitlement; the permission belongs to the app, and its children inherit it.
- Backend: `audio/backends/pipewire.py` (today's code) and `audio/backends/sidecar.py` behind one
  interface: `list_devices`, `record_command`, `sample_level`, `capture_apps` (sidecar: none for now).
- Echo cancellation reports "not on this system" (the UI already shows unsupported reasons).
- Self-test: plays nothing; checks that the recorders produce audio (the Linux one uses pw-play).

Done when: a two-hour meeting with mic and system audio records, transcribes with live transcript
and live speakers, and survives a crash of the app (recovery) on real Windows and Mac hardware.
Capture cannot be tested in CI; the sidecar gets unit tests for the WAV writer and a `--fake`
source for the backend tests.

### Phase 2: packages, signing, updates (about a week, plus the accounts)
- Windows: NSIS per-user installer (no admin rights), the updater's `windows-x86_64` entry.
- macOS: `.app` in a `.dmg`, the updater's `darwin-aarch64` entry (`.app.tar.gz`).
- Signing: Windows Authenticode (a code-signing certificate or Azure Trusted Signing); Apple
  Developer ID plus notarization of the app and every sidecar (uv, link, capture, ffmpeg). Without
  them, SmartScreen and Gatekeeper block the download for most people.
- scripts: the bash ones (fetch-uv.sh, stage-backend.sh with rsync, build-all.sh) get Windows
  equivalents or run under Git Bash in CI; scripts/updater-manifest.py learns the new platforms.
- Smoke test per OS: install, first launch, backend healthy, a file transcribed (no audio devices
  on CI runners).

### Phase 3: platform polish (ongoing)
- Shortcuts: Cmd on macOS (controller.svelte.ts handles Ctrl only, plus the hints in several
  components); macOS app menu and Cmd+Q through the quit dialog.
- Keychain prompts: macOS ties Keychain items to the calling executable, which is the venv's Python;
  move key access into the signed shell.
- Firewall hint per OS (netsh on Windows; macOS prompts by itself).
- GPU: WhisperX and pyannote work on Windows with CUDA (torch has win_amd64 CUDA wheels); NeMo
  does not, so Windows uses onnx or pyannote speakers. macOS: CoreML for Parakeet and Metal for the
  built-in model (llama.cpp's macOS build), CPU otherwise.
- The built-in summary model: llama.cpp's Windows (Vulkan/CPU zip) and macOS (Metal) builds, pinned
  by sha256 like the Linux ones; `.exe` and zip handling in local_llm.py.
- Meeting-app detection for auto-record: IAudioSessionManager2 on Windows, Core Audio process
  objects on macOS; then the `KNOWN` app names per OS.
- Desktop calendars: EventKit on macOS, maybe; ICS meanwhile.

## Decisions for Corey

1. **Encryption at rest off Linux**: find or publish sqlcipher3 wheels for Windows and macOS (a
   fork such as sqlcipher3-wheels, if maintained; or our own CI building against vendored
   SQLCipher), or ship encryption as Linux-only at first.
2. **Signing**: an Apple Developer account ($99/year) and a Windows code-signing route. Unsigned
   builds are fine for testers, not for anyone else.
3. **Minimum macOS 14.2** (system-audio taps without a virtual driver), Apple silicon only.
4. **Test hardware**: a Windows PC and a Mac with real meetings (Zoom, Teams, Meet in a browser);
   CI cannot test capture.

## Order and size

Phase 0 first, behind feature flags, merged as it goes (Linux CI stays green). Phase 1 on Windows
before macOS (WASAPI loopback is the easier half). A first Windows tester build after Phase 1
Windows plus Phase 2's unsigned installer; macOS after its capture works and is signed. Rough total:
five to seven weeks of focused work, most of the risk in macOS capture and signing.
