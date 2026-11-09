# Devices and people: one place, two questions (design proposal)

2026-11-09. Input: the inventory in [2026-11-09-connections-review.md](2026-11-09-connections-review.md).
Status: proposal for Corey; nothing built.

Today the app offers five ways to reach a computer, five pairing ceremonies, six meanings of
"Share", and options whose effect hangs on settings in other tabs (the relay on an AI switch, team
mode as a side effect of "Share this computer"). None of the options is Corey's scenario A, and an
individual sees team-server knobs next to everyday ones.

The proposal answers two questions in one Settings tab, **Devices & people**, in this order:

1. **What is this computer to my other devices?** (scenario A)
2. **Who do I share with, and how?** (scenario B)

Team servers become a third, explicit role that an individual never meets by accident.

---

## 1. The model

### A computer has one role

| Role | What it means | Who picks it |
|---|---|---|
| **Main computer** (default) | Holds the library and does the work: recording, transcription, summaries, models, storage. Other devices use it. | Everyone, at first. Corey's desktop. |
| **Uses my main computer** | Shows the main computer's library and sends its recordings there. Keeps nothing of its own except recordings waiting to go. | Corey's laptop. |
| **Team server** | Several people sign in to it (team mode). Has accounts, roles, records period, flagged phrases. | An admin, on purpose. |

The role is chosen once, in the setup wizard ("Is this your main computer, or do you already have
one?") and in the tab. Choosing it turns on what it needs. The person never sees an API token, a
"Remote access" box or a Save button for these.

### My devices (scenario A)

On the main computer, one list: **My devices**. One button: **Add a device**, which shows one invite
(a code and a QR code). Laptops paste it; phones scan it. Each row says what the device is and what
it can do ("Laptop · uses this computer · last seen 5 min ago", "Phone · sends recordings"), with
Remove.

What a laptop that **uses my main computer** gets:

- **The main computer's library**, live, through the tunnel (today's remote access).
- **Recording with the laptop's own microphone and call audio**, done by the main computer's
  engines. While connected, the laptop's backend captures as it does today and streams each source
  to the main computer as PCM (the browser recorder's `/api/record` stream, which already reconnects
  and is per source). The main computer records it, so live transcription, live speakers, the
  copilot and the final transcription all run on the main computer's GPU. Nothing about the meeting
  differs from one recorded on the main computer.
- **When the main computer cannot be reached**, recording still works. The laptop records into a
  local outbox and says so ("Main computer not reachable: this recording waits here and goes to it
  when it is"). When the main computer is back, the laptop hands each waiting recording over (audio
  and notes typed during it), the main computer transcribes it as if recorded there, and the
  laptop's copy is deleted.
- Optional: **Keep a copy of the library on this laptop** (today's library sync) so meetings can
  be read and searched with the main computer off. Off by default. See decision D2.

What a phone gets: v1 as today (record in the page or pick a file, upload; the main computer
transcribes), but paired from the same **Add a device** button and listed in **My devices**. It
still needs an address the phone can reach (Tailscale today). v2 (later): the phone reaches the main
computer through the relay, as the guest page already does in the browser (guest-wasm), with a
read view of the library.

Two equal main computers that sync (Corey's setup today) stay supported: **Another main computer**,
under "More" in My devices. It is today's library sync with today's rules (each records and works
for itself).

### People (scenario B)

Three ways to share, named by **who the other person is**, under one **Share** button on a meeting:

| In the Share menu | For | They get | They need | Your computer on? |
|---|---|---|---|---|
| **People with Mnemosyne** (contacts) | People you work with regularly | A copy of the parts you pick, kept up to date; they can comment, tick items, type in the notes | Mnemosyne | For updates |
| **A link to the live notes** (guest notes) | Anyone in the meeting, now | Type in the notes, comment, see the agenda | A browser | Yes, while the link is used |
| **A page** (publishing) | Anyone, afterwards | A curated page of the meeting, optionally kept up to date and open to comments | A browser | No to read |
| **Team members** (team server only) | Accounts on the same server | Read, listen, tick, comment, edit notes | An account | It is the server |

In Settings, **People** lists your contacts (your card, add a person, their machines), and **Links**
holds the defaults for pages and live-notes links (expiry, where pages are kept).

### Advanced (collapsed, one place)

Everything a self-hoster or a team admin needs, never shown to an individual by default:
**Servers** ("Use Mnemosyne's relay and notes server", custom relays, publish host and upload token,
guest page address, guest notes host, how guests reach the notes), **Connect to a backend by
address** (today's Backend URL + token, for a team server or Tailscale), and the API token.

### Team server (only in that role)

People and access, Allow members to publish, Allow guest notes, Records period, Flagged phrases,
Use my Tailscale name, Keep serving when I quit, Keep this computer awake. Choosing the role says
what changes: "Others sign in to this computer. Library sync and People with Mnemosyne stop, and
publishing and guest notes are off for members until you allow them."

---

## 2. Behaviour changes it needs

1. **Roles** (`machine_role`: main / satellite / team) replace the inferred mix of `remote_access`,
   `api_token`, `team_mode` and `share_on_network`. A migration maps today's state: team mode or
   sharing on → team; a remote connection in the shell → satellite; else main. The underlying
   settings stay (the role sets them), so nothing else changes at once.
2. **Prerequisites made automatic**: adding a device or a person generates the API token if none,
   turns `remote_access` on and saves. Sync and Contacts no longer say "set an API token above".
3. **One invite for a device**: the invite carries what both remote access and (optional) sync need;
   the laptop does both pairings itself. Phones get the same invite as a QR code.
4. **Laptop records, main computer works** (new): the satellite's backend streams captured PCM to
   the main computer's `/api/record` while connected; an outbox and a hand-off route
   (`POST /api/devices/handoff`: audio parts + notes + times, the main computer is the origin) when
   not. The laptop's backend keeps running in the satellite role, as it does today, for capture and
   the outbox only.
5. **Offline**: the satellite's UI shows a clear "not reachable" state with Record still available
   (to the outbox), instead of a dead window; with a library copy on, it shows the copy read-only.
6. **`use_our_servers` no longer follows the AI tab's "Allow OpenAI and Anthropic"**; it is its own
   switch under Advanced → Servers (automatic = on, except team servers).
7. **One Share button** in the meeting header (menu above), replacing Share / Share / Invite guests /
   Publish… as separate entry points (Publish stays in the menu and in MeetingActions).
8. **Names**: "Share" only for giving people access to a meeting; "Stop sharing" only for that.
   "Remote access" disappears from the UI (it is how My devices works). "Server mode" disappears.
   "This computer" means this computer only.

Fixes that need no decision (do them first, separately): the remote-access status names n0's relays
(`RemoteAccessHome.svelte:60`); a contact's machine is labelled "phone" (`PhoneLink.svelte:130`); the
Publish host label and placeholder disagree (`SettingsPanel.svelte:1388-1389`); "run it with
`--host 0.0.0.0`" is advice the desktop app cannot follow (`:1465`, `routes/mobile.py:77-81`);
`shared_in_*` get switches under People.

---

## 3. Decisions for Corey

- **D1. Laptop streams to the main computer while connected** (the main computer records, live
  work on its GPU), and records locally only when it cannot reach it. Recommended: yes. The
  alternative (always record locally, hand over at stop) is simpler but loses live transcript,
  speakers and copilot from the main computer during the meeting.
- **D2. Offline copy of the library on a laptop** off by default (recordings still work offline).
  Recommended: off by default, one switch. A computer of yours that syncs today as a full library
  stays "Another main computer" (D6) until you switch it to "uses my main computer" with the copy
  on, so nothing you have now is lost.
- **D3. Phone v1** stays upload-only over Tailscale, only re-homed in My devices. Recommended: yes;
  a relay path with a read view is its own milestone.
- **D4. "Share this computer with my team" becomes the Team server role**, with its consequences
  stated. Recommended: yes. Sharing with people who do not run Mnemosyne is covered by live-notes
  links and pages; a team server is for an organization.
- **D5. Mnemosyne's servers stop depending on "Allow OpenAI and Anthropic".** Recommended: yes.
- **D6. Two equal main computers** stay supported under My devices → More. Recommended: yes, as is.
- **D7. Order of work**: fixes first, then roles + Settings tab + automatic prerequisites (no
  behaviour change), then one Share button, then the satellite recording (D1) and offline (D2).

**Answered 2026-11-09:** Corey took every recommendation (D1-D7 as written).

---

## 4. Tasks

Branch `connections`. Verification for every task: single backend test files
(`cd backend && uv run pytest tests/<file> -q`), ruff on touched files, `pnpm check` at 0 errors, Vitest one
file at a time, `cargo check` in src-tauri when Rust changes; the full suite and the e2e run in CI only
(CLAUDE.md: this machine cannot). Never play audio. e2e specs that click a moved or renamed control are
updated by the task that moves it (grep tests-e2e/ for the old label).

The role is derived, not stored: **team** when `team_mode` is on; **uses my main computer** when the desktop
shell is connected through remote access (the frontend knows: `connection` not local + a remote connection);
else **main**. No migration.

1. **Fixes** (medium). The remote-access status naming n0's relays (`RemoteAccessHome.svelte:60`: blank =
   Mnemosyne's relay unless `use_our_servers` is off, then "no relay: same network only"); the device kind
   label (`PhoneLink.svelte:130`: phone / computer / sync / contact from the pairing kinds and sync peers);
   the Publish host label vs placeholder (`SettingsPanel.svelte:1388-1389`); the `--host 0.0.0.0` advice
   not shown in the desktop app (`SettingsPanel.svelte:1465`, `routes/mobile.py:77-81`);
   `resolve_use_our_servers` no longer follows `cloud_models` (config.py: automatic = on except team mode;
   tests; the "Our servers" paragraph of CLAUDE.md and docs that say "cloud_models off").
2. **Automatic prerequisites and the device list API** (careful; 1). Every pairing route that needs an API
   token and remote access (computer, sync, contact and phone codes) makes them itself: a shared
   `prepare_pairing(ctx)` generates and persists `api_token` when empty, turns `remote_access` on, starts the
   link and waits for it (bounded), and answers 409 with a plain reason when it cannot (no sidecar).
   `GET /api/devices` (admin): one list of what is connected to this computer, each with a `kind` (`phone`,
   `computer`, `sync`, `contact`), name, `last_seen_at`, and what it can do (one short phrase per kind).
   `GET /api/system/role` → `main` | `team`. Tests; `pnpm gen:api`.
3. **The Devices and people tab** (careful; 2). The mockup in this doc: a new Settings tab "Devices and
   people" (SettingsPanel.svelte tabs) with sections This computer (the role picker: Main computer / Uses my
   main computer → the RemoteConnect paste flow / Team server → its consequence text, then today's "Share
   this computer" controls), My devices (one list from `/api/devices`; Add a device → a computer invite or a
   phone QR; More: another main computer = SyncSettings), People (ContactsSettings + switches for
   `shared_in_ask`, `shared_in_search`, `shared_in_digest`, `shared_in_obsidian`), Links for people without
   Mnemosyne (where pages are kept, expiry, live-notes end hours), Advanced collapsed (Use Mnemosyne's relay
   and notes server, Relays always visible, Publish host + Upload token, Guest page address, Guest notes host,
   How guests reach the notes, Connect to a backend by address with its API token, this computer's API token),
   Team server section only in the team role (People and access, Allow members to publish, Allow guest notes,
   Flagged phrases, Tailscale name, Keep sharing when I quit, Keep awake). "Server mode" goes; Records stay in
   General. In the uses-main role, a line at the top: "These are your main computer's settings" except for the
   section about this computer. Components keep their logic; only their place and headings change. Labels per
   section 2 point 8 ("Share" only for giving people access).
4. **Setup wizard: the first question** (medium; 3). SetupWizard.svelte: before Audio, "Is this your main
   computer?" — "Yes, it does the work" (today's steps) or "No, I already have one" (paste the invite from
   the main computer's Add a device: the RemoteConnect flow; then Done, skipping engines and model downloads).
5. **One Share button** (medium; 3). SessionView.svelte header: one "Share" menu replacing ShareMenu,
   PeopleShareMenu, GuestNotesMenu and Publish… as separate buttons: "People with Mnemosyne…" (the contacts
   dialog), "A link to the live notes…" (GuestNotesMenu's content), "A page…" (PublishDialog), and in team mode
   "Team members…" (ShareMenu's content). The published badge stays; MeetingActions keeps Publish…; the
   recording screen keeps its own live-notes button. Empty states point to Settings → Devices and people.
6. **Main computer side of a device** (heavy; 2). (a) `PUT /api/system/main` on the laptop's backend, in
   auth.py `LOCAL_PATHS`: the shell tells its own backend the main computer's tunnel URL, token and name when
   remote access connects (and at launch), `DELETE` when it disconnects (src-tauri/src/remote.rs). (b) On the
   main computer, `POST /api/devices/handoff` (DESKTOP devices only): a finished recording from a device
   (audio per source and part, title, started_at, notes markdown) becomes a meeting here, origin this
   computer, with history lines on both sides and the usual finish/auto-transcribe; idempotent by a handoff id.
   Tests with two apps over httpx.ASGITransport as test_sync_*.py do.
7. **The device records, the main computer works** (heavy; 6). On the laptop's backend: `POST
   /api/satellite/start|stop` (local only). Connected: start a browser-style recording on the main computer
   (`/api/audio/start-browser`), capture locally as today, and stream each source's PCM to the main
   computer's `/api/record/{id}/{source}`; always keep the local files too, and if the stream had gaps, send
   the complete local audio through the handoff route before the main computer transcribes (no audio lost on
   a network drop; never replace audio after transcription has started: CLAUDE.md, parts). Not reachable:
   record locally into an outbox and hand it over when the main computer is back (retry with backoff; the
   outbox survives restarts; the local copy is deleted only after the main computer confirms). Demo mode
   and tests use a fake capture (never real audio). Tests for: stream ok, stream with a gap, offline then
   hand-off, restart with a waiting recording.
8. **Device UI** (careful; 3, 7). In the uses-main role: Record goes to the local backend's satellite routes,
   the device list comes from the local backend (this laptop's microphone and call audio), and the recording
   screen shows the meeting from the main computer as usual. Main computer unreachable: a clear state ("Main
   computer not reachable. Recordings are kept here and go to it when it is.") with Record still available and
   "n recordings waiting" when the outbox is not empty, instead of a dead window. "Keep a copy of the library
   on this laptop" in the This computer section turns library sync on with the main computer through the
   existing tunnel (the laptop asks the main computer for a sync invite and redeems it itself, no paste).
9. **Docs and e2e** (careful; 1-8). docs/remote-access.md rewritten around My devices (main computer,
   laptop, phone; the old step lists go), docs/team-server.md (the Team server role), docs/api-reference.md
   (new routes), docs/architecture.md (one paragraph), CLAUDE.md (a roadmap paragraph; Gotchas: the role is
   derived; satellite recordings stream and hand off). e2e: the tab's sections and role picker, the one Share
   menu, the device offline state with a waiting recording (two demo backends, the second on 8038 as
   main-less device), all passing in CI.

Order: 1 → 2 → 3 → (4, 5) and 6 → 7 → 8 → 9. Files shared: SettingsPanel.svelte (1, 3), SessionView.svelte (5,
8), auth.py (6), routes/pairing.py (2), remote.rs (6, 8).
