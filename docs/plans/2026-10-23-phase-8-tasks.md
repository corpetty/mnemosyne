# Phase 8 tasks: sharing with other people's computers

Status: in progress, 2026-10-23. The design is "Phase 8" in 2026-10-12-hashseq-collaboration.md; this
file is the build order, drafted against 56e391e and checked against the code. Open questions take
the design's defaults: shared-with-me meetings stay out of Ask, semantic search, the digest and
Obsidian (four `shared_in_*` settings, off); hardening ships first.

## Corrections to the design (from reading the code)

- **C1 Rules through a contact Principal.** The hub enforces team mode's own rules
  (`comments._Meeting.rights`, `_set_action_item_flag`, `access.can_edit_notes`, `_SEES`) by
  running each contribution as `Principal(user_id="p:<16 hex>", name, role=MEMBER)`. `_SEES` reads
  a view `share_grants` = `session_shares` ∪ live `peer_shares`; `shared_notes.may_edit` also
  needs the share to include notes.
- **C2 Tokens shared by all of a person's machines.** One random 32-byte `secret` per contact pair
  in the `contacts` row (travels in library sync). The token X presents to Y = HMAC-SHA256(secret,
  "mnemosyne/share/to:" + Y's key). Every machine writes CONTACT devices for each endpoint on the
  card with the hash of the token the contact presents.
- **C3 Sidecar card passthrough.** `PairRequest` / `ControlRequest::Pair` gain `card`, forwarded to
  `/api/pairing/redeem`; `PairReply` returns the redeemer's `card`. Sync pairing unchanged.
- **C4 Cards carry tickets** `{person, name, machines: [{name, ticket}], at, sig}`. Contacts are
  global: one `GET /api/sync/contacts` step per library-sync round (merge by person, newer
  `updated_at`, removed wins a tie). `peer_shares` travel in `SyncSession.peer_shares`.
- **C5 Key adoption at sync pairing.** The pair payload carries `{person_pub, has_people_data}`;
  redeem answers 409 "these computers belong to different people" when both have data and keys
  differ, else who adopts; then `GET|PUT /api/sync/person-key` over the tunnel.
- **C6 `shared_from` rows** never enter `changes_since`, leave no tombstone on delete, stay out of
  task linking; Ask, search, digest, vault export and export-all skip them unless `shared_in_*`.
- **C7 Recipient access with no Principal.** `_check_write` refuses shared_from rows ("Shared by
  <name>: you can comment, tick action items and type in the notes") unless `access.internal`
  (ContextVar, `access.internally()`; the notes fold sets it). Comments on shared_from meetings run
  as `contacts.my_principal(app)`. Frontend `canChange` excludes shared_from.
- **C8** `origin.is_foreign()` = `origin or shared_from`; "The audio is with <name>". The recipient
  writes with `repo.apply_shared()`, never `apply_synced`.
- **C9 Live notes on the recipient.** Same hashseq object (`notes_origin(id)`); the recipient's
  backend relays locally; `notes_doc` is united with `sync_notes`; local frames (author `p:<me>`)
  are pushed as `notes_frames`; the hub appends them via `shared_notes.accept_frames`
  (`receive_frame` refactored onto it), never via `/ws/notes`.
- **C10** Comment ids minted on either side; the outbox is FIFO per meeting; the next
  SharedMeeting replaces the recipient's comments whole; rejected ones show their reason.
- **C11** Recipient cursors per (contact, owner endpoint); a share change `_touch`es its meeting;
  tombstones = revoked peer_shares + `sync_tombstones` of meetings the contact had.
- **C12** SharedMeeting `digest` = sha256(canonical JSON), signed; `signatures(subject, signer,
  sig, at)`, subject = record id or frame sha256, also the idempotency check.
- **C13** "Hide identifiers" covers summary and transcript, not notes or comments; the dialog says so.
- **C14** The hub pokes recipients (`POST /api/share/poke` on their side) after changes.
- **C15** CONTACT tokens open only `/api/share/*`, never `/api/sync/*` or `/ws`. Team mode: 409.
- **C16 Hardening.** `SyncPeerStore.add` refuses an id bound to another endpoint; `_hello` id
  mismatch is an error state "pair again".
- **C17 (Corey)** "People" collides with the People view: Settings tab name to decide at M5.
- **C18 (Corey)** e2e: a demo-mode seed (contact "Ada Lovelace", one shared meeting) for one
  single-backend spec; the protocol is covered by three-app ASGITransport tests.
- **C19** `SCHEMA_VERSION` 12 → 13, tables via `SCHEMA`, `sessions.shared_from` via
  `_migrate_columns`; `person.key` (0600 hex) or `person.key.enc` (AES-GCM, `derive(master,
  "person")`) with encryption at rest; `encryption.enable/disable` convert it.

## Tasks (tier, deps)

M1 hardening (own commit)
1. Bind sync peer ids to endpoints (medium): sync_peers.py, sync_service.py; tests for id hijack,
   hello mismatch, a PHONE token refused on /api/sync/*.

M2 person key and contacts
3. Person key service (careful): services/person.py, encryption.py, context.py.
4. Contacts storage and cards (careful; 3): services/contacts.py, models/contact.py, sqlite.py
   `contacts`, pairing.py CONTACT kind.
5. Link sidecar card passthrough (medium): link/src/lib.rs, main.rs, README, tests/control.rs,
   backend/tests/fake_link.py.
6. Contact pairing, invite kind `contact` (careful; 3, 4, 5): routes/pairing.py, routes/contacts.py.
7. Auth: CONTACT tokens open /api/share/* as the contact (medium; 4): auth.py, access.py.
8. Contacts and the key in library sync (careful; 3, 4, 6, 1): routes/sync.py, sync_service.py,
   sync_peers.py.

M3 shares and hub (owner side)
9. Share storage, `shared_from`, `share_grants`, signatures (careful; 4).
10. SharedMeeting projection (careful; 3, 9): models/shared_meeting.py, services/shares.py.
11. Hub routes and share CRUD (careful; 7, 9, 10): routes/share.py, peer-share routes.
12. Hub accepts contributions (heavy; 9, 10, 11).
13. Hub announces to recipients (medium; 6, 11).

M4 recipient side
14. Pull and store shared meetings (heavy; 9, 10, 11): services/share_pull.py, `apply_shared`.
15. Recipient access rules and local writes (heavy; 7, 9, 14).
16. Outbox: pushing contributions (careful; 12, 14, 15).
17. Contact removal and unshare end to end (medium; 14, 16).
18. Demo seed and docs (light; 14).

M5 UI
19. Generated API types and stores (medium; backend done).
20. Settings → People (medium; 19).
21. ShareMenu People section and parts dialog, PartPicker from PublishDialog (careful; 19).
22. Shared with me: sidebar group, header chip, read-only state (medium; 19).
23. Waiting line and bylines (medium; 19, 22).
24. e2e on the demo seed (medium; 18, 20-23).

Found while building: backups leave person.key out, like the other key and token files, so a
restore on a new machine means a new identity and pairing contacts again (v1; a sealed copy in
encrypted backups is a candidate later). A stand-in sync id (the peer's id could not be fetched at
pairing) is adopted once at the first hello, then bound. Tunnelled TCP reached the backend from
127.0.0.1 like the desktop app, so with no `api_token` auth was off for remote peers and
`/api/system/attach` counted as local: the sidecar now connects from 127.0.0.2 and auth treats
that as never local (always a token, no LOCAL_PATHS). CONTACT devices never get DEVICE_PATHS.

Risks: the real multi-machine path (cards with several tickets, pokes across the tunnel) is only
covered by the fake sidecar until a manual two-machine check; hub folds per contact per round
(cache later if needed); a leaked contact secret compromises both directions until re-paired.
