# Live speaker labels from Nemotron streaming

Asked for 2026-09-29 ("plan Nemotron streaming"). Six items, one commit each, in this order.
Unreleased until Corey says so.

Checks before every commit: in `backend/` `uv run ruff check . && uv run ruff format --check . &&
uv run pytest`; `bash scripts/gen-api-types.sh --check` (without `--check` after model changes);
`pnpm check`; `pnpm test:e2e`.

Constraints (CLAUDE.md): never play audio on this machine. Test live features offline by feeding
a file through `LiveTranscriber`. Other Claude sessions share this checkout, so commit files by
name and stage only your own hunks of shared files. Downloading the AMI corpus again needs
Corey's OK (see Open questions).

---

## Where we are

Live lines get a speaker in two layers today:

1. **Online clustering** (`transcription/live_speakers.py`). Each committed line is embedded with
   pyannote's embedding model and assigned to the nearest cluster (threshold 0.45), then named
   from voice profiles. It is instant but coarse: short lines embed badly, and clusters split and
   merge (`live_relabel` events).
2. **Re-diarization every 30 s** (`transcription/live_rediarize.py`, NVIDIA only). The offline
   Nemotron diarizer runs over the last 20 minutes of the source, and lines whose speaker changed
   go out as `live_labels`. Clustering is then renamed to agree.

So on NVIDIA a line can show the wrong speaker for up to 30 s before it is corrected. Every pass
re-diarizes up to 20 minutes of audio to label a few new lines.

The after-0.8 plan (item 2) meant to drive Sortformer's streaming step directly. That was put
off because the feature context, offsets and speaker cache had to be handled by hand. Since then
NeMo has shipped a working reference we can follow: `nemo/agents/voice_agent/pipecat/services/nemo/streaming_diar.py`
plus `CacheFeatureBufferer` in `.../nemo/utils.py`, both in the installed NeMo. They are marked
"will be deprecated", so we copy the approach and do not import them.

## What streaming gives us

Nemotron-3-Diarization is a Streaming Sortformer. It keeps an **arrival-order speaker cache**
(AOSC) plus a FIFO queue between chunks. Speaker index *k* stays the same person for the whole
stream (up to 8 speakers), with no clustering or renaming. The published configurations, in
80 ms frames, from the model card:

| Config      | Input latency | spkcache_len | fifo_len | chunk_len | right_context | update_period |
|-------------|---------------|--------------|----------|-----------|---------------|---------------|
| offline     | 30.4 s        | 264          | 40       | 340       | 40            | 300           |
| low latency | 1.04 s        | 264          | 264      | 9         | 4             | 222           |

We use **low latency**. The live transcriber commits lines every ~5 s with a 1 s margin, so lower
latency buys nothing. Each step attends over spkcache + fifo + chunk (~540 encoder frames). That
should be a few ms on a GPU, with ~7 steps per 5 s tick per source (to be measured in item 1).

The model config has `streaming_mode: true` (so no peak normalisation, which is right for chunks),
`high_resolution: true` and `output_subsampling_factor: 1`. Predictions are therefore per 10 ms
frame, 8 speakers each. The preprocessor is a 128-bin mel at 16 kHz, 25 ms window, 10 ms stride,
with `dither: 1e-05`.

**Separate model instance.** Streaming parameters live on `model.sortformer_modules`, and the
offline diarizer sets the offline config on its own instance (`diarizers/nemotron.py`, `OFFLINE`).
If the two shared a model, a final job run during a recording (transcribing another meeting)
would change the chunk size under a live stream. The checkpoint is 200 MB, so the live stream
gets its own instance. It is never the engine's instance, unlike `ModelService.live_rediarizer`
today.

---

## 1. `NemotronStream`: the streaming driver, checked against NeMo's own

New `transcription/diarizers/nemotron_stream.py`:

- `NemotronStreamModel` loads the checkpoint (as `NemotronDiarizer.load` does: `from_pretrained`,
  `map_location`, `eval()`), applies the low-latency config, and calls `_check_streaming_parameters()`.
  It has `load()`, `unload()` and `is_loaded()`, and a `stream()` that returns a new
  `NemotronStream`.
- `NemotronStream` holds one source's state:
  - `push(pcm: np.ndarray, rate: int) -> SpeakerFrames`. It resamples to 16 kHz (int16 or
    float32 in), appends to a small audio ring, and runs every complete chunk (`chunk_len` +
    `right_context` frames available). It returns the new 10 ms frames as `(first_frame_index,
    probs[n, 8])`.
  - Features: mel over the chunk's audio plus `left_offset`/`right_offset` feature frames of
    context, sliced like `sortformer_modules.streaming_feat_loader` does for a whole file. Pass the
    same `left_offset`/`right_offset` to `forward_streaming_step`.
  - `flush()` runs the tail at stop with no right context.
  - `frontier` is the time (seconds from the recording's start) up to which frames are final.
- **Bounded memory.** `forward_streaming_step` concatenates onto `total_preds`. Pass an empty
  tensor each step and keep only the returned chunk, so GPU memory stays flat over a 3-hour
  meeting. Check the sync path (`async_streaming` is unset in the config, so False) does not read
  earlier `total_preds`.
- Run all torch work in `asyncio.to_thread`, under a lock per model (one GPU queue for all sources).

Tests:

- `tests/test_nemotron_stream.py` with a fake model (no NeMo). Cover chunking, resampling,
  frontier, flush, and frame indices across pushes of odd sizes.
- **Equivalence check** (skipped without NeMo + CUDA, so it runs locally, not in CI). Stream a
  real recording from Corey's data dir through `push()` in random-sized pieces, with `dither` set
  to 0. Compare with `model.forward_streaming` on the whole file's features under the same config
  (NeMo's own chunked loop). Per-frame argmax must agree on ≥ 99% of active frames. This check is
  the whole point of the item: it is what the after-0.8 attempt lacked.
- Record in the commit: ms per step, steps per second of audio, and peak VRAM on the RTX 2080 Ti.

## 2. `mnemosyne-bench --live`: measure before switching

Add a `--live` mode to `bench.py`. It replays a recording through `LiveTranscriber` as if live,
with no sleeping: write the audio into a growing temp WAV in 5 s steps and call `tick()` after
each. The copilot and mentions are off. It scores the **speaker accuracy of committed live lines**
(existing `speaker_accuracy`) against the reference, and reports it for each labelling mode:

- `clustering`: embeddings and online clustering only;
- `clustering+rediarize`: today's NVIDIA default. Drive `LiveRediarizer.pass_once` every 30 s
  of audio;
- `streaming`: item 3. This mode is added here but only reports "unavailable" until item 3 lands.

Report both the label a line had when first shown and its label at the end (after corrections).
The first is what people see while the meeting is on.

Reference data, no downloads needed:

- `--session <id>`: Corey's meetings whose speakers he corrected in the transcript editor.
  Pick 3–4 with 3+ remote speakers.
- `--synthetic`: two espeak voices. A smoke test only.
- AMI ES2004a–d with per-word references, only if Corey OKs downloading it again.

This commit records the baseline numbers for `clustering` and `clustering+rediarize`.

## 3. Streaming labels in the live transcript

In `LiveTranscriber`, when a source has a `stream`:

- `_tick_source` pushes every new sample (the `new` it already reads from `WavTail`) into the
  stream *before* committing lines, **including during the silence skip**. The skip drops
  transcriber work, not audio, so the timeline stays aligned.
- New `SpeakerTimeline` (same module as item 1): per source, one byte per 10 ms (the most active
  speaker above `pred_score_threshold`, or none) plus overlap flags. That is 360 KB per hour, kept
  in memory for the whole recording. `speaker_between(start, end)` returns the speaker with the
  most active frames in that span.
- `_label`, streaming path: the line's speaker is `speaker_between(start, end)` mapped to a display
  label (item 4). The embedder and clusterer are not called.
- Lines past the frontier: a line that ends after `stream.frontier` is labelled from the frames
  it has. It goes on a short `pending` list, is re-checked on the next tick, and any change goes
  out as `live_labels`. The frontend already handles that event.
- Per word: when the transcriber returned word times, split a committed line where the speaker
  changes, using `assign.assign_speakers` with turns built from the timeline. Only split when each
  side keeps ≥ 2 words. The pieces go out as separate `live_segment` events.
- `LiveRediarizer` does not run for a source that has a stream. Whether it should run on top is
  decided by the item 2 numbers (see Open questions).
- Stop: `run()`'s final flush tick also calls `stream.flush()` before labelling the last lines.
- Log at the end of the recording: steps, mean and p95 ms per step, and time spent diarizing
  compared with audio length.

Tests use a fake stream with a scripted speaker timeline. They cover lines labelled by the
timeline, pending lines corrected by `live_labels`, silence still pushed, words split at a
change, no split for a single word, and flush at stop. Then run item 2's bench in `streaming`
mode and put the numbers in the commit.

## 4. Names: voice profiles, and the same people across parts

Sortformer speakers are anonymous indices. New `LiveSpeakerNames` maps (source, index) to a
display label:

- A new index gets the next free "Speaker n", across sources as today.
- **Voice profiles.** Once an index has ≥ 3 s of clean speech (no overlap in the timeline), embed
  its clean spans with the pyannote embedder. Reuse `embedding_spans` from `diarizers/nemotron.py`
  on turns built from the timeline, and match through the same `match_names` closure `pipeline.py`
  builds, which never gives the local user's name to a system-channel voice. Retry every 60 s of
  that speaker's speech until matched, and stop after a match. A rename goes out as
  `live_relabel`, which already updates committed lines and the frontend.
- **Parts.** Each part of a meeting is a new recording, so the stream (and its indices) starts
  over. Keep each label's mean embedding in `app.live_voices[session_id]`, in memory, dropped when
  the session's last live job ends plus 1 hour. The next part's `LiveSpeakerNames` matches new
  speakers against those first (threshold `speaker_match_threshold`), so "Speaker 2" in part 1 is
  still "Speaker 2" in part 2. After an app restart this falls back to fresh numbering, which is
  acceptable.
- **More than 8 speakers.** Sortformer merges the ninth voice into an existing index. The live
  view can't fix that. The final transcript (offline Nemotron plus per-word assignment) is
  unchanged. Note it in the docs.
- The embedder loads only when it is needed for naming. Without it (no HF token) labels stay
  "Speaker n", as today.

Tests: new indices get fresh labels, a profile match renames once, local user names are never
used for system voices, parts reuse labels, and there is no embedder in the no-token case.

## 5. Selection, settings, fallback

- Setting `live_diarizer: "auto" | "streaming" | "clustering"`, default `auto`. `auto` means
  streaming when `nemotron_available()` (NeMo and CUDA; never on CPU, where the step would compete
  with the desktop the way live Parakeet once did). `live_rediarize` keeps its meaning for the
  clustering path only. It is a new field with a default, so no `config_version` migration is
  needed. Add it to `LIVE_SETTINGS` in `registry.py` (next to `live_rediarize`), so changing it rebuilds the live models.
- `ModelService.live_stream_model` is built by `registry.build_live_stream_model(settings)`. It
  is never shared with the engine (see "Separate model instance") and is unloaded by
  `unload_if_idle` like the other models.
- Pre-load: `prepare()` in `pipeline.py` starts loading it in the thread it already uses. Until it
  is ready, lines are labelled by clustering.
- **Fallback.** If the load fails, or any step raises: log once with a traceback, send the status
  "Live · speaker detection (fallback)", drop the stream for that source, and carry on with the
  clustering path. To make that instant, `prepare()` still builds the clusterer when streaming is
  selected. It is cheap: no model loads until it is used.
- Status line: "Live · speakers by Nemotron" when streaming is active.
- Settings → Transcription: a "Live speaker labels" select (Automatic / Nemotron streaming /
  Voice clustering) next to the existing live options in `SettingsPanel.svelte`. The
  re-diarization select is disabled unless clustering is in use. Run `pnpm gen:api`.
- e2e: demo mode keeps clustering. The demo diarizer is unchanged, and so are the e2e strings.

Tests: auto resolves on and off CUDA, a load failure falls back, a step failure mid-recording
falls back and keeps the labels already shown, and the settings round-trip.

## 6. Docs

- `docs/architecture.md`: the live labelling path, with a diagram of the stream → timeline →
  labels → names flow.
- `docs/api-reference.md`: the `live_diarizer` setting. No new events (`live_labels` and
  `live_relabel` are reused).
- CLAUDE.md roadmap: replace "live diarization with Nemotron's streaming mode" in "Candidates
  next" with a line on what was built and the item 2/3 numbers. Add a Gotchas line: the streaming
  model is a separate instance on purpose.

---

## Open questions for Corey

Answered 2026-09-29: AMI download OK; the sessions are mine to pick; the re-diarization question is
decided by the numbers. As built: the Edinburgh AMI server stalled, so `scripts/fetch-ami.py` reads
the Hugging Face mirror (diarizers-community/ami, speaker turns without words; ~900 MB read, ~250 MB
kept), and the live bench scores lines by time overlap with those turns. Re-diarization stays off
with streaming: on ES2004a its end-of-meeting accuracy (93.5%) is below streaming alone (97.4%),
because it relabels whole lines where streaming splits them at speaker changes.

1. **AMI data.** Is it OK to download AMI ES2004a–d again (headset mix + word-level references,
   ~150 MB, to `~/.cache/mnemosyne-trials/ami/`)? Without it, item 2 uses your own corrected
   meetings and the synthetic set. Those are real but fewer, and unpublished.
2. **Keep the 30 s re-diarization on top of streaming?** Proposed: off, unless item 2 shows it
   adds ≥ 1 point of end-of-meeting line accuracy over streaming alone. If it goes on, it would
   update `LiveSpeakerNames` instead of the clusterer.
3. **Which sessions to benchmark.** Name 3–4 meetings you've corrected speakers in, or leave it to
   me: I'll pick the longest ones with 3+ speakers and edited transcripts, and only read them
   locally.

## Not in this plan

- Streaming ASR paired with the diarizer (NVIDIA's multitalker Parakeet, which runs one ASR stream
  per speaker). It is a different transcriber architecture; revisit if live Parakeet on CPU
  becomes the bottleneck.
- A "who's talking now" indicator in the UI. The timeline makes it cheap (a `live_speaking` event
  per tick), but it is a UI decision for later.
- The Transformers port of the model (`processor.set_streaming_mode`). Our locked transformers
  (4.57.6) doesn't have it, and WhisperX pins transformers, so NeMo stays the runtime.
