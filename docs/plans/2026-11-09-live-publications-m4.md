# Live publications M4: notes anchors, per-reader revocation, RoomConnection, "this computer" live

Status: plan, 2026-11-09, against main at abb1d5a = Release 0.30.0. Nothing built yet.

0.30.0 (2026-11-06) shipped docs/plans/2026-11-05-live-publications.md whole, 13b included; its "M4 later (not
scheduled)" list is this milestone, plus what Corey asked for on 2026-11-09. Every statement below about the
code was checked against it; files and lines are named (line numbers are those of abb1d5a).

The four, all to be built:

1. **Reader comments anchored on the published notes.** Today a reader comments on decisions, questions,
   action items, quotes, the summary text and the meeting as a whole (live_publications.py `OP_TARGETS` l.428:
   `"", body, decision, question, item, quote`); `_Shown.anchor` l.460 returns None for `notes` ("the notes,
   and anything readers cannot write on", l.494). The M4 sketch was "a sidecar call mapping plain-text offsets
   of the last fold to element ids, then `notes_ids` and the server-side mark as for web readers". The sketch
   holds in its second half and is wrong in its first: see C1-C4.
2. **Per-reader revocation.** The owner removes one reader (by `rid`) without changing the link. What that can
   mean when every reader holds the frames key is worked out in C7-C9 and said plainly in "What blocking does
   and does not do".
3. **A shared `RoomConnection`** for guest_notes.Bridge and PubBridge (old C11). Behaviour-preserving; the
   tests that pin it are listed in task 3.
4. **Live updates and comments for "this computer" publications.** Two routes were sketched; both are weighed
   in "Item 4"; the backend proxy is recommended, with what it does and does not reach said in one sentence.

## The design in one paragraph

Nothing in M4 changes what a publication IS (a redacted, encrypted snapshot per version, FORMAT 1, the key in
the link) or who holds which key (the link key for every reader, the owner's Ed25519/X25519 pair on the
publications row, a per-browser reader key behind `rid`). (1) A reader's notes comment is an ordinary `body`-style
quote anchor (`exact`/`prefix`/`suffix` of the RENDERED, redacted notes) that the bridge places itself: it
locates the passage in the current fold's plain text by aligning the redacted rendering with the document's
text (one `SequenceMatcher`, verified by re-redacting the landing span), then asks the sidecar to put the
`comment:<id>` mark on that plain-text range in ONE run with the fold it located against (a new `notes doc` op
`mark_comment_at`, positions instead of element ids: notes-core's `mark_comment` already turns ids into
positions before marking, doc.rs l.533-554), under the meeting's notes lock so no frame moves the text between
the fold and the mark; THREADS then carries the thread with `target: "notes"` and the marked text (redacted) as
its quote, and the viewer places it in its notes box exactly as it places `body` threads in the summary.
(2) Blocking a reader is a list of `rid`s on the publications row: the bridge drops every op from a blocked
`rid` before anything else and tells readers' pages through THREADS (`blocked`, ids only) so the composer
disappears; existing comments are hidden with 13b's `hide` when the owner asks for it. It stops that identity
from writing; it cannot stop anyone from reading, and a cleared browser is a new reader: the docs and the
dialog say so, and "Replace the link" (unpublish + publish with the same selection, one button) is the only
real revocation. (3) `RoomConnection` (services/room_connection.py) owns what both bridges' `_connect_loop`s
do identically today (guest_notes.py l.901-1005, live_publications.py l.1372-1463: dial, join, welcome, ping,
ack/seq, count, error memo, pump with unacked, backoff and close-code classification); each bridge keeps its
own hooks (what a 4410 means, what goes out at join, where `seq` is persisted). (4) A "this computer"
publication gets its room on the rooms sidecar's embedded publish host (link/src/host.rs: a full publish-host on
127.0.0.1, already started on demand for iroh guest rooms), the backend serves the pointer from the
publication's `meta.json` on its own `GET /pub/{id}` and proxies `GET /live/{room}` (both listeners) to that
host, only for rooms its live local publications point at; the viewer is unchanged (same-origin `wss://`, the
pointer header it already reads). Readers reach it exactly where they reach the link today (`local_reach`:
the team listener or `phone_url`); nothing here makes a "this computer" link reachable from outside without
one of those or a publish host (iroh links would; weighed and deferred).

## Corrections to the sketch and to the obvious design (from the code)

- **C1 The manifest's notes are not the fold's plain text, and "offsets of the last fold" is the wrong
  starting point.** `build_content` (publishing.py l.342-345) publishes `_notes_text(_own_notes(...))`:
  `own_notes` (notes_sections.own_notes, the document minus the backend's `sec:`/`sum:`/`cp:` sections,
  l.213-218) → `plain_notes` (chips flattened to their text, notes_chips.py l.283) → image lines kept as
  `![caption](mnemosyne:image/...)` or `[image: caption]` (l.439-465) → `Redaction.text` (identifiers, then
  names, l.286-309) → the viewer's `prepareNotes` (viewer/src/notes.ts l.35) → the Markdown component → the
  reader selects in `textContent`. A reader therefore has offsets into a text that differs from the fold's
  `plain` (sqlite.py `notes_marks` l.2551: the sidecar's `plain`, every line of the document, names as they
  are, chips as their text, U+001F/`\t` between cells) by: missing section lines, replaced names and
  identifiers, image syntax, markdown markers the renderer drops. The mapping must therefore be computed by the
  bridge from the reader's `exact`/`prefix`/`suffix`, not sent by the viewer as offsets, and it must tolerate
  the redaction: a passage with a replaced name cannot be found by text in the real document (the same problem
  C8 of the old plan solved for `body` with the "On: …" fallback; here it is solved properly, with alignment,
  and the fallback stays for what alignment cannot place).
- **C2 "then `notes_ids`" is what web readers do, and the bridge has no element ids.** `comments.create_marked`
  (comments.py l.628-644) takes `req.notes_ids` = two element ids the EDITOR knows (`seqIdAt`) and calls
  `shared_notes.mark_comment(first, last)` (l.819), whose sidecar op `mark_comment` takes ids
  (notes-core/src/api.rs l.26-31) and turns them into positions with `position_of` before `mark_inline(start,
  end + 1)` (doc.rs l.539-553). The viewer has no document and no ids. Two honest options: a sidecar op
  `ids_at` (positions → ids) and then the existing `mark_comment`, two runs; or `mark_comment_at` (positions in,
  the same `mark_inline`), one run. The second is a ten-line change in api.rs/doc.rs, and `plain` offsets are
  exactly seq positions: `read_marks` pushes one model char per seq char (doc.rs l.382-440) and
  `render::plain` only maps CELL to `\t` (render.rs l.35-39), so the `comments` ranges the backend already
  stores (`marks_of`, shared_notes.py l.312) and the new op's `start`/`end` are in one space. Recommended:
  `mark_comment_at`.
- **C3 The fold the offsets came from must be the fold that is marked.** `notes_marks` is the LAST fold; frames
  appended since (`notes_catchup`, sqlite.py l.2367) move positions. `mark_comment_blocking` (shared_notes.py
  l.557-601) folds `_state` (snapshot + unfolded frames) inside `hub(app).lock(session_id)`, and every frame
  append takes that lock, so the right shape is one blocking function that, under the lock, runs `fold` to
  get the current `plain`, calls a Python `locate(plain)` callback, and runs `mark_comment_at` against the
  same snapshot and frames: two sidecar runs, zero races. `shared_notes.mark_comment_located_blocking` (new),
  beside `mark_comment_blocking`, which keeps `_store_written` (l.523) so the ranges are stored and the frames
  broadcast (`_deliver`, l.838).
- **C4 The notes are shared with people who are not readers.** A `comment:<id>` mark travels in the document's
  frames to every notes client: guests (guest_notes.Bridge seals every owner-side frame out, l.1115 area) and
  phase-8 recipients. `threads_for_guests` (guest_notes.py l.577-585) sends EVERY thread whose target is
  `notes`, so a reader's notes comment reaches the meeting's guests as a comment by "<name> (reader)", as a
  guest's reaches readers today only through the owner's filtered THREADS (reader-started threads only,
  `_reader_view` l.556). This is the existing rule "reader comments are ordinary comments" (docs/publishing.md
  "Comments from readers"); it is stated in the docs for the notes case too. Hidden (`hidden_at`) comments:
  `threads_for_guests` does not filter them today; not changed here (out of scope, noted as a risk).
- **C5 `comments.create` refuses `notes` without the sidecar, with the wrong exception for the bridge.**
  `create` raises `NotesNotShared` (a RuntimeError, comments.py l.76, l.589) when `shared_notes.available()` is
  false; `_take` catches `BadComment`, `CommentNotFound`, `Forbidden`, `Hidden`, `ValueError` (l.1614-1620), so
  it would log "a reader's frame failed" (l.1559) and drop the op silently. The bridge checks
  `shared_notes.available()` itself (off the loop the first time, l.141-150) and, without it, keeps the thread
  on the meeting with "On: …" as the `body` fallback does (`_on_meeting`, l.684). META says whether notes can
  be commented on, so the viewer never offers what the owner cannot take (C6).
- **C6 An older owner does not know `target: "notes"`.** `_parse_op` returns None for a target outside
  `OP_TARGETS` (l.667) → "bad", dropped without a word. The viewer must know before offering: META gains
  `notes: bool` (comments on, `sel.notes`, notes published non-empty, sidecar available). `MetaFrame` is
  `extra="forbid"` with exactly `comments, follow, at` (l.400-407): the field is added there, the frame key-set
  assertion in test_live_publications.py is updated, and the viewer's `onMeta` (live.ts l.418) reads it as
  optional (an older owner: false). publication_live_v1.json's META sample stays valid (a frame without the
  field is still one an older owner sends; the vectors are not regenerated).
- **C7 A room proves nothing about who a reader is, and `rid` is self-made.** `rid` is the hash of a key the
  reader's browser generated (viewer: `mnemosyne.reader.<pub id>.key`, old plan task 12); nothing binds it to a
  person. Blocking a `rid` blocks a browser profile. A reader who clears storage, or opens another browser, is
  a new `rid` with a fresh `RATE_OPS` budget (20 per 10 minutes, l.424-425, per `rid`, `_rate_ok` l.1669). So
  blocking is moderation, not access control, and a per-publication cap is needed beside the per-rid one, or
  a determined reader mints `rid`s faster than the owner blocks them (C9).
- **C8 Nothing can stop a reader from reading.** Every version is fetched from `GET /pub/{id}` by anyone with
  the link (no identity on that path, app.rs; routes/publishing.py l.262-284); the room token is HKDF of the link
  key (pub_live_format.py `room_token` l.201), so every reader joins the room and hears VERSION, THREADS and
  META; the frames key is HKDF of the link key too, so every reader opens them. A blocked reader therefore
  still sees every new version and every thread the other readers see. The only revocation of READING is a new
  link: a new publication id and key (`publishing.publish` makes both on a first publish, l.1037-1296) and the
  old one 410 ("Revoking, honestly", docs/publishing.md l.82). That is today's Unpublish + Publish; a button
  that does both with the stored selection is cheap and is offered as task 7.
- **C9 Where the block list lives.** Not in `PublicationSelection`: the dialog sends the WHOLE selection on
  every Update (PublishDialog → `PUT /api/sessions/{id}/publication`, routes/publishing.py l.120-140), so a
  block made from the comments panel between two dialog openings would be overwritten by the dialog's stale
  copy. Not in `<pub_dir>/readers.json` alone (l.726-756): that file is the origin machine's and the other
  computer of a library-sync pair could neither see nor make a block. A new publications column
  `blocked_readers` (JSON: `{rid: {"at": iso, "on": bool}}`, newest `at` wins per rid, so block and unblock both
  travel and a union merge is right), migrated in `_migrate_columns` as `sign_key`/`box_key` were (sqlite.py
  l.917), in `_PUB_COLS` (l.2125), carried by `SyncPublication` (models/sync_session.py l.115-137) and merged in
  `merge_publications` per rid, exposed in the API `Publication` as `blocked_readers: list[BlockedReader]` (rid,
  name last seen, at) and never in the manifest, `publications.json` or history details (the `rid` itself is
  harmless, but the rule "the row's live columns stay off every export" is simpler to test than exceptions).
- **C10 The two connect loops are the same loop with different edges.** Identical: the `websockets` dial with
  `max_size`, `compression=None`, `open_timeout=JOIN_TIMEOUT`, `proxy=None` on loopback (guest_notes l.917-924,
  live_publications l.1397-1404); the `join` message and `welcome` wait (l.927-935 / l.1406-1410); `ping`→`pong`
  (l.1041-1043 / l.1503-1505); `ack` → `unacked.popleft()` + `_advance(seq)` (l.1053-1056 / l.1513-1517);
  `count` (l.1057 / l.1518); `error` → `_last_error` (l.1063-1066 / l.1522-1524); the binary tag dispatch with
  `seq` in bytes 1-9 (l.1069-1087 / l.1528-1547); the pump that appends to `unacked` and sends `tag || env`
  (l.1007-1015 / l.1478-1485); `ConnectionClosed`/`InvalidStatus`/network exception mapping (l.950-966 /
  l.1428-1439); `CLOSE_TOO_LARGE` + `log_full` → snapshot then rejoin; `CLOSE_LAGGED` → rejoin at once;
  `CLOSE_FULL` → retry (l.976-996 / l.1452-1461); the backoff table walk. Different, and staying in each bridge:
  what a `welcome.ended` means (guest: `_host_ended`, stop; pub: lose the room); what 4410/4401/4403/404/410
  mean (guest: `_host_deleted` and stop, pub: `_lose_room` and a new room); dropping the largest unacked frame
  on 4413 (guest only, l.985-993); presence queue dropping (guest, `_drop_queued_presence`); what is queued at
  join (pub: META and the newest VERSION, `_joined` l.1465) and cleared at disconnect (pub: everything,
  `_disconnected` l.1471; guest: notes frames kept, presence dropped); where `seq` is persisted (guest: the
  row's seq column via `repo.set_guest_room`, l.1089-1094; pub: `room.json`, `_advance` l.1685); how an
  outgoing frame is made (guest: pre-sealed envelope in the queue; pub: a `_Frame` model signed at send time,
  `_seal` l.1487); the room lookup before dialing (pub only, `_ensure_room`). The extraction takes the first
  list, parameterises `MAX_MESSAGE` and the backoff table, and exposes the second as hooks.
- **C11 The rooms sidecar's embedded host is a full publish host with the pointer route, on loopback.**
  link/src/host.rs `start_loopback_host` builds `mnemosyne_publish_host::app::router` with
  `cfg.upload_token = Some(...)`, `trust_proxy = false`, rate limits off (l.33-45), on `127.0.0.1:0`;
  `RoomsLink.base_url` (services/link.py l.316-319) is its address while the sidecar runs, and
  `RoomsLink.upload_token` (l.312-314) the `X-Upload-Token` room creation needs (app.rs `create_room` l.585-614
  via `auth_of`; guest_notes `Link.upload` l.339-343 already sends it for loopback rooms). So a "this
  computer" publication's room can be an ordinary room there, created and extended by exactly the calls
  PubBridge makes to a remote host (`_put_room` l.1328-1347), with two differences: the upload token is the
  sidecar's (publishing `_host` l.683-686 only adds the configured publish host's), and the pointer is not kept
  by the host (the backend serves the publication, not the embedded host) but in the publication's own
  `meta.json` (`read_meta`/`_write_json`, publishing.py l.578-589), returned as `X-Pub-Room` by the backend's
  `read_manifest` (routes/publishing.py l.262-284) beside `X-Pub-Version` on the 200 and the 304.
- **C12 Readers cannot reach the embedded host, so the backend proxies the socket.** The embedded host
  listens on loopback only; readers come to the backend's origin (the team listener is a second uvicorn on the
  SAME app, team_host.py l.194-254, so a route is on both; `phone_url` is whatever reverse-proxies to the
  backend port). `/live/{room}` is open already: `auth.is_open` (api/auth.py l.56-60) opens everything outside
  `/api/`, `/ws` and `/ws/...`. The route must proxy ONLY rooms that a live local publication of this machine
  points at (the Bridges registry knows them, `_read_room` l.845): the same embedded host also holds this
  machine's iroh guest rooms (`_open_iroh`, guest_notes.py l.1854), whose only protection beyond the room token
  would otherwise be that nobody could reach them. The viewer's CSP `connect-src 'self'` (routes/publishing.py
  l.31) covers a same-origin `wss://` (old C14, confirmed in both e2e browsers).
- **C13 The room's host moves ports; the sidecar stops when idle.** `RoomsLink` listens on a new port after
  each start (guest Bridge computes the URL on every attempt for that reason, l.916); `_maybe_stop_rooms`
  (guest_notes.py l.2299-2316) stops the sidecar when no guest room is served, which would take a local
  publication's room connection down (the room's state survives on disk, `RoomStore::open` rooms.rs l.249-263,
  and the bridge would reconnect when something starts the sidecar again, but nothing would). The local bridge
  resolves the host each attempt, starts the sidecar with `ctx.ensure_rooms()` (context.py l.387-396) and
  `_maybe_stop_rooms` asks `live_publications.local_rooms_in_use(app)` before stopping; `_reserve` (l.2286) need
  not re-serve anything for host rooms. `awake.py` l.46-48 asks `guest_notes.needs_awake`; a live local
  publication keeps the computer awake the same way (readers may be commenting into this computer).
- **C14 `wanted()` and `check_live` hard-code the host.** `wanted` (l.802-812) requires `place == "host"` and a
  `host_url`; `check_live` (publishing.py l.917-929) refuses `follow`/`comments` for `where != "host"` with
  `LIVE_NEEDS_HOST` (l.71); `to_model`'s `_live_fields` (l.868-878) already reports whatever `status()` says.
  Item 4 relaxes both for `place == "local"` and gives `status()` a reason for a local publication that cannot
  go live here (no sidecar binary: "Live updates for this computer's links need mnemosyne-link"; served by
  another computer: today's `served_by`). `expires_days: None` ("never", local only) meets `_room_expiry`
  (l.876-881): the cap of ROOM_DAYS applies, extended every 24 h as today.
- **C15 The e2e can exercise a local live publication without any host.** tests-e2e/publish.spec.ts publishes
  to "this computer" against the demo backend on 8018; the e2e job builds the link sidecar (ci.yml), which is
  the only thing the local route needs; the second page opens `http://127.0.0.1:8018/p/<id>#<key>` and the
  socket is `ws://127.0.0.1:8018/live/<room>`. publish-live.spec.ts (l.13-115) is the template.

## Decisions for Corey (each with a recommendation)

**Answered 2026-11-09:** Corey took every recommendation, with one change: item 4 ("this computer" live, M4.3: tasks 8-11) is dropped for now. Under the connections redesign (2026-11-09-connections-design.md) the team listener becomes the Team server role, so a local link reaches only a tailnet; route B (iroh links) is the one worth doing later. Also in scope: `threads_for_guests` stops sending comments with `hidden_at` (the gap flagged under Risks).

1. **Notes comments: alignment or a second rendering?** Recommended: alignment (the design below). The
   alternative is making every transformation in C1 offset-tracking (`plain_notes`, `_notes_text`,
   `redact_identifiers`, `redact_contacts`, `Names.text`, `own_notes`): six functions in four modules, each
   touched for one feature. The alignment is one pure module with a verification step that makes a wrong
   landing impossible (the span is re-redacted and compared; a mismatch keeps the thread on the meeting with
   "On: …", exactly what `body` does today). Cost of "no": that refactor, ~3 tasks more.
2. **Where a reader's notes thread lands when it cannot be placed.** Recommended: on the meeting as a whole
   with the quote in its text ("On: …"), the `body` rule (l.684-690). Alternative: refuse the op (the reader sees
   "Not sent"). Keeping it is what the old plan decided for the summary; readers' words are never dropped.
3. **Blocking: silent or told?** Recommended: told. THREADS gains `blocked: [rid]` (ids only, like `pending`);
   the blocked reader's page hides the composer and says "The owner has turned off comments for you"; the
   other readers' pages ignore the list (they learn nothing they did not have: every `rid` is in THREADS
   already, l.510). Silent (ops just never land, "Not sent" forever) leaks nothing more but reads as a broken
   page. Cost of "told": one field, one line in the viewer.
4. **Block alone, or block and hide?** Recommended: the dialog offers both, default "Block and hide their
   comments" ticked; hiding uses 13b's `comments.hide` (l.773) on each of that reader's comments, so unblocking
   later does not unhide (the owner shows them again one by one, as today). Alternative: blocking filters that
   reader's threads out of THREADS while blocked (automatic, reversible): simpler for the owner, but then the
   other readers' pages lose threads they may be replying to, and `_reader_view` grows a second hidden rule
   beside `hidden_at`. Explicit hide is one rule.
5. **A per-publication rate cap beside the per-rid one (C7)?** Recommended: yes. `RATE_TOTAL` = 60 applied ops
   per 10 minutes per publication, the 61st and later dropped ("rate"), constants not settings. Without it a
   reader who clears storage after every 20 ops has no cap at all; with `moderate` on they still reach nobody,
   but the owner's panel fills. Cost: ten lines in `_rate_ok`.
6. **"Replace the link" button (C8)?** Recommended: yes, task 7, medium: unpublish then publish with the stored
   selection in one job (history `publication_replaced`, a seal as Publish gets, `SEAL_PUBLISHED`), the
   dialog's "Revoke" help text pointing at it. It is the only thing that ends a blocked reader's READING, and
   the docs' "Revoking, honestly" section already describes doing it by hand. Cost of "no": the per-reader
   block is the only new control and the docs stay as they are.
7. **"This computer" live: the backend proxy (A) or iroh links (B)?** Recommended: A now; B not scheduled (the
   weighing is in Item 4). A is one Python route, a local variant of the room host and the pointer in
   `meta.json`; the viewer does not change. B is a format-2 link for publications (`#2.<id>.<key>.<endpoint>.
   <relay>`), an HTTP-over-iroh request protocol in link/src/guest.rs (today's proxy only carries the room's
   WebSocket, l.391-466), guest-wasm in the viewer (+~1 MB, built only by CI), a viewer page served from a
   static origin for such links (GitHub Pages or our host, as the guest page is), the CSP with relay origins
   (`guest_csp`, routes/publishing.py l.59-73): four heavy tasks and the only route that reaches people outside
   Corey's network without a publish host, through our relay (encrypted bytes only). A reaches whoever
   `local_reach` reaches today, which for Corey's always-on desktop is his team over Tailscale and anyone a
   `phone_url` address (a Tailscale Funnel or Cloudflare tunnel, docs/remote-access.md) lets in.
8. **Should local publications preselect `follow` and `comments` as hosted ones do?** Recommended: yes when
   `local_reach` is `team` or `phone_url`, no (switches shown, off) for `this_device`: the link opens for nobody
   else there, so live is moot. The copy change is one line (below). The parallel settings review may rename
   or re-explain "this computer"; this plan touches that copy as little as it can and lists every string.
9. **Notes comments and `moderate`.** No decision needed: a notes comment is a reader comment like any other
   and 13b's rules apply unchanged (`unapproved`, `pending`).

## Item 1: reader comments on the published notes

**What the reader sends.** `OP_TARGETS` gains `"notes"`; the op's anchor is the fixed six-key shape (old plan,
task 12) with `target: "notes"`, `exact` = the selected text of the notes box without outer whitespace,
`prefix`/`suffix` up to 32 characters of the notes box's `textContent` (viewer: `passageAnchor`, comments.ts
l.156-172, generalised to take the target; `readSelection` in Meeting.svelte l.75-102 made to watch two boxes,
`summaryBox` and `notesBox` l.58). `_parse_op` (l.637-676) accepts the target with the same limits as `body`
(`exact` ≤ MAX_TEXT, prefix/suffix ≤ MAX_CONTEXT).

**Where it goes (the bridge, `_create` l.1639-1656).** `_placed` (l.693-723) returns, for `notes`, a
`CommentCreate(target="notes", exact=a["exact"], text=...)` plus the quote, when `sel.notes` and
`shown.content["notes"]` is non-empty, else None (not on a part of the publication). `_create` then calls a new
`_create_on_notes(session, op, req, actor)`:

1. `comments.create(app, sid, req, comment_id=op.id, actor=actor)` as for web readers: `_anchor` (comments.py
   l.196-222) stores `{"mark": "comment:<id>"}` and `quote = exact`; the comment exists, detached until marked.
2. `shared_notes.mark_comment_located_blocking(app, sid, op.id, locate, author_id, author)` off the event loop
   (C3): under the notes lock, `fold` → `plain`; `locate(plain)`; `mark_comment_at` (C2) with the same
   snapshot and frames; frames appended as the reader (`author_id = "reader:<rid>"`, so the frame log and the
   presence of a mark say who, as guests' do), `_store_written`, `_deliver`. When `locate` returns None, or the
   sidecar errors (unknown range, no binary), the comment stays as it is: detached, with its quote, and
   `_Shown.anchor` still sends it to readers with `quote` (so THREADS shows it; the viewer places it by text or
   on the meeting). Nothing falls back to "On: …" here: the thread already has the right target and quote.
   (Decision 2 covers only the case where `sel.notes` is off or `create` itself refuses.)
3. `comments._publish` runs inside `create`; the `comments` bus event brings THREADS (`threads_soon`).

**`locate(plain)` (new pure module services/notes_anchor.py, Decision 1).** Inputs: the fold's `plain` (seq
positions, C2), the session, the selection, `Redaction` (publishing.py l.286), the reader's anchor. Steps:

- R = the rendered redacted notes as the reader saw them: `plain_markdown(_notes_text(own_notes, sel, text)[0])`
  with image lines rendered as the viewer renders them (`imageText(caption)` in viewer/src/notes.ts l.70 has a
  Python twin here; one test pins the two strings). `locate_body` (comments.py l.306-355) finds `exact` in R
  with prefix/suffix and the fuzzy fallback: it already takes the text as a parameter, so it is called on R
  as it is called on the summary (its `plain_markdown` on an already-plain R is idempotent: a test).
- P = `plain` with sections cut out: `notes_sections.split` gives the document lines that are the backend's
  (`sec:` refs, from `repo.notes_refs`, sqlite.py l.2568); their lines are blanked (replaced by empty lines of
  the same count, so positions elsewhere do not move). Then `SequenceMatcher(None, text(P_plain_notes), R)`
  where `text()` is the redaction applied to P line by line (the names and identifier rules do not cross
  lines): equal opcodes map 1:1, replace opcodes map a position proportionally inside the block and clamp to
  its edges. Map R's `[start, end)` back to P (= `plain`) positions; widen to the enclosing token boundaries
  when a mapping falls inside a replaced name ("Person 2" ↔ "Ada Lovelace").
- Verify: `_norm(text(plain[s:e]))` against `_norm(exact)` with `_fragment_ratio` ≥ `FUZZY` (comments.py) → the
  span; else None (detached, as above). The verification is what makes a wrong alignment harmless.
- Caps: notes up to 1,000,000 characters are aligned (the `shared_notes` doc cap is larger, but a THREADS and a
  COMMENT are tiny: the cost is the matcher, O(n·m) worst case; over the cap → None, detached).

**What readers get back (THREADS).** `_Shown.anchor` gives `{target: "notes", target_id: None, quote_id: None,
quote}` where `quote` = the marked text as the publication shows it: `place_notes(comment_id, root.quote,
marks)` (comments.py l.371-389) gives the current range in the stored `plain`; the bridge takes
`text(plain[s:e])` (redacted) when the range exists and the root is not deleted or detached, else `text(root.quote)`
as `body` does (l.481). `changed`/`detached` ride as today (`_Shown.comment` l.496). `sel.notes` off → None
(hidden from readers), as `body` is hidden when `sel.summary` is off.

**The viewer.** `anchorKey` (comments.ts l.179-185) maps `target: "notes"` to `"notes"`; Meeting.svelte places
notes threads in the notes section the way `body` threads sit in the summary (the "On: …" line from `quote`,
`anchorLine` l.188-198 falls back to `a.quote`); the comment affordance and count on the notes section appear
only when `meta.notes` is true (C6); with comments on but `meta.notes` false (an older owner, no sidecar, notes
not published), nothing is offered on the notes. Phone layout: the Comments tab lists them like the rest.

**Security.** Who holds what does not change: the op is boxed to the owner's X25519 key and signed by the
reader's key as every COMMENT is; the mark is authored on the owner's machine by the sidecar; a reader never
touches the document (no frames key for the notes room exists in the viewer; the notes document and its
history never leave the owner's machines and the guests' room). What a malicious reader can do: send an `exact`
that is not in the notes (not found → detached thread with their text, visible to the owner and readers under
"On: …"/quote; the caps bound it), or a prefix/suffix that steers among repeats (at worst a different repeat of
the same text). What the host can do: nothing new (ciphertext and sizes). What a guest sees: a reader's thread
on the notes, as C4 says.

## Item 2: per-reader revocation

**State.** `publications.blocked_readers` (C9). Written by `repo.set_blocked_reader(pub_id, rid, on, at,
name)` through a route `POST /api/sessions/{id}/publication/readers/{rid}/block` and `.../unblock` (owner or
admin: the publication's rights, `check_may_publish` l.92, plus `may_see_link`'s rule in `to_model` l.842), with
`?hide=1` on block to also `comments.hide` every comment whose `author_id == "reader:<rid>"` in that meeting
(Decision 4; history `comment_hidden` per comment as 13b logs, one `reader_blocked` / `reader_unblocked` line
with the rid and the name last seen, never a comment's text). The row's change wakes the bridge like a
selection change does (`update_row` → `threads_soon`, l.1248-1259), so THREADS carries the new `blocked` within
2 s.

**The bridge.** `_take` (l.1565): after the `rid` check and before `wanted`/`_switches`, `if rid in blocked:
return "blocked"` (a new outcome in `outcomes`, logged like the others). `_rate_ok` gains the per-publication
deque (Decision 5). `ThreadsFrame.blocked: list[str] = []` (ids, sorted); the key-set test adds it; `_trim`
(l.774) keeps `blocked` whole (a few ids). Edits and deletes from a blocked rid are dropped too: their existing
comments freeze as they are until the owner hides or deletes them.

**The viewer.** `Follower.onThreads` (live.ts l.423-430) keeps `blocked` (tolerant parse like `parsePending`);
`ReaderComments.svelte` hides the composer and every Reply/Edit/Delete when the reader's own `rid` is listed
and shows "The owner has turned off comments for you" where the composer was; pending ops for that rid stop
retrying (they would never be acknowledged by a THREADS). Other readers: no change.

**The owner's UI.** CommentThread.svelte's action row for reader comments (l.225-235, beside Approve/Hide) gains
"Block this reader" → a small confirm with the "also hide their comments" tick → the route; on a blocked
reader's comments the "From the published link" caption (l.167-169) adds "· blocked" and the action is
"Unblock". PublishDialog's published line lists "n readers blocked" with a popover to unblock (names last
seen, from the comments). The publishing store (src/lib/stores/publishing.svelte.ts) carries
`blocked_readers` from the API model.

**What blocking does and does not do (the text for the dialog's help and docs/publishing.md).** It stops that
reader's browser from adding, editing or deleting comments on this link, and, if ticked, hides what they wrote
from the other readers. It does not stop them reading: everyone with the link reads every version, and a
blocked reader who clears their browser is a new reader, who can comment again (`moderate` keeps such comments
from the others until approved; the rate cap bounds the flood). To stop someone reading, replace the link
(Decision 6) and send the new one to the people who should keep it.

**Security.** The block list is the owner's decision applied at the owner's bridge, the only place ops are
opened (the box key); a reader cannot forge another reader's `rid` (the signature over `reader_pub`, old plan
"Data and formats") so nobody can get a third party blocked by misbehaving under their id, and nobody can
unblock themselves (the list is on the row, never in a frame a reader sends). `blocked` in THREADS reveals which
`rid`s the owner blocked: `rid`s are already in every THREADS comment; a blocked reader whose comments were all
hidden is still named by `rid` only, which means nothing outside the room.

## Item 3: `RoomConnection` (C10)

`services/room_connection.py`: one class, asyncio, no sealing, no document, no publication knowledge.

- Construction: `RoomConnection(hooks, *, max_message, backoff, join_timeout, label)`. `hooks` is a small
  Protocol the two bridges implement: `url() -> str` (computed per attempt: C13 and guest_notes l.916),
  `join_message() -> dict` (token and `since`), `on_welcome(welcome) -> WelcomeAction` (CONTINUE / STOP /
  LEAVE_ROOM), `on_text(msg: dict) -> bool` (anything the loop does not handle itself: `count`, `media`,
  `ended`, `error` are handled by the loop and ALSO passed on, so the guest bridge keeps its media handler and
  the pub bridge its `_ended`), `on_binary(tag, seq, payload)`, `on_ack()`, `advance(seq)`, `on_connected()`,
  `on_disconnected()`, `on_closed(code, status, last_error) -> CloseAction` (RETRY_NOW, RETRY_BACKOFF, STOP,
  and a `then` coroutine for "snapshot then rejoin"), `encode(item) -> bytes` for the pump (guest: identity on
  `tag || env`; pub: sign and prefix), `stop_requested() -> bool`.
- Owned by the loop: the dial and the `proxy=None if loopback else True` rule; the welcome wait; the pump task
  and the `unacked` deque (the guest's `T_NOTES`-only accounting is an `encode`/`track(item) -> bool` hook);
  `ping`/`pong`; the `attempt` counter and the backoff sleep; the exception mapping to `(code, status)`;
  `CLOSE_TOO_LARGE` + `log_full`, `CLOSE_LAGGED`, `CLOSE_FULL` handling, as both loops do them today, with the
  guest's extra "drop the largest unacked on a 4413" as a hook decision (`on_closed` may remove from
  `unacked`, which it is handed).
- The bridges keep: everything in C10's second list. Lines of code: guest `_connect_loop`+`_pump`+`_read`+
  `_on_text` (l.901-1067) shrink to the hooks; pub l.1372-1527 the same. Constants (`CLOSE_*`, `T_*`) move to
  room_connection.py and are re-exported where tests import them (test_live_publications.py imports
  `live_publications.CLOSE_*`? the task checks and keeps the names).
- No change to fake_guest_room.py (the fake host speaks the wire, not the class). Behaviour pinned by:
  test_guest_notes.py (73 tests; the socket ones: `test_start_registers_the_room_and_posts_a_snapshot` l.124,
  `test_a_guest_frame_lands_in_the_log_under_the_guest` l.218, `test_frames_from_the_owners_side_in_the_log_are_
  not_applied_again` l.251, `test_no_presence_is_queued_while_the_room_is_away` l.469,
  `test_presence_left_in_the_queue_is_dropped_on_a_disconnect` l.500, `test_resume_rejoins_from_the_last_seq_
  and_catches_up` l.624, `test_a_room_the_host_deleted_stops_the_bridge` l.653, `test_a_full_room_is_retried_
  not_given_up` l.677, `test_a_room_deleted_while_away_is_closed_gone_at_the_upgrade` l.692,
  `test_a_lagged_bridge_rejoins_with_its_seq` l.702, `test_resume_does_not_apply_our_own_frames_again` l.981,
  the media ones l.1780-1900), test_guest_notes_routes.py (16), test_live_publications.py (46; the socket ones:
  `test_a_4410_makes_a_new_room_and_moves_the_pointer` l.678, `test_a_stale_pointer_at_start_gets_a_new_room`
  l.712, `test_an_older_host_is_unsupported_and_gets_no_room` l.725, `test_heartbeat_and_the_rooms_expiry`
  l.825, `test_resume_joins_the_same_room_with_the_seq_it_applied` l.875, `test_no_room_no_push` l.915,
  `test_readers_frames_go_to_the_hook_and_forged_owner_frames_nowhere` l.925, the leak tests l.1390+), and the
  e2e guest-notes.spec.ts, guest-notes-iroh.spec.ts, publish-live.spec.ts in CI. The task adds
  test_room_connection.py for the loop alone against `FakeRoomHost` (join, ack order, lagged rejoin, full
  retry, 4413 log_full → hook's snapshot, proxy flag on loopback).

## Item 4: live for "this computer" publications

**The two routes, weighed.**

| | A: backend proxy to the embedded host | B: iroh links for publications |
|---|---|---|
| Reach | whoever reaches the backend today: the team listener (Tailscale name or LAN, `local_reach` l.808-821), `phone_url` (a Funnel/tunnel address = anyone with the link) | anyone with the link, anywhere, through the relay (ours by default), no host, no tunnel |
| What moves | backend: one WebSocket route + a local room host variant + `X-Pub-Room` from meta.json; viewer: nothing; link: nothing | link: an HTTP-over-iroh protocol (manifest + blobs, up to 20 MiB, range requests for clips) beside the room proxy; guest-wasm (CI-only build) into the viewer; viewer: format-2 parsing, a second transport for `openHosted`/`loadClip`/`loadImage` and the room; a static origin serving the viewer for such links; CSP with relays; the Publish dialog's link copy |
| Data our servers see | nothing | relayed ciphertext (as guest notes) |
| Tiers | 1 heavy (bridge + route), 1 medium (dialog), 1 careful (e2e) | 4 heavy + 1 careful + the CI dance for guest-wasm |
| Old links | unchanged | unchanged (format 1 keeps working; format 2 is new links only) |
| Fits "one always-on desktop as the base" | yes: the base serves, the team reaches it over Tailscale; outsiders through `phone_url` | yes, and outsiders too |

Recommendation: A (Decision 7). Plainly: with A, a "this computer" link is live for exactly the people who can
open it today; it does not become reachable by people outside Corey's network unless `phone_url` is a public
address (Tailscale Funnel to the backend port, which docs/remote-access.md describes) or a publish host is used.
B is what would do that without a host; it is a milestone of its own and is NOT scheduled here. (A third
shape, publishing local publications ONTO the embedded host and tunnelling it with services/tunnel.py as
guest notes do, was looked at and dropped: a Cloudflare quick tunnel's address changes on every start, so a
link would die with the tunnel; Funnel is just `phone_url` with extra steps.)

**Design (A).**

- **Room host abstraction** (in live_publications.py, after task 3): `class _RoomHost` with two
  implementations. `_RemoteHost(row)`: today's code (`GET /pub/{id}` for the pointer, `PUT /pub/{id}/room`,
  `PUT /live/{room}`, `_http` with the publication's token). `_LocalHost(app, row)`: `base()` =
  `(await app.ensure_rooms()).base_url` (per attempt, C13; a `LinkError` → state `unsupported`, reason "Live
  updates for this computer's links need mnemosyne-link" when no binary, "offline" with the error otherwise);
  requests through guest_notes' `_request` with `upload=app.rooms_link.upload_token` (l.479-520) and the
  publication's token as Bearer (room writes are authorised by its hash, rooms.rs `authorize` l.238); pointer
  read/write = `meta.json["room"]` (`read_meta`, `_write_json`), cleared at `retire`; room deletion as today.
  `wanted()` accepts `place == "local"` when `_origin_here` (the serving machine; `served_by` None) and the
  sidecar binary exists (`shared_notes.available()`-style probe of `find_binary`).
- **Pointer served by the backend.** `read_manifest` (routes/publishing.py l.262-284) adds `X-Pub-Room` when
  `meta.get("room")` is a valid room id, on the 200 and the 304; `local_manifest`/`_live_meta` unchanged.
- **The proxy route.** `@router.websocket("/live/{room}")` in a new api/live_proxy.py (the pattern of
  api/notes_ws.py): refuse (close 4410 after accept, as the host does for a browser's sake, app.rs l.733-746)
  unless `room` is pointed at by a live local publication of this machine (`live_publications.proxied_rooms(ctx)`:
  the Bridges registry's current room ids); `ctx.locked` → 4410 too; connect `ws://<base_url>/live/<room>` with
  `websockets` (max size = the host's cap + 64 KiB, as app.rs l.730), then two pumps: client→host (text and
  bytes as they are; the host's own caps and codes apply) and host→client; the host's close code and reason are
  passed through (`ws.close(code=...)`); a connection the host refuses (dial fails: the sidecar is between
  restarts) closes 1012 (service restart): the viewer's room client backs off and rejoins on any code it does
  not know (src/lib/rooms/client.ts l.805-823), whereas 4409 would rejoin at once (l.819-821) and 4429 would
  show the room as "full". Caps of its own: `PROXY_PER_ROOM` = 64 and `PROXY_TOTAL` = 256 connections (the embedded
  host's per-address limits are off, C11; the backend is the exposed face), 4429 beyond. No auth: the path is
  open (C12) and the room token is the gate, exactly as on a publish host.
- **Bridge start/stop.** `Bridges.resume` (l.972) and `ensure` take local rows too; `retire` on unpublish
  deletes the room on the embedded host and clears `meta.json["room"]` (the order of old C13 as corrected:
  publication first, then room; for local both are this machine's, so the viewer's 410 is immediate anyway).
  `_maybe_stop_rooms` consults `live_publications.local_rooms_in_use(app)` (C13); `awake.py` adds
  `live_publications.needs_awake(ctx)`.
- **Policy.** `check_live` (publishing.py l.917): `where == "local"` is allowed; `shared_from` still refused.
  `status()` (l.84) reasons for local: no sidecar binary → `unsupported`; `local_reach == "this_device"` →
  `live` still (the room works for this machine's own browser; the dialog's amber warning already says nobody
  else can open the link). Library sync: the serving machine (`origin`) bridges; the other computer cannot
  Update a local publication today (`served_elsewhere_message`, l.107) so C12's one-shot push does not arise.
- **Viewer.** No change: `openHosted` reads `X-Pub-Room` (load.ts l.287), `Follower` joins `roomUrl(room,
  location)` = `wss://<origin>/live/<room>` (client.ts l.140-142), the manifest's `live` keys come from the row
  as for hosted publications (`build_manifest` writes them when a switch is on). One check in the task: the
  pointer on a 304 (`read_manifest` adds it to `headers` before the 304 branch).
- **Copy changes (kept small; the settings review may rename "this computer"):**
  - PublishDialog.svelte l.330-331: the `local` branch's "Live updates and comments need a publish host." goes;
    the two checkboxes (and 13b's indented one) render for `local` as for `host` (the `{#if pub.target ===
    'host'}` at l.305 becomes `host || local`).
  - A new one-line help under them for `local`: "Readers who can open this link get updates and may comment
    while this computer is on." (only while `local_reach !== 'this_device'`).
  - `status()` reasons (API strings, shown in the published line l.352): "Live updates for this computer's links
    need mnemosyne-link"; "Not connected to this computer's rooms" (sidecar not ready).
  - publishing.py `LIVE_NEEDS_HOST` is no longer raised for `local`; the constant is removed or kept for
    `where == "file"`-like refusals (publication_file has its own `check_request`; the task decides, prefer
    removal).
  - docs/publishing.md "Where it goes" and "Kept up to date": one sentence each. No settings copy changes.
- **Security.** The threat that is new: the backend's two listeners now carry a WebSocket to a loopback service
  that also holds this machine's iroh guest rooms. The allow-list (`proxied_rooms`) is the defence; a test
  joins a guest room's id through the proxy and gets 4410. The room token is derived from the link key as on a
  host, so holding the link is still what admits a reader; the embedded host's log, snapshot and frame caps
  apply unchanged. A malicious reader can hold up to 64 proxied connections per room and 256 in all before 4429;
  the backend's event loop carries bytes only. The host (the embedded one) sees ciphertext; its store is under
  `<data_dir>/guest_rooms/host` (host.rs l.37), on this machine, not sealed by encryption at rest (as guest rooms'
  logs are not today; noted in Risks).

## Data and formats

- **FORMAT stays 1.** No manifest field changes. `publication_v1.json` and `publication_live_v1.json` are not
  regenerated.
- **Frames (pub-live space).** META gains `notes: bool` (C6); THREADS gains `blocked: list[str]` (Decision 3);
  a THREADS anchor may carry `target: "notes"`; a COMMENT op's anchor may carry `target: "notes"`. All additive;
  `extra="forbid"` models grow by exactly these fields; the key-set test in test_live_publications.py lists them.
  The viewer reads `notes` and `blocked` as optional. The vectors' sample frames remain valid frames (an owner
  of 0.30.0 sends exactly them) and their tests pass unchanged; no new vector file (the fields are plain JSON
  booleans and string lists under the same signature rule; the signed-message rule does not change).
- **Sidecar.** `notes doc` gains `{"op":"mark_comment_at","notes","snapshot","frames","start","end",
  "comment"}` → the `mark_comment` answer shape (api.rs l.26-31): `start`/`end` are `[start, end)` code points
  of the answer's `plain` (= seq positions, C2); errors: out of range, `end <= start`, empty comment. notes-core
  `Notes::mark_comment_at(start, end, comment)` beside `mark_comment` (doc.rs l.533), sharing the `mark_inline`
  call. notes-core changes mean scripts/build-notes-wasm.sh and committing `src/lib/notes/pkg` if the bytes
  change (the CI `notes` job diffs it; CLAUDE.md "After changing notes-core").
- **Database.** `publications.blocked_readers TEXT` (JSON object; NULL/absent = `{}`), migration in
  `_migrate_columns`, in `_PUB_COLS`/`_PUB_UPSERT`, `SyncPublication.blocked_readers: dict = {}`, merged per
  rid by newest `at`. Never in `publications_record` (test), the manifest or history details. No other schema
  change: the local room id lives in `meta.json` (and `room.json` as today), the pointer never in the database.
- **API.** `Publication.blocked_readers: list[BlockedReader]` (`rid`, `name`, `at`); `LiveState` unchanged
  (`live_reason` carries the local reasons); two routes under `/api/sessions/{id}/publication/readers/{rid}`
  (`block` with `hide: bool` body, `unblock`); `POST /api/sessions/{id}/publication/replace` (task 7, a `publish`
  job). `pnpm gen:api` after each.
- **Files.** `<pub_dir>/meta.json` gains `room` for local publications (readers' pointer); `room.json` keeps
  the bridge's seq as today.
- **History.** `reader_blocked` / `reader_unblocked` (rid, name last seen, hidden count); `publication_replaced`
  (old id, new id; sealed as a publish). The Parts & history card lists them as it lists `publication_*`.

## Tasks (tier, deps)

Files in brackets are what a task may edit; tasks that run at the same time never share a file. Backend tests
one file at a time (`cd backend && uv run pytest tests/<file> -q`), Vitest one file at a time (`pnpm exec vitest
run <file>`), `pnpm check` at 0 errors, ruff on touched files, `cargo test` per crate; viewer/guest/notes-wasm
builds, the full e2e and the full backend suite only in CI (this machine cannot run them: CLAUDE.md). Never play
audio.

### M4.0 the connection (first: everything in the bridges builds on it)

1. **Extract `RoomConnection`** (careful). [new backend/mnemosyne/services/room_connection.py,
   services/guest_notes.py (Bridge: `_connect_loop`, `_pump`, `_read`, `_on_text`, `_on_binary` become hooks;
   nothing else), services/live_publications.py (PubBridge: the same five), new backend/tests/
   test_room_connection.py; backend/tests/test_guest_notes.py and test_live_publications.py only if an import
   of a moved constant must change] As "Item 3". Behaviour-preserving: the two bridges' observable behaviour
   (wire, timing classes, persisted seq, what is queued/dropped at join and disconnect) is unchanged. Verify:
   `uv run pytest tests/test_guest_notes.py -q`, `tests/test_guest_notes_routes.py`, `tests/test_live_publications.py`,
   `tests/test_room_connection.py` (new: the loop alone against `FakeRoomHost`), ruff; in CI: e2e guest-notes,
   guest-notes-iroh, publish-live.

### M4.1 notes anchors (2a, 2b, 2c in parallel after nothing; 2d after 1, 2a, 2b; 2e after 2c, 2d)

2a. **Sidecar op `mark_comment_at`** (medium, Rust). [notes-core/src/doc.rs (`mark_comment_at`, refactor the
   shared tail of `mark_comment`), notes-core/src/api.rs (the request variant and its doc comment),
   notes-core tests (a range marked by positions equals one marked by the ids at those positions; out of range
   and `end <= start` are errors; U+001F cells count as one position), src/lib/notes/pkg only if
   scripts/build-notes-wasm.sh changes it] Verify: `cargo test -p notes-core`, `cargo test` in link (the `notes
   doc` command), `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`; run
   scripts/build-notes-wasm.sh and commit `src/lib/notes/pkg` if it differs (the CI `notes` job).
2b. **`notes_anchor.locate` and the locked mark** (heavy). [new backend/mnemosyne/services/notes_anchor.py,
   services/shared_notes.py (`mark_comment_located_blocking` + an async `mark_comment_located`, beside
   `mark_comment_blocking`; `_run_doc` of the new op), services/comments.py only to make `locate_body` reusable
   if it needs a signature change (prefer none), new backend/tests/test_notes_anchor.py, backend/tests/
   test_shared_notes.py (the locked mark against tests/fake_link.py's toy `notes doc`, which gains the op)]
   As "Item 1 locate": R from `_notes_text` + `plain_markdown` + the image twin; P with sections blanked; the
   matcher; the verification. Tests: a passage found as is; a passage containing a redacted name ("Person 2"
   in R, "Ada Lovelace" in the document) lands on the real text; a passage inside a chip's text; a passage in a
   table cell; one after an image line; a passage that exists only in a summary section (never placed: None);
   prefix/suffix choosing among two repeats; a passage the notes no longer contain (None); the twin
   `imageText` string pinned against viewer/src/notes.ts l.70-78 by a copied constant. Verify: the two test
   files, `uv run pytest tests/test_comments_notes.py -q`, ruff.
2c. **Viewer: comment on the notes** (medium). [viewer/src/Meeting.svelte (`readSelection` over two boxes, the
   notes section's affordance and count gated by `meta.notes`), viewer/src/comments.ts (`passageAnchor(target)`,
   `anchorKey` → "notes", `anchorLine`), viewer/src/live.ts (`onMeta` reads `notes`), viewer/src/
   ReaderComments.svelte (placement of notes threads), viewer/src/*.test.ts] Verify: `pnpm exec vitest run
   viewer/src`, `pnpm check`.
2d. **Bridge: notes ops in, notes threads out** (heavy; 1, 2a, 2b). [services/live_publications.py (`OP_TARGETS`,
   `_parse_op`, `_placed`, `_create_on_notes`, `_Shown.anchor` for `notes`, `MetaFrame.notes`,
   `send_meta` computes it, the availability probe), backend/tests/test_live_publications.py] Tests: a reader's
   op on the notes → a `notes` comment with actor `reader:<rid>` and a mark whose range (`notes_marks`) covers
   the real text; THREADS carries `target: "notes"` with the redacted marked text as `quote`; `sel.notes` off →
   not placed and not shown; no sidecar → the comment exists detached and THREADS still shows it by quote;
   META `notes` true only with comments on, notes published and the sidecar there; the key-set assertion gains
   `notes`; a frame from the fake room's log replayed twice marks once (the replay rule, `_ops`). Verify: the
   test file, `uv run pytest tests/test_comments.py -q`, `tests/test_guest_notes.py` (C4: a reader's notes thread
   appears in `threads_for_guests`, asserted), ruff.
2e. **e2e notes comments** (careful; 2c, 2d). [tests-e2e/publish-live.spec.ts (extend)] Against the local
   publish host on 8028 as the existing spec: publish with notes ticked; the reader selects a sentence of the
   notes and comments; the owner's app shows the thread on the notes (the comment mark in the editor, the panel
   caption "(reader)"); a second reader page shows it under the notes with the quote; the owner edits the
   notes line → the thread follows (`changed` false, same text) and a deleted line → "detached" shown. Done
   when it passes in CI (Chromium and WebKit).

### M4.2 blocking (3 after 1; 4 after 3; 5 after 3; 6 after 4, 5; 7 any time after 3)

3. **Block list: column, sync, API, bridge** (careful; 1). [backend/mnemosyne/storage/sqlite.py (column,
   migration, `_PUB_COLS`/`_PUB_UPSERT`, `set_blocked_reader`), models/sync_session.py (`SyncPublication` +
   merge), models/publication.py (`BlockedReader`, `Publication.blocked_readers`), services/publishing.py
   (`to_model`; `block_reader`/`unblock_reader` with the hide option through `comments.hide`; history lines),
   services/live_publications.py (`_take` "blocked", `RATE_TOTAL`, `ThreadsFrame.blocked`, `_trim`),
   api/routes/publishing.py (the two routes), backend/tests/test_publishing.py, test_live_publications.py,
   test_sync_publications.py, test_publishing_records.py (the column is absent from `publications_record`)]
   `pnpm gen:api`. Tests: block → the next op from that rid is "blocked" and nothing is written; its existing
   comments stay unless `hide`; `hide` sets `hidden_at` on each and THREADS drops them; THREADS lists the rid in
   `blocked`; unblock → ops land again, hidden comments stay hidden; a block made on one computer reaches the
   other through sync and an unblock made later there wins; the 61st applied op in 10 minutes across rids is
   dropped; the key-set assertion gains `blocked`. Verify: the test files, ruff, `pnpm check`.
4. **Viewer: blocked state** (light; 3). [viewer/src/live.ts (`blocked` parsed), viewer/src/ReaderComments.svelte
   (composer and controls hidden, the one line of copy, pending ops stop), viewer/src/*.test.ts] Verify: `pnpm
   exec vitest run viewer/src`, `pnpm check`.
5. **Owner UI: block and unblock** (medium; 3). [src/lib/components/CommentThread.svelte (the action and the
   confirm with the hide tick; "· blocked" caption; Unblock), src/lib/components/PublishDialog.svelte (the
   "n readers blocked" line and popover; the help text of Decision 4's wording), src/lib/stores/
   publishing.svelte.ts + publishing.test.ts, src/lib/api (the two calls)] Verify: `pnpm exec vitest run
   src/lib/stores/publishing.test.ts`, `pnpm check`.
6. **e2e blocking** (careful; 4, 5). [tests-e2e/publish-live.spec.ts (extend)] A reader comments; the owner
   blocks with hide; the reader's page loses the composer and shows the line; the second reader no longer sees
   the thread; the owner unblocks; the reader comments again. Done when it passes in CI.
7. **"Replace the link"** (medium; 3; optional, Decision 6). [services/publishing.py (`replace`: unpublish then
   publish with the stored selection in one job; history `publication_replaced`; the seal), api/routes/
   publishing.py (`POST .../publication/replace`), src/lib/components/PublishDialog.svelte (a "Replace the
   link" action beside Unpublish with its help: "Everyone with the old link loses it; send the new one to the
   people who should keep reading"), src/lib/stores/publishing.svelte.ts, backend/tests/test_publishing.py,
   test_publishing_records.py] The new publication keeps `follow`/`comments`/`moderate`, gets fresh owner keys
   (the old pair ends with the old publication), an empty block list (the old readers are gone with the link),
   and for a hosted one the bridge moves to the new room. Verify: the test files, ruff, `pnpm check`,
   `pnpm gen:api`.

### M4.3 "this computer" live (8 after 1; 9 after 8; 10 after 8; 11 after 9, 10)

8. **Local room host, pointer in meta.json, the proxy route** (heavy; 1). [services/live_publications.py
   (`_RoomHost` with `_RemoteHost`/`_LocalHost`, `wanted` for local, `status` reasons, `proxied_rooms`,
   `local_rooms_in_use`, `needs_awake`), services/publishing.py (`check_live` for local; `read_manifest`'s data:
   none, the pointer is read in the route), api/routes/publishing.py (`X-Pub-Room` on 200 and 304), new
   backend/mnemosyne/api/live_proxy.py (the WebSocket route, caps, the allow-list), api/app.py (one
   `include_router` line beside `notes_ws_router`, l.143), services/guest_notes.py (`_maybe_stop_rooms` asks live_publications),
   services/awake.py (one line), backend/tests/test_live_publications.py, tests/test_publishing.py (the local
   `check_live`), new tests/test_live_proxy.py (a websockets client against the ASGI app via the test server
   pattern test_guest_notes.py uses for the fake host; the embedded host is `FakeRoomHost` reached through a
   stubbed `rooms_link.base_url`)] Tests: a local publication with `follow` gets a room on the fake embedded
   host with the sidecar's upload token, `meta.json["room"]` set, `X-Pub-Room` on GET and 304; the proxy
   relays join/welcome/frames/acks both ways and passes the host's close code through; a room id that is not a
   live local publication's (a guest room's) is closed 4410; the 65th connection to a room is 4429; the sidecar
   stopping and starting again (a new `base_url`) is followed by the bridge; unpublish deletes the room and
   clears the pointer; `_maybe_stop_rooms` keeps the sidecar while a local publication is live; a machine
   without the binary reports `unsupported` with the reason; a `shared_from` meeting is still refused. Verify:
   the test files, `uv run pytest tests/test_guest_notes.py -q` (the stop rule), ruff.
9. **Dialog copy for local live** (light; 8). [src/lib/components/PublishDialog.svelte (the `host || local`
   condition, the one-line help, preselection per Decision 8 in src/lib/stores/publishing.svelte.ts `toSend`/
   defaults), publishing.test.ts] Verify: `pnpm exec vitest run src/lib/stores/publishing.test.ts`, `pnpm check`.
10. **Viewer check against the backend origin** (light; 8). [viewer/src/load.test.ts or live.test.ts only if a
    gap is found; expected: no change] Confirm with a Vitest fake fetch that a 304 carrying `X-Pub-Room` moves
    the room, and that `roomUrl` on an `http:` origin gives `ws:` (the e2e backend is plain http). Verify:
    `pnpm exec vitest run viewer/src`.
11. **e2e local live** (careful; 9, 10). [tests-e2e/publish-live.spec.ts (extend) or a new
    tests-e2e/publish-local-live.spec.ts] Against the demo backend on 8018 with no publish host set (the
    spec's `beforeEach` already clears it, l.10): publish to "this computer" with both switches; open the link
    in a second page; a summary edit reaches the viewer without a reload; a reader comment reaches the owner;
    the dialog shows "Live · 1 reading"; unpublish → "No longer published". The e2e job builds the link sidecar
    (C15). Done when it passes in CI.

### M4.4 docs and close

12. **Docs** (light; 2e, 6, 11). [docs/publishing.md ("Comments from readers": the notes; a new "Blocking a
    reader" subsection with the "does and does not" text; "Revoking, honestly": Replace the link; "Where it
    goes"/"Kept up to date": this computer's links), docs/api-reference.md (the routes, the frame fields,
    `X-Pub-Room` from the backend, `/live/{room}` on the backend, `mark_comment_at`), docs/architecture.md (one
    paragraph), docs/guest-notes.md (C4: readers' notes comments reach guests), CLAUDE.md (one roadmap
    paragraph; the Gotcha about `extra="forbid"` frames gains "META `notes`, THREADS `blocked`"), this file
    (status + "changed while building")]
13. **Close** (light; 12). [backend/mnemosyne/api/static/viewer/ (`pnpm build:viewer`), src/lib/notes/pkg if
    2a changed it, `pnpm gen:api` once more] Push; watch `viewer`, `notes`, `backend`, `e2e`, publish-host and
    link jobs. No host redeploy is needed (the publish host does not change in M4).

## Files shared across tasks (sequence, never parallel)

- services/live_publications.py: 1, then 2d, then 3, then 8 (one worker for 2d and 3 if they run close
  together). services/guest_notes.py: 1 then 8. services/shared_notes.py: 2b only. services/publishing.py: 3,
  then 7, then 8. services/comments.py: 2b only (if at all). api/routes/publishing.py: 3, then 7, then 8.
  storage/sqlite.py, models/sync_session.py, models/publication.py: 3 only. notes-core/src: 2a only.
  src/lib/notes/pkg: 2a only. viewer/src/live.ts, ReaderComments.svelte, Meeting.svelte, comments.ts: 2c then 4
  then 10. src/lib/components/PublishDialog.svelte: 5, then 7, then 9. CommentThread.svelte: 5 only.
  src/lib/stores/publishing.svelte.ts: 5, then 7, then 9. tests-e2e/publish-live.spec.ts: 2e, then 6, then 11.
  backend/tests/test_live_publications.py: 1, 2d, 3, 8 in that order. backend/tests/fake_link.py: 2b only.
- Generated: schema.d.ts (3, 7, 8: `pnpm gen:api`), static/viewer (13), src/lib/notes/pkg (2a).
- Parallel lanes once task 1 is done: {2a, 2b, 2c} together; 3 after 2d (both edit live_publications.py);
  {4, 5} after 3; 8 after 3; {9, 10} after 8; 7 any time after 3 (publishing.py: after 3, before 8).

## What only CI can check

The viewer bundle diff (`viewer` job), the notes wasm diff (`notes` job, if 2a changes bytes), the e2e specs
(publish host on 8028 for 2e and 6; the demo backend with the built link sidecar for 11; Chromium and WebKit),
the full backend suite. Locally: single test files, `pnpm check`, Vitest per file, `cargo test` per crate, ruff.

## Risks

- **Alignment on long or repetitive notes.** `SequenceMatcher` is quadratic in the worst case; notes over the
  cap are not placed (detached, shown by quote). A passage that repeats verbatim (a template line) may land on
  the wrong repeat when prefix/suffix are equal too; the verification cannot tell them apart. Acceptable: the
  thread shows the right text, on a line with the same text.
- **A reader's mark in the document reaches guests and shared-with recipients (C4)** and, through
  `threads_for_guests`, so does the thread, under "(reader)". The docs say so. `hidden_at` is not applied to
  guests' view today; a hidden reader comment would still reach guests. Out of M4's scope; flagged.
- **Blocking is weak by construction (C7-C8)** and the plan says so everywhere the owner meets it. The honest
  remedy is Replace the link (task 7); if Corey declines 7, the help text must still point at Unpublish +
  Publish by hand.
- **The block list in THREADS names rids.** Every reader already sees every commenting rid; a reader whose
  comments were all hidden is now visible as a bare rid in `blocked`. Decision 3 accepts that; the alternative
  (silent) is a worse page.
- **The backend proxy is a new exposed WebSocket on both listeners.** The allow-list (C12) and the caps are the
  defence; a test for a guest room's id is mandatory. The embedded host's room logs and snapshots are plain
  ciphertext files under `<data_dir>/guest_rooms/host`, not sealed by encryption at rest (true for guest rooms
  since 0.25.0); the frames are AES-GCM under the link key, so at rest they are as safe as the publication's own
  `manifest` file beside them.
- **The rooms sidecar's life.** Local live publications now keep the sidecar running and the computer awake while
  they are live; a person who publishes "this computer" with the switches on and forgets sees the sidecar in
  the process list and the sleep inhibitor held. `status()` says "Live · n reading" in the dialog; the Parts &
  history card does not change. If that surprises people, a setting can turn local live off; not added now.
- **`RoomConnection` is a refactor of two loops that were deliberately written apart (old C11).** The test lists
  in task 1 are the net; the subtle differences in C10's second list must stay in the hooks, not be
  "harmonised". A worker who finds the two loops disagree on something not in that list stops and reports.
- **Preselection for local (Decision 8)** interacts with the settings review that may reshape "this computer";
  the copy here is three strings and one condition, all listed, so it can be redone cheaply.
- **The notes wasm.** 2a's change to notes-core may change `src/lib/notes/pkg` bytes; the pinned toolchain
  (rustc 1.93.1, wasm-bindgen 0.2.129 at ~/.cache/mnemosyne-tools) must be present locally, or the `notes` CI
  job goes red for a reason unrelated to the feature.
- **The viewer's size and `pnpm build:viewer`.** No new dependency; the bundle grows by the notes affordance
  and the blocked state only; the CI `viewer` job diffs the committed build, so task 13 must run.

## Changed while building

(empty)
