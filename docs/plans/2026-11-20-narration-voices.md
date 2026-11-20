# Narration in your own voice: plan

Status: phase 1 built on `docs/narration-voices-plan` (2026-11-20) with KittenTTS 2 as the engine, see "KittenTTS" and "Progress" at the end. The text below is the plan as written; file:line references are against `4b91407` (Release 0.33.0).
against that commit; re-find them by name before editing. Anything I could not check is marked **(verify)**.
I ran no model and no benchmark.

## Goal

A person records (or uploads) a few minutes of their own voice. Mnemosyne makes a **narration voice** from it,
and can then read any text in that voice: a meeting summary, the weekly digest, notes, a follow-up draft, or
text pasted in. The result is an audio file you can play in the app (with the line being read highlighted),
save to the Obsidian vault next to the note, or publish.

The same rules as the rest of the app apply:

- **Local first.** Synthesis runs on the person's own machine by default. A cloud engine is an explicit opt-in.
- **Your voice only.** A voice can be made only by the person it belongs to, proven by a spoken consent
  check, and only its owner can use it.
- **Pluggable.** A new `Synthesizer` Protocol, like `Transcriber` and `Diarizer`; engines installed on demand.

## What I found

1. **There is no text-to-speech anywhere today.** No TTS module, route or dependency in `backend/`, `src/` or
   `docs/`. This is new ground, so the plan adds a stage rather than changing one.
2. **The voice profiles we have cannot speak.** `speaker_voices` / `session_voices` (storage/sqlite.py:217-233)
   hold speaker-verification embeddings (192-d TitaNet, 256-d WeSpeaker/pyannote). They identify a voice; they
   do not hold enough to synthesize one. Cloning needs **real reference audio plus its transcript**.
3. **No enrolment audio is kept** (finding 7 of docs/plans/2026-11-17-speakers-without-torch.md). So narration
   voices need their own clip storage. That is also the right call for privacy: reference clips are
   biometric data and should live apart from meetings.
4. **But much of what we need already exists:**
   - *Heavy engines in their own venv, installed on demand, hash-pinned, model checked by sha256*:
     services/phonon_install.py. A torch-based TTS engine fits this pattern exactly and stays out of
     `uv.lock`.
   - *Long work as a Job with progress over the EventBus* (jobs.py): synthesis of a long digest is a Job.
   - *Audio quality signals*: audio/levels.py, audio/health.py, and DeepFilterNet in
     audio/noise_suppression.py to clean a noisy upload.
   - *A transcriber* to get the exact transcript of each reference clip (most cloning models need the
     reference text) and to round-trip check synthesized audio.
   - *A speaker embedder that runs everywhere* (transcription/speaker_embedders.py, `resolve_speaker_embedder`)
     to check that every clip and the consent statement are the same person, and to score clone similarity.
   - *Consent and history*: `Consent` and `log_consent` (api/routes/audio.py:55-63, 298-304) and the
     per-meeting history (`history.log`) give us a place to record consent and every narration.
   - *Users and roles* on the team server (services/users.py) to own voices.
   - *The glossary* (transcription/glossary.py) is a ready list of names and jargon that need pronunciation.
   - *Encryption at rest* (services/encryption.py) for the clips.
   - *sherpa-onnx is already a dependency* (pyproject `onnx` extra, >=1.13.8), and it runs ZipVoice
     zero-shot cloning on the CPU **(verify the version that added ZipVoice)**.
5. **Per-source attribution means we already know which audio is the user's own voice**: their mic track,
   labelled with their name. That could later feed a voice without a separate recording session (phase 3),
   but only with clear opt-in.
6. **CLAUDE.md gotcha applies directly**: never play test audio on the dev machine (the synthetic voice that
   went into Corey's podcast, 2026-09-25). Every test here works on files, never the speakers.

## Engines

Licence decides most of this. Many popular cloning models have non-commercial weights; Mnemosyne should ship
only what anyone may use.

| Engine | Licence | Runs on | Notes |
|---|---|---|---|
| **Chatterbox Multilingual V3** (Resemble AI) | MIT | NVIDIA GPU; CPU slow | ~500M, 23+ languages, ~5 s reference enough. **Every output carries Resemble's PerTh watermark**, which is what we want. Turbo variant (350M) is English, faster. |
| **Qwen3-TTS Base 0.6B / 1.7B** | Apache 2.0 | GPU; Mac via `mlx-audio` | 3-second cloning with reference text, 10 languages. MLX checkpoints exist for both sizes. CPU speed unknown **(verify)**. No built-in watermark. |
| **ZipVoice** via sherpa-onnx | Apache 2.0 **(verify)** | CPU, no torch | Fits the CPU/Mac-without-extras path like the onnx diarizer. int8 distilled English/Chinese model + vocos vocoder; 4 flow steps. Batch only. Quality below the two above **(verify)**. |
| OpenAI-compatible speech server / ElevenLabs | service terms | cloud | Opt-in only, like OpenAI/Anthropic summaries. ElevenLabs requires a consent attestation per clone and a spoken verification for its Professional clones. |
| ~~XTTS-v2, F5-TTS, Fish Speech / OpenAudio S1-mini~~ | non-commercial weights | | Excluded. IndexTTS-2 has a custom bilibili licence: excluded unless read closely. |

Proposed `auto`, same spirit as the diarizer: **Chatterbox** with working CUDA, else **Qwen3-TTS via MLX** on
Apple silicon, else **ZipVoice (sherpa-onnx)** on the CPU, else none. Task 0 decides whether this holds.

## Design

### The Synthesizer stage

`backend/mnemosyne/synthesis/` alongside `transcription/`:

```python
class Synthesizer(Protocol):
    name: str
    space: str                      # engine family; a voice is prepared per engine, like voice spaces
    sample_rate: int
    watermarked: bool               # True for Chatterbox; others get our own mark (below)
    async def prepare(self, clips: list[RefClip]) -> VoicePrompt: ...   # pick/condition reference audio
    async def speak(self, prompt: VoicePrompt, text: str) -> np.ndarray: ...
```

- Built from settings in `synthesis/registry.py`; imported lazily so the API starts without torch, and a
  `FakeSynthesizer` in tests/fakes.py (a sine per word) for tests and demo mode.
- Torch engines run as a local server in their own venv, `<data_dir>/tts/<engine>/venv`, installed by
  `services/tts_install.py` cloned from phonon_install.py (hash-pinned requirements, sha256-pinned model,
  `HF_HUB_OFFLINE=1` after install). ZipVoice runs in-process through sherpa-onnx.
- Like voice spaces, a voice's prepared prompt is tagged with the engine that made it and never reused by
  another engine. The source clips stay, so switching engine just re-prepares.

### Making a voice (enrolment)

Two ways in, both ending in the same checks:

1. **Record in the app**: a guided script of ~40 short sentences chosen for phonetic coverage (about
   2-3 minutes), read one at a time with the level meter showing. Re-record any line.
2. **Upload** files (any format the import path already reads). Long files are cut into clean single-speaker
   stretches by the existing VAD + diarizer.

Every clip then goes through:

- level, clipping and noise checks (levels.py, health.py), DeepFilterNet if noisy;
- **one speaker**: diarize; reject clips with more than one voice;
- **same person**: embed every clip with `resolve_speaker_embedder`; reject outliers against the clip median;
- **exact transcript** from the transcriber, which the person can correct (reference text matters);
- choose the best 10-30 s as the engine's reference (engines differ; Task 0 tunes it).

**Consent check (required, in the app, live).** The person reads aloud a fixed statement plus a few random
words shown only then:

> "I am <name>. I agree that Mnemosyne may make a synthetic copy of my voice for my own use. <random words>."

It passes when the transcript matches and the speaker embedding matches the enrolment clips above the
profile threshold (`thresholds(space, settings)`). An upload alone can never make a voice: someone with a
podcast episode of another person cannot pass the live check. The recording, the result and who did it go to
the history log, and are kept with the voice as its proof of consent.

### Narrating

A `narrate` Job takes a voice and a source (`summary`, `digest`, `notes`, `followup`, `text`):

1. **Speakable text**: markdown to plain speech (drop `[[ ]]`, links, checkboxes, tables become sentences;
   "Action items" reads as a list with owners). Optional "rewrite for listening" pass through the configured
   LLM provider, off by default.
2. **Pronunciation**: glossary terms get a respelling the person can set ("Mnemosyne" → "nee-MOSS-ih-nee").
3. **Chunking** by sentence into ~200-400 characters; each chunk synthesized; short crossfades; loudness
   normalised; encoded to OGG/Opus like recordings.
4. **Round-trip check**: transcribe the result and flag chunks with high word error (hallucinated or skipped
   words, the usual failure of these models); re-synthesize them once.
5. Store chunk timings so playback highlights the line being read, reusing the transcript playback sync.

Where it shows up:

- a **Listen** button on summaries, digests and notes; a "Narrate text…" box for anything else;
- **Obsidian export**: the audio saved into the vault and embedded (`![[2026-11-20 Weekly.ogg]]`);
- **publications**: an optional audio track on a published page;
- **MCP**: a `narrate` tool (returns a file path), so Claude can produce a spoken briefing.

### Safety and records

- **Owner only.** A voice belongs to a user (`owner_id`); on a team server only its owner may narrate with
  it or delete it. Admins can see that a voice exists and delete it; they cannot use it. No sharing in v1.
- **Marked as synthetic.** Chatterbox watermarks every output. For the other engines, embed an inaudible
  watermark too (Meta's AudioSeal is the candidate, MIT **(verify)**), plus `SYNTHETIC=mnemosyne` metadata
  tags in the OGG.
- **Never fed back in.** Imported audio is checked for the watermark: if found, the meeting is flagged
  "contains synthetic speech" and its voices are **not** learned into speaker profiles, so a narration can
  never be auto-labelled as the real person or strengthen their profile.
- **Every narration logged**: voice, source, text hash, engine, time, user. Visible to the owner.
- **Biometric data handling**: clips and prompts encrypted at rest, never synced to other devices or
  included in shares or backups by default, removed completely when the voice is deleted (logged in
  `deletions`). The cloud engines need an extra confirmation that names the provider before any clip leaves
  the machine.
- **No live voice output.** v1 renders files only: no reading into a call or a virtual mic. That keeps it
  a narration feature, not an impersonation tool.

### Why watermark

A watermark is a signal hidden in the generated audio. People can't hear it, but software can detect it,
and it survives ordinary handling (MP3/Opus compression, trimming, mixing). Chatterbox's PerTh hides it in
sound masked by louder sound; AudioSeal uses a neural generator and detector, with detection per frame. It
does three jobs here:

1. **Mnemosyne must not fool itself.** The app learns voices: name a speaker once and it labels that person
   in later meetings, and each rename adds to their profile's running mean. A narration played into a call,
   or a narration file imported, sounds like the real person *to our own embedder*, because a good clone is
   built to fool exactly that kind of model. Without a mark the app would label the clone as the person and
   could fold the clone into the person's profile. With one, import and live capture can check for it, flag
   the meeting "contains synthetic speech" and keep those voices out of `speaker_voices`. Nothing else
   tells a voice from its clone reliably.
2. **It protects the person whose voice it is.** Hosts and anyone with public recordings already have lots of
   real audio online. A clone could put words in their mouth (a fake endorsement, a fake ad read, a scam).
   The mark lets them show a clip came from a synthesizer. Resemble offers a detector for PerTh, and
   Chatterbox stamps every output whether or not anyone remembers to.
3. **Rules and records are heading this way.** The EU AI Act (Article 50) asks providers of generative systems
   to mark synthetic audio in a machine-readable way, with obligations from August 2026 **(verify scope for an
   app like ours)**; US rules are a patchwork (state voice-likeness laws such as Tennessee's ELVIS Act; the
   FCC treats AI voices in robocalls as "artificial"). The records features (seals, legal holds, flagged
   phrases) also need synthetic audio to stay distinguishable from real recordings.

**What it does not do.** It does not stop a determined bad actor: the engines are open source, so the marking
code can be removed from one's own copy, and heavy processing or re-recording through a speaker can weaken
it. The `SYNTHETIC=mnemosyne` metadata tag is weaker still (anyone can delete it). So the watermark keeps
honest use honest and the app's own loop clean; the live consent check is the main defence against misuse
(it controls who can *make* a voice; the mark identifies the output afterwards).

**Cost.** Inaudible and cheap: a small step after synthesis (or none, for Chatterbox). Proposal: always on, no
setting to turn it off (question 7).

### Data

```sql
CREATE TABLE narration_voices (
    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, name TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'en',
    consent_path TEXT NOT NULL, consent_at TEXT NOT NULL, consent_score REAL NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE narration_voice_clips (
    voice_id TEXT NOT NULL REFERENCES narration_voices(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL, path TEXT NOT NULL, text TEXT NOT NULL,
    seconds REAL NOT NULL, quality REAL NOT NULL, selected INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (voice_id, idx)
);
CREATE TABLE narration_prompts (          -- one prepared reference per voice and engine
    voice_id TEXT NOT NULL REFERENCES narration_voices(id) ON DELETE CASCADE,
    space TEXT NOT NULL, path TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY (voice_id, space)
);
CREATE TABLE narrations (
    id TEXT PRIMARY KEY, voice_id TEXT REFERENCES narration_voices(id) ON DELETE SET NULL,
    user_id TEXT NOT NULL, source_kind TEXT NOT NULL, source_id TEXT,
    text_hash TEXT NOT NULL, engine TEXT NOT NULL, model TEXT NOT NULL,
    path TEXT NOT NULL, chunks TEXT NOT NULL DEFAULT '[]', watermarked INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
```

Files under `<data_dir>/voices/<voice id>/` (encrypted) and `<data_dir>/narrations/`.

### API (docs/api-reference.md, then `pnpm gen:api`)

- `POST /api/voices` → new voice (draft); `GET /api/voices`; `DELETE /api/voices/{id}`
- `GET /api/voices/script` → the guided sentences
- `POST /api/voices/{id}/clips` (upload or recorded take) → checks run as a Job; `PATCH` a clip's text
- `GET /api/voices/{id}/consent-challenge` → statement + random words; `POST /api/voices/{id}/consent`
- `POST /api/voices/{id}/preview` → 10-second sample
- `POST /api/narrations` → Job; `GET /api/narrations/{id}` and `/audio`
- `GET /api/system` gains `synthesizer_in_use` and what is installed

Frontend: a **Voices** section under Settings → Devices and people (record, upload, consent, preview), a
`NarrationStore` in `src/lib/stores/`, and the Listen buttons wired through `controller.svelte.ts`.

## Tasks

**Task 0. Bake-off (decides the engines; no product code).** Add `mnemosyne-bench --tts`: for 3-4 consenting
voices (us), enrol each engine from the same 30 s and synthesize the same 20 paragraphs (a real summary, a
digest, glossary-heavy text). Score each with what we already have:
- *speaker similarity*: cosine between reference and output with the WeSpeaker embedder;
- *intelligibility*: WER of Phonon/Parakeet on the output against the input text;
- *speed*: real-time factor on an NVIDIA box, the Mac, and a plain CPU laptop;
- a blind listening pass by the two of us.
Pass mark proposal: similarity ≥ the live profile threshold, WER ≤ 5 %, faster than real time on that
machine's chosen engine.

**Phase 1. One person, desktop (MVP).**
1. `synthesis/` Protocol, registry, `FakeSynthesizer`, demo-mode output.
2. `tts_install.py` for the chosen GPU/Mac engine; sherpa-onnx ZipVoice in-process for CPU.
3. Enrolment: script, recording and upload, clip checks, transcript editing, consent check.
4. `narrate` Job: speakable text, glossary respellings, chunking, round-trip check, Opus output.
5. Listen on summaries and digests; synced highlighting; Obsidian embed.
6. Watermark (engine or AudioSeal), metadata tags, synthetic-audio detection on import.
7. Tests: everything against `FakeSynthesizer` and fixture files; e2e in demo mode. No audio played.

**Phase 2. Team server and browser.** Voices owned per user; enrolment and consent through browser
recording (same path as `StartBrowserRecordingRequest`); per-user narration logs; admin delete.

**Phase 3. Later, if wanted.**
- Build or top up a voice from the user's own mic tracks in past meetings (opt-in, owner only, same checks).
- A fine-tuned "studio" voice (LoRA on the chosen engine) from 30+ minutes of clean audio for higher quality.
- Streaming playback while long pieces render.
- More sources: podcast show notes, intros and ad reads.
- Cloud engines (ElevenLabs, OpenAI-compatible) behind the explicit opt-in.

## Questions for Corey

1. Is narration for **the user's own voice only**, or should a person be able to share a voice with a
   teammate (e.g. a co-host reading their segment)? The plan says own-only for v1.
2. Which hardware must narration work on at launch: NVIDIA only, or the Mac and plain CPU too? That decides
   whether ZipVoice is in phase 1.
3. Languages beyond English at launch?
4. Is a cloud engine wanted at all, given "local-first"?
5. Should narrated audio be publishable on the public pages and guest notes, or stay private for now?
6. Default to "rewrite for listening" through the LLM, or read the text exactly as written?
7. Watermark always on with no off switch, or a setting (default on)? The plan says always on.

## Sources

- Licences and candidates: [SiliconFlow, open-source voice cloning 2026](https://www.siliconflow.com/articles/best-open-source-models-for-voice-cloning),
  [XTTS v2 licence (CPML)](https://www.promptquorum.com/power-local-llm/local-tts-voice-cloning-piper-coqui-xtts),
  [open-source ElevenLabs alternatives 2026](https://blog.bymar.co/posts/open-source-voice-cloning-alternatives-elevenlabs-2026/)
- Chatterbox: [model card](https://huggingface.co/ResembleAI/chatterbox/blob/main/README.md),
  [Multilingual V3](https://www.resemble.ai/learn/models/chatterbox-multilingual),
  [Turbo](https://www.resemble.ai/learn/models/chatterbox-turbo)
- Qwen3-TTS: [paper](https://arxiv.org/html/2601.15621),
  [model card (Apache 2.0)](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice/blob/main/README.md),
  [mlx-audio support](https://blaizzy.github.io/mlx-audio/models/tts/qwen3-tts/)
- ZipVoice: [sherpa-onnx docs](https://csukuangfj.github.io/sherpa/onnx/tts/zipvoice.html),
  [paper](https://arxiv.org/html/2506.13053v3)
- Consent practice: [ElevenLabs voice cloning API](https://elevenlabs.io/blog/voice-cloning-api),
  [ElevenLabs usage policy summary](https://conductatlas.com/platform/elevenlabs/elevenlabs-usage-policy/voice-cloning-without-consent-prohibition/)

## KittenTTS (evaluated 2026-11-20)

Corey asked on the PR to evaluate [KittenTTS](https://github.com/KittenML/KittenTTS). Read from its repository, model
card and the kittenml 0.9.8 wheel; **I ran no model** (it could not be installed on the machine this was built on),
so speed and quality are the project's own claims until Task 0 measures them.

- **What fits.** KittenTTS 2 (1.7B, ternary) clones a voice zero-shot from 5-30 s plus the clip's transcript
  (`generate(text, reference=wav, reference_text=...)`), 24 kHz, 20 languages, sentence chunking built in. One model
  covers every machine we support: PyTorch on a Linux CPU or GPU, MLX on Apple silicon (`backend="mlx"`), a llama.cpp
  fork for a faster CPU, vLLM for NVIDIA. Its own numbers: 0.81 speaker similarity cloning an unseen recording. That
  is one engine where the plan had three (Chatterbox, Qwen3-TTS, ZipVoice).
- **Licence (the catch).** The code is Apache-2.0; the **weights are under the Stellon Labs Community License**, not
  an open-source licence: free for people and organisations under US$1M yearly revenue *and* under US$1M funding,
  commercial use must register with Stellon Labs, revocable, the AUP is incorporated by reference (only available by
  contacting them), outputs may not train or distil other models, and "Powered by KittenML" must be shown. Each user
  downloads the weights themselves after accepting it in the app. Mnemosyne itself stays MIT. **Corey: is that
  acceptable for Mnemosyne's users, and for petty.services?**
- **No watermark.** kittenml lists `resemble-perth` as a dependency but never imports it, so its outputs are unmarked.
  We add AudioSeal (MIT code and weights, `facebook/audioseal`) in the worker.
- **What it pulls in.** torch, transformers 5, diffusers, spaCy, misaki and more (115 pinned packages on Linux), and
  at load it fetches Chatterbox-Turbo's S3Gen vocoder (`ResembleAI/chatterbox-turbo`, MIT). Its own venv, like Phonon.
- **Smaller siblings** (nano 17M, pico 7M, mini 82M, ONNX on CPU) have built-in voices only, no cloning.

Decision for phase 1: KittenTTS 2 behind the `Synthesizer` Protocol, with the licence accepted at install. If the
licence is a problem, Chatterbox (MIT, PerTh watermark built in) goes behind the same Protocol: only
synthesis/kitten.py, its worker and the requirement files are engine-specific.

## Progress

Phase 1, built 2026-11-20 on `docs/narration-voices-plan`:

- **Synthesis stage** (backend/mnemosyne/synthesis/): `Synthesizer` Protocol, `RefClip`, `VoicePrompt`, `Speech`,
  `pick_reference` (engine.py); `FakeSynthesizer` for tests and demo mode (fake.py); speakable text, chunks and
  pronunciations (text.py, setting `narration_pronunciations`); the engine choice (registry.py, setting
  `synthesizer` auto|kitten|demo|none).
- **KittenTTS 2** (kitten.py, kitten_worker.py): a worker in `<data_dir>/tts/kitten/venv` over stdin/stdout JSON
  lines, started on first use; the model pinned at revision `c93cb7c`, loaded from a local copy with
  `HF_HUB_OFFLINE=1`; every output watermarked with AudioSeal. Installer services/tts_install.py (Phonon's steps:
  hashed requirements for Linux and Mac, regenerated by `backend/scripts/lock-kitten.sh`; licence acceptance
  required; the SHA-256 of every model file recorded, `verify`).
- **Voices** (services/voices.py; storage/voices.py tables `narration_voices`, `narration_voice_clips`,
  `narration_prompts`, `narrations`): guided script or uploads cut into pieces, level/clipping/noise/speech checks,
  the same-person check with the speaker embedder voice profiles use, transcripts the person can correct, the live
  consent check, owner-only access, files sealed by encryption at rest (`convert_files`, hooked into
  services/encryption.py).
- **Narration** (services/narration.py): the `narrate` job (summary, own notes, follow-up, digest, text), chunked
  synthesis, evened loudness, the transcription round trip, OGG/Opus tagged `SYNTHETIC=mnemosyne`, chunk times,
  history `narrated`. An import of a tagged file logs `synthetic_audio`, and `SpeakerService.rename` never enrols
  from such a meeting (`has_synthetic`).
- **API** routes/voices.py (docs/api-reference.md "Narration voices"); **UI** Settings → Voices
  (VoicesSettings.svelte, VoiceStudio.svelte) and Listen on summaries and digests (NarrateButton.svelte).
- **Tests** test_voices.py, test_synthesis_kitten.py (a stand-in worker), test_synthesis_text.py.

Not done yet, in order:

1. **Task 0 for real**: install KittenTTS on an NVIDIA box and the Mac and measure speaker similarity, round-trip WER
   and real-time factor (a `mnemosyne-bench --tts` mode is not written). Until then the CPU speed is unknown; on Linux
   the install uses PyTorch's CPU build even with NVIDIA (CUDA torch or kitten-tts-2-cpp is a follow-up).
2. **Watermark detection on import**: only the metadata tag is checked today (cheap, and anyone can strip it); the
   worker already has AudioSeal's detector (`detect`), not yet called on import.
3. Listen on notes and follow-ups in the UI (the API takes them), Obsidian embeds, publications, the MCP `narrate`
   tool.
4. Phase 2: enrolment in a team server's browser (the API and owner checks are in; untried there).

---

Generated By Dee's Ghost & Claude
