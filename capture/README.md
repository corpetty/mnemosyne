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

Fake sources (every OS) are for tests: `--fake-fast` (no real-time pacing), `--fake-duration`,
`--fake-switch-rate-after 2s:16000`, `--fake-vanish-after 1s`, `--fake-rate`, `--fake-channels`.
Hidden: `--size-interval-ms`, `--stats-interval-ms`.

`backend/tests/test_capture_sidecar.py` drives the built binary (set `MNEMOSYNE_CAPTURE_BIN`, or
build it so `capture/target/debug` has it).
