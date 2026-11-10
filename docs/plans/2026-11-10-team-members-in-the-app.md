# Team members in the app (plan)

2026-11-10. Code read at `origin/main` (0.31.0 plus f356d40). Paths are relative to
`backend/mnemosyne/` unless they start with `src/`, `src-tauri/`, `link/`, `docs/` or `backend/tests/`.

Corey's decisions this builds to (2026-11-10): (1) a teammate's app (Mac or Linux, nothing Mac-specific) joins a team
hosted on someone's main computer and shows its own library and the team's side by side; (2) where a team recording's
work runs is the member's choice, the host's machine opt-in; (3) app members reach the host only over iroh to the
tunnel port, never the owner's tailnet or other machines; browser members by Tailscale machine sharing (docs and a
hint, no code); (4) the host's copy of a team meeting is the record; team meetings never leave the host for the
owner's other machines, vault, digest or contacts; a member's personal meetings never reach the host unless put in
the team.

---

## 0. What exists and is reused

- **Sign-in and roles** (services/users.py, access.py): a users.json invite is a hashed one-time code with a chosen
  TTL (f356d40); `redeem` mints a browser token. `admin_refusal` keeps admin = owner on a host.
- **Devices** (services/pairing.py): kinds PHONE / DESKTOP / CONTACT; DESKTOP and CONTACT carry `endpoint_id`. The
  sidecar (link/src/lib.rs `Home::is_paired`, `DeviceEntry`) re-reads paired_devices.json and lets ANY entry with a
  matching `endpoint_id` open `T` (tunnel) streams; `P` streams redeem a code by posting
  `/api/pairing/redeem {code, name, endpoint_id, peer?, card?}` to the backend. The kind is not read by the sidecar,
  so a new device kind needs no link change.
- **Who a request is** (api/auth.py): channels `desktop | api_token | device | user | contact | phone | local`;
  `OWNER_CHANNELS` = desktop, api_token, device, local; `is_tunnelled` from the tunnel listener
  (services/tunnel_listener.py); `require_owner_channel` on every personal route; reads through
  `repo.read_scope()` / `_sees` (storage/sqlite.py:1160-1236); owner stamping `create_session` (sqlite.py:1428-1431:
  `session.owner_id or access.user_id() or host_owner`).
- **Satellite** (services/satellite.py, api/routes/satellite.py): capture here, PCM streamed to the main computer's
  `/api/record/{rid}/{source}` after `POST /api/audio/start-browser` (audio.py:223-242 stamps `by_device` from
  `state.device_id`); outbox + `POST /api/connections/handoff` (api/routes/handoff.py: multipart `meta` + WAV/Ogg
  per source and part, idempotent by `<device id>:<handoff id>`, REPLACE only before transcription and only by the
  device that streamed, `_sender` accepts DESKTOP devices and the owner). `app.main_computer`
  (services/main_computer.py: url, token, name) is the one destination today.
- **Sidecar control channel** (services/link.py `LinkService.request`, link/README.md): `pair` (from this machine's
  own endpoint, returns the token and the redeem reply's `card`), `dial` (serves a peer's backend on a loopback port
  this machine's requests go to), `hangup`; `on_ready` hooks re-dial after a restart (services/sync_peers.py
  `dial_all`). This is how library sync reaches a peer without the desktop shell. `prepare_pairing`
  (services/pairing_prep.py) makes the API token and starts the sidecar.
- **One portable meeting** (models/sync_session.py `SyncSession`; services/sync_state.py `export_session`;
  sqlite.py:5086 `apply_synced(s, origin, vv, bump, owner)` which never takes the document's owner and, for
  `origin == ""`, leaves status, audio_file and recordings rows to this machine).
- **Frontend**: one connection (src/lib/stores/connection.svelte.ts: `url`, `token`, `remote`, `localToken`); every
  API call, `/ws`, `/ws/notes/{id}` and `<audio>` URLs build on `connectionState.url` (api/backend.ts:116 `base()`,
  stores/websocket.svelte.ts:17, notes/doc.svelte.ts:108, browser-capture.ts:199). RemoteConnect.svelte repoints it
  and reloads. api/local.ts talks to `LOCAL_BACKEND` with `localToken` for the satellite routes.

---

## 1. Design

### 1.1 Joining: a MEMBER device

A team invite stays what it is (`POST /api/users` / `POST /api/users/{id}/invite` → `Invite.code`). On a hosting
computer the response grows `app_invite: str | None` = `<home ticket>#<code>` (`ctx.link.status.ticket`, made by
`prepare_pairing(ctx, link=True)`; `app_invite_problem: str` when the sidecar cannot run). One code, two forms: the
browser link `https://<host>:8443/?invite=<code>` and the app invite; whichever is used first uses the code up.

Redemption runs over the sidecar's `P` stream exactly like pairing a laptop: the member's own backend asks its sidecar
`pair(invite, name)`; the host's sidecar posts `/api/pairing/redeem` with the vouched `endpoint_id`. In
api/routes/pairing.py `redeem`, before the pairing-code branches: when `ctx.pairing.code_kind(code)` is None and
`ctx.users.invited(code)` is a user → the member branch:

- refuse (409) unless `config.hosts_team(ctx.settings)` (a dedicated server has no sidecar: later), the user is not
  disabled, and `endpoint_id` is set (400 "This invite is redeemed by the app through remote access");
- `users.use_invite(code) -> User` (new: consumes the invite, mints NO browser token);
- `pairing.add_member(name, endpoint_id, user_id) -> (PairedDevice, token)`: a new kind `MEMBER = "member"` with a
  new field `user_id: str | None`; one device per endpoint per user (an earlier device of that endpoint for the same
  user is replaced; a different user on the same endpoint is refused: one person per machine, as contacts);
- reply `RedeemResponse(device, token, sync=None, card={"team": {"name": <host name>, "user": {id, name, role},
  "consent_required": ...}})`: the `card` field is already an opaque pass-through in the sidecar (`PairReply.card`).

**Who the token is** (auth.py): a new branch before the DESKTOP/PHONE one: `device.kind == MEMBER` →
`is_tunnelled(scope)` required (else 401: an app member's token is never accepted on the main port or the team
listener), `user = ctx.users.get(device.user_id)` must exist and not be disabled, then `state.device_id`,
`state.user_id`, `state.channel = MEMBER_DEVICE` (new constant `"member"`, NOT in `OWNER_CHANNELS`) and
`access.current = Principal(user.id, user.name, user.role)`. Everything downstream is the browser member's rule:
`_sees`, `can_write`, `can_edit_notes`, `require_admin`, `require_owner_channel` (403), websocket visibility,
`records_apply` (team meeting), supervision. `_desktop_owner` cannot apply (tunnelled). `is_owner_channel`: the
`USER` clause needs `loopback_main`, so a MEMBER token never qualifies even for the owner's own user id.

**Revocation**: `pairing.members_for(user_id)` / `revoke_members(user_id)`. routes/users.py `change_user` with
`disabled=True` and `sign_out_everywhere` call it, so the sidecar (which re-reads the file per connection and
stream) cuts the connection and the token dies; `verify` also checks `user.disabled` on every request, so a disabled
user is out before the file is re-read. `UserInfo.devices` lists MEMBER devices beside browser tokens (`device` =
the app's name, e.g. "Pat's MacBook (app)"). A member leaving (member side, 1.2) calls `DELETE /api/pairing/me`
(new, open to the device itself: revokes only the calling device) best-effort, then forgets locally.

**Several devices per person**: each device redeems its own invite (as browsers do today) → several MEMBER devices
with the same `user_id`. `connected_devices` (api/routes/connections.py) leaves MEMBER devices out: they are
people, shown under People and access, not "my devices".

### 1.2 The member side: the local backend proxies the team

Recommendation: the member's **own backend** holds the team connection and proxies it; the UI never holds the team
token and the desktop shell (remote.rs) is not involved at all.

Why: (a) the token stays out of the webview and localStorage (today's remote-access token sits there; no need to
add a second); (b) the shell has no API token and need not learn one: the backend already has the sidecar, the
`dial` op and `on_ready` re-dialing (sync_peers.py:173-195), so joining is `prepare_pairing` + `pair` + `dial`,
the same three steps `pair_with_invite` does for sync, and identical on Linux and macOS; (c) the outbox worker, the
reachability probe and the recording destination live in one process that already owns them (satellite.py); (d) the
dial port is OS-picked and changes at every sidecar restart: a UI that talked to it directly would have to ask the
backend for it anyway. Cost: an HTTP + WebSocket forwarder (~250 lines, httpx streaming + `websockets`), and one
extra loopback hop. The alternative (UI → dial port with the token, as RemoteConnect does today) saves the
forwarder and nothing else.

`services/team_member.py`: `TeamStore` at `<data_dir>/team.json` (0600: host ticket + endpoint id, host name, the
member token, `user {id, name, role}`, `work: "here" | "host"`, `signed_out_at`), `join(invite, name)`, `leave()`,
`dial()` (kept `listen`; re-dialed from a `link.on_ready` hook and at startup), `reachable()` (a `/health` probe
through the dial port, cached like `Satellite.reachable`), and a `Destination(url="http://127.0.0.1:<listen>",
token, name)` with the same three fields `MainComputer` has, for satellite.py. A 401/4401 from the host marks
`signed_out_at` (the UI says "You were signed out of <team>; ask for a new invite") and stops the proxy with 409.
Joining is refused while this computer uses a main computer (`ctx.main_computer`), hosts a team, or is a dedicated
server; v1 is one team per computer.

`api/routes/team_member.py`, prefix `/api/joined`, every route `require_owner_channel` plus satellite.py's
`_this_computer` (never through the tunnel or the team port: another machine must not drive this one's team
membership): `GET /api/joined` (status: joined, host name, user, reachable, work mode, consent required on the
host, pending moves), `POST /api/joined/join {invite, name}` (runs `prepare_pairing(ctx, link=True)` first; the
window takes a new local API token the way `localSyncPair` does via `/api/pairing/prepare`), `DELETE /api/joined`,
`PUT /api/joined/work {work}`, and the forwarder `/api/joined/{path:path}` for `path` starting `api/`, `ws`, `ws/`
or `health` only: method, query (minus `token`), headers (minus `authorization`, `host`, proxy headers) and a
streamed body go to `http://127.0.0.1:<listen>/<path>` with `Authorization: Bearer <member token>`; the response
streams back with status and headers (range requests for audio, records export zips, `read=None` timeouts for Ask).
WebSockets (`/api/joined/ws`, `/api/joined/ws/notes/{id}`) are bridged both ways, close codes carried. The member's
`/api/record/...` stream is not proxied: satellite.py opens it itself on the destination.

**Frontend**: the window switches libraries by repointing the one connection, as RemoteConnect already does:
`connectionState.library: 'mine' | 'team'`; team = `url = LOCAL_BACKEND + '/api/joined'`, `token = localToken`
(persisted under `mnemosyne.connection` with `library`, so the choice survives a reload). Nothing in
api/backend.ts, websocket.svelte.ts or notes/doc.svelte.ts changes: they build on `connectionState.url`. The host
then answers `/api/users/me` as the member (`team_mode`, `hosts_team`, `owner: false`, `role`), so the UI's existing
member behaviour (read-only settings, MyPreferences, owner badges in SessionList.svelte:142, `canChange`) applies
unchanged. Required adjustments: `isLocal` (a URL with a path is not local: `LOCAL_URL` already fails it),
`usesMain` stays "remote access to a main computer"; add `inTeam`; satellite.svelte.ts `follow()` polls the local
backend in the team library too (its `connectionState.url !== LOCAL_BACKEND` test); `ownsPersonal` false in the
team library (it is, via `me.owner`). The switch is a soft one: `connectionState.save(...)` then
`controller.connect()` again (no `location.reload()` if the stores reset cleanly; else reload as RemoteConnect
does). "Side by side" in v1 = both libraries one click apart in the sidebar, each shown whole with the recording pill
and outbox visible from either; a merged list would need every session-bound API call to know its backend (open
question Q1).

**What the member's machine does with team meetings**: nothing. Search, Ask, tasks, people, topics, digest, Obsidian,
library sync, contacts and the MCP server on the member's machine read the member's own database, which never holds a
team meeting (no session is made locally for the team, as satellite.py does today for the main computer). In the team
library those features are the host's, run by the host under the member's principal (search, Ask and tasks already
respect `_sees`; Obsidian is the host's vault and refuses non-owners: `host_scope.require_vault_owner`). No local
cache: offline, the team library shows "Team not reachable" (SatelliteBanner-style) and the switch back to "My
library" (Q2 says no copy).

### 1.3 Recording into the team: two work modes

Where the work runs is a **per-member default on the member's machine** (`team.json work`, Settings → Devices and
people → Your team) with a **per-recording override** in the Record control (a destination menu: My library / Team,
on <host> / Team, here). Default `here` (Corey: own resources by default, the host opt-in). Per meeting is where
the choice matters (a long call on a slow laptop goes to the host; a quick one stays), per member saves asking
every time; a per-team setting on the host would take the choice away from the member.

**(a) The host does the work** = the satellite path with the team as destination. satellite.py: `start(...,
destination: "main" | "team")`; `OutboxItem.destination`; `_run_outbox` resolves each item's destination at send time
(`app.main_computer` or `team_member.destination(app)`), skipping items whose destination is gone; `reachable(dest)`;
status answers per destination (`SatelliteStatus.team: SatelliteMain | None`, `team_reachable`). On the host the
member's MEMBER token starts a browser-style recording as the member (owner = member via `access.user_id()`,
`by_device` = the MEMBER device), live transcript, live speakers and copilot run there, the final transcription per the
host's `auto_transcribe`, gaps and offline periods go through the hand-off as today. handoff.py `_sender` accepts
MEMBER devices (sender name = user name + device); REPLACE keeps its `by_device` check, so a member can only complete
a meeting their own device streamed; a new meeting is stamped the member. `levels` and `capture_health` are
session-less bus events (team-hosting plan 1(a)), so a member's recording screen has no meters, as the laptop's does
not today.

**(b) The member's machine does the work** = record into the local library with a `for_team` mark, then MOVE. The
mark lives in `<data_dir>/team_moves.json` (`services/team_move.py`: pending session ids with added_at, attempts,
last_error; a `moved` list mapping local id → host id for the UI). A pending meeting moves when it has no active job
(finish, transcribe, summarize, copilot), is transcribed (or has no audio), and the host is reachable; retried with
satellite.py's BACKOFF; "Put in the team…" in MeetingActions adds any local meeting the member owns to the same list
(refused for `origin`/`shared_from` meetings: the audio is not here, or it is someone else's).

**The format: hand-off v2.** The same `POST /api/connections/handoff` multipart, with `HandoffMeta` extended by
`transcribed: bool = False`, `session: SyncSession | None` (the member's `sync_state.export_session`, `origin` and
`owner_id` ignored on receipt) and media parts `media:<sha>` for the rows in `session.media` (WebP/JPEG, type by magic
number, sha checked before anything is stored: the rule every receive path follows). Audio goes as today: one Ogg per
`Recording` row with its `part` (handoff.py already converts Ogg to WAV per part and re-joins with `add_part`, so the
joined timeline the transcript's times refer to is rebuilt from the same files in the same order). Host side,
`_take`: when `transcribed`, `_finish_all` runs with `want_transcribe=False`, then (a new step after the last part's
save) applies the document with `repo.apply_synced(s, origin="", vv={}, bump=True, owner=<member>)`, followed by the
post-apply steps `sync_service._take_locked` runs today (notes doc union via services/sync_notes.py, media rows,
`settle_notes_items`), factored into one helper both call; then `supervision.scan(replace=True)`,
`sharing.share_with_invitees`, `task_links.reconcile` for the meeting, `history.log("handed_over", transcribed=True,
parts=n)`. The record (seals, versions) starts on the host at arrival; the member's history events travel in
`session.events` (uids), so consent, recording and transcription lines are in the host's history. A hand-off marked
`transcribed` for a REPLACE is refused (409): a streamed meeting is completed with audio only.

Why not SharedMeeting: it is a redacted projection of chosen parts for a contact; the host needs the whole meeting.
Why not library sync: sync is bidirectional between one person's machines with version vectors and tombstones; here
the meeting changes hands once.

**After the move the host's copy is the only one**: the local session is deleted once the host answered with its
id (idempotent by `handoff_id` = the local session id, so a lost answer does not make two), the move is logged in
the backend log and in `moved`, and the UI switches to the team library at the new meeting. Renames, transcript and
summary edits, notes, comments and ticks are done on the host through the proxy as the member, under the host's
records rules (versions, seals, `check_delete`, legal hold). Nothing flows back. One record, one place.

### 1.4 Live notes, comments, copilot, presence

All host-side through the proxy: `/api/joined/ws/notes/{id}` is the host's relay (`can_edit_notes` as the member:
own meetings and those shared with them), comments via the host's routes, presence carets as today. Copilot: mode (a)
runs on the host for the member's recording (its `copilot_notes` events are session-scoped, so the member hears
them); mode (b) runs locally during the recording and its result travels in `SyncSession.copilot_notes` and the
notes' Copilot section (`notes_sections` are in the notes markdown and doc). Images pasted into a mode (b) meeting's
notes travel as media parts (1.3).

### 1.5 Consent and records

- Mode (a): unchanged: start-browser answers 400 without consent when the host's `require_consent` is on
  (audio.py:252-255); satellite treats 400 as "record nothing". The member's UI must ask with the HOST's rule:
  `GET /api/joined` carries `consent_required` (the local backend reads the host's `/health`), and the recording
  controller uses it for team destinations (today it reads `connectionState.consentRequired` of the connected
  backend, which in "My library" is the member's own).
- Mode (b): the UI asks at start for a `for_team` recording when the host requires it; the consent event is logged
  locally and travels. Enforcement: the host refuses a `transcribed` hand-off without a `consent` event in
  `session.events` when `require_consent` (409 "The team asks how the people in the meeting agreed to be recorded";
  the move stays pending with that error and the UI offers to add it: `POST /api/joined/moves/{id}/consent`).
- Records: a team meeting made by either mode is a member's meeting on a hosting computer: `records_apply` true,
  retention, legal hold, flagged phrases, deletions log all apply on the host (services/records.py, supervision.py
  need no change). Audio reaches the host in both modes. Deleting from the app goes through the host's `check_delete`.

### 1.6 Access invariants (one test each; new file backend/tests/test_member_devices.py, reusing
`test_hosting_access.py`'s `machine()`/`Probe` helpers with a `member_app` caller: tunnelled, MEMBER token)

| # | Invariant |
|---|---|
| M1 | A MEMBER token is accepted only through the tunnel: 401 on the main port from 127.0.0.1 and on the team listener |
| M2 | Its channel is `member`, never an owner channel: every `require_owner_channel` route (sync, pairing codes/devices/remote, contacts, peer shares, `/api/connections/devices`, `/api/satellite/*`, settings PUT, `PUT /api/team`) answers 403; admin routes (`/api/users` POST/PATCH) 403 |
| M3 | It reads only its own meetings and team grants: another member's meeting 404, the owner's personal meeting 404, a `shared_from` meeting 404; a reviewer's MEMBER device reads team-wide (TEAM_WIDE) and never the owner's unshared ones |
| M4 | `start-browser` and a hand-off by a MEMBER device make meetings owned by that user; a REPLACE of a meeting another device streamed is 409; a `transcribed` REPLACE is 409 |
| M5 | Disabling the user or "sign out everywhere" revokes its devices and the token answers 401 at once; `pairing.verify` of a revoked token is None |
| M6 | Redeeming a user invite over `/api/pairing/redeem` works only with an `endpoint_id`, only on a hosting computer, and uses the code up (the browser link then fails) |
| M7 | `/ws` for a MEMBER device hears no session-less event outside `TEAM_EVENTS` (test_hosting_visibility.py's websocket test, one more caller) |
| M8 | The member's own backend: `/api/joined/*` is 403 through the tunnel and on the team port; the forwarder never forwards a path outside `api/`, `ws`, `health`; the forwarded request carries the member token and not the local one; a 401 from the host marks the membership signed out |
| M9 | The host cannot reach the member's library: the member's sidecar has no entry for the host's endpoint (link's existing "not paired" test, asserted once more with a MEMBER-kind entry present in the member's own file) |
| M10 | A team meeting made by a hand-off v2 is owned by the member, `origin ""`, `shared_from ""`; it is inside OWN/PERSONAL/VIEW exclusions as any member meeting (test_sync_hosting.py: a sync peer never receives it; test_hosting_vault.py: never exported) |
| M11 | A member's personal meeting never reaches the host: no local session is created for a destination-team recording; only meetings in `team_moves` are sent; `origin`/`shared_from` meetings are refused |

---

## 2. UI changes needed (design is Corey's; this is the list)

- **TeamPeopleSettings.svelte**: the invite box shows both forms (browser link, app invite) when `app_invite` is set,
  with the problem text otherwise; the per-person device list shows app devices.
- **TeamHostSettings.svelte**: a hint for browser members off the LAN: Tailscale machine sharing of this computer
  only (share the machine to their account, an ACL for port 8443; link to docs/team-server.md "Members from home").
- **New `TeamMembership.svelte`** (Settings → Devices and people, section "Your team", shown in the main role when
  not hosting): paste an invite + this computer's name → Join; when joined: host name, who I am there, work-mode
  default (here / on the host), pending moves with Retry / Add consent / Remove from the queue, Leave.
- **Sidebar / SessionList**: the library switch (My library / <team name>) with the team's unreachable state; in the
  team library the existing owner badge and `canChange` do the rest.
- **Record control** (HomeView/AppHeader record button): destination menu when joined (My library / Team on <host> /
  Team, here); consent dialog unchanged but driven by the host's rule for team destinations.
- **SatelliteBanner / OutboxList**: wording per destination ("goes to <team>"), moves listed beside outbox items.
- **MeetingActions.svelte**: "Put in the team…" for local meetings when joined (confirm: it leaves this library).
- **AppHeader**: the connection label names the library.
- **ThisComputer.svelte**: "Uses my main computer" and "Join a team" are exclusive; text says so.
- Setup wizard: no new step in v1 (join from Settings).

---

## 3. Tasks

Verification everywhere: single backend test files (`cd backend && uv run pytest tests/<file> -q`), ruff on touched
files, `pnpm check` at 0 errors, `pnpm gen:api` after model changes, `cargo test -p mnemosyne-link` only in T1,
Vitest one file at a time. Full suites and e2e in CI only. Never a device, never audio (FakeCapture).

| # | Task | Tier | Deps | Files | Verify |
|---|---|---|---|---|---|
| T1 | **MEMBER devices on the host**: `MEMBER`, `user_id`, `add_member`, `members_for`, `revoke_members` (services/pairing.py); `users.use_invite` (services/users.py); the member branch in `redeem` and `DELETE /api/pairing/me` (api/routes/pairing.py); the `MEMBER_DEVICE` channel in api/auth.py (tunnel-only, disabled check, Principal); revocation on disable / sign out (api/routes/users.py); `UserInfo.devices` with app devices. A link test that `is_paired` accepts a `member` entry and refuses a missing one (link/src/lib.rs tests). | Careful | none | pairing.py, users.py, auth.py, routes/pairing.py, routes/users.py, link tests | new test_member_devices.py (M1-M3, M5-M7), test_echo_cancel_and_auth.py, test_users.py, test_pairing_prep.py; `cargo test -p mnemosyne-link` |
| T2 | **The app invite**: `Invite.app_invite` / `app_invite_problem` via `prepare_pairing(ctx, link=True)` on a hosting computer; `pnpm gen:api`; TeamPeopleSettings shows both forms and app devices; TeamHostSettings Tailscale hint. | Medium | T1 | routes/users.py, TeamPeopleSettings.svelte, TeamHostSettings.svelte, schema.d.ts | test_users.py (invite with/without sidecar), `pnpm check` |
| T3 | **Hand-offs and streams from members**: `_sender` accepts MEMBER (name from the user), owner stamping asserted, REPLACE rules (M4); `start-browser` as a member asserted. | Medium | T1 | routes/handoff.py, tests | test_handoff.py, test_satellite.py (add), test_member_devices.py (M4) |
| T4 | **The member's backend joins and proxies**: services/team_member.py (store, join via `prepare_pairing` + `link.request("pair")`, dial + re-dial on `on_ready`, reachable, Destination, signed-out handling), api/routes/team_member.py (`/api/joined` status/join/leave/work + the HTTP and WebSocket forwarder), AppContext wiring (startup dial, shutdown hangup), `pnpm gen:api`. | Heavy | T1 (end-to-end test) | services/team_member.py (new), api/routes/team_member.py (new), api/context.py, api/app.py | new test_team_member.py: two apps, the host served by uvicorn on a free port as test_satellite.py does, the member over ASGITransport with `listen` pointed at it (no iroh in tests: `TeamStore.listen` settable); forwarder cases: GET/PUT/range/stream, WS `/ws` and `/ws/notes`, token swap, path refusal, 401 → signed out (M8) |
| T5 | **Satellite destinations**: `destination` on start/items/status, per-destination worker and probe, `_this_computer` unchanged; api/local.ts and stores/satellite.svelte.ts follow. | Careful | T3, T4 | services/satellite.py, api/routes/satellite.py, src/lib/api/local.ts, src/lib/stores/satellite.svelte.ts | test_satellite.py (the existing cases run once per destination; main gone while team stays) |
| T6 | **Hand-off v2 and moves**: host receive (`transcribed`, `session`, media parts, apply + post-apply helper shared with sync_service `_take_locked`, supervision/share/reconcile/history, consent refusal, REPLACE refusal); member services/team_move.py (pending list, worker with BACKOFF, `move(session_id)` building the multipart from `export_session` + Ogg per recording + media, local delete after the answer), routes `POST /api/joined/moves/{session_id}`, `GET`, `DELETE`, `/consent`; `for_team` on the local start routes (audio.py start/import: a flag that only adds to the pending list). | Heavy | T3, T4 | routes/handoff.py, services/sync_service.py (factor the helper), services/team_move.py (new), api/routes/team_member.py, routes/audio.py | new test_handoff_v2.py (two apps over ASGITransport as test_handoff.py: transcript, summary, notes doc, comments, media and events arrive; owner = member; consent 409; idempotent retry; local copy gone only after the answer; M10, M11) |
| T7 | **Consent and host rule in the member's UI**: `consent_required` in `/api/joined`, used for team destinations; the pending-move consent flow. | Medium | T5, T6 | controller.svelte.ts, stores/satellite.svelte.ts, TeamMembership.svelte | Vitest controller/satellite store tests; test_team_member.py (status carries the host's rule) |
| T8 | **Frontend**: library switch (connection.svelte.ts `library`, `inTeam`; soft reconnect), TeamMembership.svelte, Record destination menu, banners and moves list, "Put in the team…", header label, ThisComputer text. Design decisions are Corey's (section 2). | Careful | T4, T5, T6 | listed in section 2 | `pnpm check`; Vitest connection.test.ts, satellite.test.ts; tests-e2e/team-members.spec.ts (see T9) |
| T9 | **Docs and e2e**: docs/team-server.md (members in the app; "Members from home" with Tailscale machine sharing: share only the host machine, ACL to 8443; app members need none of it), docs/remote-access.md (one line), docs/api-reference.md (`/api/joined`, hand-off v2, `app_invite`, MEMBER devices), docs/architecture.md paragraph, CLAUDE.md (roadmap paragraph + Gotchas: MEMBER tokens tunnel-only, the proxy, moves delete the local copy). e2e: a demo seam `POST /api/demo/join-team {url, token}` that sets the member's TeamStore to a direct URL (no iroh in CI) against the second demo backend (8058) in hosting mode: switch, read a team meeting, record with destination team (FakeCapture), put a local meeting in the team. | Careful | T1-T8 | docs, tests-e2e/team-members.spec.ts, routes/demo.py | CI green (the e2e job) |

Order: T1 → (T2, T3, T4 in parallel) → (T5, T6 in parallel: T6 keeps the member side in team_move.py so it does not
touch satellite.py) → T7 → T8 → T9. Shared files to watch: routes/handoff.py (T3, T6), api/routes/team_member.py
(T4, T6, T7), routes/users.py (T1, T2).

---

## 4. Risks

- R1 The forwarder is a second door into the host: it only ever adds the member token and only to the dial port of
  the one joined team; M8 and `_this_computer` keep other machines from using it. A bug that forwarded the local
  token would hand the member's own API token to the host: the token-swap test is the one that matters.
- R2 WebSocket bridging under load (live transcript events, notes frames at typing speed) and long streams (a 1 GB
  records export) through httpx: test the stream cases; keep the forwarder dumb (no buffering of bodies).
- R3 Mode (b) rebuilds the joined timeline on the host from the same Ogg files; `add_part` measures with ffprobe,
  so the offsets match. A member meeting with a `combined` history (two meetings joined locally) is still one set
  of parts in order; assert a two-part meeting in test_handoff_v2.py.
- R4 Sync-apply helper extraction (`_take_locked`'s post-apply steps) touches library sync; run test_sync_service.py,
  test_sync_notes.py, test_sync_media.py after T6.
- R5 The member's sidecar announces the member's machine by mDNS on the office LAN (`Lan::Advertise`), as any
  computer with remote access on does; its tunnel listener accepts no endpoint (M9). Acceptable; say so in docs.
- R6 A user disabled while mid-recording in mode (a): the stream closes 4401, satellite marks a gap and queues a
  hand-off that will be refused; the item ends `failed` with the reason (existing behaviour).
- R7 The redeem route is open (OPEN_PATHS): the member branch must not reveal whether a code is a user invite beyond
  success/failure; same error text as an invalid pairing code.

- R8 (orchestrator) A Mac teammate needs the Mac app first: PR #24 (capture sidecar) merged and released. Mode (a) on a
  Mac runs satellite.py over capture.py's darwin switch (mnemosyne-capture writes the same WAV header byte for byte,
  checked), but satellite capture has never run on a real Mac: the teammate's first mode-(a) recording is its test.
- R9 (orchestrator) Browser members through Tailscale machine sharing: under the default allow-all policy a sharee
  reaches EVERY port on the host. Docs must give the grant (`autogroup:member` → `*`; `autogroup:shared` →
  `<host 100.x>` `tcp:8443`), warn that `src: "*"` includes sharees and that a grants section denies whatever no rule
  matches, and note tagged machines cannot be shared and Tailnet Lock must sign the sharee's node.

## 5. Open questions for Corey

- Q1 Side by side = a library switch in the sidebar (one list shown at a time, the recording pill and outbox visible
  from both), recommended for v1. A merged list needs per-session backend routing across api/backend.ts.
- Q2 Mode (b) MOVES the meeting (the local copy is deleted after the host confirms), recommended: one record. The
  alternative, a read-only local copy, is a second copy outside the team's retention.
- Q3 Default work mode `here` with the host opt-in per recording, as said; confirm the override belongs in the
  Record control rather than in the consent step.
- Q4 Dedicated team servers taking app members: out of v1 (they run no sidecar). Confirm.

## 6. Out of scope

Dedicated servers with app members; more than one team per computer; an offline cache of the team library; app
members inviting others; phone members; the setup wizard joining a team; Windows; changes to the link crate beyond a
test.
## Answered 2026-11-10 (Corey)

- Q1: a library switch in the sidebar (My library / <team>), one shown at a time.
- Q2: a moved meeting is deleted on the member's machine once the host has it.
- Q3: a default in Settings plus a choice per recording in the Record button's menu.
- Q4: dedicated team servers stay out of v1 (not asked; out of scope as planned).
- The Mac app (PR #24) is not merged first: it ships together with this.
