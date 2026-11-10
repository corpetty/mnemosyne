# mnemosyne-capture

Audio capture sidecar. It keeps `pw-record`'s contract with the backend (see
`backend/mnemosyne/audio/capture.py`), so the live transcript, level meters, capture health,
recovery and the Opus conversion do not change. On macOS it is what records (Core Audio: the
microphone and, through a process tap, the system's output); Linux records with `pw-record`.
Design: `docs/plans/2026-11-09-macos.md` ("The capture sidecar").

```bash
cargo test                                   # on any OS: the WAV writer, resampler and fake sources
cargo run -- record --fake tone out.wav      # a 440 Hz tone, in real time; Ctrl-C stops it cleanly
cargo run -- devices                         # JSON, the backend's AudioDevice shape ([] without a real source)
cargo run -- doctor                          # JSON: version, os, what can be recorded here
```

## record

```
mnemosyne-capture record --target <uid|system> [--rate 48000] [--channels 1] [--log <file>]
                         [--stdin-stop] <out.wav>
mnemosyne-capture record --fake <tone[:hz]|silence|file:<path.wav>> [...] <out.wav>
```

- The file starts with the 44-byte header the backend's `wav_header()` writes (RIFF and data
  sizes 0xFFFFFFFF), then 16-bit little-endian mono PCM, written about every 100 ms. Every 5 s the
  two sizes are rewritten to what is in the file, so a recorder killed outright leaves a valid
  WAV; `services/recovery.py` `repair_wav` finishes the job.
- SIGTERM, SIGINT (or end of stdin with `--stdin-stop`) writes the final sizes and exits 0. The
  backend sends SIGTERM and kills after 5 s.
- Whatever the source's rate, the output is one fixed rate (default 48 kHz; rubato's FFT
  resampler), downmixed to mono. A source may change rate mid-recording (AirPods opening the
  microphone): the old run is drained, the new one resampled, the file stays one WAV.
- Exit codes: 0 stopped or the source ended, 1 the source failed or disappeared (what was
  recorded is kept, with valid sizes), 2 usage or not supported on this system.
- `--log` appends `<UTC time> <event> key=value ...` lines: `started`, `input`, `rate_change`,
  every 60 s `stats` (buffers and exact-zero buffers per source) plus `digital_silence` when a
  whole interval was zero, `error`, `stopped`.

## macOS (`src/macos/`)

- `devices`: Core Audio's devices. `id` is the AudioObjectID, `name` the device UID (stable
  across reboots and replugging: it is what the UI saves and what `--target` takes),
  `description` the device's name. A device with input channels is an `Audio/Source`, one with
  output channels an `Audio/Sink`; a headset is both (two entries, one id). `default` marks the
  system's default input and output. First comes `{id: 1, name: "system", description: "System
  audio (all apps)", media_class: "Audio/Sink"}` (1 is kAudioObjectSystemObject, never a real
  device). Listing needs no permission.
- `--target <uid>`: the microphone, a cpal input stream owned by a thread of its own (cpal
  streams stay on the thread that made them). cpal ends the stream when the device's rate
  changes (AirPods switching to the hands-free profile when a call opens the mic): the stream
  is opened again at the new rate and the file sees a `rate_change`. A device that is away for
  more than about 5 s (25 tries, 200 ms apart) ends the recording with exit 1 and an `error`
  line. The microphone permission is asked when the stream starts.
- `--target system`: a **global** process tap (`CATapDescription
  initStereoGlobalTapButExcludeProcesses` with an empty list: every process, whichever output
  device it plays to, so switching to AirPods mid-call keeps the far end), private and unmuted,
  read through a private aggregate device (tap list with drift compensation, tap auto-start,
  the default output device as main sub-device when there is one) whose IOProc hands the
  buffers over. When the default output device changes, or nothing arrives for 10 s, the tap
  and the aggregate are built again (a `note` line). The "System Audio Recording Only"
  permission is checked when the IOProc is **created** on the tap's aggregate device
  (`AudioDeviceCreateIOProcID`), not when the tap is made: on the hosted runner (macOS 15.8.2,
  no grant, nobody to click) the tap and the aggregate device are created in 80 ms and that
  call never returns, while an IOProc on a tap-less aggregate returns in 20 ms. So `record
  --target system` opens no file until the prompt is answered. A denied tap delivers exact
  zeros that look like silence, which the `stats` and `digital_silence` log lines make
  visible. `MNEMOSYNE_CAPTURE_TRACE=1` prints each Core Audio step with its time. The aggregate device and the tap are destroyed on
  stop and on SIGTERM (the source is dropped before the process exits); they belong to this
  process's HAL client, so coreaudiod drops them with the client when the process is killed
  outright (expected; to be confirmed on a real Mac).
- `doctor` adds `macos_version`, `mic_authorization` (not_determined, restricted, denied,
  authorized), `default_input` / `default_output` (id, uid, name, rate), `taps_supported` and
  `sources: ["fake", "device", "system"]`.
- `--log` gets `note` lines from the sources (`source=mic stream_ended=...`,
  `source=system reason=default_output_changed output=...`).

Crates (pinned): cpal 0.18.2, objc2 0.6.5, objc2-core-audio / -core-audio-types /
-core-foundation / -foundation 0.3.2 (what cpal 0.18 itself uses; its
`host/coreaudio/macos/loopback.rs`, insidegui/AudioCap and makeusabrew/audiotee are the
references). `cargo check --target aarch64-apple-darwin` type-checks it on Linux once
`rustup target add aarch64-apple-darwin` is done (no SDK needed); only the macOS CI job links
and runs it. Hosted runners have no TCC grants and maybe no audio device, so the macOS tests
only check shapes and that the tap either opens, fails with a clear error, or waits at the
permission step after the tap and the aggregate device were made (the test prints probes of
where an IOProc waits).

Fake sources (every OS) are for tests: `--fake-fast` (no real-time pacing), `--fake-duration`,
`--fake-switch-rate-after 2s:16000`, `--fake-vanish-after 1s`, `--fake-rate`, `--fake-channels`.
Hidden: `--size-interval-ms`, `--stats-interval-ms`.

`backend/tests/test_capture_sidecar.py` drives the built binary (set `MNEMOSYNE_CAPTURE_BIN`, or
build it so `capture/target/debug` has it).
