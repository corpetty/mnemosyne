# A main computer that also hosts a team (investigation + design draft)

2026-11-09. Branch read: `conn-trial` (worktree `.claude/worktrees/connections`, HEAD 50b9ff4). No repository file

Line numbers were taken before #18-#20 merged; re-find them by name before editing.
was changed. All line numbers are from that tree. Paths below are relative to `backend/mnemosyne/` unless they start
with `src/`, `docs/` or `backend/tests/`.

Goal (Corey): his always-on desktop stays his **main computer** (laptop uses it and hands off, library sync with his
other machines, People sharing with contacts) **and** hosts team members who sign in from a browser on the team
listener. A dedicated team server (deploy/team/) keeps today's behaviour.

---

## 0. The facts the design rests on

1. Two kinds of team server already exist and are distinguishable without a new setting:
   - **desktop-hosted**: `team_host.turn_on` sets `team_mode=True, share_on_network=True, team_owner_id=<owner>`
     (services/team_host.py:350-371); `turn_off` clears `team_mode`/`share_on_network` but **keeps
     `team_owner_id`** (team_host.py:391-397).
   - **dedicated**: deploy/team/mnemosyne.service sets only `TEAM_MODE=true` (no owner, no `share_on_network`).
   So "hosting" = `team_mode and team_owner_id` and "dedicated" = `team_mode and not team_owner_id`. This keeps
   CLAUDE.md's rule "the role is derived, never stored; do not add a machine_role setting".
2. Who a request is (api/auth.py):
   - tokenless request from the desktop window on 127.0.0.1, main port, no proxy headers → `Principal(owner, admin)`
     only in team mode with an owner (`_desktop_owner`, auth.py:163-178; `from_desktop_app` auth.py:184-194);
   - the API token → `None` = unrestricted (auth.py:98-99);
   - a user token (users.json) → `Principal(user)` with their role (auth.py:100-107);
   - a DESKTOP device token (paired laptop, sync peer; tunnel or not) → `None` = unrestricted, `state.device_id`
     set (auth.py:118-120);
   - a CONTACT device token → `Principal("p:<16hex>", member)` only on `/api/share/` and only when not
     `team_mode` (auth.py:146-161, 152);
   - a PHONE token → `None`, only DEVICE_PATHS (auth.py:46, 118).
3. Read filtering is one rule in the repository: `access.read_owner()` (access.py:72-75) is non-None **only for
   the member role**; then `_SEES` = own or `share_grants` to me or `'*'` (storage/sqlite.py:1126-1127), applied by
   `_readable` (1129-1138), `visible_ids` (1170-1177), `list_summaries` (1187-1201), `get` (1228-1236). Reviewers
   and admins see **every** meeting, `shared_from` (contacts') meetings included. The WebSocket uses the same rule
   (api/websocket.py:23-46) and lets every event with no `session_id` through to everyone (`meeting(None)` → True,
   websocket.py:29-31).
4. `share_grants` is a VIEW over `session_shares` (team shares, `'*'` = everyone) UNION `peer_shares` as `p:<16hex>`
   (sqlite.py:909-918). A contact principal therefore matches `'*'` team grants too.
5. New meetings get `session.owner_id or access.user_id()` (sqlite.py:1340-1347): `''` for everything that runs
   unrestricted (API token, DESKTOP devices → hand-offs and laptop streams, phone uploads, auto-record, sync apply).
   `adopt_unowned` (sqlite.py:3411-3421) gives every `''` meeting, `shared_from` ones included, to the owner, but only
   once, at `turn_on`.
6. Library sync picks meetings with raw SQL and no owner filter: `changes_since` (sqlite.py:4739-4752: only
   `shared_from = ''`), `serve_changes` (services/sync_service.py:867-898), tombstones (sqlite.py `tombstones_since`,
   carry the meeting **name**). Incoming meetings are written with the peer's `owner_id` verbatim, also on update
   (sqlite.py:4925-4972). `take` touches any existing id after a `created_at` check (sync_service.py:723-760).
   `take_tombstone` calls `sessions.delete_session` directly: no legal hold, no records period, no deletion log
   (sync_service.py:834-854 vs services/records.py:765-786 `check_delete`).
7. Background personal work runs unrestricted (`None`): Obsidian auto-export after every summary
   (services/pipeline.py:405-418, 608-609), the vault read-back over `list_summaries()` (services/vault_sync.py:810-845),
   the digest per user on a team server, written to the **server's** `obsidian_vault_path` for every user
   (api/context.py:484-501, pipeline.py:746-749; `prefs.SETTINGS_FIELDS` has no vault field, prefs.py:38-46).

---

## 1. Inventory: where team mode changes behaviour

### (a) Access model: sign-in, roles, who sees what

| Where | What | Note for hosting |
|---|---|---|
| api/auth.py:83 | `required = api_token or team_mode or tunnel`: sign-in needed | keep |
| auth.py:100-107 | user token → Principal with role | keep |
| auth.py:118-120 | DESKTOP device → unrestricted | on a host these must be the owner's only (pairing is admin-gated, see (b)) |
| auth.py:152 | CONTACT principal refused in team mode | becomes "refused on a dedicated server" |
| auth.py:163-178 | tokenless desktop window = owner (`team_owner_id`) | keep; the "owner channel" |
| access.py:72-75 | only members are filtered | reviewers/admins see the owner's personal meetings and contacts' meetings: must change on a host |
| access.py:78-101 | `can_write`, `can_edit_notes`, `is_admin`, `require_admin` | admin = everything incl. machine settings, sync, pairing, contacts |
| storage/sqlite.py:1126-1236 | `_SEES`, `_readable`, `_check_write` (Hidden = 404), `visible_ids`, `list_summaries`, `get` | the one place to add the host rule |
| sqlite.py:909-918 | `share_grants` view: team shares + peer shares | `'*'` also matches contacts |
| api/websocket.py:23-46 | events filtered for members only; session-less events to all | reviewers hear the owner's live transcript; members hear `meeting_app`, `sync`, `share_pull`, `share_outbox`, `levels`, `capture_health` (bus types found by grep; the list is not exhaustive) |
| api/app.py:160-161 | `/health` `auth_required`, `team_mode` | add nothing secret |
| api/routes/users.py:28, 98-119 | `/api/users/me` returns `team_mode`; `None` principal shown as admin | add a "personal features" flag (task 7) |
| api/routes/system.py:102-111 | `GET /api/system/role` → `team` when `team_mode` | decision D1 |
| api/routes/team.py:42-45, 80 | `_local` + admin to turn hosting on/off | `_local` ignores proxy headers (unlike `from_desktop_app`): `tailscale serve localhost:8008` arrives as 127.0.0.1 on the main port (risk R6) |
| api/context.py:212-218 | `calendar_for`: each person's ICS on a team server | keep |
| services/prefs.py:62, 78 | per-person preferences; share new meetings with the team | keep |
| services/sharing.py:52 | share with invited team members | keep |
| services/notes_items.py:318-321 | link notes items as the meeting's owner (member principal) | keep |
| services/task_links.py:672-682 | `_visible_to(owner)`: linking only reaches the owner's own + granted meetings in team mode | already the right scope; but an owner of `''` → `None` = all (fixed by stamping, task 2) |
| services/live_publications.py:314-316 | auto-updates run as the publisher; `_NOBODY` in team mode when unknown | owner's publications made through the API token have no user → `_NOBODY`; check in task 6 |

### (b) Personal features refused (with why)

| Where | Refusal | Why it exists (what would leak or break) |
|---|---|---|
| api/routes/sync.py:102-104, used at 115, 128, 152, 160, 169 | every sync route 409 | `serve_changes` would ship **every** meeting (members' included, fact 6) to the peer; tombstones carry names; incoming docs could overwrite members' meetings by id; the `ADMIN` dependency would let any team admin call them |
| services/sync_peers.py:218, 313 | pairing for sync | a team admin (not the person) could pair their own computer and pull the whole library and the person key (`/api/sync/person-key`, sync.py:196-215) |
| api/routes/pairing.py:86-94, 157, 242 | sync and contact codes, contact redeem | as above; contacts would bind to "the machine's person" while several people use it |
| pairing.py:147 | `prepare` returns no new API token in team mode | the window must stay tokenless to be the owner (a token makes it the API token's unrestricted caller) |
| services/contacts.py:47-49 (`_check`), routes/contacts.py:25-26 | all contacts | the person key and contacts are one person's; a team admin is not that person |
| api/routes/share.py:36-41 | `/api/share/*` (hub) 409 | contact principals use `share_grants`, which also matches team `'*'` grants (fact 4) |
| api/routes/peer_shares.py:20 | sharing a meeting with a contact 409 | `_check` only needs write access: an admin could share a member's meeting to an outside person |
| services/share_pull.py:156, 216; share_announce.py:50, 79; share_push.py:62, 121, 397 | recipient/hub loops off | same; `shared_from` meetings (owner `''`) would be readable by reviewers/admins |
| services/sync_service.py:435, 582, 621 | contacts/media pulls in sync | same |
| api/routes/handoff.py:276 | "A team server does not take hand-offs" | the new meeting gets owner `''` (fact 5); any team admin could have paired a laptop |
| api/routes/connections.py:63-67 | device list leaves out sync peers and contacts | follows the refusals |
| api/routes/demo.py:23; services/share_demo.py:79 | demo contact seed | follows the refusals (test-only) |

### (c) Defaults that change in team mode

| Where | What |
|---|---|
| config.py:525-535; services/ours.py:36-39 | `use_our_servers` automatic = **off** in team mode: on a host the laptop's remote access and guest notes lose Mnemosyne's relay |
| services/publishing.py:90-92 | publishing off unless `allow_publishing`: applies to the **owner too** |
| services/guest_notes.py:396 | guest notes off unless `allow_guest_notes`: applies to the owner too |
| api/context.py:484-501 | digest per user, each written to the server's vault (fact 7) |
| services/prefs.py:62 | a meeting's work uses its owner's preferences |
| app.py:160 | auth required |
| `cloud_models` | independent of team mode since the connections work (config.py:530-535 docstring); unchanged |
| records (`records_retention_years`, legal hold, Review) | settings are global, not team-only (Settings → General); supervision queue filters with `visible_ids` (sqlite.py:3236-3239) |

### (d) UI

| Where | What |
|---|---|
| src/lib/components/ThisComputer.svelte:19-31, 58-83 | three role cards; Team server card text "Library sync and sharing with people who use Mnemosyne stop" (77-80) |
| src/lib/components/SettingsPanel.svelte:34-46 | `readOnly` for non-admins; `role` = team when `GET /api/system/role` says so |
| SettingsPanel.svelte:1425 | My devices only for `role === 'main'` |
| SettingsPanel.svelte:1442 | People (contacts, `shared_in_*`) only for `role !== 'team'` |
| SettingsPanel.svelte:1603-1640 | Team server section (people, allow switches, flagged phrases, TeamHostSettings options) |
| SettingsPanel.svelte:449 | MyPreferences in General when `team_mode` and signed in |
| src/lib/app/controller.svelte.ts:756, 960, 972 | contacts loaded only without team mode; team state loaded with it |
| src/lib/components/PeopleShareMenu.svelte:30, 39 | "People with Mnemosyne" hidden in team mode |
| src/lib/components/ShareMenu.svelte:22 | "Team members" only in team mode |
| src/lib/stores/publishing.svelte.ts:161; stores/guestNotes.svelte.ts:16 | publish / live-notes allowed = not team mode or allowed |
| src/lib/components/SessionList.svelte:142 | owner badge on others' meetings |

---

## 2. Design

### 2.1 Role model

- **Main computer** (no team mode), unchanged.
- **Main computer hosting a team** = today's desktop-hosted team (`team_mode` + `team_owner_id`). New: every
  personal feature stays on **for the owner**, scoped to the owner's meetings. Only the owner is admin.
- **Team server** (dedicated: `team_mode`, no owner) = exactly today, all refusals kept.
- **Uses my main computer**: unchanged. A computer that hosts a team cannot also use another main computer
  (refuse `turn_on` while `services/main_computer` has one, and `PUT /api/system/main` while hosting).

One derived helper, used everywhere a refusal now says `team_mode`:

```python
# config.py (derived, never stored)
def hosts_team(s) -> bool:           # a main computer that hosts a team right now
    return s.team_mode and bool(s.team_owner_id)
def personal_allowed(s) -> bool:     # personal features exist on this computer at all
    return not s.team_mode or bool(s.team_owner_id)   # False only on a dedicated server
```

(`team_owner_id` without `team_mode` = hosting was turned off; the scoping rule in 2.4 keys on `team_owner_id`
alone, so it still applies then.)

### 2.2 Who is the owner ("owner channel")

On a hosting computer a request is the owner's when it is:
1. the tokenless desktop window (`_desktop_owner`), or
2. the API token, or
3. a DESKTOP device token (a paired laptop / sync peer: only the owner can pair one, rule I3), or
4. the owner's own user token **only on the main port from loopback**, never on `team_port` (browser sessions of the
   owner on the team listener are treated as the team web app: they read as admin but cannot reach personal
   endpoints).

Everything else (members, reviewers, contacts, phones) is not. Implement as a value the middleware puts in
`scope["state"]["channel"]` (`desktop` | `api_token` | `device` | `user` | `contact` | `phone`) plus a dependency
`require_owner_channel` (403 "Only the owner of this computer can do this") used by every personal route.

### 2.3 Visibility rule on a hosting computer

Let H = `team_owner_id`. A meeting is **personal** when `owner_id IN ('', H) OR shared_from <> ''`; else it is a
**team** meeting (owned by a member).

| Caller | Reads |
|---|---|
| owner channel (H) | everything (admin, as today) |
| member | own + `session_shares` to them or `'*'` (as today) |
| reviewer | every **team** meeting + personal meetings granted to them in `session_shares` (never `shared_from`, never peer-share grants) |
| other admin | **not allowed on a host** (rule I2); a dedicated server keeps admins = everything |
| contact (`p:`) | only their `peer_shares` rows: the `'*'` alternative of `_SEES` must not apply to `p:` principals |

Implementation: `access.read_owner()` grows into a small read scope (`ALL`, `MEMBER(uid)`, `TEAM_WIDE(uid)`); the
repository gets `host_owner` (set by `AppContext.apply_settings`) and one SQL fragment per scope. Every reader that
uses `read_owner`/`visible_ids`/`_readable` (repo, websocket.py:26, supervision queue, tasks, comments, search)
inherits it. WebSocket: on a host, session-less events go only to the owner channel unless their type is in an
explicit `TEAM_EVENTS` allowlist (job events keep their own `owner_id` rule).

### 2.4 Scope per personal feature (the core rule)

Two scopes, both computed in SQL from H:

- **OWN(H)**: `owner_id IN ('', H) AND shared_from = ''`, the owner's own meetings.
- **VIEW(H)**: OWN(H) ∪ team meetings granted to H in `session_shares` (H or `'*'`) ∪ `shared_from` meetings
  subject to the `shared_in_*` switches, i.e. what H would see as a member. This is `task_links._visible_to`
  (task_links.py:672-682) plus `''`.

The scope applies whenever `team_owner_id` is set, **not only while hosting**: after hosting is turned off the
members' meetings are still in the database and must never start flowing to peers, contacts or the vault.
Without `team_owner_id` and without team mode (a plain main computer) both scopes are "everything", as today.

| Feature | Scope | Where |
|---|---|---|
| Library sync, serving | OWN | `changes_since`/`serve_changes`; tombstones only for OWN meetings (store the owner on `sync_tombstones` or check at write, sqlite.py:2869, 4802); `/api/sync/media` (sync.py:240) 404 outside OWN |
| Library sync, taking | OWN | `take` refuses (clash) an existing id outside OWN; new meetings stamped H; `take_tombstone` ignores ids outside OWN |
| Sync `owner_id` on the wire | normalise | send `''` for H; on receipt write H when hosting (sqlite.py:4925-4972), so a sync round never resets ownership |
| People shares (owner side) | OWN | `peer_shares` create (peer_shares.py) 403 outside OWN; hub serves only `peer_shares` rows (as now) |
| Shared with me (recipient) | `shared_from`, owner `''`, H-only visibility | share_pull/push/announce run again; reviewers never see them (2.3) |
| Hand-off, laptop stream, phone upload | new meeting owned by H | handoff.py, audio.py `start-browser`, import: stamp H via the repository default |
| Ask, semantic search via Ask | VIEW when asked by the owner | pipeline.py:670-690 runs the job in a VIEW scope |
| Keyword search, library list, Tasks in the owner's UI | everything (admin) | unchanged; owner badge (SessionList.svelte:142). See D3 |
| Weekly digest | owner: VIEW; members: their member view as now; **only the owner's digest is written to the vault** | context.py:484-501, pipeline.py:746-749 |
| Obsidian (auto-export, export-all, daily note, topic/people notes, decisions log, Ask notes, vault read-back) | OWN (+ `shared_from` per `shared_in_obsidian`) | pipeline.py:405-418, routes/export.py (224-360), vault_sync.py:828 |
| Task/decision links | per meeting owner (`_visible_to`) | unchanged; stamping fixes `''` |
| Records sealing, retention | all meetings | unchanged (one database, one chain) |
| Sync tombstone vs records | must respect legal hold and the records period | fact 6: today a peer's delete bypasses `check_delete` |

**A member's meeting shared with the owner** (recommendation, D4): readable live by the owner and his laptop
(remote view through the tunnel), included in his Ask and digest (VIEW), **never** library-synced, People-shared or
exported to Obsidian. Reasons: sync has no unshare (a revoked grant would leave copies on every peer); sync applies
merges as the internal writer with no access checks (sync_service.py `_take_locked`), so a peer's edit to the copy
would come back as a full write to a member's meeting; and the copy would leave the team's records/retention
regime.

### 2.5 Turning hosting on and off (existing data)

- **On**: today's `adopt_unowned(H)` (sqlite.py:3411-3421) but excluding `shared_from` rows; existing sync peers,
  contacts, peer shares, paired devices and the person key are **kept and keep working** (today they silently stop:
  share loops return early, sync routes 409). Other admins in users.json: refuse to turn on, or demote them to
  reviewer with a message (D2). Refuse when this computer uses a main computer.
- **While on**: new meetings by unrestricted callers are stamped H (repository default owner), so `''` never
  appears again except `shared_from` rows; a startup pass adopts stray `''` rows (sync from an old peer).
- **Off**: unchanged (`team_owner_id` kept). Members' meetings stay, owned by them; OWN keeps them out of sync,
  shares and the vault; the owner's UI then sees them as the single local user (no access control without team mode).
  Optional "Remove members' meetings" stays out of scope.

### 2.6 Defaults

- `use_our_servers` automatic: on unless **dedicated** (`resolve_use_our_servers(..., team_mode=team_mode and not
  team_owner_id)`, config.py:530-535, ours.py:36-39; test_ours.py:32-89 cases split into hosted/dedicated).
- Publishing and guest notes: the owner channel is always allowed; `allow_publishing` / `allow_guest_notes` govern
  everyone else (publishing.py:90-92, guest_notes.py:396; stores/publishing.svelte.ts:161, stores/guestNotes.svelte.ts:16).
- `cloud_models`, consent, records period, flagged phrases: global as today. Flagged phrases scan the owner's
  personal meetings too; reviewers only see team ones in Review (via 2.3). See D5.
- `GET /api/settings` (open to members today, routes/settings.py:69-71): on a host, non-owner callers get a reduced
  view without personal fields (vault path, relays, phone URL, sync/contacts settings). See R4.

### 2.7 Security invariants (one test each)

| # | Invariant | Test (file, new unless noted) |
|---|---|---|
| I1 | On a dedicated server (team_mode, no owner) every personal route still refuses as today | keep test_sync_service.py:643-648, test_sync_peers.py:203-258, test_share_hub.py:100/176/425, test_contacts.py:288, test_pairing_prep.py:145/240; add `team_owner_id` = "" explicitly |
| I2 | On a host only H is admin: adding/redeeming/editing a user with role admin is refused | test_hosting_access.py |
| I3 | Personal endpoints (`/api/sync/*`, `/api/pairing/codes|prepare|devices|remote`, `/api/contacts/*`, peer-shares, `/api/connections/*`, `PUT /api/settings` personal fields, `/api/team` change) answer 403 to a member, a reviewer, the owner's own user token on `team_port`, a contact and a phone | test_hosting_access.py (parametrised over routes × callers) |
| I4 | A sync peer of a host receives only OWN meetings, no member tombstone or name, and 404 on a member meeting's media | test_sync_hosting.py (two apps over ASGITransport, as test_sync_service.py) |
| I5 | A sync doc or tombstone naming a member meeting's id never changes or deletes it | test_sync_hosting.py |
| I6 | Meetings arriving by sync, hand-off, laptop stream or phone upload on a host are owned by H; a sync round never resets `owner_id` | test_sync_hosting.py, test_handoff.py (add), test_satellite.py (add) |
| I7 | A reviewer never lists, reads, searches, hears events of, or sees in Review a personal meeting of H that was not granted to them, nor any `shared_from` meeting | test_hosting_visibility.py |
| I8 | A contact principal does not match `'*'` team grants (a contribution to a meeting shared with everyone is refused "not shared with you") | test_share_hub.py (add) |
| I9 | Peer-sharing a member's meeting is refused even for H | test_share_hub.py (add) |
| I10 | Obsidian auto-export, export-all and the vault read-back never write or read a member meeting's note; only H's digest goes to the vault | test_hosting_vault.py |
| I11 | Ask run by H on a host cites no member meeting that was not granted to H | test_hosting_visibility.py |
| I12 | Session-less WebSocket events (meeting_app, sync, share_pull, share_outbox, levels, capture_health, satellite) reach only the owner channel on a host | test_hosting_visibility.py (websocket) |
| I13 | A sync tombstone does not delete a meeting on legal hold or inside the records period (kept, history line, tombstone not passed on) | test_sync_service.py (add) or test_records.py (add) |
| I14 | After turning hosting off, OWN still excludes members' meetings from sync, shares and the vault | test_sync_hosting.py |
| I15 | The owner's publishing and guest notes work with `allow_publishing`/`allow_guest_notes` off; a member's are refused | test_publishing.py, test_guest_notes_routes.py (add) |

---

## 3. Tasks

Verification for every task: single test files only (`cd backend && uv run pytest tests/<file> -q`), ruff on touched
files, `pnpm check` at 0 errors and Vitest one file at a time for frontend tasks, `pnpm gen:api` after model changes.
Never the full suite or builds on this machine; the full suite and e2e run in CI.

1. **Derived helpers and the owner channel** (Careful). `hosts_team`/`personal_allowed` (config.py, beside
   `resolve_use_our_servers`); middleware records `state.channel` (api/auth.py); `require_owner_channel` dependency
   (access.py or api/auth.py); `use_our_servers` automatic on unless dedicated (config.py:530-535, services/ours.py:36-39).
   Files: config.py, api/auth.py, access.py, services/ours.py. Verify: test_ours.py, test_echo_cancel_and_auth.py,
   new test_hosting_access.py (channel matrix only). Deps: none.
2. **Owner stamping and adoption** (Careful). Repository `host_owner` + default owner for new meetings
   (sqlite.py:1340-1347); `adopt_unowned` skips `shared_from` (3411-3421); a startup adoption when hosting;
   `turn_on` refuses with other admins (or demotes, per D2) and while a main computer is set (team_host.py:350-371).
   Verify: test_team_host.py, test_handoff.py, test_satellite.py, test_users.py. Deps: 1.
3. **Visibility on a host** (Heavy). Read scope in access.py (member / team-wide / all); `_SEES`, `_readable`,
   `visible_ids`, `list_summaries`, `get`, `_check_write` use it (sqlite.py:1126-1236); `p:` principals never match
   `'*'` (I8); websocket allowlist for session-less events (api/websocket.py:23-46); reduced settings view for
   non-owner callers (routes/settings.py:69-71). Verify: new test_hosting_visibility.py (I7, I11 later, I12),
   test_team_server.py, test_sharing.py, test_supervision.py, test_comments_team.py. Deps: 1, 2.
4. **Admin and personal routes guarded by the owner channel** (Careful). Only H admin on a host (routes/users.py
   create/update/redeem, cli `users add`); `require_owner_channel` on sync, pairing (all kinds), contacts,
   peer-shares, connections, handoff, settings PUT (or its personal fields), team change (replace `_local` in
   routes/team.py:42-45 by `request_from_desktop_app`). Verify: test_hosting_access.py (I2, I3), test_team_host.py
   (test_a_proxy_on_this_machine_is_not_the_owner). Deps: 1.
5. **Library sync on a host** (Heavy). Replace `_no_team` (sync.py:102-104) and sync_peers.py:218/313,
   pairing.py:86-94/242, sync_service.py:435/582/621 checks by `personal_allowed`; OWN scope in `changes_since`,
   `serve_changes`, tombstones (writing and serving), `/api/sync/media`, `take`/`take_tombstone`; owner
   normalisation on export/write (services/sync_state.py:80, sqlite.py:4925-4972); `take_tombstone` through
   `records.check_delete` semantics (I13). Verify: new test_sync_hosting.py (I4, I5, I6, I14), test_sync_service.py,
   test_sync_peers.py, test_sync_contacts.py. Deps: 2, 3, 4.
6. **People sharing on a host** (Careful). `personal_allowed` instead of `team_mode` in contacts.py:47-49,
   auth.py:152, share.py:36-41, peer_shares.py:20 (plus OWN check, I9), share_pull.py:156/216,
   share_announce.py:50/79, share_push.py:62/121/397, pairing.py:91-94/157, connections.py:63-67, share_demo.py:79,
   demo.py:23; publishing.py:90-92 and guest_notes.py:396 allow the owner channel (I15); live_publications.py:316
   resolves `''` publisher to H on a host. Verify: test_share_hub.py, test_contacts.py, test_contacts_pairing.py,
   test_share_announce.py, test_publishing.py, test_guest_notes_routes.py, test_live_publications.py. Deps: 3, 4.
7. **Hand-off and devices on a host** (Medium). Drop handoff.py:276 for hosts (keep for dedicated);
   `start-browser`/import stamped via task 2; `/api/users/me` and `/api/system/role` expose what the UI needs (D1:
   e.g. `personal: bool`, `hosts_team: bool`), `pnpm gen:api`. Verify: test_handoff.py, test_satellite.py,
   test_users.py. Deps: 2, 4.
8. **Vault, digest, Ask scoping** (Careful). OWN for auto-export (pipeline.py:405-418, 608-609), routes/export.py
   (224-360), vault_sync.py:828; digest: only H's to the vault (context.py:484-501, pipeline.py:746-749), H's digest
   in VIEW; Ask job in VIEW (pipeline.py:670-690). Verify: new test_hosting_vault.py (I10), test_digest.py,
   test_ask.py, test_vault_sync.py, test_obsidian_tasks.py. Deps: 3.
9. **UI** (Careful; design decisions D1/D3 first). ThisComputer.svelte (hosting as part of Main computer or a
   separate card, per D1), SettingsPanel.svelte:1425/1442/1603 (My devices and People shown when hosting; Team
   server section kept), controller.svelte.ts:756, PeopleShareMenu.svelte:30/39, publishing.svelte.ts:161,
   guestNotes.svelte.ts:16 gate on the new `personal` flag instead of `!team_mode`; consequence text at
   ThisComputer.svelte:77-80 rewritten. Verify: `pnpm check`, tests-e2e/devices.spec.ts updated (CI). Deps: 7.
10. **Docs** (Light). docs/team-server.md ("From your desktop": what stays on), docs/remote-access.md (sync no
    longer refused on a hosting computer, only on a dedicated server), docs/api-reference.md, CLAUDE.md (Gotchas:
    OWN/VIEW, owner channel; the Devices-and-people paragraph). Deps: 1-9.

Order: 1 → (2, 4) → 3 → (5, 6, 7, 8) → 9 → 10. Shared files: auth.py (1, 4, 6), sqlite.py (2, 3, 5),
pairing.py (4, 5, 6), pipeline.py (8 only).

---

## 4. Decisions for Corey (each with a recommendation)

- **D1. How the role is shown.** (a) "Main computer" with a "Host a team on this computer" switch, the Team server
  card only describing a dedicated server; or (b) keep three cards and make Team server mean "hosting" on the
  desktop. Recommended: (a); `GET /api/system/role` stays `main` with `hosts_team: true`, `team` only for a dedicated
  server. (UI wording and layout are Corey's call.)
- **D2. Other admins on a hosting computer.** Recommended: not allowed; turning hosting on with other admins is
  refused with "Make them reviewers first" (no silent demotion). Admin on a host means "the owner".
- **D3. Members' meetings in the owner's own library.** Recommended: keep today's admin view (all, with the owner
  badge) and scope only what copies or processes data (sync, shares, Obsidian, digest, Ask). Alternative: a "Team"
  filter so the default library shows only the owner's view (more UI work).
- **D4. A member's meeting shared with the owner**: readable live and in Ask/digest, never synced, People-shared or
  exported. Recommended as stated in 2.4.
- **D5. Records and flagged phrases on the owner's personal meetings.** Recommended: one records period and one
  phrase list for the whole computer (simple, one chain); the owner sets 0/empty if he does not want them for his
  own meetings. Alternative: team meetings only (needs a scope in retention and supervision).
- **D6. Sync tombstones vs records.** Recommended: a delete from a peer never removes a meeting on legal hold or in
  its records period; it is logged in the meeting's history and the tombstone is not passed on. This also fixes the
  same gap on a plain main computer with a records period.
- **D7. MCP server on a hosting computer.** It talks to 127.0.0.1 without a token (mcp_server.py:6, 33-38), so it is
  the owner and sees every member meeting. Recommended: VIEW scope for MCP calls (needs a header or a token of its
  own) or document it.

## 5. Risks

- R1. The visibility change (task 3) touches the one read rule everything uses; a miss leaks a meeting. Mitigation:
  the parametrised I3/I7 tests over every listing route, and a test that each `read_owner` caller goes through the
  new scope (grep in the test).
- R2. Session-less WebSocket events are many and emitted through helpers; the allowlist must default to deny on a
  host. The bus type list in 1(a) came from a grep and is not complete.
- R3. Existing desktop-hosted teams created before this change may have meetings owned `''` (made by the API token,
  sync or hand-offs after `turn_on`) and other admins; the startup adoption and D2 handle them, but they must be
  checked on Corey's machine before turning sync on there.
- R4. `GET /api/settings` gives members every non-secret setting today (vault path, relays, phone URL); on a host
  that is the owner's personal configuration. Task 3 reduces it; the frontend must still work for members with the
  reduced view.
- R5. A DESKTOP device (laptop, sync peer) remains unrestricted on a host: it can read and play members' meetings
  through the tunnel. Accepted because only the owner can pair one (I3); worth stating in docs/team-server.md.
- R6. routes/team.py `_local` accepts proxied requests from 127.0.0.1 (no proxy-header check), unlike
  `from_desktop_app`; task 4 replaces it.
- R7. Digest on a dedicated server writes every member's digest into the server's vault today (fact 7); task 8 fixes
  it for hosts, and the same fix (only when the digest owner is the host owner, else never) should apply to a
  dedicated server too.
- R8. Recording concurrency (the owner recording locally while a member records in the browser) is today's
  desktop-hosted behaviour and not changed or checked here.

## Answered 2026-11-09 (Corey)

- D1: Main computer + a "Host a team on this computer" switch; the Team server card means only a dedicated server.
- D2: only the owner is admin on a hosting computer; turning hosting on with other admins is refused ("make them reviewers first").
- D3: members' meetings stay in the owner's library with the owner badge; only what copies or processes data is scoped.
- D5: **team meetings only** (not the recommendation): records period, legal hold and flagged phrases apply to members'
  meetings; the owner's personal meetings (OWN) stay outside retention and phrase review. This needs a scope in
  retention (services/records.py, storage_service retention pass) and supervision (services/supervision.py) on a host;
  sealing itself stays for every meeting (one chain per meeting, harmless). Add an invariant: I16 retention never
  deletes and the phrase scan never flags an OWN meeting on a host.
- D4, D6, D7: taken as recommended (member meetings shared with the owner never synced/People-shared/exported; peer
  tombstones respect legal hold and the records period; MCP in VIEW scope).
- Order: team hosting first; macOS Phase 0 alongside on files that do not overlap.
