# Phase 9 tasks: guest notes

Status: in progress, 2026-10-24. The design is "Phase 9" in 2026-10-12-hashseq-collaboration.md;
this file is the build order, drafted against cd2e681 and checked against the code.

Decided while planning (Corey away, defaults taken): frames from the owner's side (the owner's
typing, the copilot, phase 8 contributions) reach guests as the owner's in v1 (`_deliver` carries
no author); guest comments are ordinary comments (searchable, synced, in the records); the guest
page's CSP keeps `connect-src 'self'` (Safari < 15.4 cannot connect).

## Corrections to the design (from reading the code)

- **G1 Guest page without SvelteKit.** `src/lib/notes/*` and `editor/*` import only each other;
  `doc.svelte.ts` and `SharedNotes.svelte` are bound to app stores, so the page gets its own
  `guest/src/doc.ts` (on `sync.ts`) and composes `createEditorView`, `attachInput`, `UndoStack`,
  `commands`, `comments.ts`. `guest/` is a Vite entry like `viewer/` (base `/n/`, out
  `backend/mnemosyne/api/static/guest`), the wasm an emitted asset (committed pkg bytes, so the
  build stays reproducible). CI's viewer job also diffs `static/guest`.
- **G2 CSP and types.** The guest page needs `script-src 'self' 'wasm-unsafe-eval'`; both servers
  add `wasm -> application/wasm`.
- **G3 Sealing in Python** (`services/guest_format.py`, mirrored by `guest/src/format.ts`):
  `frames_key = HKDF(key, "mnemosyne/guest/frames")`, `room_token = b64url(HKDF(key,
  "mnemosyne/guest/room"))`; envelope `kind(1) || nonce(12) || AES-GCM(pad(plaintext))`, AAD
  `mnemosyne/guest/1|<room>|<kind>`; kinds notes=1, snapshot=2, comment=3, meta=4, presence=5;
  notes plaintext `u8 len gid || gid || u8 len name || name || raw frame`. Vectors
  `backend/tests/vectors/guest_v1.json`, checked by Python, TS and (room token) Rust.
- **G4 Snapshots by the owner's bridge**: `fold_blocking` + `notes_catchup`, sealed canonical JSON
  `{format, notes, frames, threads, meta}`; at start, every 3 min while dirty, at end;
  `PUT /live/{room}/snapshot` with `X-Room-Since` prunes the host log.
- **G5 A hidden Room member** catches every local frame (`_deliver` is where all paths end); guest
  frames go in through `accept_frames(..., skip=bridge_member)`, never echoed back.
- **G6 Room protocol** (axum `ws`): create/snapshot/end/delete over HTTP with the publisher token
  (+ upload token on a closed host); join over WebSocket with `room_token`, `welcome`, snapshot,
  log replay since `since`, live relay (`0x01||seq||env`), presence (`0x02`), `count`, `ended`.
  Frame cap 2 MiB + 1 KiB (pastes can be 2 MiB, as `shared_notes.MAX_FRAME`), log 8 MiB, snapshot
  8 MiB, 64 connections, rooms ≤ 7 days.
- **G7 Guest comments**: the page mints ids and `notes_ids`; the bridge calls `comments.create`
  with an explicit `actor=("guest:<gid>", "<name> (guest)")` and marks with `mark_comment`
  (the mark belongs to the owner's server); edit/delete own only; resolve stays with the owner;
  threads on the notes re-sealed after each `comments` event.
- **G8 Agenda** in a sealed `meta` envelope `{title, date, owner, agenda:[{text, covered}],
  ended}` from `AgendaItem.covered` and `copilot_notes` events.
- **G9 Host choice**: `guest_host`, else `publish_host`, else ours (`OUR_GUEST_HOST`, "" until it
  runs); none → 409 "No host for guest notes". `http://`/`ws://` accepted (e2e).
- **G10 End and expiry** on the host (`ended_at`, clamped expiry) and in `guest_rooms`; auto-end
  `guest_notes_end_hours` (12) after `recording_stopped`.
- **G11 Serving**: host `/n/{room}` from `PUBLISH_GUEST_DIR`; backend the same from `static/guest`;
  `/n/` an open path.
- **G12 e2e** builds publish-host and starts it as a third web server on 8028.
- **G13 Records/history**: `guest_notes_started|ended|stopped`, `EVENT_SEALS`, `guest_notes.json`
  in the records zip (never key or token).
- **G14 Team mode**: `allow_guest_notes` (off), owner or admin; refused for shared_from meetings
  and without the sidecar.

## Tasks (tier, deps)

M1 publish-host rooms
1. Room store and log (heavy): publish-host/src/rooms.rs, config.rs, lib.rs.
2. Live routes and WebSocket (heavy; 1): app.rs, Cargo.toml/lock, `/n/` routes.
3. Deploy files (light): Dockerfile, deploy/publish/compose.yaml + README.
4. Rooms contract tests (careful; 2, 5): publish-host/tests/rooms.rs.

M2 format, settings, bridge
5. Guest format, Python, vectors (careful).
6. Guest format, TypeScript (medium; 5).
7. Settings, schema, model (medium).
8. shared_notes and comments hooks (careful).
9. Bridge service (heavy; 5, 7, 8).
10. Routes, lifecycle, records, static (careful; 9).
11. Bridge coverage (careful; 10).

M3 guest page
12. Guest Vite entry (medium; 6).
13. Room client and document (heavy; 12).
14. Guest editor and shell (heavy; 13).
15. Agenda and comments on the page (careful; 14).
16. Build, commit, CI diff (light; 15).

M4 owner UI and docs
17. API client, store, notes types (medium; 10).
18. Owner controls (careful; 17).
19. Docs (light; last).

M5 e2e
20. CI and local host for e2e (medium; 16).
21. Guest notes e2e spec (careful; 18, 20).

Risks: owner offline for long while guests type fills the 8 MiB log; nothing runs end to end
locally (Playwright in CI only); our public host does not exist yet.
