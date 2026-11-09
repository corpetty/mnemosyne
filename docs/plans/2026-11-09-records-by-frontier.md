# Records by frontier: seals over a manifest of the meeting's parts

Status: built 2026-11-09 on branch records-v2 (see "Changed while building" at the end). Plan, 2026-11-09. From the "Later" list of
docs/plans/2026-10-12-hashseq-collaboration.md (line 1041): "Records: a meeting's frontier is a hash of
its exact state, a candidate for sealing record versions (services/records.py); clips as artifacts can
be erased for retention while the rest still verifies." Corey asked for it to be built.

Short version: the idea as worded does not fit the code (most of a meeting is not a CRDT, and clips are
not artifacts of anything), but what it is after is worth having and is not hard: seal a **manifest** of
per-part digests (a Merkle-style root over the meeting) instead of one hash of two parts, digest audio
by its plaintext so a deleted file stays a verifiable entry, and stop the record card from crying
"changed outside the app" after things the app itself did. The notes' hashseq frontier is an optional
extra, recommended for later.

## 1. Facts (as of abb1d5a)

### How records work today

- **What a seal covers.** `content_of` (backend/mnemosyne/storage/records.py:10-18) is the transcript
  lines' `speaker/start/end/text` and the summary markdown. Nothing else: not `summary_data`, the notes,
  comments, notes items, attendees, the name. `content_hash` = SHA-256 of canonical JSON (:21-26).
- **Audio in a seal.** `audio_hashes` (services/records.py:59-61) hashes each file **as stored**: an
  encrypted file's ciphertext, keyed by its file name including `.enc`. `audio_files` (:52-56) is the
  `recordings` rows plus `audio_file`; `[]` when `session.origin` is set (recorded on another computer).
  Hashes are cached in memory by (path, size, mtime) (:37-49).
- **The chain.** `chain_hash(prev, content, audio, at, reason)` (storage/records.py:29-31).
  `add_seal` (storage/sqlite.py:2928-2956) skips a seal when content and audio equal the last one,
  unless `force`. Table `session_seals` (sqlite.py:393-403): `at, reason, content_hash, audio (JSON),
  prev, chain`, `ON DELETE CASCADE` with the meeting.
- **EVENT_SEALS** (services/records.py:67-76): publish / update / unpublish and guest notes
  started / ended / stopped are sealed with `force=True`, so the chain shows when they happened even if
  content did not change. `seal()` (:79-95) never raises and returns None for a meeting with neither
  transcript nor summary. `seal_later` (:98-105) runs `seal` in a thread on the running loop.
- **Who seals.** pipeline.py:305 and :340 ("transcribed"), :595 ("summarized"); routes/segments.py:37
  ("edited"); routes/speakers.py:47 ("speaker renamed"); summary_edit.py:214 and summary_readback.py:410
  (summary edits); publishing.py:1283, :1360; publication_file.py:212; guest_notes.py:1648. No other
  write path seals: not comments, notes, ticks, library sync's apply, audio deletion, combine,
  encryption on/off.
- **Verify** (services/records.py:127-165): recompute each seal's chain hash and `prev`, then compare the
  **last** seal with now: "The transcript or summary differs from the last seal", "Audio file X is
  missing / differs / was added after the last seal". For an `origin` meeting the audio is skipped
  (:143-150). `Verification` = `ok, seals, last_sealed_at, chain_head, problems` (:119-124), served in
  `GET /api/sessions/{id}/records` (api/routes/records.py:76-85).
- **Versions.** `keep_version` (sqlite.py:2873-2906) stores the earlier transcript+summary (and
  `summary_data`) on "summarized again" (:1404), "transcribed again" (:1441), "edited" (:2839), speaker
  renames (:4433). Restoring a summary goes through summary_edit.
- **Retention and holds.** `kept_until` (records.py:171-179), `check_delete` (:182-197: legal hold
  refuses; inside the period an admin with a reason; logs to `deletions`), `automatic_deletion_allowed`
  (:200-205), `protected_ids` (:108-116) used by `run_retention` (api/context.py:404-422) and the cleanup
  route (routes/storage.py:51-53). `deletions` table (sqlite.py:456-466) has no file names and no seal
  reference. Audio deletion is `StorageService.delete_audio` (services/storage_service.py:88-104):
  `clear_audio` (sqlite.py:1479-1492, which `_touch`es) then `rmtree(recordings/<id>)`.
  Retention by age (`cleanup`, :117-132) calls `delete_audio(reason="retention")`: a history event, but
  **no `deletions` row**, despite the module docstring's "Every deletion is logged".
- **Export** (records.py:210-372): meeting.json, transcript.txt/json, summary.md/json, versions,
  history, seals.json (with a verification), supervision, publications, guest_notes, `audio/`
  (decrypted), `media/`, deletions.json, README, SHA256SUMS. **No notes and no comments.** The README
  itself says (:237) the seals cover "the notes' text, not them" for images, but the seals do not cover
  the notes' text either.
- **UI.** components/RecordsCard.svelte:73 shows a red "changed outside the app" whenever
  `verification.ok` is false; :88-98 list problems and the chain head. Shown for every meeting with
  content (SessionView.svelte:371). No CLI verifies anything (cli.py:106-113 has `users`, `prefetch`).
- **Tests.** tests/test_records.py (8 tests; :52-82 the tamper test asserts the exact string "The
  transcript or summary differs from the last seal"; :170-187 images do not change seals),
  tests/test_publishing_records.py (event seals, reasons, under a legal hold).

### False alarms that exist today

These follow from the facts above (read from the code, not reproduced):

1. **Audio deleted legitimately** (after the records period, by an admin with a reason, or by
   retention) leaves the last seal naming files that are gone: verify says "Audio file X is missing"
   and the card says "changed outside the app". Nothing reseals after `delete_audio`.
2. **Encryption turned on or off** rewrites `x.ogg` as `x.ogg.enc` with a random nonce
   (storage/crypto.py:187, services/encryption.py) and nothing reseals: "missing" plus "added".
3. **Library sync** writes a peer's transcript and summary with `apply_synced` (sqlite.py:4747-4790)
   without a version or a seal: a meeting sealed on this machine and then edited on the other shows
   "differs from the last seal" here, and on the origin when the peer's edit merges in.
4. An export's `audio/` is plaintext but an encrypted meeting's seal hashed ciphertext: the export's
   audio can never be checked against the seal.

### What state a meeting has, and which parts have a frontier

| Part | Where | Kind | Frontier? |
|---|---|---|---|
| Shared notes | `shared_notes` snapshot + `shared_notes_ops` log (sqlite.py:329-357), folded by `mnemosyne-link notes doc` (notes-core/src/api.rs:1-40); `Session.notes` is the folded markdown | hashseq HashWeb | **Yes.** `HashWeb::frontier()` (hashseq 94f5e3c src/hashweb.rs:215) and `encode_frontier` (src/encoding.rs:3424) give the per-object tips; op ids hash content and parents, so the tips commit to the whole history. The sidecar does not return it today; Python cannot compute it (no bindings). |
| Summary document | `summary_docs` (sqlite.py:321-325), services/summary_doc.py:1-20 | per-block merge histories (`notes merge`, link/src/notes.rs) | Not a usable one: histories are started with `random_id()` (link/src/notes.rs:121-125), are per machine, do not travel (models/sync_session.py:4-7) and are **forgotten** when the sidecar fails (`SummaryDocUnavailable`). `summary_data` is the projection everything reads. |
| Vault note merge | `vault_notes` (sqlite.py:310-317) | same merge histories | Same: local machinery, not the record. |
| Transcript | `segments` rows | rows | no |
| Summary, summary_data | `sessions` columns | rows (JSON) | no |
| Comments | `comments` (sqlite.py:237-256) | rows | no |
| Notes items | `sessions.notes_items` | derived at every fold without `_touch` (sqlite.py:2717-2719) | no |
| Publications, guest rooms | `publications`, `guest_rooms` | rows | no |
| Notes images | `notes_media` rows + `<data_dir>/notes_media/<id>/<sha>.<ext>[.enc]` | blobs addressed by SHA-256; the sha is inside the notes markdown (`mnemosyne:image/<sha>.<ext>@WxH`) | no, but content-addressed |
| Clips | `recordings/<id>/clips/<first>-<last>.ogg[.enc]` (routes/clips.py:43, services/clips.py:51) | a cache cut on demand from the mixed audio, overwritten on the next cut, removed with the audio folder | no; not part of any record today |
| Audio | `recordings` rows + files | blobs | no |

Change tracking that already exists: every write that travels by library sync calls `_touch`
(sqlite.py:4548-4577), which advances the meeting's `sync_seq`, including `apply_synced` and
`clear_audio`. A row edited behind the app's back does not. Precedent for watching it:
services/live_publications.py `Watcher` (poll every 5 s, quiet 10 s, `MAX_WAIT` 60 s; tests set
`interval` 0 and tick it; started in api/context.py:321-323, :654).

## 2. What "sealed by frontier" would add, honestly

- **Tamper evidence.** Today's seal already catches an edited transcript or summary. What is missing is
  *coverage* (notes, comments, summary_data, attendees are unsealed) and *precision* (no way to tell
  "the app changed it, not sealed yet" from "changed outside the app"). A frontier does not help with
  either; per-part digests plus the `sync_seq` at seal time do.
- **Cheaper seals.** Barely. Hashing the row parts is microseconds; audio is the cost and is already
  cached. A small real gain: a seal can reuse the last seal's audio digest when the file's size and
  mtime are unchanged (verify still rehashes).
- **Verifying a version without storing a copy.** Only in the sense that a copy held elsewhere (an
  export, a backup, the other computer, a counterparty's copy of one transcript) can be checked against
  the sealed digest. Per-part digests make that selective: hand over the transcript and the manifest,
  and the other side can check it against a chain head written down earlier without seeing the notes or
  comments. Rebuilding an old *notes* state from its frontier is not possible with what we have: there is
  no checkout-at-clock in hashseq, and our op log is not complete (it starts over for shared meetings,
  sqlite.py:3578-3585, and a synced document can be adopted whole).
- **Proving an export matches.** Yes, and this is the most useful part: today SHA256SUMS proves the zip
  was not changed after export, but nothing ties the files to the seals (no notes or comments in the
  zip, audio digests over ciphertext). With part digests over the exported files and plaintext audio
  digests, a standalone checker can recompute the chain head from the zip alone.
- **Erasure that keeps verifying.** Yes. This is the property the idea names. It is not about clips
  (a cache, never sealed, already erased with the audio folder) but about **audio**: digest each file's
  plaintext, keep the digest in every later manifest under `erased` with when, who and why, and verify
  treats an erased file as expected. An earlier copy of the audio (an export) still checks against it.

**Is a single "meeting frontier" well-defined?** No. Only the notes are a hashseq document with a
canonical frontier. The summary and vault histories have random, per-machine ids and can be reset;
everything else is rows. The well-defined thing is a Merkle-style root: the hash of a manifest of
per-part digests. The notes' frontier can be one entry in it (section 6, deferred).

## 3. Construction

### 3.1 Part digests

`part_digest(name, payload) = sha256(canonical({"part": name, "v": 1, "data": payload}))` with
storage/records.py `canonical`. Payloads (recommended set, decision D1):

| Part | Payload |
|---|---|
| `transcript` | `content_of(session)["transcript"]` (same fields as v1) |
| `summary` | `session.summary` |
| `summary_data` | `summary_data.model_dump(mode="json", exclude_defaults=True)` or null (exclude_defaults so a field added in a later release does not change the digest of an unchanged summary) |
| `notes` | `session.notes`, the folded markdown, raw (it carries image shas, chips, refs; decision D8) |
| `comments` | every comment of the meeting sorted by id, fields: id, parent_id, target, target_id, anchor, quote, text, author_id, author, mentions, created_at, updated_at, resolved_at, resolved_by, deleted, approved_at, hidden_at |
| `meta` | name, participants, attendees, meeting_type, owner_id, agenda |
| `external_notes` | each one's id, source, text, added_at (they feed the summary) |

Not parts: notes items (derived per machine without `_touch`; their lines and done state are in the
notes, their tracking fields are in summary_data's items or are tracking metadata), copilot notes
(copied into the notes when `copilot_to_notes`), bookmarks, live flags, assets, publications (their
events are already force-sealed and exported), summary documents, vault notes, clips.

A test vector file `backend/tests/vectors/records_v2.json` pins the digests of a fixed meeting, so a
change to any payload definition fails CI instead of silently breaking every existing seal.

### 3.2 The manifest

```json
{
  "format": 2,
  "session": "<id>",
  "rev": 1234,
  "content_v1": "<content_hash(content_of(session))>",
  "parts": {"transcript": "<hex>", "summary": "<hex>", "summary_data": "<hex>", "notes": "<hex>",
            "comments": "<hex>", "meta": "<hex>", "external_notes": "<hex>"},
  "audio": {"<name without .enc>": {"sha256": "<plaintext sha>", "bytes": 123}},
  "audio_here": true,
  "erased": {"<name>": {"sha256": "...", "bytes": 123, "at": "...", "by": "...", "reason": "...",
                         "deletion": 17, "matched": true}}
}
```

- `rev` is the meeting's `sync_seq` when sealed: an app write after it advances `sync_seq`, a row edited
  in the database does not.
- `content_v1` lets an auditor see across the format change that the first v2 seal covers the same
  transcript and summary as the last v1 seal.
- `audio_here` is false for an `origin` meeting (no audio on this computer; `audio` is `{}`).
- `erased` is cumulative: once a file is erased it is listed in every later manifest. `matched` says
  whether the file still equalled the last seal's digest when it was erased (false is recorded, not a
  reason to refuse the deletion: decision D4). `deletion` is the `deletions` row id.
- `manifest_hash = sha256(canonical(manifest))`.

### 3.3 The seal row and the chain

`session_seals` gains `format INTEGER NOT NULL DEFAULT 1`, `manifest TEXT` and `audio_stat TEXT`
(per file size and mtime_ns, local, not chained: only for reusing digests). A v2 row stores
`content_hash = manifest_hash`, `audio = '{}'`, the manifest JSON, and

`chain = sha256(canonical(["mnemosyne/seal/2", prev, manifest_hash, at, reason]))`.

A v2 row's `prev` is whatever row came before, v1 or v2: one chain per meeting, never restarted.

`add_seal` skips when the manifest hash equals the last v2 row's (unless `force`). When the last row is
v1, the next seal is always written: that is the upgrade (reason as given, or "sealed in the new format").

### 3.4 Verify

1. Every row: `prev` follows; v1 rows recomputed with today's `chain_hash`; v2 rows recompute
   `manifest_hash` from the stored manifest (must equal `content_hash`) and the v2 chain hash.
2. Between consecutive v2 rows: an audio entry may leave `audio` only into `erased` with the same sha,
   and an `erased` entry never disappears or changes ("Audio file X left the record at seal N without
   being erased"). An erasure's `deletion` row must exist.
3. Last row v1: today's comparison, except a `name` / `name.enc` pair counts as "re-encrypted since the
   last seal; open the record once to seal it in the new format" (informational, not a problem), since
   ciphertext cannot be compared.
4. Last row v2: build the current manifest (audio rehashed in full, through the in-memory cache) and
   compare part by part, file by file. A difference while `sync_seq > rev` is **pending** (changed in
   the app, the sealer will seal it); with `sync_seq == rev` it is a problem: "The transcript or summary
   differs from the last seal" for those two parts (the existing string, kept), "The notes differ from
   the last seal", etc. Erased files are expected to be absent.
5. Images: each image sha referenced by the current notes whose blob is here must hash to its name
   ("An image in the notes was changed": content-addressed, so the notes digest already commits to it).
6. Locked encryption: audio digests are "unavailable" (no problem raised), the API already answers 423.

`Verification` gains `pending: bool`, `format: int` (of the last seal), `parts: list[PartCheck]`
(`name`, `status`: same / differs / pending / unavailable) and `erased: list[Erasure]` (`name, sha256,
bytes, at, by, reason`). `ok` is false only for problems; pending stays ok.

### 3.5 Who seals

- The explicit seals stay as they are (they give meaningful reasons) and now write v2.
- New explicit seals: audio erasure ("audio deleted", 3.6), combine ("combined", on the target), and
  before encryption converts a meeting's audio whose last seal is v1.
- A **record sealer** (services/record_sealer.py, the live_publications Watcher pattern): polls meetings
  whose `sync_seq` passed its cursor (meta `records_sealer_seq`, set to the current value on first
  start so an upgrade does not seal the whole library at once), waits for 60 s of quiet, at most
  10 min, skips meetings that are recording or have a job (their own seals follow), skips `shared_from`
  meetings, and seals with reason `changed: notes, comments` (the parts that differ from the last
  seal). This is what removes false alarms 3 (sync) and the new ones that wider coverage would
  otherwise create. Decision D2.

### 3.6 Erasure for retention

- `StorageService.delete_audio` gets a hook (set in `AppContext.build`, callbacks not imports) run
  **before** the files go: `records.seal_erasure(app, session_id, by, reason, deletion_id)` hashes each
  audio file's plaintext, compares it with the last seal, moves the files from `audio` to `erased`
  and seals "audio deleted". Clips go with the folder as today: never sealed, nothing to record.
- If the erasure seal cannot be made (locked key, I/O error): retention skips that meeting this pass
  (logged); a manual deletion answers 409 "The record could not be sealed; try again". Decision D4.
- Retention by age also writes a `deletions` row (`what="audio"`, by "retention", reason "older than N
  days"), which the docstring already promises.
- `log_deletion` stores the meeting's last `chain` and `content_hash` (new columns `chain_head`,
  `manifest_hash`, default ''), so a deleted meeting's record, whose seals cascade away, is still named
  in the deletion log and in deletions.json. Decision D7.
- Notes images and other blobs: not erased by anything today. If a later retention rule erases them,
  the same `erased` mechanism works keyed by sha (the notes digest keeps committing to it).

### 3.7 Old seals and migration

- No rewrite of existing rows; v1 rows verify exactly as today, forever.
- Additive columns in `_migrate_columns` (sqlite.py:783).
- Upgrade is lazy (decision D5): the next seal of any kind writes v2; opening the Record card
  (`GET …/records`) appends "sealed in the new format" when the last seal is v1 and still matches;
  erasure and encryption conversion upgrade first. A one-off startup pass over all meetings is not
  recommended (hashes every audio file; this machine has frozen under less).

### 3.8 Library sync, sharing, backups

- Seals stay local bookkeeping (models/sync_session.py:4-7), recommended (D11): each machine keeps its
  own chain of what it held. The sealer seals what sync brings, so a peer no longer shows sync as
  tampering. A peer's manifests have `audio_here: false`.
- Because the part digests are deterministic over content, two machines with the same meeting have the
  same part digests; showing "same record as on <origin>" is possible later by carrying the origin's
  last manifest in `SyncSession`. Not in this plan.
- Phase 8 recipients (`shared_from`): not sealed. Backups carry the database, so the seals; unchanged.

### 3.9 Export and a checker

- Per meeting, new files: `notes.md` (the raw notes markdown), `comments.json` (the comments as the part
  payload defines them), `external_notes.json`, and `record.json`: `{chain_head, format, manifest (the
  last seal's), parts: {name: file it is recomputed from}, erased: [...]}`. meeting.json gains
  `owner_id` and `agenda` so `meta` can be recomputed from it. seals.json rows carry their manifests.
  README updated.
- `mnemosyne-backend records verify <export.zip>` (cli.py dispatch like `users`; logic in
  services/records_check.py, which imports only storage/records.py and the models, never AppContext):
  checks SHA256SUMS, recomputes every part digest from the files, the manifest hash, the whole chain
  from seals.json, each `audio/` file's sha against the manifest, and lists erased audio. Prints one
  block per meeting and exits 0 or 1. Nothing needs the app, the database or the sidecar.

### 3.10 What the UI shows (for Corey: wording and layout are his, D10)

Proposed, within the existing RecordsCard:
- the red "changed outside the app" only for real problems (unchanged rule: `!ok`);
- "Changes since <time> are being sealed" when `pending`;
- one line per erased file: "Audio deleted <date> by <who>: <reason>. Still verifiable against copies
  (SHA-256 …)";
- problems listed per part as today. No new dialog, no new view.

## 4. Decisions for Corey

**Answered 2026-11-09:** Corey took every recommendation; D10 is the wording proposed in 3.10. Task 10 (notes frontier) stays deferred.

- **D1 Coverage.** Which parts are sealed. Recommendation: transcript, summary, summary_data, notes,
  comments, meta, external_notes (3.1); not notes items, copilot notes, bookmarks, assets, clips.
- **D2 Automatic sealing.** A sealer that seals every app change after 60 s quiet (at most 10 min),
  vs. explicit seals at chosen write paths only. Recommendation: the sealer. Without it, wider coverage
  turns every comment or notes edit into a red "changed outside the app" until the next transcription.
  Cost: a row forged in the database and followed within minutes by any app edit gets sealed as if the
  app wrote it. Today that already happens at the next explicit seal; the window gets shorter, not
  new. The real defence stays the chain head written down outside (exports, backups).
- **D3 Plaintext audio digests.** Recommendation: yes. Stable across encryption on/off, checkable
  against an export. Cost: hashing an encrypted file decrypts it in chunks (EncryptedFile.read).
- **D4 When the erasure cannot be sealed.** Recommendation: retention skips the meeting this pass;
  a manual deletion refuses with 409. And when the file no longer matches the last seal at erasure:
  record `matched: false` and delete anyway (retention is policy), shown as a problem on the card.
- **D5 Upgrading v1 chains.** Recommendation: lazy (next seal, opening the record, before erasure or
  encryption conversion); no startup pass.
- **D6 Notes frontier in the manifest.** Recommendation: not now (section 6).
- **D7 Chain head in the deletion log.** Recommendation: yes, for meetings and audio.
- **D8 Notes as raw markdown in the digest and the export.** Recommendation: raw (`Session.notes`): it
  is the exact record and the export is a file, not a rendering. Alternative: `plain_notes` (chips as
  names, images as `[image: caption]`), which loses the image shas the digest relies on.
- **D9 A checker CLI for exports.** Recommendation: yes, `mnemosyne-backend records verify <zip>`.
- **D10 Record card wording** (3.10). Corey's call; the plan's tasks implement whatever he picks.
- **D11 Seals across library sync.** Recommendation: stay local.
- **D12 Notes-only meetings.** Seal meetings with notes or comments but no transcript or summary
  (today `seal()` returns None for them). Recommendation: yes.

## 5. Tasks

Verification for every backend task: the named single test files with
`cd backend && uv run pytest tests/<file> -q`, plus `uv run ruff check . && uv run ruff format --check .`.
Never the full suite locally (CLAUDE.md: it OOM-killed the app); CI runs it.

1. **Manifest functions.** Part payloads and digests, manifest, `manifest_hash`, `chain_hash_v2`,
   comparison helpers returning per-part and per-file differences. Pure functions, no I/O.
   Test vectors file and its generator script (regenerated only on purpose, like the publication
   vectors). Tier: careful. Depends on: none. Files: backend/mnemosyne/storage/records.py,
   backend/scripts/make_records_vectors.py (new), backend/tests/vectors/records_v2.json (new),
   backend/tests/test_records_manifest.py (new). Verify: test_records_manifest.py.
2. **Schema and repository.** Columns `format`, `manifest`, `audio_stat` on `session_seals`;
   `chain_head`, `manifest_hash` on `deletions`; migrations; `add_seal` for v2 (skip rule on manifest
   hash; always write after a v1 row); `seals()` returning the new fields; `log_deletion` filling the
   head from the last seal; `sync_seq_of(session_id)`; `seal_candidates(after_seq, limit)` (includes
   meetings `changes_since` filters out only by `shared_from`, same rule). Tier: medium. Depends on: 1.
   Files: backend/mnemosyne/storage/sqlite.py. Verify: test_records.py (unchanged, still green),
   a migration test in test_records_manifest.py on a v1-only database.
3. **Seal and verify v2.** `seal()` builds the manifest (plaintext audio digests streamed through
   `EncryptedFile`, cache by path/size/mtime_ns, reuse of the last seal's digest when `audio_stat`
   matches, `audio_here`), notes-only meetings (D12); `verify()` for mixed chains, erasure continuity,
   pending via `rev`, the `.enc` rename tolerance for v1, image sha checks, `Verification` extended
   (`pending`, `format`, `parts`, `erased`); lazy upgrade on `GET …/records` (D5). Keep the existing
   problem strings for transcript/summary/audio. Tier: heavy. Depends on: 1, 2. Files:
   backend/mnemosyne/services/records.py, backend/mnemosyne/api/routes/records.py,
   backend/tests/test_records.py. Verify: test_records.py, test_publishing_records.py,
   test_records_manifest.py.
4. **Erasure and deletion.** The `delete_audio` hook and `seal_erasure` (3.6), refusal rules (D4),
   `deletions` rows for retention, combine sealing its target, encryption conversion upgrading a v1 last
   seal before it converts (services/encryption.py `_convert_audio` caller). Tier: careful. Depends on:
   3. Files: backend/mnemosyne/services/storage_service.py, backend/mnemosyne/services/records.py,
   backend/mnemosyne/api/context.py (hook, run_retention), backend/mnemosyne/api/routes/storage.py,
   backend/mnemosyne/services/combine.py, backend/mnemosyne/services/encryption.py,
   backend/tests/test_records_erasure.py (new: delete with reason then verify ok with one erasure;
   retention; swapped file before erasure gives `matched: false`; encryption on then verify ok, using
   MemoryKeyStore; deleted meeting's head in `deletions`). Verify: test_records_erasure.py,
   test_records.py.
5. **The record sealer.** services/record_sealer.py (cursor, quiet 60 s, max wait 10 min, skip busy and
   `shared_from`, reason listing changed parts, `interval` 0 in tests and a `tick()`), started and
   stopped in AppContext like the live watcher. A coverage test: for each covered part, its app write
   path advances `sync_seq` (comment add/edit/resolve/approve/hide, notes fold and the notes route,
   summary edit, tick, rename, attendees, external notes, `apply_synced`); any path that does not is
   either fixed with `_touch` or reported back before the part is covered. Tier: careful. Depends on: 3.
   Files: backend/mnemosyne/services/record_sealer.py (new), backend/mnemosyne/api/context.py,
   backend/tests/test_record_sealer.py (new), backend/tests/conftest.py (interval 0, if the fixture
   needs it). Verify: test_record_sealer.py, test_records.py, test_publishing_records.py.
6. **Export.** notes.md, comments.json, external_notes.json, record.json, meeting.json `owner_id` and
   `agenda`, manifests in seals.json, README text. Tier: medium. Depends on: 3 (4 for `erased`). Files:
   backend/mnemosyne/services/records.py, backend/tests/test_records.py (export tests). Verify:
   test_records.py.
7. **Export checker CLI.** services/records_check.py (pure) and the `records verify` dispatch in cli.py;
   tests build an export with `export_zip`, check it passes, then change one byte of transcript.json,
   of notes.md, of an audio file and of seals.json and see each named; an export after an erasure
   passes and lists it. Tier: medium. Depends on: 6. Files: backend/mnemosyne/services/records_check.py
   (new), backend/mnemosyne/cli.py, backend/tests/test_records_check.py (new). Verify:
   test_records_check.py; `uv run mnemosyne-backend records verify <zip from the test>` by hand once.
8. **API types and the Record card.** `pnpm gen:api`; RecordsCard per D10. Tier: light (after Corey
   settles D10). Depends on: 3, 4. Files: src/lib/api/schema.d.ts (generated),
   src/lib/components/RecordsCard.svelte. Verify: `pnpm gen:api` (no diff after), `pnpm check` (0
   errors). The e2e record specs, if any, run in CI.
9. **Docs.** docs/team-server.md "Records", docs/api-reference.md records rows (fields of
   `Verification`, the erasure, the CLI), docs/architecture.md if it describes seals, a short CLAUDE.md
   entry (records seal a v2 manifest; payload changes need the vectors; the sealer; erasure hook).
   Tier: light. Depends on: 1-8. Files: those docs, CLAUDE.md. Verify: read through.
10. **(Deferred, D6) Notes frontier.** Section 6. Tier: heavy. Depends on: 3.

Order: 1 → 2 → 3 → (4, 5, 6 in parallel; they share services/records.py, see below) → 7 → 8 → 9.

### Files shared across tasks

- backend/mnemosyne/storage/records.py: 1 (writes), 3 and 7 (import).
- backend/mnemosyne/services/records.py: 3, 4, 6 (separate functions; 4 adds `seal_erasure`, 6 changes
  `export_zip`; serialize or stage by hunk, see memory "shared checkout").
- backend/mnemosyne/storage/sqlite.py: 2, and 4 only if a `_touch` is missing (via 5's coverage test).
- backend/mnemosyne/api/context.py: 4 (hook, retention) and 5 (sealer lifecycle).
- backend/tests/test_records.py: 3, 6.
- src/lib/api/schema.d.ts: regenerated once in 8 after 3 and 4 changed the models.

## 6. The notes frontier (deferred)

What it would be: the `notes doc` answers carry `frontier` (hex SHA-256 of `encode_frontier(web)`), the
backend keeps it beside the snapshot, and the `notes` part becomes `{"md": digest, "frontier": hex}`.
A sidecar op `contains` would check that the last seal's tips are still in the current document: "the
notes' history only grew since seal N", which a markdown digest cannot say.

Why not now:
- The frontier changes when the markdown does not (typing then deleting, a catch-up `api` edit, a ref
  set by the copilot), so the sealer would seal "changed: notes" with nothing visible changed.
- It exists only where the sidecar folds: no sidecar (LegacyNotesEditor), a fold that failed, or a
  machine that has not folded the latest frames gives a different or no value; library sync converges
  only after both sides fold the union. Verify would have to treat it as optional anyway.
- The log is not complete (shared meetings start over, synced documents are adopted whole), so an old
  frontier cannot be turned back into old text; the check is "contained", not "show me seal N".
- It costs a notes-core API change (notes-core is also compiled into the wasm: run
  scripts/build-notes-wasm.sh and confirm src/lib/notes/pkg is byte-identical, or commit it), a link
  rebuild, and fake-sidecar updates in tests/fake_link.py.

Worth revisiting when ops are signed (hashseq plan, "Prerequisites" 2): then a frontier is an
attestation of who wrote what, and sealing it would mean something a markdown digest cannot.

## 7. Risks

- **Coverage without `_touch`.** A covered part written by a path that does not advance `sync_seq`
  shows as tampering (worse than today). Task 5's coverage test is the guard; notes items are left out
  for this reason (sqlite.py:2717-2719).
- **Payload drift.** A model change (a renamed or removed summary_data field, a new comment column)
  changes digests of unchanged meetings. `exclude_defaults` handles added fields; the vectors file makes
  anything else fail CI.
- **Laundering window** (D2), stated above.
- **CPU and memory.** The first verify or seal after a start hashes the meeting's audio, now decrypting
  encrypted files. Same order as today; the sealer reuses digests by size and mtime.
- **v1 meetings never opened again** keep v1 seals and keep today's false alarms until their next seal
  or erasure. Acceptable; the `.enc` tolerance covers the encryption case.
- **Deleted meetings lose their seals** (cascade); D7 keeps the head in the deletion log only.
- **Concurrent edits** (task 3, 4, 6 in one file; another session in the same checkout): stage by hunk.

## 8. Changed while building

- Verify compares with `compare_step`: an `erased` entry holds the measured sha (what the file hashed to
  when it was erased) and `matched` says whether it equalled the sha sealed earlier.
- `summary_data.json` was added to the export (the `summary_data` part is otherwise not in any file).
- A file no seal listed gets a pre-seal (entered in the record) before its erasure is sealed, so
  nothing is erased that the chain never knew.
- "up to date" seals: when the app touched a meeting (`rev` ahead) without changing a part, the sealer
  still seals, which closes a gap where a later outside edit would have hidden behind the stale `rev`
  as pending.
- `adopt_unowned` now `_touch`es each adopted meeting, so adoption is a version rather than a false alarm.
- The notes frontier is still deferred (section 6).
