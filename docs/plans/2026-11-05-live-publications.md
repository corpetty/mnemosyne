# Live publications: design and tasks

Status: built 2026-11-05 on branch live-publications (PR #17)

Cut from main at 2b30a8d = Release 0.29.0. Phase 7 (publishing), phases 9 and 10 (guest notes and their rooms), "Our servers",
images in the notes and the summary in the notes (docs/plans/2026-11-04-summaries-in-notes.md) are the
surroundings. Every statement below about the code was checked against it; the files and functions are named.

The request (Corey, 2026-11-05), from the hashseq plan's "Later" list ("Publishing: later, readers following
live edits or commenting (a hashseq space)"): a reader holding a publication link sees the meeting update as
the owner changes it, without reloading and without a new link, and can comment; the comments come back to the
owner. Plan first, build after review.

## The design in one paragraph

A publication stays what phase 7 made it: a curated, redacted, encrypted SNAPSHOT per version behind
`https://<host>/p/<id>#<key>`; nothing here hands readers the live notes document or any hashseq history
(the "Later" line's "a hashseq space" is the one part of the request this plan declines, C1). Live means two
things on top of that. (1) **Versions are pushed, not fetched**: a hosted publication gets a guest-notes room
beside it on the same publish host (publish-host/src/rooms.rs, unchanged), whose id the host hands out with the
manifest (`X-Pub-Room`, the one host change); the owner's backend keeps a small bridge in that room and posts a
sealed `VERSION {version, sha256}` frame after every publish, and the viewer, joined with a token derived from
the link's key, re-fetches `/pub/<id>`, checks the ETag against the frame, opens the new manifest and swaps it
in place; everywhere a room cannot exist (a "this computer" publication, an older host, the single-file export)
the viewer polls the manifest's ETag once a minute instead, which is also how old viewers and old hosts degrade.
(2) **The owner's backend republishes by itself** when a publication is marked "keep up to date" (`follow` in
its selection): a watcher on the library-sync change log (`repo.changes_since`) debounces changes to meetings
with such a publication and submits the ordinary `publish` job with the STORED selection, so redaction, names,
the attendee list, the frozen image list and the quote spans are re-applied by exactly the code a manual
Update runs; nothing a reader gets can exceed the selection, and a test proves it. Comments ride the same room:
a reader, after typing a name, seals `COMMENT` frames the bridge opens and writes through
`services/comments.py` with an explicit actor `reader:<rid>` (as guests are `guest:<gid>`), anchored on the
blocks the manifest names (decision, question, action item ids; a quote's transcript lines; the summary text
by quote); the bridge seals the readers' threads (and the owner's replies in them, redacted like the manifest)
back as the room's snapshot, so every reader sees the same threads, and the owner's own threads never leave the
app. Because every reader holds the room's frames key, the room alone proves nothing about who sent a frame
(C16, C17): each publication therefore has an owner key pair, Ed25519 to sign and X25519 to receive, whose
public halves ride in the manifest (`live: {sign, box}`, sealed under the link key and uploadable only with
the publisher token) and whose private halves sit on the publications row beside key and token; every
owner→reader frame (VERSION, THREADS, META) is signed and the viewer drops what does not verify against the
manifest it fetched, and every reader→owner COMMENT op is sealed to the owner's X25519 key and signed with a
per-reader Ed25519 key kept in the reader's browser (`rid` = a hash of its public key), so other readers can
neither read it from the room's log nor edit or delete under its id, and THREADS is the only way readers see
comments. The host still sees only ciphertext and ids.

## Decided while planning (safe defaults; * = Corey may want to revisit)

- Two new fields on `PublicationSelection` (models/publication.py): `follow: bool = False` ("Keep the link up
  to date") and `comments: bool = False` ("Readers may comment"). The model defaults are off so every stored
  row, synced row and old client parses as today; the Publish dialog preselects BOTH on for a publication that
  goes to a host, off for "this computer" (no room there in v1, below). *
- Auto-update cadence: a change counts once the meeting has been quiet for 10 s, at most one automatic
  version every 30 s per meeting, never while a `publish` job for it is active. Constants in
  services/live_publications.py, not settings. During a recording with the copilot writing into the notes this
  is at most two versions a minute, a 4-8 KiB manifest each: ~1 MiB an hour of the host's 200 MiB/day budget. *
- What counts as a change: the same question `behind` answers today (publishing.py `is_behind` l.802: the
  digest of `build_content` differs from `published_digest`), so the watcher never publishes an identical
  version. The watcher is woken by the change log every write already feeds (`_touch`, sqlite.py l.4502), not
  by a new hook (C6).
- Automatic versions are history lines (`publication_auto_updated`, with `version`), never record seals. The
  seal stays on the person's acts: Publish, Update (which is also where `follow`/`comments` are turned on or
  off: they are selection fields) and Unpublish, as today (`SEAL_PUBLISHED`/`SEAL_UPDATED`/`SEAL_UNPUBLISHED`,
  publishing.py l.65). `records.publications_record` lists the automatic versions from history with
  `auto: true`. Rationale: the disclosure decision is "publish these parts and keep them current", made once;
  120 seals an hour would make the chain unreadable (C9). *
- With `follow` on, `images: None` ("all of the notes' images") is frozen into the explicit list of the images
  the dialog showed at publish time, so a picture pasted into the notes later never goes out unseen; the dialog
  lists it as unticked at the next Update. Every other part is already a closed choice (parts are booleans,
  quotes are spans, attendees an explicit list; anyone else becomes "Person N"). *
- The live channel is a room on the SAME publish host as the publication, created with the publication's own
  publisher token (rooms.rs `authorize` checks `token_hash` the way publications do), with a random id of its
  own that the host stores beside the publication (`Meta.room`, C3) rather than the publication id, so a room
  the host has idled out or expired (rooms live at most `room_max_days` = 7 and `room_idle_hours` = 24,
  config.rs l.107-111, and a deleted id is refused for a while, rooms.rs l.73) can be replaced without
  touching the publication. The bridge extends the room weekly and heartbeats it so it normally lives as long
  as the owner's computer is on. *
- Crypto: the same envelope as guest notes (AES-256-GCM, HKDF from the link's key, padded, kind byte), in a
  namespace of its own: AAD `mnemosyne/pub-live/1|<room>|<kind>`, HKDF infos `mnemosyne/pub/frames` and
  `mnemosyne/pub/room`. A guest-notes frame can never open as a publication frame or the reverse, and the
  publication key stays the only secret in the link (no link format change, C4).
- Only the ORIGIN machine (services/origin.py `is_foreign` False; for a "this computer" publication the
  machine that serves it, `served_by` None) runs the watcher and the bridge for a meeting. The other computer of
  a library-sync pair may still Update by hand (today's rule) and then sends one VERSION frame and disconnects.
- "This computer" publications get automatic versions (the watcher is local) and the viewer's ETag poll, but no
  room and no comments in v1: the backend has no `/live/` socket and readers reach it through the team listener
  or `phone_url` (`local_reach`, publishing.py l.781), not through the rooms sidecar. The dialog says "Live
  updates and comments need a publish host". Later milestone (M4) below. *
- Readers comment under a name they type (kept in the browser like a guest's); `rid` is derived from the
  reader's own signing key (below), not random; the actor is `("reader:<rid>", "<name> (reader)")`. Anyone
  with the link may comment while `comments` is on; there is no second, comment-capable key (C4). Readers
  see each other's threads, only through the owner's signed THREADS. *
- Which threads reach readers: only threads whose first comment is a reader's, with every reply in them (the
  owner's replies included, text passed through the selection's redaction and names, author shown by
  `Names.name`); the owner's own threads, and anything on an unpublished part, never go out. Moderation is
  phase 5's: the owner deletes (`_Meeting.rights`: `owns`) or resolves; a deleted comment reaches readers as
  "removed"; no approve-before-show queue in v1. *
- Reader comments anchor on what the manifest names: a decision / question / action item by its block id
  (`Decision.id` etc. are the summary ids, publishing.py l.329-347), a quote by its transcript lines (the
  bridge maps the quote id back to `first`/`last` through `build_content`'s specs), the summary text by
  `exact`/`prefix`/`suffix`, and the meeting as a whole (target ""). Notes and chapters: comment on the meeting
  as a whole in v1 (C8). *
- Caps the bridge enforces on readers (the host only knows frames): a comment ≤ 4,000 characters, a name
  ≤ 80 (guest_notes `MAX_NAME`), at most 20 comment operations per reader per 10 minutes, edit/delete only
  their own id (comments.py `rights`: `mine`). The host's own caps apply unchanged (frame 2 MiB, log 8 MiB,
  64 connections, write budget).
- A publication of a meeting someone shared with me (`sessions.shared_from`) cannot be live or take comments
  (C10); publishing it stays as it is today.
- The single-file export never goes live (no network; its CSP makes no requests): the footer says so.
- The watcher runs the publish job as the meeting's owner (an `access.Principal` set for the task the way
  `shares.accept_contributions` sets a contact's, l.488), so team-server rules (`check_may_publish`) hold.
- **Owner keys per publication** (round 1, D1): an Ed25519 signing key and an X25519 box key, generated with
  the publication (and for an existing publication at its first Update with `follow` or `comments` on), kept
  in two new columns of the publications row (`sign_key`, `box_key`, raw private bytes, sealed at rest with
  the database like `key`), travelling in `SyncSession.publications` like key and token, never in
  `publications.json`, the records export or any history line. Public halves in the manifest's optional
  `live` field. No rotation in v1: Unpublish ends the pair with the publication.
- **Reader keys** (D2): the viewer generates an Ed25519 key per publication on first comment, keeps it in
  localStorage beside the name, and `rid` = the first 16 hex characters of SHA-256 of the public key. A cleared
  browser is a new reader (the old comments stay, now nobody's to edit), as a guest's cleared `gid` is.
- **Curves in the browser** (D4): `@noble/curves` (audited; `ed25519` and `x25519` modules only, ~35 KB
  minified) pinned to an exact version in package.json at task 2 (the newest 1.x release on that day,
  recorded in this file's "changed while building"), for Ed25519 and X25519; AES-GCM, HKDF and SHA-256 stay
  WebCrypto (already used). WebCrypto's own Ed25519/X25519 ("secure curves") only arrived in Chromium 137,
  Firefox 130 and Safari 17, and the viewer must open on older readers' browsers too; one code path is also
  one set of vectors. The desktop app's WebKitGTK never runs the live client (the Publish dialog's preview is
  Meeting.svelte on an unsealed manifest, publishing.py `preview` l.505), so it is not a target. Python:
  `cryptography>=43` (backend/pyproject.toml l.23; Ed25519 already in services/person.py `PersonKeys`,
  X25519 and HKDF in the same package), nothing new to install.
- **FORMAT stays 1** (D3): `live` is an optional, additive manifest field (absent from the canonical JSON when
  unset, as `images` is, publication_format.py `manifest_bytes` l.223), so an old viewer, which keeps unknown
  fields (format.ts `parseManifest` l.213), opens the manifest and shows it as today; `publication_v1.json`
  is not regenerated; a new vector file beside it carries a manifest with `live`.
- **Approve-before-show becomes a switch in M2** (D2 re-weigh): with THREADS the only path to readers, hiding
  a comment is a filter in `threads_for_readers`; what it costs is on the owner's side (a `hidden`/`approved`
  state per comment: a column, the sync model, an Approve button): one careful task (13b). Default off. *

## Questions for Corey (the ones that change the work)

Answered 2026-11-05: Corey took every recommendation. 1 yes (preselected for hosted), 2 one link with the
per-publication switch, 3 no (M4), 4 no seals for automatic versions, 5 reader threads only, 6 yes: task 13b is
in, the switch off by default.

1. **Should "keep up to date" be the default for new hosted publications?** Recommended: yes (the dialog
   preselects it; the model default stays off). It is what the request describes, and the selection bounds
   what goes out. Cost of "no": one line in the dialog; readers of most links then still see only manual
   updates pushed live.
2. **Comments from anyone with the link, or a separate "may comment" link?** Recommended: anyone with the
   link, switchable per publication (`comments`), plus Unpublish as the kill switch (as today). A second key
   means link format 2 for publications (`#2.<key>.<comment key>`), a viewer that parses both, and the owner
   managing two links; about +3 tasks. Round 1's per-reader keys do not change this: they say which reader
   wrote what, not who may write; the bridge could additionally admit only readers whose public key the owner
   approved once (a "first comment needs approval" variant of 13b) if a closed circle is wanted without a
   second link.
6. **Approve reader comments before other readers see them?** Recommended: a per-publication switch, off by
   default (task 13b, one careful task: a comment state column + sync + an Approve button). With it off, a
   reader's comment reaches the others at the next THREADS; the owner still deletes. Dropping 13b from M2
   saves that task; the design keeps the door open either way.
3. **Live for "this computer" publications in v1?** Recommended: no (M4). Doing it means either a WebSocket
   `/live/` on the backend that proxies to the rooms sidecar's loopback host (readers arrive on the team
   listener or `phone_url`), or iroh links for publications (format 2 plus guest-wasm in the viewer, +1 MB).
   Either is a milestone of its own (M4 sketches the backend-proxy route, the cheaper one).
4. **Seals for automatic versions?** Recommended: none (history only, listed in `publications.json`). If
   records must show each version in the chain, make auto versions seal with a new reason
   `publication auto-updated` and accept the chain length; one line in `publishing.publish`.
5. **Reader threads only, or also the owner's threads on published parts?** Recommended: reader threads only
   (nothing the owner wrote for the team leaks by default). Sending the owner's threads too is a filter change
   in `threads_for_readers` plus a per-thread "share with readers" switch in the comments panel (+1 task).

## Corrections to the request and to the obvious design (from the code)

- **C1 Not a hashseq space.** The viewer renders `Manifest.notes`, which is `_notes_text(own_notes)`:
  chips flattened to text, names and identifiers redacted, images filtered to the chosen list (publishing.py
  `_notes_text` l.415, `_own_notes` l.209). A live notes document would carry the unredacted text, every
  editor's history and the sections the summary writes (`sec:` lines), and text in an op cannot be erased
  (hashseq plan, prerequisite 1). Live therefore stays "a new sealed version per change"; the room carries
  pointers and comments, never content.
- **C2 The behind test already exists and is the right trigger.** `is_behind` (l.802) rebuilds the content
  with the stored selection and compares `digest(content, specs)` (l.467) with `published_digest`. The watcher
  reuses it; no second notion of "changed".
- **C3 Rooms cannot be re-created under their id, and publications outlive rooms.** `create_room` clamps
  `X-Room-Expires` to `room_max_days` (app.rs l.539, config.rs l.107: 7 days); a room idles out after
  `room_idle_hours` (24); `RoomMeta.deleted_at` means "a re-create of the id is refused for a while" (rooms.rs
  l.73). A publication lives up to 365 days. So the room is NOT the publication id: the host keeps the current
  room id in the publication's `Meta` (store.rs l.60 has no such field), set by the owner (`X-Pub-Room` on
  `PUT /pub/{id}`, app.rs l.380, and a new `PUT /pub/{id}/room`) and returned on `GET /pub/{id}` (l.309,
  beside `X-Pub-Version`), and the bridge makes a fresh room when the old one is gone.
- **C4 The link does not change.** The viewer already reads id and key from its address (load.ts
  `parseLocation` l.127); the room id comes from the manifest response and the room token from the key (HKDF,
  as guest_format.py `room_token` l.143 does for guests). An older viewer ignores the header and shows the
  version it loaded; an older host answers `PUT /pub/{id}/room` with 404 and the bridge reports "this host
  does not support live updates" (`Publication.live = "unsupported"`).
- **C5 One sealing module, two namespaces.** guest_format.py hard-codes `AAD_PREFIX` and the HKDF infos
  (l.62-65, `aad` l.181, `seal` l.194 via `frames_key`); guest/src/format.ts is its mirror. Both become
  parameterised by a `Space` (prefix, infos, kinds), with the guest space as the default so guest_v1.json and
  every guest test pass unchanged, and a `pub_live` space beside it. In TS the envelope primitives move to
  `src/lib/sealed/envelope.ts` so the viewer (a separate Vite entry, viewer/vite.config.ts) can import them
  without reaching into guest/src.
- **C6 No new repository hook.** `_touch` (sqlite.py l.4502) runs inside the writer's transaction, so a
  hook on it would fire before the commit; `_after_summary` (l.4533) covers summaries only. But every
  travelling write already moves the meeting in the change log, and `repo.changes_since(seq)` /
  `current_sync_seq()` (l.4551, l.4567) are what the sync service polls (sync_service.py l.274). The watcher
  polls the same counter every 5 s: no schema or hook change, 5-15 s of latency, and it catches speaker
  renames, attendee changes, transcript edits of quoted lines, notes folds and ticks alike.
- **C7 The room client and the reader UI need no wasm.** guest/src/room.ts is pure transport (no crypto, no
  document, l.1-3), transports.ts/link.ts are the guest link's; the viewer only needs `RoomClient` with one
  `host` transport (`roomUrl`, l.138) and `since`. It moves to `src/lib/rooms/client.ts` with guest/src/room.ts
  re-exporting it (its tests move with it). The viewer bundle grows by the client and the envelope code only.
- **C8 Reader anchors must be ones comments.py can re-find.** `_anchor` (l.195): decisions, questions and
  items need `target_id` (the manifest has the ids); a transcript span needs `idx`/`idx_to` (`_segment_anchor`
  l.157), which a reader does not have but the bridge computes from the quote id (`build_content` specs
  `first`/`last`, l.397-407); the summary text needs `exact` found in `plain_markdown(session.summary)`
  (l.211) and the reader's text is the REDACTED summary, so a sentence with a replaced name may not be found:
  the bridge tries, and on `BadComment` keeps the thread as a comment on the meeting as a whole with the quote
  in its text ("On: …"), never drops it. Notes threads need `notes_ids` (element ids) that the rendered
  markdown does not carry: not in v1.
- **C9 Seals would explode.** `records.seal` forces a seal for every `EVENT_SEALS` reason (l.67-91) and
  `publishing.publish` seals every version (l.1186). Automatic versions skip `seal_later` (the `auto` flag),
  log `publication_auto_updated`, and `publications_record` (l.398) adds that kind to the ones it reads.
- **C10 Shared-with-me meetings.** `check_may_publish` (l.88) has no `shared_from` rule, and `comments.create`
  on such a meeting enqueues the comment to the owner through `share_push.enqueue_comment` (l.612) signed as
  the recipient: a reader's comment would reach the sharer as the recipient's own words. `follow` and
  `comments` are refused for `shared_from` meetings in `check_request` (as guest notes refuse them,
  guest_notes.py l.386).
- **C11 Reusing the guest bridge wholesale is not realistic.** `guest_notes.Bridge` (l.771-1630) is bound to
  `shared_notes` (hidden member, frames, media, presence). The publication bridge is a new, small class in
  services/live_publications.py that copies the connection pattern (`_connect_loop` l.901: join with the room
  token and `since`, welcome, backoff, ack handling) and imports the module-level helpers (`_request`,
  `_call`, `_host_message`, `post_snapshot`'s shape) rather than subclassing. Extracting a shared
  `RoomConnection` is listed as later cleanup, not a prerequisite.
- **C12 The non-origin computer may update a hosted publication** (publishing.py `served_elsewhere_message`
  l.103: "A hosted publication can be changed from any computer"; `_host_upload` tells the two apart by the
  manifest ETag, l.726). Automatic versions are origin-only (one watcher), but a manual Update from the other
  computer must still reach open viewers: the publish job sends one VERSION frame through a one-shot
  connection (it holds key and token; it reads the room id from `GET /pub/{id}`). If no room exists it does
  nothing (viewers poll).
- **C13 Expiry has two clocks.** The host sweeps an expired publication to a tombstone (410, store.rs
  `deleted_at`) and the viewer already maps 410 to "No longer published" (load.ts l.158); `expiredOn` (l.109)
  also judges the manifest's `expires_at` locally. The room's expiry is kept ≤ the publication's; after
  unpublish the bridge deletes the room first (readers get 4410 and show "No longer published" at once), then
  the publication as today (`unpublish` l.1222).
- **C14 The viewer's CSP is `connect-src 'self'`** (routes/publishing.py l.31; app.rs the same string). A
  same-origin `wss://` is covered by `'self'` in current Chromium, Firefox and WebKit (CSP3), which the e2e
  (Chromium and WebKit) confirms; if WebKit balks, both servers add the explicit `wss://<host>` form.
- **C15 History shows every event.** `history.log` (history.py l.36) has no coalescing and the Parts & history
  card lists each line; a two-hour recording with `follow` gives up to ~240 `publication_auto_updated`
  lines. The card groups consecutive ones ("Link kept up to date: 37 versions, last 14:32"); the rows stay.
- **C16 The room authenticates nobody** (round 1, D1). live.rs relays any connection's `0x01 || envelope` to
  every other one (header l.1-55); `Event::Frame { from }` (rooms.rs l.82) is a connection id used only to
  skip the echo and is not on the wire. Every reader derives the frames key from the link key, so a reader can
  seal a THREADS with invented "Owner" replies, a META turning comments off, or a VERSION. Hence the owner's
  Ed25519 key, published in the manifest (which only the publisher token can upload and which is AEAD-sealed
  under the link key, so the host cannot plant a key), and signatures on every owner→reader frame.
- **C17 The log is public to the room** (D2). A logged frame is replayed to every joiner (`join_with_meta`,
  live.rs l.353-377) for the room's life, so a COMMENT sealed under the frames key is readable by every reader,
  deleted or not, and `rid` in a THREADS lets anyone edit or delete under it. Hence COMMENT ops are sealed to
  the owner's X25519 key and signed by a per-reader key; readers learn comments only from the owner's signed
  THREADS. Readers can still see that a COMMENT frame exists and its padded size (one 256-byte block for most
  comments), nothing else.
- **C18 Adding a manifest field without a format bump.** The Python models are `_Strict` (`extra="forbid"`,
  publication_format.py l.136): `Manifest` gets `live: LiveKeys | None = None` (`LiveKeys(_Strict)` with
  `sign` and `box`, 64 hex each), dropped from the canonical JSON when None (as `images` is), so every existing
  publication's bytes, digest and `publication_v1.json` are unchanged and FORMAT stays 1; `parse_manifest`
  accepts it. The viewer's `parseManifest` already passes unknown fields through; `openHosted` reads
  `manifest.live` and only then joins a room; without it (an owner older than this feature, or an existing
  publication not yet Updated) the viewer polls and offers no comments, whatever `X-Pub-Room` says.
- **C19 No curves in WebCrypto on older browsers** (D4): `@noble/curves`, pinned, as decided above.

## Data and formats

- `PublicationSelection.follow`, `.comments` (bools, default False). Stored inside the row's `selection`
  JSON (sqlite.py publications table l.472-489): no new column for them; `pnpm gen:api`.
- Two new publications columns `sign_key BLOB`, `box_key BLOB` (NULL until live is first turned on; migration
  in `_migrate_columns` as `origin` was, l.906-909; `_PUB_COLS`/`_PUB_UPSERT` grow from 16 values), carried
  by `SyncPublication` (models/sync_session.py) and `merge_publications` like `key`/`token`, never by
  `records.publications_record` or the API `Publication` model.
- `Manifest.live: LiveKeys | None` (`LiveKeys(_Strict)`: `sign`, `box`, hex of the raw 32-byte public keys),
  written by `build_manifest` from the row when either switch is on (C18). The digest (`digest()` l.467) is of
  `content`, which does not include `live`, so turning live on alone does not make a publication "behind".
- `Publication` (API) gains `live: Literal["live", "offline", "unsupported", "off"]` (the bridge is connected;
  the owner's bridge is not running or cannot reach the host; the host or place cannot do it, with
  `live_reason: str`; neither follow nor comments is on) and `readers: int` (the room's `count` minus the
  bridge, 0 when unknown).
- The host's `Meta.room: Option<String>` (store.rs), written by `X-Pub-Room` on `PUT /pub/{id}` and by
  `PUT /pub/{id}/room` (publisher token; empty body clears it), returned as `X-Pub-Room` on `GET /pub/{id}`
  (also on the 304), cleared by `delete`. Not in the manifest (the manifest is content; the room is
  transport), so format 1 and `publication_v1.json` do not change.
- Nothing new in the backend's own `/pub/` routes: a "this computer" publication has no room (v1).
- Frames in the `pub_live` space. Every frame is still a room envelope (kind byte, nonce, AES-GCM under the
  frames key, 256-byte padding, AAD `mnemosyne/pub-live/1|<room>|<kind>`), so the host and the room client
  see the same shape as guest frames; what differs is the plaintext:
  - **Owner → readers, signed** (VERSION = 1, THREADS = 2, META = 3): plaintext = `sig(64) || body`, `body` the
    canonical JSON below, `sig` = Ed25519 by the owner's signing key over `"mnemosyne/pub-live/1|<room>|<kind>|"
    || body` (room and kind bound, so a frame cannot be replayed into another room or as another kind). The
    viewer verifies with `manifest.live.sign` and drops anything else without a message (a hostile reader
    sees nothing happen). The room snapshot is a THREADS frame, verified the same way.
    - VERSION: `{version, sha256, published_at, expires_at}`; the viewer still trusts only the manifest it
      then fetches (ETag = `sha256`, version higher than shown); the signature stops a reader from making
      every other reader re-fetch in a loop.
    - THREADS: `{threads: [{root, replies}], at}` with each comment `{id, parent_id, author, rid, text,
      anchor: {target, target_id, quote_id, quote}, created_at, updated_at, resolved, deleted, detached,
      changed}` (guest_notes `_guest_comment` l.557 with `rid` for `gid`, an `anchor` the viewer can place,
      and `at` so an older THREADS replayed after a newer one is ignored).
    - META: `{comments: bool, follow: bool, at}`; also the bridge's heartbeat.
  - **Reader → owner, boxed and signed** (COMMENT = 4): plaintext = `eph(32) || nonce(12) || AES-GCM(ct)`
    where `eph` is a fresh X25519 public key per frame, the AES key = HKDF-SHA256(X25519(eph_private,
    owner box key), info `mnemosyne/pub/box`, salt = the room id bytes), AAD `"mnemosyne/pub-live/1|<room>|4|"
    || hex(eph)`; the boxed content is `sig(64) || reader_pub(32) || op`, `sig` = the reader's Ed25519 over
    `"mnemosyne/pub-live/1|<room>|4|" || reader_pub || op`, `op` = `{op: create|reply|edit|delete, id (12 hex,
    minted by the reader as guests do, comments.py `create` l.560), parent_id, text, name, at, anchor:
    {target: ""|body|decision|question|item|quote, target_id, quote_id, exact, prefix, suffix}}`. The bridge
    (the only holder of the box private key) opens it, checks the signature, derives `rid` from
    `reader_pub`, and applies the op as actor `reader:<rid>`; an edit/delete whose `id` belongs to another
    `rid` is refused by comments.py's own rights (`mine`), and an op whose `at` is older than the last applied
    for that id is ignored (a replayed frame does nothing). Other readers can open neither the box nor the
    signature's subject.
  - Outgoing frames are pydantic models with `extra="forbid"` and only these fields; a test serialises every
    outgoing frame of a bridge run and asserts the key set (the leak test's second half).
  - **Bytes settled at task 1** (services/pub_live_format.py, the TS side matches these):
    - Every pub-live kind pads to 256-byte blocks, THREADS (also when posted as the room's snapshot)
      included: there is no 4096 snapshot block in this space. Kinds: VERSION 1, THREADS 2, META 3,
      COMMENT 4; no media kind, no plain-JSON kind.
    - `frames_key`/`room_token`: HKDF-SHA256 with an EMPTY salt, length 32, from the 32-byte link key, as
      guest_format does; the room token is unpadded base64url.
    - Signed message = UTF-8 `mnemosyne/pub-live/1|<room>|<kind decimal>|` followed by the body bytes.
      Ed25519 is pure RFC 8032; private keys are the 32-byte seed. `body` and `op` are JSON OBJECTS in
      canonical JSON (sorted keys, no whitespace, UTF-8, non-ASCII as is: publication_format
      `canonical_json`); the receiver verifies the bytes as received and does not re-canonicalise.
    - Box: X25519 per RFC 7748 (32-byte scalar, clamped by the library); an all-zero shared secret
      (low-order point) is refused. HKDF salt = the ASCII bytes of the 16-character room id string (NOT
      its 8 hex-decoded bytes), info `mnemosyne/pub/box`, length 32. `hex(eph)` in the inner AAD is 64
      lowercase hex characters. Inner nonce 12 random bytes; inner AES-GCM output is ciphertext || 16-byte
      tag; the boxed content is NOT padded (the outer envelope pads). In the reader's signed message
      `reader_pub` is the raw 32 bytes.
    - `rid` = first 16 lowercase hex characters of SHA-256 over the raw 32-byte Ed25519 public key.
    - `LiveKeys.sign`/`.box`: 64 lowercase hex (`^[0-9a-f]{64}$`), raw 32-byte public keys; unknown fields
      inside `live` are refused like the rest of the manifest.
    - publication_live_v1.json (scripts/make_live_vectors.py): `key_b64url`, `pub_id`, `room`,
      `frames_key_hex`, `room_token`, `owner` {sign/box private and public hex}, `reader` {private_hex,
      public_hex, rid}, `manifest` {version, json, aad, nonce_hex, blob_b64url, sha256} (with `live`),
      `samples.version|threads|meta` {kind, aad, nonce_hex, json, signed_message_hex, sig_hex,
      plaintext_hex, padded_length, envelope_b64url}, `samples.comment` {kind, aad, nonce_hex, json,
      reader_message_hex, reader_sig_hex, content_hex, eph_private_hex, eph_public_hex, shared_hex,
      box_key_hex, box_aad, box_nonce_hex, plaintext_hex (eph || nonce || ct), padded_length,
      envelope_b64url, rid}, and `refused` {threads_signed_by_reader, version_unsigned}: well-sealed
      envelopes that must NOT verify under `owner.sign_public_hex`.
- Test vectors: `backend/tests/vectors/publication_live_v1.json` (link key, room id, room token, frames key;
  fixed owner sign/box key pairs, one fixed reader key pair, fixed nonces and ephemeral key; one sealed and
  signed VERSION, one THREADS, one boxed COMMENT with its expected `rid`; a manifest JSON with `live` and its
  sealed blob), checked by Python (test_live_publications.py) and TS (viewer/src/live.test.ts), as
  guest_v1.json is. `publication_v1.json` is untouched.

## The owner's side (services/live_publications.py)

- **Watcher** (one task, started in `AppContext.build` beside `guest_notes.resume`, context.py l.642, stopped
  at shutdown): every 5 s `current_sync_seq()`; on change `changes_since(last)` → meeting ids; for each with a
  live row (`repo.live_publication`) whose selection has `follow`, that is not `shared_from`, not foreign
  (`is_foreign`), not served elsewhere, mark `dirty[sid] = now`. Each tick, a dirty meeting that has been
  quiet 10 s, whose last automatic version is ≥ 30 s old and has no active publish job
  (`ctx.jobs.list(session_id=…, active_only=True)`, as the route checks, routes/publishing.py l.134) is
  checked with `is_behind`; when behind, `ctx.jobs.submit("publish", run)` with `publishing.publish(…, auto=True)`
  under the owner's principal. Jobs show in the UI like a manual one ("Publishing · automatic").
- **`publishing.publish(auto=True)`**: same body; `history.log(... "publication_auto_updated" ...)` instead of
  `publication_updated`, no `seal_later`, `published_by` kept; afterwards `live_publications.version_published
  (app, row, manifest_sha)` (also from a manual publish) which sends the VERSION frame through the bridge, or
  one-shot (C12). With `follow` on and `sel.images is None`, the selection stored is the explicit list
  (`content["images"]` ids) (decided above).
- **Bridge** (`PubBridge`, one per live hosted publication of this machine, `follow or comments` on): at
  start `GET /pub/{id}` for `X-Pub-Room`; when none or the room is gone (4410 at join, 404/410 at `PUT
  /live/{room}`), create one (`PUT /live/{room}` with `X-Room-Token` = sha-irrelevant token from
  `gf.room_token` in the pub space and `X-Room-Expires` = min(publication expiry, now + 7 d)), then
  `PUT /pub/{id}/room`. Join over the WebSocket like guest_notes (`join` with the room token and `since`),
  send META, post the THREADS snapshot (`PUT /live/{room}/snapshot` with `X-Room-Since`), then relay: COMMENT
  frames in, VERSION/THREADS/META out. Housekeeping: extend the room's expiry every 24 h (`PUT /live/{room}`
  "moves its expiry", app.rs l.536), META heartbeat every 12 h (idle rule), reconnect with the guest bridge's
  backoff. Comments from readers → `_on_comment` as guest_notes l.1136 does, with the anchor mapping of C8 and
  the caps above; after each `comments` bus event for the meeting (and after `comments.resolve`/delete by the
  owner) re-seal THREADS (debounced 2 s) as a frame and as the snapshot. Stopped and the room deleted at
  unpublish, expiry (`sweep_expired`), meeting deletion (`forget_session`) and when both switches turn off;
  on a team server the same, in the server process.
- **`threads_for_readers(app, session_id, sel)`**: `comments.threads` under `shared_notes.unrestricted()`
  (as l.579), keep threads whose root `author_id` starts with `reader:`; drop roots whose target is a part the
  selection does not show (`decision` needs `sel.decisions`, …; `segment` needs a quote whose lines contain
  it); text and quote through the same `text()`/`Names` the manifest uses (`build_content` l.294-301 built
  once per call); owner-side authors as `names.name(author) or "Owner"`; reader authors as typed.
- Settings and policy: `check_request` refuses `follow`/`comments` for `shared_from` meetings and for
  `where = "local"` in v1 ("Live updates and comments need a publish host"); team servers: nothing beyond
  `allow_publishing` (the host does not know who reads).

## The viewer (viewer/src)

- `live.ts` (new, pure): `pollManifest(id, etag)` (every 60 s, `If-None-Match`, a 304 is nothing, a new
  `X-Pub-Version` opens and swaps; 404/410 → the existing states); `openLive(id, key, room, liveKeys)` → a
  `RoomClient` (`src/lib/rooms/client.ts`) with one host transport, the room token from the key, frames opened
  in the pub space and VERIFIED against `liveKeys.sign` (unsigned or bad: dropped, counted for a debug line,
  never shown); `onVersion` re-fetches with `cache: 'no-cache'` and accepts only when the ETag equals the
  frame's sha and the version is higher than the one shown; `onThreads` (newest `at` wins), `onMeta`, `count`.
  A manifest without `live` → poll only, no room, no composer (C18). `since` kept per room id in memory (the
  viewer keeps no copy: a publication is fetched, not reconstructed). Reader side (M2): `readerKeys(pubId)`
  (Ed25519 from `@noble/curves`, localStorage), `boxOp(op, liveKeys.box)` (X25519 + HKDF + AES-GCM as in
  "Data and formats"), `rid` from the public key; the signing key never leaves localStorage and is not in
  the URL or any frame (only its public half, inside the box).
- `load.ts`: `openHosted` returns `room` (from the header) and `etag` with the ready state; a `swap(manifest)`
  path that keeps `loadClip`/`loadImage` (same id and key; blobs are content-addressed and the image loader
  caches per image id, l.37).
- `Viewer.svelte`: after `ready`, start the poll and, with a room, the live client; on a new manifest replace
  `view.manifest` in place (scroll kept; a quiet "Updated 14:32" in the footer, `footerLine` in display.ts);
  on 4410 from the room or 410 from the poll → "No longer published" (the room going away alone only
  re-reads the header for a new room id); 4401 → stop live, keep polling.
- `Meeting.svelte`: a status line ("Kept up to date · live · 3 reading" / "Checks for updates every
  minute"); in M2 a comment affordance on each decision, question, action item and quote, a selection
  comment on the summary, a "Comment on this meeting" box, and `ReaderComments.svelte` (threads, reply, edit
  and delete own, name card on first comment; the pure helpers from guest/src/comments.ts: `PendingOps`,
  `withPending`, `orderThreads`, `ago`, `mintId`). Phone layout: a Comments tab as the guest page has (Tabs
  pattern, guest/src/Tabs.svelte).
- `single.html`: no live, no comments ("This file does not update" in the footer).
- Built by `pnpm build:viewer` and committed; the CI `viewer` job diffs it (ci.yml l.51-75). `pnpm
  build:guest` too, because guest/src now re-exports moved modules.

## The owner's UI

- PublishDialog.svelte (Where section l.276): two checkboxes for a host target, "Keep the link up to date"
  (help: "Publishes a new version by itself when the meeting changes, with these same choices; nothing you
  did not tick is added") and "Readers may comment" (help: "Anyone with the link can comment under a name
  they type; their comments appear here as '(reader)'"); greyed with the reason for "this computer". The
  published line (l.352) gains the live state and readers ("Live · 2 reading" / "Not connected to the host" /
  "This host cannot push updates: readers check every minute"). "behind" is not shown while `follow` is on
  (the watcher is about to fix it); it is when an automatic publish failed (the row keeps `live_reason`).
- PublicationsList.svelte: a live dot and the readers count.
- Comments panel: reader threads show their author "Ana (reader)"; the owner can reply, delete, resolve as
  for guests; a small "From the published link" caption on the thread.
- Parts & history card: consecutive `publication_auto_updated` lines grouped (C15).
- Toasts: none for automatic versions; one when an automatic publish fails twice in a row ("The published link
  could not be updated: <reason>", with Open publication).

## Rules: sync, records, team, failure and offline

- Library sync: the row (with `follow`/`comments` inside `selection`) travels as today; the origin bridges; a
  manual Update from the other machine pushes one VERSION (C12); `sync_publications.settle` unchanged.
- Records: manual publish/update/unpublish seal as today; automatic versions and reader comments are history
  (`publication_auto_updated`, `commented` by "<name> (reader)"); `publications.json` lists versions with
  `auto`; reader comments are ordinary comments (searchable in `comments_fts`, synced, in the export) as
  guest comments are (docs/guest-notes.md "What stays in your meeting").
- Team servers: `allow_publishing` gates everything; the server process runs watcher and bridge; a reviewer
  sees reader threads like any comment; the link is still only the owner's or an admin's.
- Owner's computer off: the host keeps the last version (readers open it, poll, and see nothing new) and the
  room keeps readers' COMMENT frames in its log (8 MiB; the bridge replays from `since` when back); no
  automatic versions until it is back (the meeting cannot change without it either, except through the other
  synced computer, whose manual Update pushes).
- Host unreachable: the watcher's job fails as a manual publish would (`PublishError`), the row is marked
  behind with `live_reason`, the watcher retries with backoff (1, 2, 5, 10 min); the bridge reconnects with
  the guest backoff.
- Older host (no `PUT /pub/{id}/room`): `live = "unsupported"`, automatic versions still upload, viewers
  poll. Older viewer on a new host: shows the version it loaded (the same as today). Old link, new
  everything: works unchanged (the key is the same; `follow` and `comments` are off until the owner Updates).
- Unpublish: room deleted, then the publication; expiry: the host tombstones the publication and the room
  expires on its own; readers see "No longer published" / "expired" as today.
- The leak test (`test_live_publications.py::test_an_automatic_version_never_widens_the_selection`): a
  meeting with a followed publication (decisions unticked, two images chosen, two attendees listed, one
  quote); then: a new transcript line, a new image line in the notes, a new attendee, a re-summarize that adds
  decisions and an action item naming the new person, a notes paragraph with an e-mail address; after the
  watcher's version: no `segments`/`transcript` in the manifest (as test_publishing.py l.118 asserts), the
  same two images, the new person as "Person N" everywhere, no decisions, the e-mail redacted, and the
  manifest equal to `build_manifest` with the STORED selection; plus every frame the bridge sent is one of
  VERSION/THREADS/META with exactly their fields. Round 1 adds, in the same file: (a) two readers' COMMENT
  frames replayed from the fake room's log open for the bridge and for NEITHER reader's keys (the box), and
  the second reader's edit/delete under the first's comment id is refused; (b) a THREADS frame sealed with
  the frames key but signed by a reader's key (or unsigned) is dropped by the viewer's `live.ts` (Vitest,
  viewer/src/live.test.ts, with the vectors' reader key) and by the bridge if it ever arrives there; (c) the
  private keys appear in no history detail, `publications_record` output or API `Publication`.

## Tasks (tier, deps)

Files in brackets are what a task may edit; tasks that run at the same time never share a file. Backend tests
one file at a time (`cd backend && uv run pytest tests/<file> -q`), Vitest one file at a time (`pnpm exec
vitest run <file>`), `pnpm check` at 0 errors, ruff on touched files, `cargo test` per crate; the viewer and
guest builds, the full e2e and the full backend suite only in CI. Never play audio.

### M0 shared pieces (1 and 2 in parallel)

1. **Sealing namespaces, signatures and the box in Python** (heavy). [backend/mnemosyne/services/
   guest_format.py (the `Space`), new backend/mnemosyne/services/pub_live_format.py (signed frames, the box,
   `rid_of`, `LiveKeys` helpers over `cryptography`'s Ed25519/X25519/HKDF), services/publication_format.py
   (`LiveKeys`, `Manifest.live`, dropped from canonical JSON when None), backend/tests/test_guest_format.py
   (checks guest_v1.json, unchanged), new backend/tests/test_pub_live_format.py, backend/tests/
   test_publication_format.py (or wherever publication_v1.json is checked: a manifest without `live` is byte
   for byte what it was), new backend/tests/vectors/publication_live_v1.json + backend/scripts/
   make_live_vectors.py] A `Space` dataclass (aad prefix, frames info, room info, media-name info, kinds);
   guest functions take `space=GUEST` by default so every caller and test is unchanged; `PUB_LIVE` space with
   kinds VERSION/THREADS/META/COMMENT; `sign_frame`/`open_signed`, `box_op`/`open_box` exactly as "Data and
   formats" says. Vectors generated on purpose only. Tests: round trips; a frame signed with another key does
   not open; a box for another owner key does not open; a replayed box opens once more (idempotence is the
   bridge's, task 11); `rid` as specified; `publication_v1.json` unchanged. Verify: the test files, ruff.
2. **Room client, envelope code and curves into src/lib** (heavy). [guest/src/room.ts → src/lib/rooms/
   client.ts (guest/src/room.ts becomes `export * from`), guest/src/room.test.ts → src/lib/rooms/
   client.test.ts, guest/src/format.ts → the envelope primitives (hkdf, pad/unpad, seal/unseal with a
   `Space`) in src/lib/sealed/envelope.ts with guest/src/format.ts calling them (same exports), new
   src/lib/sealed/signed.ts (Ed25519 sign/verify, the X25519 box, `rid`, over `@noble/curves` pinned to an
   exact version in package.json + pnpm-lock.yaml; WebCrypto for AES-GCM/HKDF/SHA-256), new
   src/lib/sealed/envelope.test.ts and signed.test.ts, viewer/src/format.ts untouched] No behaviour change
   for the guest page: its tests and the viewer's pass as they are. Verify: `pnpm exec vitest run
   src/lib/rooms src/lib/sealed guest/src/format.test.ts guest/src/room.test.ts`, `pnpm check`; `pnpm
   build:guest` at the close (task 16). Note the pinned version in "changed while building".

### M1 versions pushed and kept up to date (3, 4 in parallel after M0; 5 after 4; 6 after 1, 3, 4; 7 after 2; 8 after 4; 9 after 1, 7; 10 last)

3. **publish-host: the room pointer** (medium, Rust). [publish-host/src/store.rs (`Meta.room`), app.rs
   (`X-Pub-Room` in `put_manifest` and `get_manifest`, new `PUT /pub/{id}/room` with `check_writer`, cleared
   in `delete`), publish-host/tests/*] Tests: pointer set by PUT manifest header and by the route, returned on
   200 and 304, absent by default, refused without the publisher token, gone after delete; an empty body
   clears it. Verify: `cargo test` in publish-host, `cargo fmt --check`, `cargo clippy --all-targets -- -D
   warnings`.
4. **Selection fields, owner keys on the row, `publish(auto)`, frozen images, history and records** (careful;
   1 for `LiveKeys`). [backend/mnemosyne/models/publication.py (`follow`, `comments`, `Publication.live`,
   `live_reason`, `readers`), storage/sqlite.py (`sign_key`, `box_key` columns + migration, `_PUB_COLS`,
   `_publication`, `_publication_values`, `store_synced_publications`), models/sync_session.py
   (`SyncPublication` + merge), services/publishing.py (`publish(auto=)`: history kind, no seal,
   `published_by` kept, images frozen, keys generated when a switch is first on and `Manifest.live` set;
   `check_request`: refuse follow/comments for local and shared_from; `to_model`: live fields from a
   `live_publications.status(row)` stub, never the keys), services/records.py (`publications_record` reads
   `publication_auto_updated`, `auto` flag), backend/tests/test_publishing.py, test_records.py,
   test_publishing_records.py, a sync test file that covers publications (test_sync_publications.py if it
   exists, else the test_sync_*.py that does)] `pnpm gen:api`. Tests: round trip of the fields; an auto
   publish logs the new kind and adds no seal (`repo.seals`); images frozen only with follow; local + follow →
   400 with the message; keys made once and kept across versions, present in the manifest only while a switch
   is on, carried by sync, absent from `publications_record`, `Publication` and history details; the
   existing no-transcript test still passes. Verify: the test files, ruff, `pnpm check`.
5. **The watcher** (heavy; 4). [new backend/mnemosyne/services/live_publications.py (watcher half),
   backend/mnemosyne/api/context.py (start/stop), new backend/tests/test_live_publications.py] As "The owner's
   side": poll `current_sync_seq`, `changes_since(seq)` paged with its `more` flag (it already skips
   `shared_from` meetings), dirty map, quiet 10 s, gap 30 s, no active job, `is_behind`,
   submit the publish job under the owner's principal, backoff on failure, origin/served-by/shared_from rules.
   Tests (fake clock and an injected `http_transport` as test_publishing does): a summary edit yields one
   automatic version after the quiet time; three edits in 8 s yield one; an identical rewrite yields none; a
   foreign meeting and a shared_from one yield none; a failing host backs off; and the leak test above. Verify:
   the test file, `uv run pytest tests/test_publishing.py -q`, ruff.
6. **The bridge: room, VERSION, META, snapshot plumbing** (heavy; 1, 3, 4). [services/live_publications.py
   (bridge half), services/publishing.py (`version_published` call after a publish and the one-shot push, C12;
   room deletion in `unpublish`, `sweep_expired`, `forget_session`), backend/tests/test_live_publications.py
   (add); the fakes are backend/tests/fake_guest_room.py (`FakeRoom`, `FakeRoomHost`) for the room and
   test_publishing.py's `FakeHost` (l.610) for `/pub/`: teach `FakeHost` the pointer header and route rather
   than writing a third fake] Room create/extend/heartbeat/re-create on 4410, the
   pointer PUT, VERSION after a publish, META at join, `status()` for `to_model`, resume at start, stop at
   unpublish. Tests against the fake room: a publish sends VERSION with the manifest's sha; a 4410 at join
   makes a new room and updates the pointer; an older host (404 on the pointer route) gives `unsupported` and
   no room; unpublish deletes the room before the publication; the outgoing-frame key-set assertion. Verify:
   the test file, `uv run pytest tests/test_guest_notes.py -q` (unchanged), ruff.
7. **Viewer: poll and live versions** (heavy; 2). [viewer/src/live.ts (new) + live.test.ts, load.ts
   (`room`, `etag` in the ready state, `swap`), Viewer.svelte, Meeting.svelte (status line only), display.ts
   (`footerLine` with "Updated …"), viewer/src/load.test.ts] Pure parts tested with a fake fetch and the fake
   socket from the room client's tests: a 304 changes nothing; a new version swaps; a VERSION frame whose sha
   does not match the fetched ETag is ignored; 410 → unpublished; 4410 re-reads the pointer; no room → poll
   only. Verify: `pnpm exec vitest run viewer/src`, `pnpm check`.
8. **Owner UI for M1** (medium; 4). [src/lib/components/PublishDialog.svelte, PublicationsList.svelte,
   src/lib/stores/publishing.svelte.ts + publishing.test.ts, the history card component (grouping, C15)]
   Verify: `pnpm exec vitest run src/lib/stores/publishing.test.ts`, `pnpm check`.
9. **Vectors cross-check in TS** (medium; 1, 2, 7). [viewer/src/live.test.ts, src/lib/sealed/signed.test.ts
   (read publication_live_v1.json: room token, frames key; open and verify the signed VERSION and THREADS;
   refuse them under the reader's key; box the COMMENT op with the fixed ephemeral key and nonce and compare
   bytes; `rid` matches; the manifest with `live` opens and `parseManifest` keeps the field)] Verify: those
   files.
10. **e2e M1** (careful; 6, 7, 8). [tests-e2e/publish-live.spec.ts (new), tests-e2e/fixtures.ts if needed]
    Against the local publish host (tests-e2e/start-publish-host.sh, 8028, the e2e job builds it, ci.yml
    l.126): publish to the host with "keep up to date"; open the link in a second page; edit the summary in
    the app; the viewer shows the new text with no navigation; the dialog shows "Live · 1 reading";
    Unpublish → the viewer says "No longer published". Chromium and WebKit (C14). Done when it passes in CI.

### M2 comments from readers (11 after 6 and 1; 12 after 7 and 2 (the frame shapes are fixed above, so 11 and 12 run in parallel); 13 after 11; 13b after 11 and 13, if question 6 says yes; 14 after 11-13)

11. **Comment intake and threads out** (heavy; 6). [services/live_publications.py (`threads_for_readers`,
    `_on_comment` with the anchor mapping of C8, caps, comments-off drop, THREADS re-seal on the `comments`
    bus event; `open_box` + signature check + `rid` from the reader's key + the `at` replay rule before any
    comments.py call), services/comments.py only if a `lenient` fallback for an unfound `body` exact is
    cleaner than catching `BadComment` in the bridge (prefer catching), backend/tests/
    test_live_publications.py] Tests: a reader's create on a decision id → a comment with actor
    `reader:<rid>`, author "Ana (reader)"; on a quote → `segment` with the right idx range; a body exact with
    a redacted name → kept on the meeting as a whole with the quote; an op whose signature does not match
    its `reader_pub`, or whose `rid` is not the comment's author, is refused; a replayed box is a no-op; the
    21st op in 10 minutes dropped; comments off → dropped and META says so; `threads_for_readers` leaves out
    the owner's own thread and a thread on an unticked part, redacts the owner's reply text, maps the author
    through `Names`; every THREADS/META sent verifies under the row's signing key; plus leak test (a). Verify:
    the test file, `uv run pytest tests/test_comments.py -q`, ruff.
12. **Viewer comments UI** (heavy; 7). [viewer/src/ReaderComments.svelte (new), NameCard.svelte (new, the
    guest Name.svelte pattern), Meeting.svelte (affordances), live.ts (COMMENT ops boxed and signed with the
    reader's key from `src/lib/sealed/signed.ts`, pending ops, name and key in localStorage), viewer/src/
    *.test.ts, viewer/viewer.css] Reuses guest/src/comments.ts's pure helpers (import from guest/src or move
    them to src/lib/comments-pure.ts if the import feels wrong: decide in the task, prefer the move). A
    reader's own comments are recognised by `rid` = hash of their stored public key. Phone layout with a
    Comments tab. Verify: `pnpm exec vitest run viewer/src`, `pnpm check`.
13. **Owner side of reader comments** (medium; 11). [src/lib/components/ the comments panel ("(reader)"
    caption, no new rights), PublishDialog.svelte (the comments switch, already drawn in 8: wire the help
    text), docs none] Verify: `pnpm check`, any touched Vitest file.
13b. **Approve before other readers see it** (careful; 11, 13; optional, question 6).
    [`PublicationSelection.moderate: bool = False`; a `hidden_at`/`approved` state on comments (sqlite.py
    comments table + migration, models/comment.py `Comment`, the SyncSession comment rows), comments.py
    `approve`/`hide` with the owner's rights, routes/comments.py two routes, `threads_for_readers` filter
    (unapproved roots and their replies stay out while `moderate` is on; the author still sees their own
    pending comment, marked "waiting for the owner", through a per-reader note in THREADS keyed by `rid`),
    the comments panel's Approve/Hide buttons, PublishDialog switch; tests in test_comments.py and
    test_live_publications.py] Verify: those test files, ruff, `pnpm check`, `pnpm gen:api`.
14. **e2e M2** (careful; 11-13). [tests-e2e/publish-live.spec.ts (extend)] A reader types a name, comments on
    a decision and on the summary text; a second reader page sees the thread after THREADS but cannot edit it
    (no control shown; a forged edit is not attempted in e2e, Vitest covers it); the owner's app shows both
    threads with "(reader)"; the owner replies; the reader's page shows the reply; the owner deletes the
    reader's comment → "removed" on both readers' pages; comments switched off → the composer disappears.
    Done when it passes in CI.

### M3 docs and close

15. **Docs** (light; 10, 14). [docs/publishing.md ("Kept up to date", "Comments from readers", what the host
    sees: room ids and frame sizes, "What readers keep"), docs/api-reference.md (the selection fields,
    `Publication.live`, `X-Pub-Room`, `PUT /pub/{id}/room`, the pub-live frames), docs/architecture.md
    (one paragraph under Publishing), deploy/publish/README.md and deploy/ours/README.md (the host must be
    rebuilt for the pointer route; note the hourly viewer pull already covers the page), CLAUDE.md (one
    roadmap paragraph), this file (status + "changed while building")]
16. **Close** (light; 15). [backend/mnemosyne/api/static/viewer/ (`pnpm build:viewer`),
    backend/mnemosyne/api/static/guest/ (`pnpm build:guest`), `pnpm gen:api` once more] Push; watch the
    `viewer`, `backend`, `e2e` and publish-host jobs; before the release, redeploy our host
    (deploy/ours) so `notes.petty.services` answers the pointer route, or the first users see
    "This host cannot push updates" until it does.

### M4 later (not scheduled)

- "This computer" publications live: a `/live/{room}` WebSocket on the backend (both listeners) proxying to
  the rooms sidecar's loopback host (link/src/host.rs, as iroh guest rooms use it), the pointer served from
  `meta.json`, and the same bridge; or iroh links for publications (format 2 in the viewer plus guest-wasm).
- Notes anchors for reader comments: a sidecar call mapping plain-text offsets of the last fold to element
  ids, then `notes_ids` and the server-side mark as for web readers.
- Approve-before-show moderation; per-reader revocation (needs per-reader keys); reader presence by name.
- Extract a `RoomConnection` shared by guest_notes.Bridge and PubBridge (C11).

## Files shared across tasks (sequence, never parallel)

- guest_format.py, publication_format.py, pub_live_format.py: 1 only. publishing.py: 4 then 6. sqlite.py:
  4 then 13b. models/sync_session.py: 4 only. live_publications.py: 5 writes the watcher half, 6 the bridge
  half (one worker for both if they run close together), 11 extends, 13b filters. models/publication.py: 4
  then 13b. context.py: 5 only. records.py: 4 only. comments.py, routes/comments.py, models/comment.py: 13b
  only. viewer/src/load.ts, Viewer.svelte: 7 only; Meeting.svelte: 7 then 12; live.ts: 7 then 12.
  src/lib/sealed/*, src/lib/rooms/*, package.json: 2 only. PublishDialog.svelte: 8 then 13 then 13b.
  publish-host/src: 3 only. guest/src/room.ts, format.ts: 2 only. tests-e2e/publish-live.spec.ts: 10 then 14.
- Generated: schema.d.ts (4 and 13b: `pnpm gen:api`), static/viewer and static/guest (16).

## What only CI can check

The viewer and guest bundle diffs (`viewer` job), the e2e (publish host on 8028, Chromium and WebKit), the full
backend suite (locally: single files), the publish-host and link crates' full test runs are fine locally
(`cargo test` per crate) but the sidecar build is CI's for the e2e.

## Risks

- A reader whose page is open sees text change under them (asked for; the status line says when). A reader
  who copied an old version keeps it: nothing revokes what was read, as docs/publishing.md already says.
- `follow` republishes whatever the meeting becomes within the selection: a wrong speaker rename or a hasty
  summary edit is public within ~15 s. The dialog's help text says so; Unpublish and the per-publication switch
  are the remedies; and "this computer" publications never auto-publish to a host.
- The room's life depends on the bridge's heartbeat: an owner's computer off for a day loses the room; readers
  then poll (a minute's latency) until the bridge makes a new one. Acceptable; M4's pointer-in-manifest-poll is
  how they find it.
- Rooms on our host count against the publisher's write budget and the host's 64 connections per room: a
  publication read by more than 63 people at once leaves the rest on the poll (4429 "full" is retried with
  backoff by the client; the viewer must treat `full` as "poll only", not as an error).
- Reader names are self-declared, like guests'; the owner's app says "(reader)" and docs say nothing proves
  them.
- Moving room.ts/format.ts changes the guest bundle bytes: the CI `viewer` job must see a rebuilt
  static/guest (task 16), or it goes red for a reason unrelated to the feature.
- `changes_since(seq, limit=200)` (sqlite.py l.4551) returns `([(id, sync_seq, sync_vv)], next seq, more)`
  and already leaves `shared_from` meetings out; the watcher pages with `more`. `keep_updated` touches (sharing
  changes) do move the seq: the digest check makes them harmless.
- WebKit and `connect-src 'self'` for `wss:` (C14): the e2e's WebKit run is the check; the fallback is one
  header string on two servers.
- The owner's private keys sit on the publications row: a records or diagnostics export that dumps rows
  whole would leak them; `publications_record` is the only reader today and task 4 asserts it skips them.
  Library sync carries them between the owner's machines as it carries `key` and `token` (already secrets).
- A reader's identity is a localStorage key: a cleared browser or another device is a new reader who cannot
  edit the old comments (the owner can delete them). Private-mode browsers lose it on close. Said in the name
  card, as the guest page says a copy stays in the browser.
- `@noble/curves` adds about 35 KB to the viewer and is a third-party dependency in a page whose trust note
  (docs/publishing.md) rests on reproducible builds: pin exactly, commit the lockfile, and the CI viewer
  diff keeps it honest. WebCrypto's curves could replace it once every target has them (Chromium ≥ 137,
  Firefox ≥ 130, Safari ≥ 17): the vectors make that swap a one-file change.
- Readers still see that COMMENT frames exist and their padded sizes (C17); the bridge's THREADS snapshot
  prunes the log (`X-Room-Since`), so the window is short.

## Changed while building

- Task 2: `@noble/curves` pinned to 1.9.7 (2025-08-15, the newest 1.x; 2.x changes the import paths), a direct
  dependency; it brings `@noble/hashes` 1.8.0. Signatures are verified strictly (`zip215: false`), as
  `cryptography` does. vitest.config.ts `include` gained `src/lib/rooms/**` and `src/lib/sealed/**`. A throwaway
  check matched every value of publication_live_v1.json; task 9 makes it a permanent test.
- Task 2 note for task 7: the viewer's Vite config has no `$lib` alias (the guest's has
  `$lib: here('../src/lib')`); task 7 adds the same alias to viewer/vite.config.ts.
- Task 5: the watcher also acts on a meeting whose first unpublished change is `MAX_WAIT` = 60 s old, quiet or not
  (still subject to GAP, the backoff and the active-job check), so steady typing in the live notes cannot keep
  readers waiting until everyone stops.
- Task 12 (viewer comments UI), decided while building:
  - The pure helpers moved to src/lib/comments/pure.ts, generic over the comment and op shapes
    (`BaseComment`, `BaseOp`, `PendingOps<O, P>` with an optional "reflected" test, `withPendingBy`
    with a `fresh` callback, `findComment`; `orderThreads(threads, keepGone)`). guest/src/comments.ts
    re-exports them and keeps its guest-typed parts (`GuestOp` builders, `isMine`, `openMarkIds`, a
    `PendingOps` subclass and `withPending` over `withPendingBy`); the guest tests are untouched.
  - COMMENT op: always exactly the keys `op, id, parent_id, text, name, at, anchor`. `create`:
    `parent_id: null`, `anchor` with all six keys (`target, target_id, quote_id, exact, prefix,
    suffix`), null where unused (the vectors' op is produced byte for byte). `reply`: `parent_id` =
    the root's id, `anchor: null`. `edit`: the new text, `parent_id: null`, `anchor: null`. `delete`:
    `text: null`, `parent_id: null`, `anchor: null`. `name` = the reader's current name on every op;
    `at` = `Date.toISOString()` (milliseconds, `Z`). Texts are trimmed; 1-4,000 code points.
  - Anchors: meeting = target `""`; decision/question/item = that target + `target_id`; a quote =
    target `"quote"` + `quote_id` (target_id null; the bridge maps it to the transcript lines); a
    summary passage = target `"body"`, `exact` = the selection without outer whitespace, `prefix` /
    `suffix` = up to 32 characters of the RENDERED summary's text (the Markdown component's
    `textContent`, not `plain_markdown`), `""` at the edges.
  - THREADS anchors are placed by `quote_id` first (whatever the target, e.g. `segment`), then
    decision/question/item + `target_id`, then `body`; anything else is the meeting (no "On:" line).
    The "On:" line shows the manifest's current text of the item or quote, else `anchor.quote`
    (so the bridge should send the exact text as `quote` for body threads).
  - Reader storage: `mnemosyne.reader.<pub id>.name` and `mnemosyne.reader.<pub id>.key` (the
    Ed25519 seed as 64 hex); the key is made at the first op. Retry re-boxes the SAME op (same id
    and `at`). An edit counts as seen when THREADS has its text OR another `updated_at` than when it
    was made (the owner's redaction may change the text). A thread deleted through stays shown as
    "removed" in the viewer (the guest page drops it), as e2e 14 expects.
  - With comments off there are no affordances at all ("n comments" included); read-only threads
    stay in the column. "n comments" counts the non-deleted comments (pending included) of the
    threads on that part ("1 comment"); the phone tab's count is open threads. A focused thread
    flashes the guest's `border-yellow-500/70` for 1.8 s.
  - Layout: with the column, `main` widens to `lg:max-w-[70rem]` so the meeting keeps its 3xl width;
    the meeting is never re-mounted when the column appears. Composer: Enter is a newline,
    Ctrl/Cmd+Enter sends, Escape drops the anchor; Cancel shows only when anchored.
  - The name card says only "Your name, shown with your comments" (the brief); the Risks line about
    saying that a cleared browser is a new reader is not in it.
- Task 6: the bridge sets the pointer (`PUT /pub/{id}/room`) BEFORE creating the room, so an older host gets no
  room at all; an older host answers that route 400 `bad_sha256` (it reads "room" as a blob hash), counted as
  unsupported like a 404. The room id and the last applied seq live in `<pub_dir>/room.json` (no schema change).
  The one-shot push (C12) joins with `since = 2**53-1`, sends the signed VERSION, waits up to 15 s for the ack and
  never creates a room. A fresh bridge also sends the current VERSION after joining (readers ignore a version
  that is not newer). Snapshots prune the log at most every 10 minutes and only after new frames.
- Task 11 (comment intake and threads out), decided while building:
  - Replay state lives in `<pub_dir>/readers.json` (no schema change): the `at` of the last op applied per comment
    id, and a stub (id, parent, author, rid, anchor without quote, times) of every comment readers were shown. A
    create/reply with a known id, or an edit/delete whose `at` is not newer, is a no-op; only applied ops count
    toward the 20-per-10-minutes cap (a copied frame cannot eat another reader's budget), measured by the bridge's
    clock at receipt. A COMMENT envelope binds the room id, so a new room cannot be fed an old room's frames.
  - A comment readers were shown that is gone from the meeting (the store hard-deletes a comment nothing replies
    to) goes out as a "removed" stand-in from that stub (`deleted: true`, text "", quote null), so e2e 14 sees
    "removed" rather than a vanished thread. Threads hidden by the selection (an unticked part) just go.
  - THREADS anchors: a quote is only ever the manifest's own text (an item's current redacted text, a quote's text
    by `quote_id`; a segment thread outside every published quote is dropped); a body thread's quote is the
    reader's exact through the text rules, null once detached; replies carry `{target: "", target_id: null,
    quote_id: null, quote: null}`; roots have `parent_id: null`; times are UTC ISO with milliseconds and `Z`.
  - Reader text and names go out as typed; owner-side comments through Names and the identifier rules, the author
    "Owner" when it is the desktop's "You"/"Me" default (else `Names.name`). A reader's reply must name a thread
    `threads_for_readers` shows. Item anchors must name an id the manifest shows; one comments.py cannot place (a
    fallback `d0` id) is kept on the meeting with "On: “…”" like an unfound body exact. THREADS is re-sealed
    (2 s debounce) on the meeting's `comments` events and on any selection change, and is skipped when the threads
    equal the last sent (an owner-only thread changes nothing readers see). Oldest threads are left out past
    1.5 MB. comments.py is unchanged (`BadComment` is caught in the bridge).
  - One redaction for the manifest and the threads: `publishing.Redaction(session, sel)` (people, wanted, `names`,
    `text()`), extracted from `build_content`, which uses it; `threads_for_readers` uses the same class.
- Task 13b (approve before other readers see it), decided while building:
  - State on a comment: `approved_at` and `hidden_at` (ISO, both nullable; columns added by `_migrate_columns`, the
    sync rows `SyncComment` carry them, so the generic three-way merge treats them as scalars; the records export
    carries them with the rest of the row). `comments.approve` sets `approved_at` and clears `hidden_at`;
    `comments.hide` sets `hidden_at` and keeps `approved_at` (hidden wins when both are set, which is also what a
    sync merge of an approve here and a hide there gives). Owner rights only: `_Meeting.can_moderate()` = `owns`
    (owner, admin, the desktop); a reader or guest acting as an explicit actor, and a meeting shared with me, never.
    `Comment.can_moderate` tells the UI. History lines `comment_approved` / `comment_hidden`; both announce a
    `comments` event, so THREADS follows. Routes `POST /api/sessions/{id}/comments/{cid}/approve|hide`.
  - `PublicationSelection.moderate` (default off) and `moderated_since`. The rule for older comments: the server
    stamps `moderated_since` at publish (`publishing.stamp_moderation`): None while `moderate` is off, the moment it
    went on, kept across later versions while it stays on; the caller's value is ignored. A reader's comment whose
    `updated_at` is before it counts as approved. `updated_at`, not `created_at`: a reader's edit after the switch is
    a new text (the bridge also clears `approved_at` on a reader's edit while `moderate` is on), so it waits again.
    Off and on again re-stamps (comments made while it was off were shown, so they stay shown). Without a date
    (a selection written by hand) nothing is grandfathered.
  - `threads_for_readers` leaves out, always: a thread whose first comment is hidden, a hidden reply (alone). While
    `comments` and `moderate` are on: a reader's first comment without approval (the whole thread, the owner's
    replies included), and a reader's reply without approval. The owner's replies need none. A change of the
    selection (so `moderate`) already re-sends THREADS (`update_row` -> `threads_soon`, from every publish).
  - THREADS has a new field `pending`: `{rid: [comment id, ...]}`, ids only, for the reader's own roots and replies
    waiting for approval, in threads that would show otherwise (not deleted, not hidden, not on an unpublished part);
    at most 50 per reader and 2,000 in all. `{}` when nothing waits (always present). A reader may not reply to a
    thread it is not shown (so not to its own waiting comment). The frame is re-sent when `pending` changes too.
  - Viewer: `Follower.threads.pending` (`parsePending`, tolerant: an older owner has none). `HeldComments`
    (`mnemosyne.reader.<pub id>.held`, one JSON entry by comment id, memory fallback, newest 50) keeps the words of
    every create, reply and edit the reader sends. A comment listed under the reader's rid is "Waiting for the owner"
    (`text-xs text-gray-500`), shown from the kept text, and its sent op counts as seen (no "Sending…"/"Not sent");
    a THREADS that shows the comment, or no longer lists it while no op for it is in flight, drops the kept text;
    nothing is dropped before a THREADS has come and the rid is known. No kept text, nothing shown. Waiting comments
    have no Reply/Edit/Delete in the viewer (v1).
  - Owner UI: `moderate` follows `comments` in `toSend()`; PublishDialog's indented checkbox as decided.
    CommentThread shows "waiting for your approval" / "hidden from readers" in the "From the published link" line and
    Approve / Hide / "Show to readers" in the action row; it uses the open meeting's publication from the publishing
    store (`moderate` on, and the comment newer than `moderated_since`), and when that is not loaded it offers Approve
    on every reader comment that is not approved. Beyond the decided UI: reader REPLIES get the same caption and
    buttons (they need approval too), and Hide is offered on a reader's visible comment whenever readers may comment
    (the kill switch, `moderate` or not).
- Unpublish order reversed from C13 (found by the M1 e2e): the publication is deleted on the host FIRST, then its
  room. A reader that hears the room close (4410) re-reads `/pub/{id}`; with the room gone first it still found the
  publication and fell back to polling, so "No longer published" took up to a minute.
- Approve-before-show (13b) was built in this round, so the "no approve queue in v1" bullet above no longer holds.
