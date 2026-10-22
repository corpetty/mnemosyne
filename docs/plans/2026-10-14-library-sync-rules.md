# Library sync: write paths, audio paths and automation (phase 3 reference)

Companion to docs/plans/2026-10-12-hashseq-collaboration.md "Phase 3 design". Built from an
investigation of the code at ea282b1 and decided by the orchestrator, 2026-10-14. SQ =
backend/mnemosyne/storage/sqlite.py. "Origin" = the machine that recorded a meeting
(`sessions.origin`, '' = this machine). Line numbers drift; names do not.

## 1. Which writes bump the meeting's version (`_touch`)

Every write goes through SessionRepository (no raw SQL elsewhere), so `_touch(session_id)` inside
each write's own `with self._lock, self._conn:` block covers everything. `_touch` also sets
`updated_at` (several writers do not today: add_recordings, external notes, asset links,
bookmarks, live flags).

Bump: `save` (new meeting: origin '', fresh vv), `update_fields` (except the fields below),
`replace_segments`, `edit_segments`, `add_recordings` (via set_audio for parts/combine/import),
`clear_audio` (the peer learns the audio is gone), external notes add/delete, `attach_asset` /
`detach_asset`, `update_asset` and `delete_asset` (bump every meeting that references the asset;
query session_assets before the delete), bookmarks add/update/delete, `move_bookmarks` and
`move_events` (bump source and target), live flags add/delete, comments add / edit / resolve /
delete (`add_comment`, `update_comment_text`, `set_comment_resolved`, `delete_comment`),
`set_action_item_done` / `_follow` / `_set_action_item_flag`, `relabel_session_speaker`,
`set_session_embeddings`.

Never bump (local bookkeeping or machine-local data): `add_event` (events travel with the
meeting, see 2), vault_notes, summary_docs, `keep_version`, seals, `log_deletion`, supervision
flags/reviews, FTS, vectors, `set_meta`, `set_attendee_emails`, `set_legal_hold`,
`set_crm_state`, shares, users, organizations, prefs, speaker profiles, asks, digests;
path-only rewrites: `rebase_paths` (backup restore) and encryption's `_convert_audio`
`set_audio` (needs a non-bumping variant: `set_audio(..., quiet=True)`).

`delete`: writes a `sync_tombstones` row instead of a bump (also for combine's absorbed
meeting: it is gone everywhere; both meetings of a combine are origin-local by construction).

The sync's own apply path is a separate repository method (not `save`, which stamps
updated_at=now and fires on_new_session): writes all tables without `_check_write`, sets the
merged `sync_vv` itself, bumps `sync_seq` only for a merge (not a fast-forward), and publishes one
`session` bus event per meeting.

## 2. What travels, field by field

- `status`: does NOT travel. A meeting's status on a non-origin machine is `completed` when it has
  segments, else `created`. (Transient states recording/encoding/transcribing/error are per
  machine.)
- `audio_file`: never applied from a peer; NULL on a non-origin machine. `recordings` rows travel
  with the origin's paths, used only for part offsets and display.
- `attendees` travel; attendee emails (`set_attendee_emails`), `legal_hold`, CRM state do not.
- `session_events`: travel with the meeting as a union by a new `uid`; event writes do not bump.
  Never sent: viewed, played, exported (access log), recover_*, interrupted, capture_problem,
  capture_ok, hubspot_* (machine-local happenings).
- Comments travel inside their meeting (`SyncSession.comments`, all columns), replaced whole on
  write and merged by id three ways (text through the merge, resolved/deleted as scalars).
- Assets: link and text assets travel (dedupe links by URL on arrival, as `find_link` does);
  file assets show "on <machine>" without bytes.
- Topic renames (services/topics.py rewrites summary_data across meetings) bump and travel as
  ordinary edits.

## 3. Audio consumers when the origin is another machine

409 "The audio is on <name>": POST /transcribe, record into an existing meeting (audio.py `_start`
when a session_id is given, restart), import a part into it (`import_audio`/`_import_part` with
session_id), `add_part`, combine (routes/sessions.py combine: 409 instead of 400), clips
get/save, history `recover`, storage `delete_audio`.
Stream from the origin when reachable, else 409: GET audio file (routes/audio.py `get_audio`,
check origin before `Path.is_file`).
Text only: quotes with `audio=True` (no clip).
Skip silently: encryption `_convert_audio` (and no bump), history `build` on-disk checks (orphans,
missing, pending, measure; still list parts from rows), records `audio_files`/`audio_hashes`
(nothing; `verify` reports no mismatch), records export zip (no audio), backup `rebase_paths`
(rows of non-origin meetings), storage report/expired/cleanup (explicit `origin == ''`),
recovery (`interrupted_sessions`, pending), pipeline `sources_for_session`.

## 4. Automation: origin only

Gate with `not session.origin` (skip silently; routes answer 409 as above):
`transcribe_session` (covers every submit site), `reset_transcribing` at startup, recovery and its
auto-transcribe, auto-summarize (inside transcription), `_auto_export` after a summarize (now
needs its own gate: summarize runs anywhere), `vault_sync.sync_vault` (needs `origin` on
SessionSummary) and `before_export`'s pull-back (manual export still writes the note anywhere),
export-all's list, HubSpot `auto_push` (manual push anywhere), auto-naming after a summarize,
`_apply_calendar`, retention and `cleanup`, copilot and live transcription (local recordings
only by construction).
Run on every machine: supervision `scan` (derived, local; rescan after a synced transcript lands),
search/vector indexing, people, topics, digest, Ask, tasks.

## 5. Ids

New meeting ids: `uuid4().hex[:12]` in models/session.py (`Session.id` default), the only place
meeting ids are generated. Nothing assumes 8 characters. Item, bookmark, recording, asset, flag
and capture ids keep their lengths.

## 6. The live notes document (phase 6)

Code: services/sync_notes.py (union, adopt, broadcast), `SyncService.take` / `serve_changes`,
`SessionRepository.store_notes_fold` and `store_synced_notes_doc`. Sidecar: `mnemosyne-link notes
doc` `merge` (notes-core), whose answer now also has `frames`.

- **What travels**: `SyncSession.notes_doc`, base64 of the canonical snapshot (equal op sets give
  equal bytes), or null for a meeting without one. `serve_changes` runs `fold_blocking` for each
  meeting first (unfolded frames, text written around the document), then exports. The base kept
  in `sync_served` leaves `notes_doc` out; the three-way text merge keeps using `notes`.
- **Taking a meeting in** (`take`, with the meeting's fold lock for the whole call, after folding
  what was typed here):
  - both have a document and they differ: union through the sidecar `merge`; `Session.notes` =
    the union's render; the three-way merge of `notes` is skipped. The union is stored first
    (`store_synced_notes_doc`: snapshot, markdown, `Session.notes` and the logged frames in one
    transaction, so no catch-up can mistake it for drift), then the rest of the meeting is
    written (`apply` for a merge, `write_session` for a fast-forward);
  - both have the same document: nothing for the document, the usual text merge of `notes`;
  - only theirs has one: the meeting is written as usual (text merged three ways, as text), then
    the document is adopted (stored with its own render as `markdown`). A difference between
    `Session.notes` and that render is drift: the next fold or join writes it into the document
    as an `api` edit. Without a base the document's render (theirs) is taken for `notes`;
  - only ours: the usual text merge; the catch-up does the rest;
  - ours dominates or equals but we lack the document: adopted anyway.
- **Connected editors** get what the merge brought: the sidecar's `frames` (artifacts, then one
  delta of the ops the local snapshot lacked) are logged as `sync (<peer>)`, folded, and
  broadcast like a client's frames. An adopted document has no frames to send: the open sockets
  get `{"type":"reload"}` and a normal close.
- **`_touch`**: `store_notes_fold` counts a new version when the stored snapshot changes (the
  document's first, formatting, comment marks, artifacts) and `Session.notes` will not change
  with it (that write counts, once). A fold that leaves the snapshot alone does not. Sync's own
  stores (`store_synced_notes_doc`) never touch: the apply path counts.
- **Failure**: a union that cannot be made (no sidecar, an error) leaves the meeting waiting
  (`_Outcome("waiting")`, logged). `SyncChanges.seqs` gives each served meeting's change number;
  `_take_page` reports the earliest waiting one and `_pull` goes on to the following pages but stores
  `since` as the minimum over the round of (earliest waiting seq - 1), so nothing else is blocked
  (`PeerState.waiting`, `waiting` in /api/sync/status). The next round asks
  again from there (the sync card says "N meetings waiting for their notes to merge"); meetings taken before it are re-taken harmlessly (equal documents make no
  sidecar call). A peer without `seqs` holds the whole page back.
