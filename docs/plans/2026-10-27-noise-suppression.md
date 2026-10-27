# Noise suppression on the echo-cancelled mic (2026-10-27)

Corey used NoiseTorch (RNNoise as a LADSPA plugin loaded through PulseAudio's `module-ladspa-sink`,
with a voice-activation gate) until it stopped working on Fedora; EasyEffects' RNNoise does a worse
job. We already hold a virtual mic in a `pw-cli` child (audio/echo_cancel.py), so a second child now
loads PipeWire's filter-chain with DeepFilterNet's LADSPA plugin behind the WebRTC canceller:

    mic -> WebRTC AEC (mono, its own NS off) "mnemosyne_aec_raw" -> DeepFilterNet -> "mnemosyne_aec_source"

## What was built

- audio/noise_suppression.py: the plugin pin (DeepFilterNet v0.5.6 x86_64, 53 MB, SHA-256 checked,
  downloaded on first use into `models_dir`) and the filter-chain command. The capture side is
  passive: PipeWire suspends the chain while nothing records from the source (0 % CPU idle; about a
  tenth of a core on this machine while recorded from).
- Settings `noise_suppression`, `noise_suppression_db` (the plugin's attenuation limit; 100 removes
  noise), `noise_suppression_record`. On the echo-cancel endpoint; a change while recording applies
  from the next recording, like the mic. UI: a row under Echo cancellation (DeviceSelector.svelte).
- Fallbacks: the filter chain not coming up puts a plain copy (filter-chain's builtin `copy`) behind
  the canceller instead; the plugin ABORTS its process (`panic!`) once its latency reaches 1 s of
  underruns (each underrun adds 10 ms and a 10 ms frame of zeros; it recovers 10 ms per ~10 s of good
  runs). The chain therefore runs in its own `pw-cli`: a crash never touches the canceller or a
  recording of `mnemosyne_aec_raw`, and `EchoCancelManager._watch` loads the copy as the visible
  source. `reason` tells the UI. Checked on real PipeWire with test node names (2026-10-27): after a
  SIGABRT of the chain the canceller and a recorder of the internal source ran on (the file kept
  growing), and a recorder of the visible source was relinked by WirePlumber to the copy once it came
  up (a gap of about a second, then the un-denoised audio).
- The source is up whenever the backend runs (it is loaded at start), so with close-to-tray it is
  available to other apps all day. Starting Mnemosyne at login does not exist yet (possible next step).

## The WER check (Phonon-2, AMI ES2004a, scripts in the session scratchpad, not committed)

Offline through the real plugin (a ctypes LADSPA host, 480-sample blocks at 48 kHz, latency 960
samples removed). Noise = pink noise at 10 dB SNR plus synthetic keyboard bursts at -6 dB re speech.

Headset mix (four people on one track; not "my mic"):

| | raw | DFN 100 dB | DFN 18 dB |
|---|---|---|---|
| clean | 22.5 % | 23.6 % | 22.6 % |
| noisy | 43.4 % | 57.0 % | 46.4 % |

Close-talk headsets (AMI IHM, each speaker's own utterances back to back, 2653 words), at AMI's
recorded level (most utterances below -45 dBFS) and normalized to -29 dBFS speech (Corey's mic: real
lines -43 to -22 dB, median -29):

| | raw | DFN 100 dB | DFN 18 dB |
|---|---|---|---|
| clean, AMI level | 9.1 % | 16.1 % | 9.1 % |
| noisy, AMI level | 22.0 % | 31.3 % | 25.2 % |
| clean, -29 dBFS | 9.0 % | 8.6 % | 8.4 % |
| noisy, -29 dBFS | 22.7 % | 30.5 % | 23.9 % |

Findings: on clean speech at a normal level it is neutral to slightly better; quiet speech (below
about -45 dBFS) is cut hard at 100 dB; in noise it always raised WER, mostly deletions. Phonon copes
with noise better than with what the suppressor removes. Hence `noise_suppression_record = false`:
our recordings take `mnemosyne_aec_raw`, other apps (calls) get the denoised mic.

Caveats: AMI is 16 kHz upsampled to 48 kHz (DeepFilterNet is a fullband model; a real mic has energy
above 8 kHz); the noise is synthetic and harsh. Worth repeating on real 48 kHz noisy recordings of
Corey's mic with a hand-corrected transcript before ever recording the denoised audio by default.

## Not done

- No in-app check of the UI (svelte-check only): the dev backend would fight the running app over
  the node names. Tests use a fake pw-cli; the DeepFilterNet command was run once on real PipeWire,
  the two-process split and the copy with test node names.
- aarch64 (DeepFilterNet ships a build; not pinned). The offline AppImage does not carry the plugin.
- Start at login.
