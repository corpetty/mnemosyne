# Connections review: syncing, connecting and sharing (facts only)

2026-11-09, on main at abb1d5a (0.30.0). An inventory of every user-facing option that connects
Mnemosyne to another machine, person or server, written as input for a reorganization. It
describes what the code does today. It proposes nothing, apart from the short list of
observations at the end. Line numbers are for this commit.

Corey's target model, for reference:
- **A. One person, several devices.** An always-on desktop does the work (recording,
  transcription, models, storage). The laptop and phone sync with it and use its resources.
- **B. Other people.** He shares notes, summaries and so on with the people he works with.

---

## Inventory

### Where the UI puts things

Settings has five tabs: General, Recording, Transcription, AI, Notes & sharing
(`src/lib/components/SettingsPanel.svelte:391-395`). The connection options are split between
**General** and **Notes & sharing**. Each meeting's header has its own buttons.

**Settings → General**, top to bottom:

| # | Block (heading as shown) | Component | Shown when | Saved how |
|---|---|---|---|---|
| 1 | (My preferences) | MyPreferences | team mode, signed-in person (`SettingsPanel.svelte:416`) | own route |
| 2 | **Share this computer with my team** | TeamHostSettings | `can_change`: desktop app on that machine and admin (`routes/team.py:40-44,61`) | takes effect at once (`PUT /api/team`) |
| 3 | **People and access** | TeamPeopleSettings | team mode and admin (`SettingsPanel.svelte:427`) | at once |
| 4 | Records, Flagged phrases | RecordsSettings | not read-only | Save button |
| 5 | **Connection** (Backend URL, API token, Connect, "Use local backend") | inline | always; Backend URL hidden when the page was served by a backend (`:448-475`) | localStorage of this window, reload |
| 5a | → **Remote access** (paste invite, "This computer's name", "Connect through remote access" / "Disconnect") | RemoteConnect | desktop app (Tauri) only, not when served by a backend (`:476-478`, `RemoteConnect.svelte:63`) | Tauri command, reload |
| 6 | Troubleshooting, Encryption | | | |
| 7 | **Server mode**: API token; checkbox "Remote access: let computers you pair reach this backend from anywhere"; "Relays (comma-separated)" (only while that box is ticked) | inline (`:1461-1490`) | always | Save button at the bottom of the tab |
| 7a | → **Remote access** status + "Pair a computer" | RemoteAccessHome (`:1491`) | not read-only | invite at once |
| 7b | → **Sync with your other computers** ("Invite a computer", paste box + "Sync", peer list) | SyncSettings (`:1492`) | not read-only | at once |
| 7c | → **People you share with** (fingerprint, "Show my invite", "Paste their invite" + "Pair", contact list) | ContactsSettings (`:1493`) | not read-only and not team mode | at once |
| 7d | → **Phone address** | inline (`:1494-1498`) | always | Save button |
| 7e | → **Record from a phone** ("Pair a phone", QR, **Paired devices** list) | PhoneLink (`:1499`) | `/api/server/phone` answers | at once |
| 8 | Window, Updates, Storage, Backup | | | |

**Settings → Notes & sharing**, at the end of the tab: the **Publishing** section
(`SettingsPanel.svelte:1369-1446`) holds:
- "Use Mnemosyne's servers", which also governs the relay used by remote access, sync,
  contacts and guest notes (`:1376-1384`; `services/ours.py:1-10`);
- "Publish host", "Upload token", "New publications expire after";
- "How guests reach the notes", "Guest page address", "Guest notes host", "End guest notes
  after a recording stops (hours)";
- for team admins only, "Allow guest notes" and "Allow members to publish";
- the publications list.

**Meeting header** (`SessionView.svelte:209-225`):
- "Published · vN" badge;
- **ShareMenu** ("Share", team mode only);
- **PeopleShareMenu** ("Share", desktop only, not team mode);
- **GuestNotesMenu** ("Invite guests");
- "Publish…" in MeetingActions (`MeetingActions.svelte:151-160`);
- a second GuestNotesMenu next to the Copilot on the recording screen (`SessionView.svelte:324`).

**Setup wizard**: its steps are Welcome, Audio, Transcription, Summaries, Notes & calendar and
Done (`SetupWizard.svelte:24`). None of them is about connections. A laptop meant to be a client
of the desktop still sets up audio and engines, and downloads models (`prepare`, `:71`).

### Settings (backend/mnemosyne/config.py)

| Setting | UI label and place | What it does | Who it is for | Interactions |
|---|---|---|---|---|
| `api_token` (`:242`) | General → Server mode → "API token" (`SettingsPanel.svelte:1466-1478`). The Connection block has a second "API token" field: the token this window *sends*. | Every `/api` request and `/ws` must carry it (`api/auth.py:69-73`). Open paths: `/health`, `/m`, redeem (`auth.py:36-45`). | A machine others reach | Needed before **any** pairing: phone, computer, sync, contact (`routes/pairing.py:88-92`, `contacts.py:56-63`, `sync_peers.py:224`). Tunnelled requests (127.0.0.2) always need a token, even without it (`auth.py:16-22,72-73`). The individual never types it anywhere else; each device gets its own token. |
| `team_mode` (`:245`) | no direct control; switched on by "Share this computer" (`team_host.py:366-368`) or set on a dedicated server | Sign-in, roles, per-person meetings (access.py, users.py) | Team server | Refuses library sync (`sync_peers.py:218`, `pairing.py:94-97`) and contacts (`pairing.py:98-101`, `contacts.py:47`). Turns publishing and guest notes off until an admin allows them (`publishing.py:85-87`, `guest_notes.py:380`). Turns the automatic `use_our_servers` off (`config.py:532-537`). Hides ContactsSettings and PeopleShareMenu, shows ShareMenu. |
| `share_on_network` (`:249`), `team_port` 8443 (`:250`), `team_owner_id` (`:251`) | "Share this computer with my team" → "Share this computer" / "Stop sharing" (`TeamHostSettings.svelte:73-193`) | A second HTTPS listener on all interfaces with a self-signed cert. It serves the web app and the API; the owner is made admin and gets every unowned meeting (`team_host.py:1-12,355-371`). Off: listener closed and team mode off (`:389-397`). | Small group on a LAN or tailnet | Implies `team_mode` (all of the above). Its address is also the base for "This computer" publications (`publishing.py:808-818`) and for team invite links (`TeamPeopleSettings.svelte:25-29`). |
| `keep_sharing_after_quit` (`:253`) | "Keep sharing when I quit Mnemosyne" (TeamHostSettings `:158-169`) | The backend keeps serving after the app quits (app_watch) | Team host | Only shown while sharing is on |
| `keep_awake_while_sharing` (`:255`) | "Keep this computer awake while it is shared" (`:170-188`) | Sleep inhibitor the whole time (services/awake.py) | Team host | Same |
| `team_tailscale_cert` (`:257`) | "Use my Tailscale name (no browser warning)" (`:127-147`) | `tailscale cert`, served by SNI on the team port | Team host on a tailnet | One of **three** separate Tailscale uses (see Confusion points) |
| `share_with_invitees` (`:260`) | **no UI** | On a team server, share a meeting with the team members whose calendar email was invited (services/sharing.py) | Team server | Admin-only via config |
| `web_dir` (`:263`) | none (the release app sets `WEB_DIR` itself, `src-tauri/src/lib.rs:728-729`) | The backend serves the web app (`api/app.py:166-169`) on the main port too, not only on the team listener | | So a Tailscale-served 8008 also opens the web app (read from the code, not tried) |
| `phone_url` (`:281`) | General → Server mode → "Phone address" (`SettingsPanel.svelte:1494-1498`) | Base of the phone page addresses (`routes/mobile.py:58-67`) | Individual with a phone | Also the base of "This computer" publication links when the team listener is off (`publishing.py:819-820`) |
| `remote_access` (`:284`) | "Remote access: let computers you pair reach this backend from anywhere" (`SettingsPanel.svelte:1480-1483`) | Runs `mnemosyne-link home`: an iroh endpoint that tunnels paired computers to 127.0.0.1:8008, announces itself by mDNS, and dials sync peers and contacts (`services/link.py:1-10`, `api/context.py:379-386`) | Individual | **Required by** "Pair a computer", library sync and contacts (`pairing.py:102-103`, `sync_peers.py:220`). **Not** required by guest notes, which run a separate `rooms` sidecar (CLAUDE.md, phase 10). |
| `remote_relays` (`:289`) | "Relays (comma-separated)", **only visible while Remote access is ticked** (`SettingsPanel.svelte:1484-1490`) | Relays for home, sync, contacts **and guest notes** (`ours.py:39-45`; docs/guest-notes.md:143) | Self-hosters | Changing it means pairing computers again (docs/remote-access.md:95-99). It governs guest notes even with Remote access off, but is hidden then. |
| `use_our_servers` (`:294`, None = automatic) | Notes & sharing → Publishing → "Use Mnemosyne's servers" (`SettingsPanel.svelte:1376-1384`) | Default relay `relay.petty.services` and default host `notes.petty.services` (`ours.py:19-58`) | Everyone | Automatic = on, **except** with team mode on or **`cloud_models` off** (an AI-tab setting) (`config.py:532-537`). Sent only once the person touches the box (`SettingsPanel.svelte:268`). |
| `publish_host`, `publish_host_token` (`:301-302`) | "Publish host (blank: none, links are served by this computer)" and "Upload token" (`:1387-1399`) | Hosted publications; the guest-notes mirror when `guest_host` is blank | Individual or team | The label says blank = none, but the placeholder says "Blank: Mnemosyne's host" (`:1388-1389`, `blankHost` `:111`) |
| `allow_publishing` (`:305`) | "Allow members to publish" (team admins only, `:1438-1443`) | Publishing on a team server | Team server | Off by default, and that includes the owner of a "Share this computer" desktop (`publishing.py:85-87`) |
| `publish_default_expiry_days` (`:307`) | "New publications expire after" | Default expiry | | |
| `guest_host` (`:312`) | "Guest notes host" (`:1423-1427`) | Room host or mirror for guest notes | | Falls back to `publish_host`, then ours (`ours.py:60-64`) |
| `allow_guest_notes` (`:313`) | "Allow guest notes" (team admins only) | | Team server | Same trap as `allow_publishing` |
| `guest_notes_end_hours` (`:314`) | "End guest notes after a recording stops (hours)" | Auto-end of the room | | |
| `guest_transport` (`:318`) | "How guests reach the notes": Automatic / "Straight to this computer (iroh)" / "A tunnel to this computer" / "A publish host" (`:1408-1417`) | auto = iroh through the relay, else a tunnel (Tailscale Funnel, else Cloudflare quick tunnel), else the host (docs/guest-notes.md:72-99) | | iroh uses `remote_relays`/ours; the tunnel uses Tailscale **Funnel** |
| `guest_page_url` (`:319`) | "Guest page address" | Where the guest page loads from (GitHub Pages by default) | Self-hosters | |
| `shared_in_ask`, `shared_in_search`, `shared_in_digest`, `shared_in_obsidian` (`:443-446`) | **no UI** (no match in `src/`) | Whether meetings shared *with* you by contacts enter Ask, the semantic index, the digest and Obsidian | Contacts recipients | Config only |
| `cloud_models` (`:228`) | AI tab, "Allow OpenAI and Anthropic" (`SettingsPanel.svelte:1024-1027`) | Cloud LLM providers | | **Also** turns the automatic `use_our_servers` off (relay and host) |
| `remote_stt_url` etc. (`:211-213`) | Transcription → "Remote OpenAI-compatible server"; Live transcriber "Remote server" | A remote speech-to-text endpoint | | Not a connection to another Mnemosyne, but uses the word "remote" |

State kept outside config.py:
- `<data_dir>/paired_devices.json`: phones, computers, sync peers' DESKTOP devices and contacts'
  CONTACT devices, all in one list (`services/pairing.py:1-12,31-40`).
- `<data_dir>/sync_peers.json`.
- The `contacts` table, and the person key in `services/person.py`.
- `<data_dir>/link.key`: the home iroh key (`link.py:4`).
- In the desktop app, `<app data>/remote.json` and `remote.key`, the laptop-side tunnel
  (`src-tauri/src/remote.rs:1-7`).

### Features: mechanism, prerequisites, other side, data, interactions

| Feature (UI name, place) | Mechanism | For | Prerequisites | Other side needs | What goes where | Interactions |
|---|---|---|---|---|---|---|
| **Connection: Backend URL + API token** (General → Connection) | The window's UI talks to another backend's HTTP/WS directly (`stores/connection.svelte.ts:1-80`) | Individual with Tailscale/LAN; team members | A reachable address (`tailscale serve`, or `--host 0.0.0.0`, which the desktop app cannot do: it always binds 127.0.0.1, `lib.rs:717`) | Nothing; this side is a thin client | Everything stays on the backend; this window holds only url+token in localStorage | The same block also holds RemoteConnect |
| **Remote access, home** (General → Server mode, "Pair a computer") | iroh endpoint (`mnemosyne-link home`); mDNS on the LAN, relay elsewhere; pairs a DESKTOP device whose token opens the **whole API** (`pairing.py:1-12`, `auth.py:109`) | Individual | api_token, remote_access, link sidecar, a relay unless on the same LAN | The desktop app (RemoteConnect) or `mnemosyne-link connect` | No data moves; the other side reads and writes through the tunnel | Also the transport for sync and contacts. Paired computers are listed under "Record from a phone → Paired devices" (`PhoneLink.svelte:124-136`) |
| **Remote access, client** (General → Connection → Remote access) | The Tauri shell redeems the invite with its own key (`remote.key`), keeps a tunnel on 127.0.0.1:8009 and points the UI at it (`remote.rs:3-7,20,180-212`). It restarts at launch (`:152-160`) | Individual | The desktop app | A home with remote access | None stored locally. The laptop's own backend still starts and keeps running, unused by the UI (`lib.rs:1212-1214`) | `isLocal` false (`connection.svelte.ts:59-63`); Settings now edits the **home's** settings |
| **Library sync** (General → Server mode → "Sync with your other computers") | Both backends PULL each other's changes over HTTP through the iroh tunnel every 30 s and on poke (`sync_service.py:1-35,118`); three-way merge; a meeting is whole (`models/sync_session.py:223-259`) | One person, 2+ computers | remote_access + api_token on **both** (`SyncSettings.svelte:89-90`; `sync_peers.py:215-225`) | Another Mnemosyne desktop of the same person | Meetings, transcripts, summaries, notes, comments, publications (key included), peer shares; contacts and the person key travel too (`sync_service.py:424-470`). **Audio never travels**: playback streams from the origin (sync rules doc, §3). Settings, voice profiles and glossary are not in SyncSession. | Refused in team mode. Pairing creates a DESKTOP device on the inviter (`routes/pairing.py:224-238`), so a sync peer also has whole-API access |
| **Phone pairing** (General → Server mode → Phone address + "Record from a phone") | One-time code → a PHONE token that opens only `/api/audio/import` and `/api/pairing/me` (`auth.py:46,109`); the `/m` page records or picks a file and uploads it (`routes/mobile.py:19-23`) | Individual | api_token; a phone-reachable address: `phone_url` (e.g. `tailscale serve`) or the backend bound off-loopback (`mobile.py:58-67`). The desktop app binds 127.0.0.1, so in practice Tailscale | A phone browser | Audio goes up to the backend, which transcribes it. The phone reads nothing (docs/remote-access.md:40-41) | No iroh path for phones. The team listener's addresses are not offered here (`phone_page_urls` uses only phone_url and the bind host) |
| **Share this computer with my team** (General, top) | HTTPS listener `:8443` + web app + invite-link sign-in (`team_host.py`) | Small group | Network reach (LAN, tailnet), firewall port | A browser; an invite link per browser (`TeamPeopleSettings.svelte:89-104`) | Everything stays on this computer; browsers record over `/api/record` WS (`routes/record.py:1-10`) and it transcribes | Turns team mode on: no sync, no contacts, the automatic "our servers" off (so the remote-access relay changes), publishing and guest notes off until allowed |
| **Share** (team) (meeting header, ShareMenu) | `session_shares` rows (services/sharing.py) | Team | team mode (`ShareMenu.svelte:73`) | An account on the same server | Nothing moves; the shared people can read, listen and tick action items (`ShareMenu.svelte:84`). They may comment (CLAUDE.md, phase 5) and edit live notes (`shared_notes.py:284-298`) | |
| **Contacts / "People you share with"** (General → Server mode) | Signed person cards; a per-pair secret derives CONTACT tokens that open only `/api/share/*` (`contacts.py:1-10`, `auth.py:100-108`). Fingerprint compared aloud (`ContactsSettings.svelte:138-143,185-194`) | Individuals with Mnemosyne | remote_access + api_token on both, not team mode (`ContactsSettings.svelte:134-136`) | A Mnemosyne desktop with the same setup; both paste each other's invite | Nothing until a meeting is shared | Contacts travel in library sync (`contacts.py:9`), so each of the person's machines pulls by itself (`share_pull.py:22-24`) |
| **Share** (people) (meeting header, PeopleShareMenu → SharePartsDialog) | The owner's machines are the hub. They serve a signed `SharedMeeting` with only the ticked parts (`shares.py:1-8`). The recipient pulls every 60 s and on poke (`share_pull.py:1-24,53`) and sends contributions back (share_push.py) | Individual to individual | A contact (above) | The contact's desktop | Parts: summary, decisions, questions, action items, chapters, topics, attendees, notes (with history), transcript (off by default), hide identifiers (`models/shared_meeting.py:20-36`). No audio (`origin.py:61-64`). The recipient may comment, tick and type in shared notes (`SharePartsDialog.svelte:102`; `shared_notes.py:289-291`) | Shown on desktops not in team mode, for meetings that are not themselves shared_from (`PeopleShareMenu.svelte:21-23`). "Add a person" jumps to Settings → General (`:66-69`) |
| **Guest notes** (meeting header "Invite guests", recording screen "Invite guests to the notes") | A sealed room. By default: iroh through the relay to a per-room endpoint of the `rooms` sidecar, mirrored on ours or `guest_host`. Else a tunnel (Tailscale Funnel or Cloudflare). Else the host (docs/guest-notes.md:72-99) | Anyone in a meeting | The link sidecar. A relay or a tunnel or a host. Not `shared_from`. On a team server, `allow_guest_notes` | A browser, no account | Only the live notes, comments on them, the agenda and images (docs/guest-notes.md:3-6) | Uses `remote_relays` (hidden unless Remote access is ticked). Ends `guest_notes_end_hours` after the recording stops |
| **Publish → "This computer"** (Publish dialog, Where) | The backend serves `/p/<id>` (also on the team listener) | Anyone who can reach this computer | Team listener running, or `phone_url`; otherwise only this machine can open it (`publishing.py:808-821`; `PublishDialog.svelte:283-289`) | A browser | A curated encrypted snapshot (no transcript, chosen quotes and clips) | No live updates or comments (`PublishDialog.svelte:330-331`). Under library sync it is served only by the computer that made it (docs/publishing.md:186-189) |
| **Publish → "Mnemosyne's server" / "Publish host"** | Encrypted versions uploaded to a host; the key is in the URL fragment | Anyone with the link | Ours (automatic) or `publish_host` (+token). Expiry ≤ 365 days | A browser | Snapshot on the host (ciphertext). Optional **Keep the link up to date** and **Readers may comment** / **Approve comments** (`PublishDialog.svelte:306-329`) | Automatic versions come only from the origin machine (`live_publications.py:12-13`). Readers' comments wait in the host's room until the owner's bridge is online (`:14-31`) |
| **Publish → "A file"** | One `single.html`, optional PBKDF2 password | Anyone handed the file | none | A browser | A self-contained snapshot | Not a publication: no update or unpublish (docs/publishing.md:46) |

Other ways out, not reviewed here: Slack/Matrix posts, GitHub/Linear/Jira issues, HubSpot,
Obsidian export, the MCP server, follow-up drafts. They live in Notes & sharing under their own
headings.

---

## Scenario A today (one person: desktop base, laptop, phone)

### What "remote access" gives the laptop

The laptop's app becomes a **thin client of the desktop's backend through the tunnel**:
- The Tauri shell holds an iroh key (`<app data>/remote.key`) and a local tunnel on
  127.0.0.1:8009. The UI's connection points at that tunnel, with the token the pairing returned
  (`remote.rs:3-7,180-212`; `RemoteConnect.svelte:35-44`).
- Every view, search, Ask, summary, export and setting is the desktop's. Settings on the laptop
  edit the desktop's configuration.

The laptop **still runs its own backend**, with its own library: the shell starts it at every
launch whatever the connection (`lib.rs:1212-1214`). The UI simply does not use it.

**Recording from the laptop's UI records the desktop**:
- The device list comes from the backend the UI is connected to (`stores/audio.svelte.ts:124`,
  `api.getDevices`). Settings says so: "Recording happens where the backend runs"
  (`SettingsPanel.svelte:453`; docs/remote-access.md:47-48,72-73).
- The browser recorder could send the laptop's microphone to the desktop over `/api/record`.
  The UI turns it on only when the page was served by a backend (team listener or `web_dir`) or
  when a hidden localStorage flag `mnemosyne.recordInBrowser=1` is set
  (`audio.svelte.ts:23-29,48`). That flag is used by the e2e tests and has no UI.
- **So, from the app, the laptop cannot record locally and hand the work to the desktop.** The
  only routes are "Import" (record elsewhere, upload the file: the desktop then transcribes it),
  or the hidden flag.

When the desktop is unreachable, nothing works until the laptop chooses "Use local backend"
(`SettingsPanel.svelte:472-474`), which reloads the window onto the laptop's own, separate
library.

### What "library sync" gives the laptop

**Two full libraries** that converge:
- Each computer records, transcribes and summarizes on its own. The machine that recorded a
  meeting is its `origin` forever (`services/origin.py:1-7`).
- Transcription, recovery, auto-summarize, auto-export, the copilot and live transcription run
  only on the origin (sync rules doc §4, `docs/plans/2026-10-14-library-sync-rules.md:73-81`).
  The desktop answers 409 "The audio is on Laptop" to Transcribe (`origin.py:61-70`).
- A manual summarize runs anywhere. A meeting recorded on the laptop is therefore transcribed by
  the **laptop's** engines (usually Parakeet on the CPU) and auto-summarized by the **laptop's**
  LLM settings. The desktop can re-summarize it by hand afterwards.
- Audio stays on the laptop. The desktop streams it from the laptop only while the laptop is
  reachable (sync rules doc §3).
- Settings, voice profiles and the glossary are per machine: SyncSession carries meeting data
  only (`models/sync_session.py:223-259`). Contacts and the person key do travel
  (`sync_service.py:424-470`).

So library sync gives "both libraries the same". It does not give "the desktop does the work".

### Can the two be combined?

Yes, as two independent pairings with **two iroh identities on the laptop**: the shell's
`remote.key` for the tunnel and the laptop backend's `link.key` for sync.
- Sync needs remote access **on the laptop's backend too** (plus an API token there).
- The laptop's UI shows one backend at a time. Switching between "remote" and "local" reloads
  the window.
- While the UI points at the desktop, the laptop backend keeps syncing in the background, since
  it is always started.
- Configuring the laptop's own sync means switching the UI back to the local backend first.

### What the phone gets

The `/m` page: record in the page (HTTPS) or with the phone's recorder app (plain http), then
upload. The phone's token opens only `/api/audio/import` and `/api/pairing/me` (`auth.py:46`).
It cannot read meetings, notes or tasks (docs/remote-access.md:40-41).

Reaching the desktop needs one of:
- `phone_url`, in practice `tailscale serve --bg localhost:8008` plus Tailscale on the phone
  (docs/remote-access.md:17-31);
- a backend bound off-loopback, which the desktop app cannot do (`lib.rs:717`).

There is no iroh or relay path for phones. The team listener would give a phone browser the full
web app, but that means team mode, and PhoneLink does not offer those addresses
(`mobile.py:58-67`).

### Step by step for A today

1. **Desktop**, Settings → General → Server mode: set an API token, tick Remote access, then
   **Save settings** at the bottom of the tab.
   - Wait for "On". The status line wrongly says "through n0's free public relays" when Relays
     is blank (`RemoteAccessHome.svelte:60`); since 0.27.0 the default is Mnemosyne's relay
     (`ours.py:39-45`).
2. **Laptop as a thin client.**
   - Desktop: "Pair a computer", copy the invite.
   - Laptop, Settings → General → Connection → Remote access: paste the invite, name the
     computer, "Connect through remote access".
3. **Or the laptop as a full library** (sync).
   - Laptop: set an API token and tick Remote access, Save.
   - Desktop: "Sync with your other computers" → "Invite a computer", copy.
   - Laptop: paste under Sync → "Sync".
4. **Phone.**
   - Install Tailscale on the desktop and the phone, then run
     `tailscale serve --bg localhost:8008` on the desktop (a terminal step).
   - Put the https name in "Phone address" and Save.
   - "Pair a phone", then scan the QR code.

### Where it falls short of A

- No mode where the laptop records with its own microphone and the desktop transcribes and
  summarizes. Remote access records the desktop's devices; sync makes the laptop do its own
  work.
- No offline fallback for the thin client: when the desktop is unreachable the laptop's UI is
  dead until switched to a different library by hand.
- The desktop's models are not used for the laptop's own recordings under sync. Auto-work runs
  on the origin, and a manual re-summarize on the desktop is the only way to use them.
- The phone is upload-only and needs Tailscale plus a terminal command. Remote access, the relay
  and pairing do not apply to it.
- Three separate pairing ceremonies (computer, sync, phone), each with its own invite. The
  shared prerequisites (API token, Remote access, Save) are spread over one long tab.
- Thin client and sync are separate identities with no combined option.

---

## Scenario B today (sharing with other people)

| | Team: "Share this computer" + Share | Contacts + Share (phase 8) | Guest notes link | Publish: Mnemosyne's server / host | Publish: This computer | Publish: A file |
|---|---|---|---|---|---|---|
| Other person needs | A browser + an invite link; network reach to :8443 (LAN/tailnet) | **Mnemosyne desktop** with API token + remote access, a contact pairing with fingerprint check | A browser | A browser | A browser that can reach the team listener or `phone_url` | A browser, the file (+ password) |
| What they get | Their own account; shared meetings: read, listen, tick items, comment, edit live notes; can record their own meetings in the browser | A signed copy of the chosen parts on their machine; comment, tick shared items, type in shared notes; transcript optional, no audio | Live notes only: type, comment on notes, see agenda, add images | Read-only curated snapshot (summary parts, chosen quotes with clips, live notes optional); optional comments, moderation | Same snapshot, read-only | Same snapshot, read-only |
| Live or snapshot | Live (same server) | Near-live: pull every 60 s + poke (`share_pull.py:53`) | Live | Snapshot; "Keep the link up to date" republishes within ~1 min | Snapshot (manual Update) | Frozen |
| Corey's computer on? | Yes, all the time they use it (`TeamHostSettings.svelte:76-79`) | For updates, yes (one of his machines on the card answers the round, `share_pull.py:5-8`); their copy reads offline | Yes over iroh or a tunnel; the host mirror keeps a copy after (docs/guest-notes.md:94-104) | No for reading; yes for automatic versions and to take in reader comments | Yes | No |
| Revoke | Unshare, disable the user | "Stop sharing" (tombstone); copies already sent stay (`ContactsSettings.svelte:93`) | End / New link / "Stop sharing the notes" | Unpublish | Unpublish | Impossible |
| Side effects | Team mode: kills sync and contacts, turns the automatic "our servers" off, disables publishing and guest notes until allowed | Needs remote access, which makes his desktop reachable to paired computers | Keeps the computer awake | Ciphertext on a third-party (or our) host | Needs the team listener (team mode) or `phone_url` | |

Notes:
- The two "Share" buttons never appear together: ShareMenu is team mode only
  (`ShareMenu.svelte:73`), PeopleShareMenu is non-team only (`PeopleShareMenu.svelte:21-23`).
  They have the same label and different semantics.
- With "Share this computer" on, Corey's own desktop is a team server. He loses contacts,
  library sync and the default relay, so his paired laptop away from home may lose its relay
  path. He also cannot publish or open guest notes until he ticks the admin options.

---

## Confusion points

### Overlapping names

1. **"Share"** means at least six different things:
   - "Share this computer with my team" (a server mode);
   - ShareMenu "Share" (team ACL);
   - PeopleShareMenu "Share" (a signed copy to a contact);
   - "Stop sharing" in TeamHostSettings (closes the server, `:189`);
   - "Stop sharing the notes" in guest notes (`GuestNotesMenu.svelte:254`);
   - "Notes & sharing", the tab with Publishing and guest-notes settings but no sharing.
   "People you share with" (contacts) sits in General → Server mode, not in "Notes & sharing".
2. **"Remote"** covers:
   - "Remote access" (iroh home, in Server mode);
   - "Remote access" (client, in Connection);
   - the "Remote OpenAI-compatible server" transcriber and "Remote server" live transcriber;
   - `remote_speaker_name` "Remote" (the label for the system-audio channel);
   - "a remote backend" in the Connection text.
   Two sub-headings are both called "Remote access" in the same tab, one for each side
   (`RemoteAccessHome.svelte:54`, `RemoteConnect.svelte:65`).
3. **"This computer"** covers:
   - the publish target (served here);
   - "Share this computer";
   - guest transport "Straight to this computer";
   - "This computer's calendars";
   - "This computer's name" (RemoteConnect);
   - "This computer:" in SyncSettings (its sync name).
4. **"Server"** covers:
   - "Server mode" (a section holding remote access, sync, contacts and phone, which are all
     individual features);
   - "team server";
   - "Mnemosyne's server" (the publish host);
   - "Use Mnemosyne's servers";
   - "transcription server".
5. **"Home"** is used in code and docs (`mnemosyne-link home`, "home machine",
   docs/remote-access.md), never in the UI. The UI says "this backend" and "another machine".
6. **"Pair" / "invite"**: there are five ceremonies.
   - "Pair a computer" (remote access), "Invite a computer" (sync), "Show my invite" (contact):
     the same `<ticket>#<code>` string shape (`routes/pairing.py:110`), each accepted only in
     its own box.
   - "Pair a phone" (a QR code with `?pair=`).
   - Team "invite link" (`?invite=`, a person's sign-in).
7. **"Paired devices"** lists phones, remote-access computers, sync peers (as DESKTOP devices)
   and contacts' machines in one list under **"Record from a phone"** (`PhoneLink.svelte:124-136`;
   `routes/pairing.py:241-243` lists every kind). The kind label is
   `d.kind === 'desktop' ? 'computer' : 'phone'` (`PhoneLink.svelte:130`), so **a contact's
   machine is labelled "phone"**. RemoteAccessHome says "remove it from the list below"
   (`:94`), meaning that list.
8. **"Relay"**:
   - Remote access status text still names n0 (`RemoteAccessHome.svelte:60`).
   - GuestNotesMenu has a "free testing relay" state (`relay_kind 'n0'`,
     `GuestNotesMenu.svelte:135-136`).
   - The settings say "Mnemosyne's relay".

### Options whose effect depends on another

9. `use_our_servers` lives in **Notes & sharing → Publishing**, but it decides the relay for
   remote access, sync and contacts in **General** (`SettingsPanel.svelte:1376-1384`; `ours.py`).
   Its automatic value flips off with **`cloud_models`** (AI tab) or team mode
   (`config.py:532-537`). Unticking "Allow OpenAI and Anthropic" can therefore remove the relay
   and the default publish host, and paired computers away from home lose their path.
10. `remote_relays` is visible only while "Remote access" is ticked (`SettingsPanel.svelte:1484`),
    yet it also routes guest notes, which do not need remote access (docs/guest-notes.md:143).
11. API token: labelled "Server mode … if the backend requires one", "leave empty for
    local-only use" (`:1464-1477`). In practice it is the prerequisite for phone, computer, sync
    and contact pairing, though the person never types it anywhere: every pairing issues its own
    token. Sync and Contacts show "Turn on remote access and set an API token above" until both
    are **saved** (`SyncSettings.svelte:89-90`, `ContactsSettings.svelte:134-136`). They read
    `settings.values`, not the unsaved form.
12. "Share this computer with my team" silently switches `team_mode` on (`team_host.py:366-368`).
    That stops library sync and contacts, hides "People you share with", flips the automatic
    "our servers" off, and turns publishing and guest notes off for the owner too
    (`publishing.py:85-87`, `guest_notes.py:380`). Nothing on the button says so.
13. `phone_url` ("Phone address") also decides where "This computer" publications can be opened
    (`publishing.py:819-820`). The Publish dialog tells the person to turn on team sharing
    instead (`PublishDialog.svelte:285`).
14. "Publish host" label says "blank: none, links are served by this computer". The placeholder
    beside it says "Blank: Mnemosyne's host" (`SettingsPanel.svelte:1388-1389`).
15. Server mode text says "run it with `--host 0.0.0.0`" (`SettingsPanel.svelte:1465-1466`), and
    PhoneLink's note says the same (`mobile.py:77-81`). The desktop app always binds 127.0.0.1
    (`lib.rs:717`), so for app users this advice cannot be followed.
16. In remote mode, the laptop's Settings edit the **desktop's** backend: Server mode, Sync,
    Contacts, Phone, models. Only the Connection block is the laptop's own, kept in localStorage.
    Nothing on the page marks which machine each section configures.

### Options an individual should never need

17. "People and access", "Allow members to publish", "Allow guest notes", "Records period",
    "Flagged phrases", `share_with_invitees`, `keep_sharing_after_quit` and the Tailscale cert are
    team-server concepts. Records and Flagged phrases show to everyone in General
    (`SettingsPanel.svelte:430-446`).
18. "Guest page address" (`guest_page_url`), "Guest notes host", "Relays", "Publish host" and
    "Upload token" are self-hosting knobs shown alongside everyday choices.
19. "Backend URL" plus manual API token, next to "Connect through remote access": two ways to
    reach another machine in one block.

### The same concept configured more than once

20. **Reaching this machine from elsewhere** has five independent mechanisms:
    - `tailscale serve` + `phone_url` (manual terminal step; phones, laptops via Backend URL);
    - remote access over iroh (computers only);
    - the team listener on 8443 (browsers);
    - the guest-notes `rooms` sidecar with Tailscale Funnel / Cloudflare (guests only);
    - "This computer" publication links riding on the team listener or `phone_url`.
21. **Tailscale** appears three ways:
    - `tailscale serve` for the phone and laptop (docs only, no UI);
    - `tailscale cert` for the team listener (a button);
    - Tailscale **Funnel** for guest-notes tunnels (automatic).
22. **Device identity**: one computer can hold the home `link.key`, the shell's `remote.key`, a
    sync identity, a person key and per-room guest keys. Each sharing form has its own list of
    who is connected:
    - Paired devices (phones, computers, contacts, as noted above);
    - Sync peers;
    - Contacts;
    - team People.
23. **Notes collaboration with others**, three ways:
    - team Share (live, edit rule);
    - contacts Share with "Notes" ticked (pulled);
    - guest notes (live, browser).
    Publishing can also include "Live notes" read-only.
24. **Turning things on for others on a team server**: `allow_publishing` and `allow_guest_notes`
    sit in Notes & sharing, while people and roles sit in General.
25. **Keeping the computer awake** has two settings: `keep_awake_while_sharing` (team), and the
    automatic inhibitor for guest notes (always on while a room is live). Recording has its own
    inhibitor.

---

## Observations

- No single option matches either of Corey's scenarios. A is split across remote access (thin
  client), library sync (peer libraries) and phone pairing (upload over Tailscale). None of them
  delivers "the laptop records, the desktop works".
- The nearest thing to "the laptop records and the desktop transcribes" already exists in the
  code: the browser recorder streaming PCM over `/api/record`. The UI enables it only for pages
  a backend served, or behind a hidden flag.
- `team_mode` is both "a dedicated team server" and a side effect of "share this computer". As
  a side effect it switches off the individual-to-individual path (contacts) and the
  multi-device path (sync).
- The relay and host defaults hang on a checkbox under Publishing, and their automatic value
  depends on an AI setting.
- The "Paired devices" list mixes four kinds under a phone heading and mislabels contacts as
  phones (`PhoneLink.svelte:130`). The remote-access status still names n0's relays
  (`RemoteAccessHome.svelte:60`). Both are small, factual bugs, independent of any redesign.
- `shared_in_*` and `share_with_invitees` have no UI.
- The setup wizard asks nothing about how this machine relates to others: base, satellite, or
  team server.
