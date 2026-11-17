# Voice profiles and live speaker labels without torch: plan

Status: tasks 1-6 built on `mac-parity` (2026-11-17; part A = tasks 1-3, part B = tasks 4-6, see "Progress" at the end). Task 7 and phase 2 open. The text below is the plan as written; file:line references are against b5ed0fc. File:line references are against that
commit; re-find them by name before editing.

Anything I could not check is marked **(verify)**. I ran no model and no benchmark here.

## Goal

The Mac (Apple silicon) and Linux machines without NVIDIA install only the base and `onnx` extras. On them:

- voice profiles never learn or match a voice (no "remember voice", no auto-naming after transcription);
- live lines on a diarized channel keep the channel label ("Remote"), with no "Speaker n" and no names;
- later parts of a multi-part meeting cannot be matched to earlier parts by voice (see finding 5).

All three need one thing: a speaker embedder that runs without torch. Offline diarization already runs without
torch (the `onnx` diarizer). This plan adds an ONNX embedder through sherpa-onnx, tags every stored voice with the
model that made it, and leaves NVIDIA machines with an HF token as they are.

## What I found

1. **The embedder is the only missing piece.** Everything that names speakers by voice goes through
   `build_live_embedder` (transcription/registry.py:169-181). It returns None unless `pyannote.audio` and `torch`
   import, and also when `diarizer` is `onnx` or `none` (registry.py:172). The `onnx` diarizer returns no
   embeddings on purpose: "voice profiles live in pyannote's space" (diarizers/onnx.py:8, 196-201).
2. **sherpa-onnx can embed arbitrary segments today.** The onnx diarizer already does it for its small-cluster
   merge: `SpeakerEmbeddingExtractor.create_stream()`, `accept_waveform(rate, samples)`, `input_finished()`,
   `compute(stream)` (diarizers/onnx.py:104-108, 134-153). In the installed sherpa-onnx 1.13.8 the extractor has
   `compute`, `create_stream`, `dim`, `is_ready`; `accept_waveform` takes float32 in [-1, 1] and **resamples
   inside** when the rate differs (its docstring), so 48 kHz live buffers can go in as they are.
   `SpeakerEmbeddingManager` (`add`, `search`, `verify`, `score`) is a C++ cosine lookup. We do not need it: our
   matching (speaker_service.py:31-58, live_speakers.py:131-229, parts.py:183-223) is Python and does more (one
   person per label, merges, renames).
3. **The model the onnx diarizer downloads is TitaNet-small**: `nemo_en_titanet_small.onnx`, 40,257,283 bytes,
   sha256 `ad4a1802…a789e` (diarizers/onnx.py:33-39), CC-BY-4.0, 192-d. Its export fails over 122.88 s, hence
   `EMBEDDING_SECONDS = 60` (onnx.py:47-49).
4. **pyannote's embedder is very likely WeSpeaker ResNet34-LM, and sherpa-onnx ships that same network as ONNX.**
   pyannote 3.1 uses `pyannote/wespeaker-voxceleb-resnet34-LM` (CC-BY-4.0, not gated), a wrapper around
   WeSpeaker's `voxceleb_resnet34_LM`. community-1 bundles `embedding/pytorch_model.bin` of 26,646,242 bytes;
   the 3.1 wrapper's is 26,645,418 bytes (HF API, 2026-11-17). That is the same size within 824 bytes, so it is
   probably the same weights in a newer checkpoint format **(verify)**. docs/architecture.md:393 already calls it
   "community-1's bundled model, 256-d". sherpa-onnx's release has `wespeaker_en_voxceleb_resnet34_LM.onnx`
   (26,530,550 bytes, sha256 `e9848563da86f263117134dfd7ad63c92355b37de492b55e325400c9d9c39012`, from the release's
   checksum.txt), exported from the same WeSpeaker checkpoint. Both front ends are Kaldi fbank (80 bins) with
   per-utterance mean removal. If the two give the same vectors for the same audio, **profiles enrolled on a GPU
   machine work on the Mac and the reverse**, and nothing needs recalibrating. Task 0 checks this.
5. **CPU machines merge later parts' speakers blindly.** pipeline.py:220-232 matches a later part's labels to the
   meeting's by voice. With no embeddings, `match_speakers({}, …)` returns `{}` (parts.py:219-222 only maps labels
   in `new`), so part 2's `SPEAKER_00` is merged with part 1's `SPEAKER_00` whoever they are. Embeddings from the
   onnx diarizer fix this too.
6. **Profiles are per machine; per-meeting voices travel.** The `speakers` table (sqlite.py:169-176: one
   `embedding` per name, a running mean) is not in `SyncSession`; library sync never carries profiles. The
   per-meeting `session_speakers` (sqlite.py:215-220) do travel: `SyncSession.speaker_embeddings`
   (models/sync_session.py:296), built at services/sync_state.py:195, merged at sync_merge.py:556-561, applied at
   sqlite.py:5217-5220. Phase 8 sharing carries no voices at all (models/shared_meeting.py:5, tested). Backups copy
   the database (services/backup.py:7).
7. **No enrolment audio is kept.** A rename adds the meeting's stored vector for that label to the profile's
   running mean (speaker_service.py:60-71, sqlite.py:4790-4818). Nothing records which meetings fed a profile. The
   meeting audio is kept (subject to retention) only on the recording machine ("audio never travels").
8. **Nothing records which model made a vector.** The only guard is the length: a new sample of another length
   replaces the profile ("embedding model changed; start over", sqlite.py:4805-4812), and the cosines return -1 on
   a length mismatch (speaker_service.py:15-23, live_speakers.py:108-112). `parts.match_speakers` has no length
   check: 192-d against 256-d raises in `np.dot`. A 256-d vector from another 256-d model would be compared
   silently.

## Where a speaker embedding is used today

| Use | Where | Embedding source |
|---|---|---|
| Final transcript, pyannote diarizer | diarizers/pyannote.py:119-133 (`speaker_embeddings` from the pipeline) | pyannote pipeline |
| Final transcript, Nemotron | diarizers/nemotron.py:132-147, `embedding_spans` :176-200 (up to 8 clean turns, 60 s per speaker); built with `build_live_embedder` at registry.py:236-240 | `PyannoteEmbedder` |
| Final transcript, onnx | none (onnx.py:8) | — |
| Engine to pipeline | `DiarizationResult.embeddings` (engine.py:34-37), `ComposedEngine.last_speaker_embeddings` (composed.py:63, 141, 170) | — |
| Later parts matched to earlier ones | pipeline.py:176, 220-232; parts.py:183-223 | stored `session_speakers` |
| Auto-naming after transcription | pipeline.py:253-258 → `SpeakerService.match` (speaker_service.py:31-58), threshold `speaker_match_threshold` 0.6 (config.py:148-150) | profiles |
| Enrol on rename | routes/speakers.py:36-50 → speaker_service.py:60-71 → `upsert_speaker_sample` sqlite.py:4790-4818 | stored `session_speakers` |
| "Has a voice" | routes/speakers.py:73-80 (`has_voice`), services/people.py:130-132 | stored / profiles |
| Combining two meetings | services/combine.py:131-145, 249-251, 329-333 | stored `session_speakers` |
| Live, voice clustering | pipeline.py:812-828 (`OnlineClusterer`, `known` = all profiles, `live_speaker_threshold` 0.45, config.py:192); live.py:500-525 (`_label`, one embed per committed line ≥ 1 s) | `app.models.live_embedder` (model_service.py:135-142) |
| Live, Nemotron streaming (NVIDIA) | `VoiceNamer` live_streaming.py:198-278, pipeline.py:848-856; voices kept in `AppContext.live_voices` (context.py:84-86, pipeline.py:918-933) | same embedder |
| Live re-diarization (NVIDIA) | live_rediarize.py:130 (`result.embeddings` from `NemotronDiarizer(embedder=…)`, registry.py:207-221) | same embedder |
| Benchmark | bench_live.py:200-216, 277-296 (`build_live_embedder`; "Voice profiles are not used") | same embedder |

What decides availability today:

- `build_live_embedder` (registry.py:169-181): diarizer setting in auto/pyannote/nemotron and pyannote + torch
  importable. It does **not** check `hf_token`; without one, `PyannoteEmbedder.load` fails on the gated model and
  the live status says "Live (speaker detection unavailable)" (live.py:229-247).
- `resolve_diarizer` (registry.py:145-166): Nemotron, else pyannote with a token, else onnx, else none.
- `/api/system` (routes/system.py:46-60, 103-117): `gpu_stack`, `hf_token` ("needed for speaker identification
  (pyannote)"), `onnx_diarizer`, `diarizer_in_use`. No field says whether voices can be recognised.

## Choice of embedder

All are in sherpa-onnx's `speaker-recongition-models` release (sizes from the GitHub API, hashes from its
checksum.txt). EER and CPU figures are the publishers' numbers, not measured here **(verify on this hardware)**.

| Model (sherpa file) | Size | Dim | Licence | VoxCeleb1-O EER (published) | CPU cost (published, 1 thread) | Same space as pyannote? |
|---|---|---|---|---|---|---|
| `wespeaker_en_voxceleb_resnet34_LM.onnx` | 26.5 MB | 256 | CC-BY-4.0 | about 0.7-0.9 % | RTF about 0.03 (CAM++ paper's ResNet34 row) | **probably yes** (finding 4) |
| `nemo_en_titanet_small.onnx` (already downloaded) | 40.3 MB | 192 | CC-BY-4.0 | about 1.2 % | similar to ResNet34 **(verify)** | no |
| `wespeaker_en_voxceleb_CAM++_LM.onnx` | 29.3 MB | 512 **(verify)** | CC-BY-4.0 | about 0.7 % | RTF about 0.013, ~2.5x faster than ResNet34 | no |
| `3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx` | 29.6 MB | 192 or 512 **(verify)** | Apache-2.0 | about 0.7 % | as CAM++ | no |
| `3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx` | 26.5 MB | 192 **(verify)** | Apache-2.0 | about 0.8 % | heavier than CAM++ | no |
| `nemo_en_titanet_large.onnx` | 101 MB | 192 | CC-BY-4.0 | about 0.7 % | ~3x TitaNet-small | no |

**Recommendation: WeSpeaker ResNet34-LM.** It is as good as the others that matter, small, CC-BY-4.0 like the
models we already ship, and it is the one model that can share a space with pyannote. If task 0 confirms that,
one voice space covers every machine: profiles, per-meeting voices that travel through library sync, combining
and the thresholds all just work. If task 0 says no, it is still a good model and we tag its space separately;
CAM++ is the fallback only if the Mac cannot afford ResNet34 live (task 7 measures it).

Keep TitaNet-small inside the onnx diarizer. Its clustering threshold is tuned for it (onnx.py:40-45); moving the
diarizer onto ResNet34 is a later, separate change with its own AMI check. The diarizer gets the embedder injected,
like Nemotron, and embeds each speaker's clean turns after diarizing (`embedding_spans`, nemotron.py:176). Cost:
one extra 26.5 MB download, and at most 60 s of audio per speaker per meeting (a few seconds of CPU).

## Voice spaces

Every stored voice records its **space**: the model family that made it. Two spaces at first:

- `pyannote`: the pyannote pipeline's embedding model (pyannote diarizer output and `PyannoteEmbedder`). Every
  vector stored today is in it: the onnx diarizer never produced one, Nemotron's come from `PyannoteEmbedder`.
- `wespeaker-r34`: the ONNX ResNet34-LM, **unless task 0 shows it equals pyannote's**, in which case the ONNX
  embedder reports `pyannote` and this second space never appears in data.

A custom `diarization_model` (config.py:215-216) is still tagged `pyannote`. That is today's assumption too; noted
under risks.

### Schema

- New table `speaker_voices(speaker_id REFERENCES speakers(id) ON DELETE CASCADE, space, embedding, sample_count,
  updated_at, PRIMARY KEY(speaker_id, space))`. A profile can hold one running mean per space.
  `speakers.embedding` stays (NOT NULL) but is no longer read; new profiles write `'[]'`. An older build then sees
  `[]` and its cosine returns -1: no false match after a downgrade.
- New table `session_voices(session_id … ON DELETE CASCADE, label, space, embedding, PRIMARY KEY(session_id,
  label, space))` replacing `session_speakers` (whose primary key cannot take a space column).
- Migration, once, in `_migrate_columns` (sqlite.py:802): copy `speakers.embedding` (non-empty) into
  `speaker_voices` with space `pyannote`; copy `session_speakers` into `session_voices` with space `pyannote`; drop
  `session_speakers`. Keyed by a `meta` flag like `task_links_migrated`.
- `SpeakerProfile` (models/speaker.py:11-17) gets `voices: dict[str, list[float]]` (space → vector) and per-space
  counts instead of `embedding`. `SpeakerProfileSummary` (the API view) is unchanged in the first deliverable.

### Rules

- **Match only within one space.** `SpeakerService.match(embeddings, space)`, `OnlineClusterer.known`,
  `VoiceNamer`, `parts.match_speakers`, `combine.relabel_speakers` all take vectors of one space. The space is the
  embedder's (live) or the diarizer result's (final).
- **Enrol into the space the meeting has.** A rename adds each of the label's stored vectors to the profile's voice
  of the same space. The length guard in `upsert_speaker_sample` stays as a second check.
- **A machine with both stacks** (NVIDIA + gpu extra + onnx extra + HF token) uses `pyannote`, as today. Storing
  both spaces per meeting there ("dual enrolment") is phase 2: it makes profiles survive a later change of stack,
  at a few CPU seconds per meeting.
- **A profile with only the other space** does not match here. It is kept, not converted. Phase 2 adds "re-learn
  voices": for each such profile, find meetings on this machine whose audio is still here and whose transcript has
  a speaker with that name, embed that speaker's clean turns with this machine's embedder (audio through
  `storage.crypto.plaintext()`), and add them as samples. Meetings received by library sync have no audio here, so
  they cannot help.
- **Library sync** carries per-meeting voices by space. `SyncSession` gets `speaker_voices: dict[space, dict[label,
  vector]] | None`; `speaker_embeddings` keeps carrying the `pyannote` space for older peers. From an older peer
  (`speaker_voices` absent), its `speaker_embeddings` are `pyannote` (true: older builds only ever made those).
  Merge each space with `by_id` as today. A meeting from the Linux GPU machine arrives on the Mac with `pyannote`
  vectors; if the spaces differ the Mac never compares them with its own. Profiles themselves still do not travel
  (open question 3).
- **Phase 8 sharing** carries no voices; nothing changes. Keep test_shared_meeting.py's schema walk passing.
- **In-memory live voices** (`AppContext.live_voices`) are keyed by session; store the space with them and drop
  them when it differs.

## The live path on a laptop

Today's clustering path embeds each committed line once (live.py:329-333, 500-525). Lines under 1 s are not
embedded (`MIN_SECONDS`, live_speakers.py:22). So the embedder sees about the diarized channel's speech once, in
pieces of 1-30 s (`max_buffer` 30 s, live.py:187), every `live_interval_seconds` (5 s, adaptive up to 4x:
live.py:541-575). Only the system channel is diarized when mic and system are separate (pipeline.py:802).

Cost estimate: ResNet34 at RTF about 0.03 on one thread is about 150 ms of one core per 5 s tick of speech; CAM++
would be about 65 ms **(verify)**. The live transcriber (Parakeet or Phonon, `live_threads` 2) costs much more per
tick, so the embedder adds roughly 10-20 % to live CPU. The tick time that `_adapt` measures includes `_label`
(live.py:250-254), so a slow embedder already backs the interval off. Give the live embedder its own instance with
one thread; the final pipeline's instance uses `min(4, cores - 1)`.

Thresholds: `live_speaker_threshold` 0.45 (clustering) and `speaker_match_threshold` 0.6 (profiles) were set for
pyannote's model. If task 0 shows one space, they carry over; task 7 still checks them once. If not, the
`wespeaker-r34` space needs its own two numbers, measured on AMI:

- clustering: `mnemosyne-bench --live --modes clustering` on ES2004a-d with the ONNX embedder (bench_live.py needs
  an `--embedder onnx|pyannote` flag and a threshold sweep);
- profile matching: a new `mnemosyne-bench --voices`: enrol each AMI speaker from one meeting (reference turns,
  `embedding_spans`), score the same people in the other three meetings (genuine) and speakers of other AMI series
  (impostors), and pick the threshold with no false accepts plus a margin.

Run either by Corey or in an on-demand CI job (onnx extra, `scripts/fetch-ami.py`), never on this machine's
shared memory budget during a normal session. ~/.cache/mnemosyne-trials/ami does not exist on this machine today.

If the spaces differ, the per-space numbers live in code next to the embedder (`cluster_threshold`,
`match_threshold`). The two settings keep meaning "for pyannote" (open question 4).

## Resolution rule

No new setting. One resolver next to `resolve_diarizer`:

```
resolve_speaker_embedder(settings) -> "pyannote" | "onnx" | "none"
  diarizer setting is "none" or "demo"           -> "none"   (as today)
  pyannote.audio + torch installed and hf_token  -> "pyannote" (NVIDIA machines with a token: unchanged)
  sherpa_onnx installed                          -> "onnx"
  else                                           -> "none"
```

Differences from today: a saved `diarizer = "onnx"` gets an embedder (today none, registry.py:172), and a machine
with pyannote but no token gets the ONNX embedder instead of one that fails to load. Both only add capability; their
profiles are empty today, so no existing voice is orphaned. `build_speaker_embedder(settings, threads)` replaces
`build_live_embedder` (keep the old name as an alias for bench_live.py until it is updated). It is used by the live
path, Nemotron, the live re-diarizer and the onnx diarizer. `LIVE_SETTINGS` and `ENGINE_SETTINGS`
(registry.py:261-305) already contain `diarizer` and `hf_token`, so a settings change rebuilds them.

## What the UI says today, and what changes

Today on a Mac or a Linux machine without NVIDIA:

| Place | Today | After |
|---|---|---|
| Live status (live.py:229-247) | plain "Live"; channel labels only | "Live · speaker detection on", lines get "Speaker n" and names |
| SpeakerBar (SpeakerBar.svelte:92-96, 107) | no "remember voice" box, no ● (no `has_voice`) | box and ● appear after a transcription |
| "Who is who?" card (SpeakerNamingCard.svelte:74, 123-127) | always offers "Remember their voices" and says "their voices will be recognized next time", which is false here today | becomes true |
| Setup wizard (SetupWizard.svelte:142-150, 375-379) | onnx: "Uses a small model on the CPU (about 50 MB …)"; the HF-token prompt (GPU stack only) says a token "lets Mnemosyne recognise voices across meetings" | download is about 75 MB; voices are recognised without a token |
| Settings → Transcription (SettingsPanel.svelte:795-797, 878, 887) | diarizer "Automatic (Nemotron …, else pyannote)", no onnx option; "pyannote model (also voice profiles)"; token "Nemotron needs it only for voice profiles" | already stale for onnx; the token is no longer needed for voice profiles |
| docs | architecture.md:393-403 ("Without torch/pyannote … channel labels"), team-server.md:196-201 (profiles need an HF token), api-reference.md:461, macos.md:219-221, 259-260 | updated |

Backend side: `/api/system` gains `speaker_embedder_in_use` ("pyannote" | "onnx" | "none"), so the UI can tell
what runs here without guessing from `hf_token` (`pnpm gen:api` after). The wording of the strings above is a UI
decision and is left to the orchestrator/Corey (open question 5); the backend tasks do not depend on it.

## Tasks

Order: 0 can run alongside 1-2. 1-6 are the first deliverable; with one voice space (task 0 yes) task 7 is a
sanity check, otherwise it blocks turning the feature on for `wespeaker-r34`. GPU machines with a token see no
change except the storage migration.

| # | What | Files | Tests | Risk |
|---|---|---|---|---|
| 0 | **Are the spaces the same?** A script that embeds 20-40 AMI turns (or any speech) with `PyannoteEmbedder` (community-1) and with the ONNX ResNet34-LM, and prints the cosine per turn (same audio) and the same/different-speaker cosine distributions of each. Pass: median same-audio cosine ≥ 0.95 and the distributions overlap. Result written into this plan; it sets `OnnxEmbedder.space`. | backend/scripts/compare-voice-spaces.py (new) | none (a tool); run by Corey on the GPU machine (needs torch, pyannote, HF token), not here | community-1's weights may differ from 3.1's; then answer "no" and use `wespeaker-r34` |
| 1 | **ONNX embedder.** `OnnxEmbedder` implementing `SpeakerEmbedder` (live_speakers.py:25-36) plus `space`: pinned `Download` for `wespeaker_en_voxceleb_resnet34_LM.onnx` (sha256 above, 26,530,550 bytes), `fetch` into `models_dir/diarization`; `load` builds a `SpeakerEmbeddingExtractor` (threads); `embed` converts int16 → float32 [-1, 1], returns None under 1 s, caps at 60 s, runs `compute` in a thread, returns a list. `space` on `PyannoteEmbedder` too. `resolve_speaker_embedder` / `build_speaker_embedder` in the registry; `build_live_embedder` kept as an alias. `ModelService.live_embedder` builds with one thread. | transcription/speaker_embedders.py (new; keeps sherpa import lazy), live_speakers.py, transcription/registry.py, services/model_service.py | test_speaker_embedders.py: a stub `sherpa_onnx` module in `sys.modules` (fixed vectors), conversions, short audio, cap; resolver combinations with `registry.installed` monkeypatched and with/without `hf_token` | sherpa's internal resampler quality at 48 kHz (accept_waveform resamples); if poor, resample with numpy first |
| 2 | **Voice spaces in storage.** `speaker_voices`, `session_voices`, migration from `speakers.embedding` / `session_speakers` (space `pyannote`), repo API by space (`set_session_voices(id, space, {label: vec})`, `get_session_voices(id) -> {space: {label: vec}}`, `upsert_speaker_sample(name, vec, space)`), `relabel_session_speaker` renames every space, `SpeakerProfile.voices`. `has_voice` = any space. | storage/sqlite.py (SCHEMA, `_migrate_columns`, 4700-4840, sync rows 5040-5080, apply 5210-5225), models/speaker.py, api/routes/speakers.py, services/people.py | test_speakers.py: migration from an old-schema file; enrol by space; a vector of the same length in another space never matches; relabel keeps all spaces | the `session_speakers` drop touches sync rows and apply; do it in the same task |
| 3 | **Spaces through the final pipeline.** `DiarizationResult.space`; pyannote diarizer sets `pyannote`; Nemotron sets its embedder's space; `OnnxDiarizer(embedder=…)` loads it with its models and returns per-speaker vectors from `embedding_spans`; `ComposedEngine.last_speaker_space`; pipeline.py:176-258 reads/stores voices of that space, matches later parts within it, `SpeakerService.match(embeddings, space)`; `rename` enrols every space the label has. Prefetch and `DIARIZER_MB["onnx"]` (45 → about 75) include the embedder. | engine.py, composed.py, diarizers/onnx.py, diarizers/nemotron.py, diarizers/pyannote.py, services/pipeline.py, services/speaker_service.py, services/model_downloads.py:36, cli.py:107-140 | FakeDiarizer (tests/fakes.py) gains `space`; pipeline test: a multi-part meeting whose parts get matched by voice; a part in another space is not matched; onnx diarizer `_embed_speakers` with a fake embedder | the onnx diarizer's `diarize` cannot run in CI; keep the embedding step a separate method |
| 4 | **Live clustering with the ONNX embedder.** pipeline.py:812-835: `known` = profiles' voices in the embedder's space; `match_names` passes the space; `VoiceNamer`/`_session_voices` store the space and drop voices of another; `live_rediarize` passes its embedder's space. | services/pipeline.py, transcription/live_streaming.py, live_rediarize.py, api/context.py:84-86 | test_live_speakers.py / test_live_rediarize.py with a fake embedder in a second space: profiles of the other space are not offered | live CPU on a slow laptop (task 7 measures) |
| 5 | **Sync and combine by space.** `SyncSession.speaker_voices`; build (sync_state.py:195) and apply; `speaker_embeddings` = the `pyannote` space for older peers; merge per space (sync_merge.py:556-561); combine.py matches and carries per space. | models/sync_session.py, services/sync_state.py, services/sync_merge.py, services/combine.py | test_sync_*: a round trip with two spaces; an older peer (no `speaker_voices`); a three-way merge per space; combine across spaces does not relabel | sync format change; older peers must not wipe the new field (same pattern as `media: … \| None`) |
| 6 | **Report it.** `/api/system.speaker_embedder_in_use`, `pnpm gen:api`; docs (architecture.md, api-reference.md, team-server.md, macos.md P3 line, CLAUDE.md gotcha on diarizer/voice profiles). UI strings only after open question 5 is settled. | api/routes/system.py, src/lib/api/schema.d.ts (generated), docs | test for the field with `registry.installed` monkeypatched | none |
| 7 | **Measure and calibrate.** bench_live.py `--embedder` flag and threshold sweep; `mnemosyne-bench --voices` (genuine vs impostor on AMI); an on-demand workflow running both with the onnx extra and fetched AMI; record live ms per tick on a Mac (the teammate) and on a Linux laptop. If the spaces differ: per-space thresholds in code. | bench_live.py, bench.py, .github/workflows/voices.yml (new, workflow_dispatch) | test_bench_live.py / test_bench.py with fakes | AMI from the HF mirror in CI: size and time; run by Corey instead if too slow |

Phase 2 (not in the first deliverable):

- Dual enrolment on machines with both stacks (store `pyannote` and the ONNX space per meeting).
- "Re-learn voices" job (rules above) for profiles that lack this machine's space.
- Profiles in library sync (open question 3).
- The onnx diarizer on ResNet34 alone (one model, retuned threshold on AMI).
- Live re-diarization on the CPU with the onnx diarizer for non-NVIDIA machines.

## Risks

- If task 0 says "same space" wrongly (close but not equal vectors), profiles would match a little worse across
  machines. The pass bar (median ≥ 0.95 on the same audio) and task 7's sanity run guard this.
- A custom `diarization_model` with a different embedding model is tagged `pyannote` like today. Low: the setting is
  advanced and its label already says "also voice profiles".
- The migration drops `session_speakers`. A downgrade loses per-meeting voices made after the upgrade (not profiles:
  they read `[]` and stop matching). Acceptable for an unreleased branch; say so in the release notes.
- Live CPU on an 8 GB Mac with Phonon or Parakeet live: measured in task 7; CAM++ is the fallback model.

## Open questions for Corey

1. Run task 0 on the GPU machine (about 10 minutes, needs the HF token)? It decides whether profiles are shared
   across all machines or kept per space.
2. Is a 26.5 MB extra download on CPU machines fine (the offline AppImage grows by the same)?
3. Should voice profiles travel with library sync between your two computers? Today they never do; with one space it
   would be simple (merge running means by name).
4. If the spaces differ: keep `live_speaker_threshold` / `speaker_match_threshold` as pyannote-only settings with
   per-space constants in code, or make them offsets that apply to every space?
5. UI wording (wizard, Settings, the "Who is who?" card): who decides, and should the Settings diarizer list gain the
   `onnx` option it lacks today?
6. Calibration runs: an on-demand CI job, or you run `mnemosyne-bench` locally on AMI?

## Decisions (2026-11-17, orchestrator; Corey can overrule)

- Task 0 cannot run on Corey's desktop today: no HF token is set and community-1 is not cached. A proxy runs
  instead: the same script against `pyannote/wespeaker-voxceleb-resnet34-LM` (not gated, very likely the same
  weights as community-1's bundled embedder). The real run, with community-1 and a token, is Corey's.
- Until task 0 really says "same", the ONNX embedder's space is `wespeaker-r34`. If it later says "same", one
  migration retags `wespeaker-r34` voices as `pyannote` and `OnnxEmbedder.space` becomes `pyannote`.
- Thresholds for `wespeaker-r34` start as constants in code equal to pyannote's (0.45 live clustering, 0.6
  profiles); the two settings keep meaning "pyannote". Task 7 calibrates them later.
- The 26.5 MB download is fine. Profiles do not travel with library sync (phase 2).
- UI: the Settings diarizer list gains "On the CPU (ONNX)" (`onnx`); the HF token's hint says it is needed for the
  pyannote and Nemotron diarizers, not for voice profiles; the wizard's onnx line says about 75 MB and that voices
  are recognised across meetings; "Who is who?" stays as it is (it becomes true).
- Implementation runs as two sequential parts: A = tasks 1-3, B = tasks 4-6. Task 7 is a follow-up.

## Task 0 (proxy) result, 2026-11-17

What was compared: **the proxy, not community-1.** `pyannote/wespeaker-voxceleb-resnet34-LM` (`Model.from_pretrained`
+ `Inference(window="whole")`, CPU, pyannote.audio 4.0.7) against sherpa-onnx 1.13.8 running
`wespeaker_en_voxceleb_resnet34_LM.onnx` (size and SHA-256 checked). Audio: the bundled AMI excerpt
(`samples/sample-meeting.ogg`, 70 s). It has no reference turns and the script did not call the onnx diarizer; it cut
17 non-overlapping 4 s windows, the loudest of each equal slot. Several speakers, but only 17 turns (the 70 s clip
holds no more). Peak memory 1.2 GB, 26 s, 2 threads.

| | |
|---|---|
| Dimension | 256 and 256 |
| Same audio, cosine(pyannote, onnx) | **median 0.547**, min 0.233, max 0.686 |
| Turn-to-turn cosine, pyannote | p5 -0.14, p25 0.02, p50 0.63, p75 0.73, p95 0.82 |
| Turn-to-turn cosine, onnx | p5 -0.01, p25 0.14, p50 0.53, p75 0.73, p95 0.80 |
| Spearman of the two pairwise-cosine matrices | 0.935 |

Conclusion: **not the same space** (bar: median >= 0.95). The two models rank pairs of turns almost the same way
(Spearman 0.93: same network family, same notion of who sounds alike), but the vectors for one clip are far apart, so
a pyannote profile cannot be matched with an ONNX vector. With the decision above, the ONNX embedder's space stays
`wespeaker-r34` until the real check says otherwise. The gap is likely a front-end difference (sample scaling of the
fbank input, mean normalisation, or the checkpoint), not different weights; not investigated here. Because this is the
proxy, community-1's bundled embedder could differ again, so the real run is still worth doing.

Real check, on a machine with the HF token and community-1 accepted (about 10 minutes on CPU):

```
cd backend && HF_TOKEN=hf_... uv run --no-sync python scripts/compare-voice-spaces.py
```

Add `--turns 17 --seconds 4` (the default turns of 30 give 17 on the 70 s sample) or pass a longer meeting file as the
first argument for more turns.

### Scaling hypothesis (tested once, 2026-11-17): wrong

The guess was that sherpa-onnx feeds the model samples at a different scale from pyannote. pyannote's WeSpeaker
wrapper multiplies the waveform by 2^15 and runs `torchaudio.compliance.kaldi.fbank` (80 bins, 25/10 ms, hamming,
`snip_edges=True`, `round_to_power_of_two=True`, dither 0), then subtracts the clip's mean over time. The ONNX file's
metadata says `normalize_samples: 0`, so sherpa's feature extractor (fed [-1, 1] by `accept_waveform`) is told to scale
to the int16 range itself. The scales were therefore probably equal already. Test: `compare-voice-spaces.py --proxy
--onnx-scale 32768` (new option, multiplies the samples before sherpa) gave same-audio median **-0.061** (min -0.115,
max 0.095), Spearman 0.455: far worse, as expected if sherpa already scales. So scale is not the cause; the 0.547
median (Spearman 0.935) stands.

What is left, not tested: sherpa's own fbank options (its defaults for dither, `snip_edges`, window type, mean
normalisation are in the C++ extractor, not in the Python config of 1.13.8, which exposes only `model`,
`num_threads`, `debug`, `provider`), or a different checkpoint behind the two files. If the ONNX embedder is to share
pyannote's space, the next check is to compute the fbank in numpy/torchaudio as pyannote does and run the ONNX model
on it directly with onnxruntime (the model takes fbank frames). That also makes a pyannote-identical front end possible
without torch (torchaudio's kaldi fbank is plain DSP), a candidate for a later task. Peak memory of this run 1.2 GB.

## Progress (2026-11-17)

Done (part A, tasks 1-3): the ONNX embedder, voice spaces in storage, spaces through the final pipeline.

Done (part B, tasks 4-6 and the UI/doc changes decided above):

- Task 4: `VoiceNamer` keeps its embedder's `space` and passes it to `match_names(embeddings, space)`;
  `AppContext.live_voices` holds `(time, space, voices)` and `_session_voices` drops voices of another space;
  `LiveRediarizer` passes the diarizer's `result.space`; the pipeline takes the namer's threshold from
  `thresholds(space, settings).match`.
- Task 5: `SyncSession.speaker_voices` (`{space: {label: vector}} | None`) beside `speaker_embeddings` (the pyannote
  space, for older peers). Apply: None replaces only the pyannote space; otherwise every space is replaced.
  Three-way merge per space; a side without the field says nothing about the other spaces (no deletion).
  `combine.relabel_speakers` matches and carries voices per space with each space's threshold. Phase 8 sharing is
  unchanged.
- Task 6: `/api/system.speaker_embedder_in_use` (`pyannote` | `onnx` | `none`), `pnpm gen:api`; Settings diarizer list
  has "On the CPU (ONNX)", the HF-token hint and the wizard's onnx / token lines were reworded; architecture,
  api-reference, team-server, macos, development and README docs and CLAUDE.md were updated.

Left:

- Task 7: calibration (`bench_live.py --embedder` and a threshold sweep, `mnemosyne-bench --voices`, an on-demand
  workflow, live ms per tick on a Mac). `wespeaker-r34` runs on pyannote's thresholds (0.45 / 0.6) until then.
- Task 0, the real run (community-1 with a token): it decides whether `wespeaker-r34` can be retagged `pyannote`.
- Phase 2: dual enrolment, "re-learn voices", profiles in library sync, the onnx diarizer on ResNet34 alone, live
  re-diarization on the CPU.
- The Settings diarizer option "Automatic (Nemotron on an NVIDIA GPU, else pyannote)" does not mention the onnx
  fallback; the wording was not part of the decided UI changes.
