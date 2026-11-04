# Images in the notes: tasks

Status: built 2026-11-04, unreleased (branch images-in-notes; changes while building below). Was a draft. The design is docs/plans/2026-10-26-images-in-notes.md
(Corey's decisions: guests may add images, phone camera included; publications include notes images,
chosen per image, all on by default; caps 2 MB an image, 50 MB a meeting, 25 MB a guest room). This file
is the build order, drafted against b82ce4d (branch images-in-notes, origin/main) and checked against the
code. Phases 6-11 of docs/plans/2026-10-12-hashseq-collaboration.md are the surroundings.

Decided while planning (safe defaults; the ones Corey may want to revisit are marked *):
- One mark value, one regex everywhere: the closed `image` mark on the line's `\n` holds
  `<sha256 hex>.<webp|jpeg>@<w>x<h>` matching `^[0-9a-f]{64}\.(webp|jpeg)@[1-9][0-9]{0,4}x[1-9][0-9]{0,4}$`
  (`IMAGE_VALUE` in notes-core, `imageValue` in src/lib/notes/images.ts, `IMAGE_VALUE` in
  services/notes_media.py). The sha is of the stored bytes (WebP or JPEG, post-downscale). The line kind is
  `image`; its text is the caption; inline formats on an image line are don't-care (cleared by normalize,
  like code lines), so the caption is plain text in markdown alt position.
- Two live `image` values on one line (concurrent edits) resolve to the FIRST value in markedSpans order,
  as `lang`/`align` do (C2): deterministic on every replica. The design's "newest wins" has no clock. *
- The hash is known before the upload (computed in the browser), so the image line is inserted at once with
  its final mark and the upload runs beside it; readers that lack the bytes show a grey box of the mark's
  size and ask again later. The design's "committed after the upload" would need a line that is not in the
  document. A failed upload shows "Not uploaded: Retry / Remove" on the block (local state keyed by sha);
  a line left dangling shows "Image not available" to others. *
- Caps are decimal, as Corey said them: `IMAGE_MAX_BYTES = 2_000_000` (the stored bytes),
  `MEETING_MEDIA_MAX_BYTES = 50_000_000` (sum of a meeting's stored blobs, the new one included),
  `ROOM_MEDIA_MAX_BYTES = 25_000_000` (sum of a room's sealed blobs). Constants, not settings. *
- Guests reach room media over the `/live/{room}` WebSocket (new binary tags), never HTTP: the iroh proxy
  carries one WebSocket stream and the guest page has no fetch (C3). The bridge uses HTTP on the room
  (`PUT/GET /live/{room}/media/{name}` with the publisher Bearer token). Both paths share one store.
- A room blob's name is `hex(HMAC-SHA256(media_name_key, plaintext sha))` with
  `media_name_key = HKDF(key, info "mnemosyne/guest/media-name")`: the host sees an opaque name, every holder
  of the link computes it from the mark's sha, so no index frame is needed and a room dedups by content. The
  design's "sha256 of the sealed bytes" cannot be found by a reader (AES-GCM nonces are random) (C4).
- Unused blobs: a `notes_media` row per (session, sha) with `last_seen_at`; every fold's `media` list touches
  the rows it names; a sweep at startup and daily deletes rows (and files) not seen for 7 days, including
  uploads no line ever referenced. The sweep runs only when the sidecar is available (no fold = no
  knowledge). Rooms have no GC: a room's media goes with the room.
- A publication's blob total stays `MAX_CLIPS_TOTAL` (20 MiB, publication_format.py), images included; the
  design's 32 MiB does not exist in the code (C8). *
- Pasting markdown with an image line from elsewhere keeps the line (the blob may be missing: grey box with
  "Image not available"); copying inside one meeting works as for any line. *
- Obsidian attachments are `<vault>/<folder>/attachments/<note stem>-<sha8>.<ext>`; the cleanup removes only
  files of the CURRENT stem that the note no longer uses, so a renamed meeting leaves its old files. *
- A recipient of a phase-8 share who adds an image uploads it to the owner (`PUT /api/share/media/...`) and
  the outbox sends it before the frames; it is the last task and can be left out of v1. *
- Old clients and old sidecars: `image` is an unknown block value to them, so the line shows as a paragraph
  holding the caption and the `image` mark is kept (marks of unknown kinds are ignored, never dropped). A
  new sidecar renders it again. Nothing is lost; no migration.
- Readers (team reviewer, shared-with-me) may load blobs with read access; uploading needs
  `shared_notes.may_edit` (the notes rule). No new setting; team servers get images with the notes.
- Images are loaded in the desktop/team web app through `fetch` with the connection's headers and shown
  from object URLs (an `<img src>` cannot carry the bearer token); cached per sha per editor session.
- The `/Image` menu item opens a hidden `<input type="file" accept="image/webp,image/jpeg,image/png,
  image/gif,image/bmp,image/svg+xml,image/*" capture="environment">`; `capture` is only honoured by phones
  (a camera option). HEIC the browser cannot decode is refused with a message ("That image format cannot be
  read here"). *
- The host broadcasts `{"type":"media","name":hex}` to a room when a blob lands, so a guest waiting for it
  fetches at once; otherwise a reader retries a missing blob on a timer (10 s, 30 s, 60 s, then on demand).
- Every `notes doc` answer carries `media` (like `tasks`), and `fold` is the only place the backend learns
  what the document references.
- The backend tells open editors about a new blob with a bus event `{"type":"notes_media","session_id",
  "sha256"}` (hand-written WS type), so a grey box fills in without polling.

## Changed while building

- A publication's images are kept in the row's `clip_blobs` under `image:<sha>` (a clip's key is a quote
  span, never that): no new column, no `SyncPublication` change.
- Task 11b (optional) was dropped.
- The sweep folds a meeting once more before deleting its stale blobs, so a meeting nobody opened keeps its images.
- `put_media_row` touches the meeting (a new version for library sync) only for a new row.
- Combining moves the other meeting's images after the notes are joined, before that meeting is deleted.
- Obsidian attachment tags are 8 hex digits of the sha that are not all digits, so `redact_exports` does not
  take them for a number.
- The bridge waits up to 30 s for an upload before it sends a frame that names the image.

## Corrections to the design (from reading the code)

- **C1 notes-core changes reach the wasm.** `Block` lives in notes-core/src/model.rs and
  client.rs's `from_json` uses `Block::parse` (`WireLine.kind`), so adding `Block::Image` (and the `image`
  attribute on `WireLine`) changes `src/lib/notes/pkg`. The wasm32 target and wasm-bindgen are not
  installed here: the CI `notes` job builds it; take its `notes-wasm-pkg` artifact
  (`gh run download <run> -n notes-wasm-pkg -D src/lib/notes/pkg`), commit, then `pnpm build:guest` at the
  close. Vitest (`src/lib/notes/test-harness.ts` reads `pkg/notes_wasm_bg.wasm`) and the e2e harness's
  `window.__notes.markdown()` go through that wasm, so unit tests that parse or render image markdown
  (clipboard.ts, paste) only pass after the artifact loop. Rendering, caret and commands over the marks need
  no wasm change (model.ts reads `markedSpans` generically).
- **C2 "Newest wins" cannot be computed.** doc.rs `read_marks` resolves `lang`/`align` as `first`
  (markedSpans order, lowest mark id) and `resolve_block` takes the first known value; hashseq marks carry
  no time. `image` resolves the same way (first value).
- **C3 Guests have no HTTP.** guest/src has no `fetch` (room.ts talks WebSocket only, the snapshot comes
  over it); link/src/guest.rs `proxy()` accepts ONE `accept_bi()` stream and pumps it to `ws://host/live/
  {room}` (frames `u32 len || tag || payload`, `MAX_PAYLOAD` 8 MiB + 64 KiB, tags TEXT/BINARY only); and
  guest-wasm/src/lib.rs exposes `send`/`recv` of that stream and is built only by CI. So the design's
  `PUT/GET /live/{room}/media/{sha}` cannot be used by a guest. Media travels over the room socket as two
  new binary kinds beside `KIND_FRAME` 0x01 and `KIND_PRESENCE` 0x02 (live.rs): up `0x04 || name(32) ||
  envelope` stores, text `{"type":"get_media","name":hex}` asks, down `0x05 || name(32) || envelope`
  answers, JSON `{"type":"media","name","ok":true}` / `{"type":"error","code":"media_missing"|
  "media_too_large"|"room_media_full"}`. live.rs `live_socket` sets `max_message_size =
  room_frame_max_bytes + 64 KiB` (2 MiB + 1024 + 64 KiB): it must grow to cover
  `room_media_max_bytes + 64 KiB`. The HTTP routes stay for the owner's bridge (httpx, publisher Bearer).
- **C4 No HTTP route checks a room token today** (`check_guest` runs only in the WebSocket join), and the
  ciphertext name is unfindable (random nonces). The room routes use the publisher token (`auth_of`,
  `check_publisher`); the name is the HMAC above; the plaintext sha is in the AAD, so a wrong blob under a
  name fails to open and shows as missing.
- **C5 The embedded host has no write budget.** publish-host `Config::defaults()` sets
  `write_bytes_per_day = 0` and link/src/host.rs `start_loopback_host` starts from it (every guest is
  127.0.0.1 there). The 25 MB room cap is the only limit on this computer; the daily budget bites only on
  deployed hosts (keyed by IP, budget.rs).
- **C6 Deleting a room removes `log` and `snapshot` only** (rooms.rs `write_tombstone`); the directory goes
  at `sweep` after `tombstone_days`. The media folder must go in `write_tombstone` too.
- **C7 Which routes to copy, which not.** The notes write route is `POST /api/sessions/{id}/notes`
  (routes/sessions.py `update_notes`: `repo.exists` then `shared_notes.may_edit`, `Forbidden` otherwise); reads
  use `_require` (`ctx.sessions.get_session`, the repository filter: hidden = 404). The media routes follow
  those. Do NOT copy routes/clips.py `get_clip`: it serves the file when `get_session` returns None (a hidden
  or unknown meeting), which is an existing access gap worth its own fix.
- **C8 Publishing caps and shapes.** publication_format.py has `MAX_CLIP` 20 MiB, `MAX_CLIPS_TOTAL`
  20 MiB, no 32 MiB; `Kind = Literal["manifest","clip"]`, `_BLOCK`/`_CAP` dicts; `Manifest` and `ClipRef`
  are `extra="forbid"`; the viewer's format.ts `aad()` accepts `'manifest' | 'clip'` and load.ts fetches
  `/pub/{id}/{sha}` per clip. Images are a third kind `image` (block 65536, cap `MAX_CLIP`), an `ImageRef`
  list on the manifest, and the viewer renders the notes through src/lib/components/Markdown.svelte
  (marked + DOMPurify): DOMPurify strips a `mnemosyne:` `src`, so the viewer needs a marked image renderer
  that maps `mnemosyne:image/<sha>...` to the decrypted blob URL (C16).
- **C9 The editor's line commands enumerate the terminator marks.** commands.ts `setTerminatorMarks`
  ({kind, lang, align}), `deleteRange` (the surviving terminator takes the FIRST line's marks: deleting an
  image line's text into the next line would make THAT line the image), `splitLine` (mid-line, the old
  terminator with its marks moves down), `joinWithPrevious` (first Backspace clears the kind, keeps the
  text), ops.ts `terminatorMarks`, clipboard.ts `selectionModel.entry` (copies kind/lang/align). All gain
  `image`, and image lines get their own rules: Enter anywhere in the caption inserts a plain line AFTER
  the image (as `splitLine` does for table rows), never splits; Backspace at the caption's start with an
  empty caption removes the whole line without handing the image to a neighbour; with a caption it does
  nothing (the image is not a kind to "clear" into a paragraph); Delete at the end of the line above an
  image line joins nothing.
- **C10 There is no drop handling and paste reads text only.** input.ts `onPaste` reads
  `clipboardData.getData('text/plain')`; `insertFromDrop` is handled as text in `onBeforeInput`; nothing
  listens to `dragover`/`drop`. Files must be looked at first (`clipboardData.files`, `items` of kind
  `file`; `dataTransfer.files` on drop with `dragover` prevented) through a new `InputOptions.onFiles(files,
  from, to): boolean` hook; the text paths stay as they are when it returns false.
- **C11 Four local fold sites and two sync ones.** shared_notes.py `_fold_locked`, `_set_text_blocking`,
  `mark_comment_blocking`, `set_kind_blocking` all call `_derive_items(app, sid, reply, seqs, author)`;
  sync_notes.py `store_union`/`adopt` call their own `_derive_items(log=False)`. The media hook goes beside
  each (`notes_media.seen(app, sid, reply.get("media"))`), and tests/fake_link.py `answer()` must return
  `media` (from `![...](mnemosyne:image/...)` lines) or no backend test sees it.
- **C12 Combine and "delete audio".** combine.py joins the notes and then `delete_session`s the other
  meeting (l.344): its blobs and rows must move into the combined meeting first. storage_service.py
  `delete_audio` and retention remove `recordings/<id>` only; `notes_media/<id>` must survive them and be
  counted in the storage report.
- **C13 Backup and records.** backup.py lists the folders three times (the tar tuple around l.149,
  `_data_members`, the `names` moved aside at restore); `notes_media` joins all three. records.py
  `export_zip` writes `audio/<name>` through `plaintext` and nothing else binary; `media/<sha>.<ext>` joins
  it and README.txt names it. The seal (`content_of`, `audio_hashes`) stays as it is: changing it would
  fail every old seal.
- **C14 Sync has no binary transfer yet.** `SyncAsset` says "file bytes never do"; test_sync_audio.py covers
  playback proxied from the origin (`routes/audio.py _proxy_audio` via `ctx.sync.client_for(peer)`), not a
  copy. `SyncService._client_for` is httpx with `Authorization: Bearer <peer token>`; `/api/sync/*` routes
  are guarded by `_sync_peer`; `_touch` runs inside repository writes, so the media rows are written by a
  repository method that touches (a new blob is a new version of the meeting).
- **C15 Shares.** `SharedMeeting` is a strict model with a schema-walk test (test_shared_meeting.py);
  `media` is a new field, filled only when `parts.notes`, covered by the digest automatically. CONTACT
  tokens are admitted only under `/api/share/` (auth.py `SHARE_PREFIX`), so blob routes go there and check
  a live share with notes for that contact (`contacts.by_pid`, `repo.peer_share`). `CONTRIBUTION_KINDS`
  is a closed tuple; a recipient's image is a new outbox kind, sent as a PUT before its frames.
- **C16 Markdown outside the editor.** Markdown.svelte would render an image line as a broken `<img>`
  (DOMPurify drops the `mnemosyne:` src). `Session.notes` is already read through `plain_notes` by the
  summary hint, MCP and publishing (C11 of phase 11); `plain_notes` must turn an image line into
  `[image: caption]` / `[image]`. notes_chips.py `_rewrite` leaves `![caption](mnemosyne:image/...)`
  alone today (`parse_chip` knows time/person/meeting only), so images get their own whole-line pass
  applied before `_rewrite` in `plain_notes` and `obsidian_notes`.
- **C17 Obsidian has an attachments precedent.** routes/export.py `_resources` copies decrypted asset files
  to `<vault>/<subfolder>/attachments/<id>-<filename>` (only if missing) and links them `[[name|title]]`;
  templates.py renders `## Notes` through `obsidian_notes(_plain_task_lines(...), link_people,
  meeting_note)`; the file is written by vault_sync.py `write_note` (3-way merge of the text outside the
  summary sections). Images follow `_resources`, with `obsidian_notes` taking an `image_file(sha, ext) ->
  str | None` callback.
- **C18 guest_format.** One frames key for every kind, AAD `mnemosyne/guest/1|<room>|<kind>`, `block_for`
  256/4096, `JSON_KINDS`, `_kind` rejects unknown kinds; the TS twin's `checkKind` refuses kinds outside
  1..5 (guest/src/format.ts:145). `MEDIA = 6` needs: an AAD with the plaintext sha
  (`mnemosyne/guest/1|<room>|6|<sha>`), block 65536, raw bytes (not JSON), the HMAC name key, and a new
  sample in backend/tests/vectors/guest_v1.json (additive: `FORMAT` stays 1, old links still open).
- **C19 Room caps are per kind.** rooms.rs `append` checks `room_frame_max_bytes` and `room_log_max_bytes`,
  `put_snapshot` `room_snapshot_max_bytes`; `RoomMeta` has `snapshot_bytes`, `log_bytes` is in
  `RoomState`. Media gets `room_media_max_bytes` (one blob, default 2 MiB + 128 KiB: a 2,000,000-byte
  image padded to 64 KiB plus the envelope) and `room_media_total_bytes` (25,000,000), `RoomMeta.media_bytes`
  persisted in meta.json, files under `<room>/media/<name>`.
- **C20 The guest's saved copy rejects extra fields.** copy.ts `validRec` accepts exactly {envelope,
  expires, ended_at, saved_at} in the IndexedDB store `copies` of db `mnemosyne-guest`; sealed images go in
  a second object store `media` (key `<room>/<name>`, value {envelope, saved_at}; db version 2), removed by
  `forget(room)` and when the copy expires (`expired`).
- **C21 `MAX_FRAME` and `MAX_ENVELOPE` are untouched.** An image adds one 0xAF artifact (~80 bytes) and a
  delta to the notes log; the bytes never enter `shared_notes_ops`, the snapshot, library sync's
  `notes_doc` or the room log. Only the room's media store and the socket's message limit grow.
- **C22 The publish host is deployed separately.** A deployed host without the media routes answers 404 to
  the bridge's PUT: the bridge logs once and goes on (guests see "Image not available"); the embedded host
  (`mnemosyne-link rooms`, link/src/host.rs `router(app)`) gets the routes by the path dependency. The
  viewer's manifest parser must ignore unknown fields (check viewer/src/format.ts; a strict TS parse would
  make an old host's viewer refuse a new manifest).

## Tasks (tier, deps)

Files in brackets are what a task may edit; two tasks that can run at the same time never share a file.
Backend tests one file at a time (`cd backend && uv run pytest tests/<file> -q`); Vitest one file at a time
(`pnpm exec vitest run <file>`); `pnpm check` at 0 errors; ruff (`uv run ruff check` + `format --check`) on
touched files; cargo per crate (`cargo test --manifest-path notes-core/Cargo.toml`, `publish-host/Cargo.toml`,
`cargo check --manifest-path link/Cargo.toml`); full e2e, the wasm builds and the guest/viewer bundles only in
CI or at the close tasks. Never play audio; tests never touch the machine (conftest's fakes stay). docs/
api-reference.md, docs/guest-notes.md, docs/publishing.md and CLAUDE.md change only in task 17.

### M1 the model and the stores (first; everything renders on top of it)

1. **notes-core: the image line** (heavy). [notes-core/src/model.rs, render.rs, parse.rs, doc.rs, api.rs,
   client.rs, lib.rs + their tests] `Block::Image` ("image"; `ALL` becomes 12), `KIND_IMAGE = "image"` (a
   closed mark on the terminator, like `lang`), `LineAttr.image: Option<String>` (the value, validated by
   `IMAGE_VALUE`: invalid values are read as None). `normalize`: `image` kept only on `Image` lines, an
   `Image` line without a valid value becomes a paragraph (kind None), inline formats on an image line are
   don't-care (`dont_care` true for its characters), no `lang`/`align` there; an `Image` line never joins a
   table run. `read_marks`: `KIND_IMAGE => line.image = first` (C2). Render: one line
   `![<caption>](mnemosyne:image/<value>)` with the caption escaped as link text (`inline()` with
   `in_link = true`, formats ignored); `needs_gap`: an image after a list, quote or table line and a list
   or table head after an image get the blank line (a markdown reader would otherwise continue the list
   with it); an empty caption renders `![](...)`. Parse: in `classify`, a `Para` line that is exactly
   `^!\[(.*)\]\((mnemosyne:image/<IMAGE_VALUE>)\)$` (caption unescaped through `parse_inline`, flags dropped)
   becomes an `Image` line; any other `![...](...)` stays literal as today. `set_text`/`reconcile`: mark
   `KIND_IMAGE` where `want.kind == Some(Block::Image)` and the raw value differs (as `lang`), tombstone it
   on other lines. `Notes::media() -> Vec<String>`: the distinct values of live `image` marks on `Image`
   lines in document order; `api.rs answer()` adds `"media": [...]` to every answer; document it in the
   module doc. `tasks()` skips image lines. client.rs: `WireLine.image: Option<String>` (skip when None),
   `to_json`/`from_json` carry it, `from_json` refuses an `Image` line without a valid value
   (`Error("lines[i]: an image line needs an image")`). Tests: render/parse round trip (empty caption,
   caption with `]`, `*`, a chip-like link in the caption stays plain), a literal `![x](https://...)` line
   stays text, `set_text` adds/changes/removes an image, two live values resolve to the first, `media` in
   `fold`/`set_text`/`merge` answers, an unknown `image` value renders as a paragraph. Done when `cargo test`,
   `cargo fmt --check` and `cargo clippy --all-targets -- -D warnings` pass for notes-core and `cargo check
   --manifest-path link/Cargo.toml` passes. Then push and take the CI `notes` job's `notes-wasm-pkg`
   artifact into src/lib/notes/pkg (C1) and commit it; `pnpm check` after that. Note for the implementer:
   the mark value is over 14 bytes, so it travels as a 0xAF artifact (nothing to do: `intern` mints it).

2. **Guest format: MEDIA** (careful). [backend/mnemosyne/services/guest_format.py, guest/src/format.ts,
   backend/scripts/make_guest_vectors.py, backend/tests/vectors/guest_v1.json,
   backend/tests/test_guest_format.py, guest/src/format.test.ts] `Kind.MEDIA = 6` (not in `JSON_KINDS`),
   `MEDIA_BLOCK = 65536`, `aad(room, kind, sha=None)` → for MEDIA `mnemosyne/guest/1|<room>|6|<sha>` (sha =
   64 lowercase hex of the plaintext, required; refused for other kinds), `seal_media(key, room, sha, data)`
   / `unseal_media(key, room, sha, env) -> bytes` which also checks `sha256(bytes) == sha`;
   `media_name(key, sha) -> str` = `hex(HMAC-SHA256(HKDF(key, info=b"mnemosyne/guest/media-name"), sha))`;
   `MEDIA_MAX = 2_000_000` plaintext bytes checked in `seal_media`. TS twin: `Kind.MEDIA`, `checkKind` to
   6, `sealMedia`/`unsealMedia`/`mediaName` with WebCrypto (HMAC key imported from the HKDF output),
   `deriveKeys` gains `mediaNameKey`. Vectors: a `media` sample (kind, aad with the sha, nonce, 1000
   plaintext bytes of a fixed pattern, padded_length 65536, envelope, `name`) added by the script; the
   docstring and format.ts header gain the rules. Done when both test files check the new sample
   byte-for-byte and the Python and TS names agree. Verify: `uv run pytest tests/test_guest_format.py -q`,
   `pnpm exec vitest run guest/src/format.test.ts`, ruff, `pnpm check`.

3. **Publish host: room media** (careful). [publish-host/src/rooms.rs, live.rs, app.rs, config.rs,
   publish-host/tests/rooms.rs, publish-host/tests/api.rs] Config `room_media_max_bytes` (2 MiB + 128 KiB)
   and `room_media_total_bytes` (25,000,000). rooms.rs: `RoomMeta.media_bytes`, `put_media(room, name,
   bytes) -> Result<bool>` (name 64 hex; 413 `media_too_large`, 413 `room_media_full`, 409 `room_ended`;
   an existing name is kept and answers false, never overwritten; `write_atomic` into `<room>/media/<name>`,
   then `meta.media_bytes += len`, saved), `open_media(room, name) -> (File, len)` (404 `media_missing`),
   `write_tombstone` removes `media/` (C6), the sweep's `remove_dir_all` already covers the rest. Routes
   (app.rs `router`): `PUT /live/{room}/media/{name}` (publisher Bearer via `check_publisher`, `read_body`
   with `room_media_max_bytes`, `charge_write`, 201/204) and `GET /live/{room}/media/{name}` (publisher
   Bearer too: guests use the socket; `IMMUTABLE` cache header, octet-stream). live.rs: `KIND_MEDIA_PUT =
   0x04` up (`name(32) || envelope`; `charge_write`; `put_media`; answer `{"type":"media","name","ok":true}`
   to the sender; broadcast `{"type":"media","name"}` to the room's other sockets through a new
   `Event::Media`), text `{"type":"get_media","name"}` → `0x05 || name(32) || envelope` or `{"type":"error",
   "code":"media_missing"}` (a per-connection cap of 20 gets in flight, excess answered `media_busy`);
   `live_socket`'s `max_message_size` = max(frame, media) + 64 KiB (C3, C19). Protocol doc at the top of
   live.rs updated. Tests (tests/rooms.rs `host_with`): put over HTTP, get over the socket (both transports
   of the same bytes), a second put of the same name is 204 and does not count twice, the total cap refuses
   and the per-blob cap refuses with the right codes, delete removes the folder, a socket put is broadcast
   to the other guest, the budget counts a socket put (`the_write_budget_covers_frames_and_snapshots...`
   pattern). Done when `cargo test --manifest-path publish-host/Cargo.toml` passes, `cargo fmt --check`,
   clippy, and `cargo check --manifest-path link/Cargo.toml` (the embedded host compiles the new router).

4. **Backend media store, routes, fold hook, GC** (heavy; 1 for the real sidecar, else the fake). [new
   backend/mnemosyne/services/notes_media.py, new backend/mnemosyne/api/routes/notes_media.py (+ its
   `include_router` in api/app.py, prefix `/api/sessions`), backend/mnemosyne/storage/sqlite.py (table
   `notes_media(session_id, sha256, ext, width, height, size, created_at, last_seen_at, by, PRIMARY
   KEY(session_id, sha256))` + repository methods `put_media_row`, `media_rows(session_id)`,
   `touch_media(session_id, shas, at)`, `delete_media_rows`, `media_total(session_id)`; `put_media_row`
   `_touch`es), backend/mnemosyne/services/shared_notes.py (one line beside each of the four
   `_derive_items` calls: `notes_media.seen(app, session_id, reply.get("media"))`),
   backend/mnemosyne/services/sync_notes.py (same beside its `_derive_items`), backend/mnemosyne/config.py
   (`notes_media_dir` property = `data_dir / "notes_media"`), backend/mnemosyne/api/context.py (start the
   sweep with the other periodic work), src/lib/types/index.ts (the `notes_media` WS event),
   backend/tests/fake_link.py (`media` in `answer()`: values of `![...](mnemosyne:image/<v>)` lines, in
   order, distinct; the markdown round trip keeps such lines), new backend/tests/test_notes_media.py]
   notes_media.py: `IMAGE_VALUE` regex, `parse_value(v) -> (sha, ext, w, h) | None`, `IMAGE_MAX_BYTES`,
   `MEETING_MEDIA_MAX_BYTES`, `sniff(data) -> "webp" | "jpeg" | None` (RIFF....WEBP / FF D8 FF),
   `store(app, session_id, sha, data, *, by) -> MediaInfo` (checks sha, type, caps, then writes
   `<notes_media>/<sid>/<sha>.<ext>` through a `.part`, seals with `storage.crypto.encrypt_file` when
   `encrypt_at_rest and file_key`, as services/assets.py `store_file` does; existing = idempotent; writes
   the row; publishes the bus event), `path(app, session_id, sha) -> Path | None` (plain or `.enc`),
   `read(app, session_id, sha) -> bytes` (through `plaintext`), `seen(app, session_id, media)` (the fold
   hook: `touch_media` for the parsed values; never raises; also calls `on_missing` hooks, see tasks 9/10/11:
   a list `MISSING_HOOKS` the other services append to), `missing(app, session_id) -> list[str]` (values
   with a row or a reference but no file), `sweep(app, now)` (`last_seen_at` is set at store time too, so
   the rule is `last_seen_at < now - 7 days`; delete file + row; only when `shared_notes.available()`),
   `remove_session(app, session_id)` (rmtree), `move_session(app, src, dst)` (for combine, task 5),
   `total_bytes(app, session_id)`. Routes: `PUT /api/sessions/{id}/notes/media/{sha}` (raw body, at most
   `IMAGE_MAX_BYTES` read, `Content-Type` image/webp or image/jpeg must match the sniffed type; `repo.exists`
   else 404; `shared_notes.may_edit` else `Forbidden`; a `shared_from` meeting → 409 "Images of a meeting
   shared with you go to its owner" until task 11b; answers `{"sha256","ext","size"}` 201, 200 when it
   was there) and `GET /api/sessions/{id}/notes/media/{sha}` (`_require`-style read access; 404 when the
   bytes are not here (yet); 423 when locked; `Cache-Control: private, max-age=31536000, immutable`,
   `Content-Type` by ext, `Content-Length`; bytes read in a thread). `pnpm gen:api` after. Tests: upload
   checks (bad sha 400, wrong magic 415, over 2 MB 413, meeting over 50 MB 413, reader 403, hidden
   meeting 404), sealed storage with `encrypt_at_rest` (file is `.enc`, GET decrypts), the fold hook
   touches rows (fake sidecar), sweep deletes an unseen row after 7 days and keeps a seen one, a row of an
   image the document still shows is never deleted, the WS event. Verify: `uv run pytest
   tests/test_notes_media.py -q`, `uv run pytest tests/test_shared_notes.py -q` (unchanged behaviour), ruff,
   `pnpm gen:api`, `pnpm check`.

5. **Media lifecycle: delete, backup, records, encryption, storage report, combine** (careful; 4).
   [backend/mnemosyne/services/session_service.py (`delete_session`: `notes_media.remove_session` beside the
   `rmtree` of recordings), services/backup.py (the three places, C13), services/records.py (`export_zip`:
   `media/<sha>.<ext>` per meeting through `notes_media.read`, README line), services/encryption.py
   (`_convert_audio` or a sibling walks `notes_media/<id>` for enable/disable), services/storage_service.py
   (`report` adds the meeting's media bytes to its size and a `notes_media_bytes` total; `delete_audio` and
   retention leave the folder), services/combine.py (`notes_media.move_session(other, target)` before the
   notes join and the delete; rows re-keyed), tests: test_backup.py, test_records.py, test_encryption.py,
   test_storage_retention.py, test_combine.py, test_sessions_api.py (deletion)] Done when each test file
   shows the folder travelling, moving or going with the meeting. Verify: those files one at a time, ruff.

### M2 the editor (after 1's artifact loop for the markdown tests; 6a can start before it)

6a. **Model, DOM and commands for image lines** (heavy; 1 for the clipboard tests only). [src/lib/notes/
   model.ts, editor/dom.ts, editor/points.ts (only if `isChrome`/extraction needs it), commands.ts, ops.ts,
   editor/clipboard.ts, editor/editor.css, new src/lib/notes/images.ts + images.test.ts, model.test.ts,
   editor/dom.test.ts, commands.test.ts] images.ts (pure): `IMAGE = 'image'` mark kind, `imageValue({sha,
   ext, w, h})`, `parseImageValue(v)` (the regex), `imageUrl(v)` = `mnemosyne:image/<v>`, `parseImageUrl`.
   model.ts: `'image'` in `LINE_KINDS`, `IMAGE` mark constant, `Line.image: string | null` (the first live
   value, C2; `null` makes the line a paragraph in `lines()` so an invalid mark never renders a block),
   `lineSig` includes it. dom.ts `buildLine` for `kind === 'image'`: a chrome island first (`<figure
   class="ne-image" contenteditable="false" data-chrome="1" data-image="<v>" data-sha="<sha>">` holding
   `<img alt="" width=w height=h>` without `src` (the host fills it: `data-state` loading | ready | missing |
   uploading | failed, a `<span class="ne-image-note">` for the state text and Retry/Remove buttons), then
   the caption's runs as today (`data-placeholder="Add a caption"` on the line, shown when the caption is
   empty, via CSS `::after` on `.ne-image-line:has(br[data-sentinel])`). The caret therefore lives in the
   caption only; the island contributes no text (`isChrome`). commands.ts: `setTerminatorMarks` and
   `deleteRange`/`splitLine`/`joinWithPrevious`/`setLineKind` know `image` (C9): `insertImageLine(doc, at,
   value): CommandResult` (an empty host line is turned into the image line; else a new line after the
   caret's line (or before, at a line start), kind `image` + `IMAGE` mark, caret in the empty caption; one
   inverse list), `removeImageLine(doc, line)` (the whole line including its terminator, marks cleared
   first so no neighbour inherits them; the document stays terminated), Enter in an image line → plain line
   after it, `setLineKind(..., kind)` over an image line clears the `IMAGE` mark (turning it into text keeps
   the caption), `toggleTask` never on image lines. ops.ts `terminatorMarks` gains `image`; clipboard.ts
   `selectionModel.entry` copies `image`, `parsePaste` keeps image lines (a `block` plan). CSS: the block
   (max-width 100%, max-height 60vh, rounded, 1px border, the grey box uses the width/height attributes
   with `aspect-ratio`), the states. Tests: lines() with an image mark (valid, invalid), DOM shape and
   signature changes when the value changes, insert/remove/Enter/Backspace/setLineKind rules, deleteRange
   across an image line never moves the image, selectionModel round trip (after the wasm lands).
   Verify: `pnpm exec vitest run src/lib/notes/images.test.ts src/lib/notes/model.test.ts
   src/lib/notes/editor/dom.test.ts src/lib/notes/commands.test.ts`, `pnpm check`.

6b. **Input hooks, the media provider, states, lightbox, menu** (heavy; 6a, 1). [src/lib/notes/editor/
   input.ts + input.test.ts, src/lib/notes/ui/types.ts, ui/NotesEditor.svelte, ui/slash.ts + slash.test.ts,
   ui/SlashMenu.svelte, new ui/Lightbox.svelte, new src/lib/notes/prepareImage.ts + prepareImage.test.ts,
   src/lib/notes/editor/harness.ts, src/routes/dev/notes-editor/+page.svelte] types.ts: `MediaProvider {
   url(sha: string): string | null (an object URL when the bytes are here, null = ask later);
   upload(image: PreparedImage): Promise<void>; subscribe(fn: (sha: string) => void): () => void (bytes
   arrived or an upload changed state); state?(sha): 'uploading' | 'failed' | null; pick?(): Promise<File |
   null> (optional: a host-provided picker) }`, `NotesHost.media?: MediaProvider`. prepareImage.ts:
   `prepareImage(file: Blob): Promise<PreparedImage {bytes: Uint8Array, ext: 'webp' | 'jpeg', w, h, sha}>`:
   `createImageBitmap(file, {imageOrientation: 'from-image'})` (fallback: an `<img>` on an object URL, for
   SVG and engines without the option), downscale to 1600 px on the long edge on a canvas, `toBlob('image/
   webp', 0.82)`; when the result's `type` is not `image/webp` (Safari, maybe WebKitGTK) encode `image/jpeg`
   0.85; refuse over `IMAGE_MAX_BYTES` ("That image is too large even after shrinking") and undecodable
   files ("That image format cannot be read here"); EXIF/GPS cannot survive a canvas re-encode (test:
   the output starts with the WebP/JPEG magic and is smaller than a limit); sha256 with
   `crypto.subtle.digest`. input.ts: `InputOptions.onFiles?(files: File[], from: number, to: number):
   boolean` called from `onPaste` (when `clipboardData.files.length`) and from new `dragover`
   (preventDefault when files are dragged) / `drop` listeners (C10) before any text handling; `undo`
   entries as for a paste. NotesEditor: `host.media` present → `onFiles` filters `image/*`, runs
   `prepareImage` per file (sequentially, a `say` per refusal), inserts with `insertImageLine` through
   `input.command`, then `media.upload(prepared)` (never blocking the insert); a `MutationObserver` +
   `media.subscribe` fill `img.src` from `media.url(sha)` and set `data-state`; Retry calls `upload` again
   (the bytes are kept in a per-sha map until success), Remove runs `removeImageLine`; a click on the
   image, or Enter/Space on the focused island, opens `Lightbox.svelte` (the same object URL, full size, Escape/click closes, focus returned);
   `/Image` in slash.ts `BLOCKS` (key `image`, aliases picture, photo, screenshot, upload, camera) shown only
   when `host.media` exists and the line is text, acting as `{kind: 'image'}` → `media.pick?.()` or the
   editor's hidden `<input type=file accept="image/*" capture="environment">`; the Insert button's menu
   gets it too. Read-only editors fill images but accept no files. The dev harness gets a fake provider
   (object URLs from the prepared bytes, a toggle to fail uploads) so e2e can drive it. Tests: input.test.ts
   (files on paste call `onFiles` and skip the text path; a drop with files; a text paste still works),
   slash.test.ts (item present only with media), prepareImage.test.ts with a stubbed canvas (`toBlob` type
   fallback, the size refusal, the sha). Verify: `pnpm exec vitest run src/lib/notes/editor/input.test.ts
   src/lib/notes/ui/slash.test.ts src/lib/notes/prepareImage.test.ts`, `pnpm check`.

7. **Desktop provider** (medium; 4, 6b). [new src/lib/app/notesMedia.ts + notesMedia.test.ts,
   src/lib/components/SharedNotes.svelte, src/lib/api/backend.ts, src/lib/stores/notes.svelte.ts (only to
   route the `notes_media` event), src/lib/app/controller.svelte.ts (only if the WS event needs a hook
   there)] `createNotesMedia(sessionId)`: `url(sha)` returns a cached object URL or starts a `fetch(GET
   .../notes/media/<sha>, {headers: connectionState.headers()})`, null now and `subscribe` fires when it
   lands; 404 → retry on the `notes_media` WS event and on the timer (10/30/60 s, then stop until a new
   event); `upload` PUTs the bytes (`Content-Type` by ext) and records `uploading`/`failed` states;
   object URLs revoked when the editor unmounts (SharedNotes's cleanup). SharedNotes: `get media()` on the
   host (undefined for `shared_from` meetings until 11b, so the menu item hides and paste says "Images
   cannot be added to a meeting shared with you yet"). Done when a screenshot pasted on the desktop shows,
   survives a reload, and a second window of the same meeting shows it. Verify: `pnpm exec vitest run
   src/lib/app/notesMedia.test.ts`, `pnpm check`.

### M3 guests (after 2, 3, 6b; 8 and 9 in parallel, their test files are disjoint)

8. **Guest page media** (careful; 2, 3, 6b). [guest/src/room.ts + room.test.ts, guest/src/doc.ts +
   doc.test.ts, guest/src/copy.ts + copy.test.ts, guest/src/Notes.svelte, new guest/src/media.ts +
   media.test.ts, guest/src/iroh.ts (only if binary frames need a size hint), guest/src/App.svelte (pass the
   provider)] room.ts: `putMedia(name, env): Promise<void>` (0x04; resolves on `{"type":"media","ok"}`,
   rejects on the error codes; queued like frames while not joined, sent once per transport),
   `getMedia(name): Promise<Uint8Array>` (text `get_media`; resolves on the matching 0x05; rejects
   `media_missing`), `onMedia(name)` for the host's broadcast. media.ts: `GuestMedia implements
   MediaProvider` over GuestDoc: `upload` = `sealMedia` + `putMedia(mediaName(sha))` (kept in memory until
   acknowledged; `failed` on rejection); `url(sha)` = object URL from the IndexedDB `media` store, else
   `getMedia` → `unsealMedia` (checks the sha) → store (C20) → object URL; a `media_missing` answer waits for
   the broadcast or the timer. copy.ts: the `media` object store, `putMedia(room, name, env)`,
   `getMedia(room, name)`, removed in `forget(room)` and with an expired copy; `MemoryCopyStore` twin.
   Notes.svelte: `media` on the host; the `/Image` picker on a phone offers the camera (the shared input's
   `capture`). Done when doc.test.ts (in-memory host) shows a guest's image reaching another guest through
   the fake room and a reload reading it from the copy store. Verify: `pnpm exec vitest run
   guest/src/room.test.ts guest/src/doc.test.ts guest/src/copy.test.ts guest/src/media.test.ts`, `pnpm check`,
   `pnpm build:guest` builds (do not commit the bundle here).

9. **Bridge: blobs both ways** (heavy; 2, 4). [backend/mnemosyne/services/guest_notes.py,
   backend/tests/fake_guest_room.py, backend/tests/test_guest_notes.py, backend/tests/test_guest_notes_routes.py
   (only if a route changes)] Owner → guests: `notes_media.store` (task 4) publishes; the bridge registry
   listens (a `notes_media` bus subscription, or `notes_media.ON_STORED` hooks) and `Bridge.upload_media(sha)`
   seals (`gf.seal_media`) and `PUT /live/{room}/media/{name}` with the publisher token (`_request`,
   `Link.host()`), once per room (primary and mirror), skipping 204; at bridge start (and after a reload)
   every blob of the meeting is uploaded once per bridge run (an in-memory `media_uploaded` set; the room
   answers 204 for a name it has, so re-uploading after a restart is harmless). A 404
   from an old host is logged once per room (C22). Guests → owner: `notes_media.MISSING_HOOKS` gets
   `guest_notes.fetch_missing(app, session_id, values)`: for each live bridge, `GET /live/{room}/media/
   {name}`, `gf.unseal_media` (sha checked), `notes_media.store(..., by="guest")` (the caps apply: a refused
   blob is logged and the line shows as missing); a fast path scans accepted 0xAF frames for `IMAGE_VALUE`
   substrings and fetches at once (optional, same function). fake_guest_room: the two media routes and the
   socket's 0x04/0x05/get_media. Tests: a desktop upload reaches both fake rooms sealed under the HMAC name
   and opens with the key; a guest's upload + frame makes the owner store the file after the fold (fake
   sidecar `media`); a wrong sha under a name is refused; caps; an old room (404) does not stop the bridge.
   Verify: `uv run pytest tests/test_guest_notes.py -q`, ruff.

### M4 travelling (after 4; 10 and 11 in parallel; 11b last and optional)

10. **Library sync** (careful; 4). [backend/mnemosyne/models/sync_session.py (`SyncMedia {sha256, ext,
    width, height, size, created_at, by}`, `SyncSession.media: list[SyncMedia] | None = None`),
    services/sync_state.py (export the rows), storage/sqlite.py `apply_synced` (upsert rows without files,
    `last_seen_at` = now), services/sync_merge.py (union by sha), services/sync_service.py (after
    `_take_page`: pull `GET /api/sync/media/{session}/{sha}` for rows without a file from the peer that
    served the page, `notes_media.store`; a `MISSING_HOOKS` entry asks the meeting's origin peer later),
    api/routes/sync.py (`GET /api/sync/media/{session_id}/{sha}`: `_sync_peer`, bytes through
    `notes_media.read`, 404 when absent), tests/test_sync_service.py or a new tests/test_sync_media.py]
    Older peers send no `media`: ours are kept. Done when a two-app test moves an image from A to B
    (bytes equal, sealed on B when B encrypts), a blob added on B travels back, and deletion tombstones the
    folder. Verify: the test file, `uv run pytest tests/test_sync_state.py -q`, ruff.

11a. **Phase-8 shares: owner → recipient** (careful; 4). [backend/mnemosyne/models/shared_meeting.py
    (`SharedMeeting.media: list[SharedMedia]`, only with `parts.notes`), services/shares.py
    (`build_shared_meeting` fills it), api/routes/share.py (`GET /api/share/media/{session_id}/{sha}`:
    `_contact`, a live share with notes for that contact, `notes_media.read`), services/share_pull.py
    (after `_pages`: fetch missing blobs with the same client, `notes_media.store`; a `MISSING_HOOKS` entry
    for later), tests/test_shared_meeting.py (the schema walk), tests/test_share_pull.py] Verify: those
    two files, ruff.

11b. **Phase-8 shares: recipient → owner** (careful; 11a, 7). * [backend/mnemosyne/services/share_push.py
    (outbox kind `media`: `PUT /api/share/media/{session_id}/{sha}` with the bytes before the frames
    batch that follows it), api/routes/share.py (the PUT: `_contact`, the share lets the contact edit notes,
    `notes_media.store(by=contact)`), api/routes/notes_media.py (a `shared_from` meeting stores locally
    and enqueues instead of 409), src/lib/components/SharedNotes.svelte (the provider also for
    `shared_from`), tests/test_share_push.py, tests/test_share_contributions.py] Verify: those files, ruff,
    `pnpm check`.

### M5 export and publishing (after 4; 12 then 13a then 13b)

12. **plain_notes, Obsidian attachments** (careful; 4). [backend/mnemosyne/services/notes_chips.py
    (`IMAGE_LINE` regex, `image_lines(md)`, `plain_notes`: `[image: caption]` / `[image]`;
    `obsidian_notes(..., image_file=None)`: `![[<file>|caption]]` when the callback gives a file, else the
    plain form), export/templates.py (passes `image_file`), api/routes/export.py (`_images(ctx, session,
    exporter)`: writes `attachments/<note stem>-<sha8>.<ext>` through `notes_media.read` when missing or
    different in size, removes `attachments/<note stem>-[0-9a-f]{8}.(webp|jpeg)` files the note no longer
    uses, returns the callback), tests/test_notes_chips.py, tests/test_export.py, tests/test_mcp_server.py
    (plain), tests/test_publishing.py only if a plain_notes assertion changes] Also grep
    `src/lib/components` for any `Markdown text={...notes` of raw notes (C16) and route it through the
    API's plain form if one exists (report it; do not widen). Verify: those test files one at a time, ruff.

13a. **Publishing: images as blobs** (careful; 4, 12; after 10 for models/sync_session.py).
    [backend/mnemosyne/services/publication_format.py (`Kind` += "image", `IMAGE_BLOCK = 65536`, cap
    `MAX_CLIP`, `ImageRef {id: plain sha, sha256: blob, size, mime, v, width, height}`, `Manifest.images:
    list[ImageRef] = []`, `seal_image`/`open_image`, `check_clips_total` covers images),
    models/publication.py (`PublicationSelection.images: list[str] | None = None` = plain shas to include,
    None = all; `PublicationDefaults.images: [{sha256, caption, width, height, size}]`),
    services/publishing.py (`build_content`: the notes text keeps `![caption](mnemosyne:image/<v>)` for
    included images and `[image: caption]` for the others via notes_chips; `publish()`: `image_blobs` on the
    row like `clip_blobs`, reuse/fetch/seal, counted in the total; defaults list the meeting's images),
    services/publication_file.py (`images: {sha: b64url}` in the payload), services/sync_publications.py +
    models/sync_session.py `SyncPublication.image_blobs` (only that class, task 10 owns the rest of the
    file), api/routes/publishing.py (blobs by sha already generic: check `meta["blobs"]` includes images),
    backend/scripts/make_publication_vectors.py + tests/vectors/publication_v1.json (an `image` sample,
    additive), tests/test_publication_format.py, tests/test_publishing.py, tests/test_publishing_file.py,
    tests/test_sync_publications.py] The manifest-has-no-transcript test stays. `pnpm gen:api`. Verify:
    those files one at a time, ruff, `pnpm check`.

13b. **Viewer and publish dialog** (careful; 13a). [viewer/src/format.ts + format.test.ts (`aad` kind
    'image', `openImage`, the vector), viewer/src/load.ts + load.test.ts (an `ImageLoader` like `ClipLoader`,
    file mode from `images`), viewer/src/Meeting.svelte + Meeting.test.ts (notes rendered with a marked
    image renderer: `mnemosyne:image/<v>` → `<img src=blob: alt=caption width height>` once decrypted, a grey
    box before, `[image: caption]` when the ref is absent; the renderer lives in viewer/src, not in
    Markdown.svelte), viewer/src/viewer.css, src/lib/components/PublishDialog.svelte + the publish store it
    uses (a checkbox per image with its thumbnail from the desktop provider's URL and caption, all on;
    `images` in the selection), src/lib/components/PublicationsList.svelte only if it lists blob counts]
    Check that viewer/src/format.ts's manifest parse ignores unknown fields (C22). Verify: `pnpm exec vitest
    run viewer/src/format.test.ts viewer/src/load.test.ts viewer/src/Meeting.test.ts`, `pnpm check`,
    `pnpm build:viewer` builds (commit the bundle in task 17 only).

### M6 proof and close

14. **e2e** (careful; 7, 8, 9, 12, 13b). [tests-e2e/notes-editor.spec.ts, tests-e2e/shared-notes.spec.ts,
    tests-e2e/guest-notes-iroh.spec.ts, tests-e2e/publish.spec.ts, tests-e2e/fixtures.ts] Harness: a PNG
    drawn on a canvas pasted through a `DataTransfer` with a File (the spec already builds DataTransfers),
    Chromium and WebKit: the image block appears, the markdown has the image line, Backspace on the empty
    caption removes it and undo restores it, the lightbox opens and closes. Demo backend: paste in window A,
    window B shows it (object URL loaded, `data-state=ready`); a reload shows it. Guest (iroh spec): the
    owner's image reaches the guest; a guest's image (pasted in the guest page) reaches the owner within the
    fold delay. Publish spec: an image listed with its checkbox, the viewer shows it, unticked shows
    "[image: ...]". Done when it passes in CI (the e2e job builds link, runs the relay and the publish
    host).

15. **Guest-wasm check** (light; 8). Nothing in guest-wasm/ changes (the media messages are ordinary binary
    frames under `MAX_PAYLOAD`); confirm by reading `guest/src/iroh.ts` with task 8 and leave guest/pkg as
    is. If a frame-size change ever becomes necessary, both frame.rs copies move together and guest/pkg
    comes from the CI artifact (CLAUDE.md).

16. **Docs for the API contract** (light; 4, 9, 10, 11a, 13a). Workers report their shapes; this task writes
    them: [docs/api-reference.md (the media routes, the `notes_media` event, `media` in sync and share,
    `images` in publications), docs/guest-notes.md (the MEDIA kind, the socket messages, the room caps),
    docs/publishing.md (images, the per-image choice, the total cap), docs/plans/2026-10-26-images-in-notes.md
    (status line + the corrections that change it: C2, C3, C4, C8), CLAUDE.md (one roadmap paragraph)].

17. **Close: bundles and commit** (light; 14, 16). [src/lib/notes/pkg (from CI, if not already committed in
    task 1), backend/mnemosyne/api/static/guest/ (`pnpm build:guest`), backend/mnemosyne/api/static/viewer/
    (`pnpm build:viewer`)] Rebuild both bundles from the final sources and commit them with the milestone.
    Never hand-merge a bundle: after a rebase, run the command again. Push and watch CI (the `notes`,
    `guest-wasm`, `viewer`, `publish-host`, `link` and e2e jobs all matter here).

## Files shared across tasks (sequence, never parallel)

- storage/sqlite.py: 4, then 10 (`apply_synced`, sync rows), then 13a (publication row). shared_notes.py: 4
  only. guest_notes.py: 9 only. notes_media.py: 4 writes it, 5/9/10/11 only call it (new hooks go through
  `MISSING_HOOKS`/`ON_STORED` lists defined in 4). models/sync_session.py: 10 then 13a. routes/share.py: 11a
  then 11b. share_push.py: 11b only. notes_chips.py: 12 only; templates.py: 12 only; publishing.py: 13a only.
- src/lib/notes: model.ts/dom.ts/commands.ts/ops.ts/clipboard.ts/editor.css in 6a; input.ts, ui/* in 6b;
  src/lib/app/notesMedia.ts and SharedNotes.svelte in 7 (11b touches SharedNotes after 7). guest/src: format.ts
  in 2, the rest in 8. viewer/src: 13b only. PublishDialog.svelte: 13b only.
- Generated: src/lib/api/schema.d.ts (4, 13a: `pnpm gen:api`), src/lib/notes/pkg (CI artifact, task 1),
  static/guest and static/viewer (17 only).
- Vectors: guest_v1.json in 2, publication_v1.json in 13a (both additive samples, `FORMAT` unchanged).

## Order that keeps main releasable

1 → (2, 3, 4) → 5 → 6a → 6b → 7: at every step the app works; before 7 nobody can insert an image (the
menu item hides without a provider) but any image line that arrives (sync, an older branch) renders. 8/9 add
guests, 10/11 travel, 12/13 export and publish, 14 proves it, 17 ships the bundles. Old apps meeting new
documents see paragraphs (decided above); old backends/sidecars keep the marks.

## Security points (checked in the tasks above)

- Type by magic number on every receive path: the PUT route (4), the bridge's fetch from a room (9), the sync
  and share pulls (10, 11a, 11b), never by `Content-Type` alone; WebP or JPEG only, so SVG (scripts) and GIF
  never reach the store: the browser converts them first (6b) and the server refuses anything else.
- The sha is checked on every receive path (`sha256(bytes) == name`), and on guests' side `unseal_media`
  checks it (the AAD binds the sha, so a swapped blob fails to open).
- EXIF/GPS are gone because the bytes are a canvas re-encode (6b); the server does not parse EXIF.
- dom.ts gives `img.src` only object URLs the provider made (never a URL from the document); the `image`
  mark value is validated by the regex before anything uses it (model.ts, notes_media.py, notes-core).
- Caps are enforced server-side (4: per image and per meeting; 3: per blob and per room; 13a: the publication
  total), in the browser only as early messages.
- Room media over HTTP needs the publisher token; over the socket it needs the joined room token (the
  socket's join), and the daily budget on deployed hosts; the HMAC name hides the plaintext hash from the
  host.
- The media routes use the notes' own access rules (`_require` to read, `may_edit` to write, C7); hidden
  meetings 404.
- Out of scope but found: routes/clips.py `get_clip` serves a clip when `get_session` is None (hidden or
  unknown meeting). Report it for a separate fix; do not copy it.

## Risks

- WebKitGTK (the desktop window) may not encode WebP on a canvas; the JPEG fallback covers it, but the
  quality/size trade-off is untested there until task 7 runs on Corey's machine.
- `createImageBitmap` of an SVG without intrinsic size yields 0×0 in Chromium; the `<img>` fallback plus an
  explicit refusal message handle it. HEIC from iPhones is undecodable in Chromium/WebKitGTK; Safari/iOS
  decodes it, so a phone guest on Safari works, a Chrome-on-Android guest gets JPEG from the camera anyway.
- The wasm artifact loop (C1) costs a CI round trip before the clipboard/paste tests and the e2e harness's
  `markdown()` can see image lines; schedule 6a/6b after task 1's artifact is committed.
- A guest page that is older than the host (GitHub Pages serves the latest, so only a cached page) sends no
  media messages and shows paragraphs; harmless.
- Playwright's WebKit may not deliver `clipboardData.files` from a synthetic ClipboardEvent; the e2e falls
  back to `drop` for WebKit if so (both paths go through `onFiles`).
- Publishing's 20 MiB total is shared by clips and images; a meeting with many screenshots may need the
  dialog's checkboxes (shown with sizes) to fit.
- The GC's 7-day rule relies on folds happening (an open editor or any write); a meeting nobody opens keeps
  its unreferenced blobs until the next fold touches it, which only the sweep's own fold could force (not
  done: the sweep never folds).

## Open questions for Corey

Decided by default (2026-11-04), listed so Corey can overrule:
- Caps are decimal bytes (2,000,000 / 50,000,000 / 25,000,000).
- A publication keeps the existing 20 MiB total for clips and images together (the design's 32 MiB was a
  mistake in the design; raising it means a format constant on both sides and on deployed hosts).
- Recipients of a phase-8 share adding images (11b) is not in v1: they see the owner's images, the menu
  item is hidden for them.
- The image line is inserted at once with its hash; other readers see a grey box until the bytes arrive.
