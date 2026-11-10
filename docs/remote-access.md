# Your devices

How to use Mnemosyne from more than one device: a laptop that records into your main computer's
library, a phone that sends recordings, or a second computer that keeps its own copy. Everything
starts at **Settings → Devices and people**; nothing needs an API token, a "Remote access" box or a
server option to be set by hand first. Adding a device makes the token and turns remote access on
by itself.

The design and its reasons are in [plans/2026-11-09-connections-design.md](plans/2026-11-09-connections-design.md).

## One main computer, other devices that use it

A **main computer** holds the library and does the work: recording, transcription, summaries,
models, storage. It is what every computer is until you say otherwise (Settings → Devices and
people → This computer). The setup wizard asks first: "Is this your main computer?"

On the main computer, **My devices** lists everything connected to it, each with what it is and when
it was last seen ("Phone · Sends recordings", "Laptop · Uses this computer"). **Remove** cuts one
off at once. **Add a device** offers two choices: a laptop or another computer, or a phone.

Mnemosyne has to run on the main computer for the others to reach it. To keep it running with the
window closed, turn on "Closing the window keeps Mnemosyne running in the tray" (General → Window).

## A laptop that uses the main computer

On the main computer: Settings → Devices and people → My devices → **Add a device → A laptop or
another computer**. Choose **New invite** and copy it (it works once, for an hour; the "works for" list next to the
button gives 15 minutes or a day instead).

On the laptop (desktop app): Settings → Devices and people → This computer → **Uses my main
computer**, paste the invite, name the laptop, and connect. In the setup wizard, answer "No, I
already have one" and paste it there. The laptop keeps its own key, runs a local tunnel
(`127.0.0.1:8009`) and restarts it with the app. **Disconnect** forgets the main computer.

What the laptop gets:

- **The main computer's library**, live, through the tunnel: browse, search, ask, edit, summarize.
  "These are your main computer's settings" tops the laptop's Settings, except for the section about
  the laptop itself.
- **Recording with the laptop's own microphone and call audio.** The laptop captures as it always
  does and streams each source to the main computer while they are connected. The main computer
  records the meeting, so live transcript, live speakers, the copilot and the final transcription
  all run on its GPU and the meeting is like one recorded there. The device pickers, level meters
  before recording and the echo canceller are the laptop's own.
- **An outbox when the main computer is away.** Record still works. The window says "Main computer
  not reachable. Recordings are kept here and go to it when it is.", the status bar counts
  "n recordings waiting", and each goes to the main computer as soon as it is back (retrying with
  growing pauses, and at once when the connection returns). The laptop's copy is deleted only after
  the main computer has answered. If the connection drops during a recording, everything is still
  recorded on the laptop and sent in full when you stop, before the main computer transcribes it.
  A waiting recording the main computer refused (for example, the meeting it streamed into was
  already transcribed) shows **Send as a new meeting** and **Discard** in the status bar list.
- **A copy of the library, if you want one.** Settings → This computer → "Keep a copy of the library
  on this laptop" turns on library sync with the main computer through the same tunnel (no invite to
  paste), so meetings and notes can be read with the main computer off. Off by default. Audio stays
  on the computer that recorded it.

A laptop recording always makes a new meeting on the main computer (it cannot add a part to an
existing one). Level meters are not shown during a laptop recording. The consent answer
("Ask before every recording") goes to the main computer only when the recording is streamed; a
recording made offline arrives without one. The main computer does not acknowledge the last bytes
in flight when you press Stop: a stream that dropped is replaced by the complete local audio, one
that never did is trusted as it arrived.

**Laptops paired before this version must be paired again once.** The desktop app now keeps the
pairing token (in `remote.json`, readable by you only) so it can tell its own backend where the main
computer is; a pairing made earlier did not keep it, and such a laptop works as before (browsing the
main computer's library) but cannot record into it until it is paired again.

## A phone

Settings → Devices and people → My devices → **Add a device → A phone**, and scan the QR code with
the phone. The code works once and expires after an hour (or what "works for" says); the phone keeps its own key, and the
API token never leaves the computer. The phone opens a small page: record in it (keep it open while
recording), or pick a file, and upload; the main computer transcribes. A phone's key only allows
uploading recordings, not reading your meetings.

The phone needs an address it can reach. The recommended one is [Tailscale](https://tailscale.com):
the backend stays on `127.0.0.1`, nothing is opened to the internet, and an HTTPS address lets the
page record inside the browser.

1. Install Tailscale on the main computer and the phone, signed in to the same tailnet, with
   **MagicDNS** and **HTTPS certificates** turned on (admin console → DNS).
2. Serve the backend on the tailnet:

   ```bash
   tailscale serve --bg localhost:8008
   ```

   The address appears in `tailscale serve status`, e.g. `https://desk.tail1234.ts.net`. It stays up
   across reboots until you turn it off with `tailscale serve --https=443 off`.
3. Put that address in **Phone address** (shown under Add a device → A phone) and save.

Everyone on your tailnet can reach a served address; the pairing key is what keeps it yours.
On a trusted LAN without Tailscale, run the backend with `--host 0.0.0.0` from a terminal (see
[development.md](development.md#server-mode-backend-on-another-machine)); phones then use
`http://<lan address>:8008` and record with their own recorder app rather than in the page, and
anyone on the network can see the traffic. The desktop app cannot be started that way.

## Two equal main computers

Two computers that both run Mnemosyne can share one library, each recording and working for itself:
meetings recorded on either appear on both, and summaries, notes and tasks edited on either merge.
Audio stays on the computer that recorded it; the other one plays it through the same encrypted
connection while that computer is reachable, and leaves transcribing to it. This is the right choice
when both are strong machines; for a laptop that should lean on the main computer, use the laptop
setup above (with the library copy if you want one).

1. On one: Settings → Devices and people → My devices → **More: another main computer that keeps
   its own library in sync** → Invite a computer. Copy the invite.
2. On the other: the same place, paste it and press Sync.

They connect directly on the same network and through the relays elsewhere, and pull each other's
changes every half minute and right after a change. Removing a computer stops the sync; what was
already synced stays on both. Sync is refused only on a dedicated team server (see [team-server.md](team-server.md)), not on a
computer that hosts a team: there it syncs only its owner's own meetings, never the team's.

## How the connection is made

Mnemosyne's remote access needs no Tailscale or VPN app: an end-to-end encrypted
[iroh](https://github.com/n0-computer/iroh) connection that only computers you pair can open.
Nothing on the main computer listens on the internet; it dials out to a relay. A paired computer's
key opens the whole API, as the API token does. Adding a device makes the token and turns remote
access on, and the status under My devices says which relay is in use.

### Local first

On the same network the two machines find each other directly: the main computer announces itself on
the local network (mDNS, under iroh's generic service name, carrying only its endpoint ID and
addresses), and a paired computer looks for it there before anything else. Traffic then goes
straight across the network, never through a relay. A paired computer only listens; it does not
announce itself on networks it visits.

### Relays (Advanced)

Relays are the fallback away from home. They only pass along packets they cannot read, and only
until the two machines find a direct path. By default Mnemosyne uses its own relay,
`https://relay.petty.services`, run from [deploy/ours/](../deploy/ours/README.md); n0's public relays
are never a default. It sees endpoint addresses, timing and sizes, and the IP address of whoever
connects, never what is sent; the [privacy notice](https://notes.petty.services/privacy) says what is
kept (counts only, no client addresses in logs).

Whether it is used follows **Use Mnemosyne's relay and notes server** (Settings → Devices and people →
Advanced; `use_our_servers`): automatic means on, except on a dedicated team server, and it has nothing to do
with the AI tab's cloud-model switch. With it off and **Relays** blank there is no relay at all:
paired computers connect directly or on the same network (mDNS). A relay you set under **Relays**
(`remote_relays`) always wins. To use your own, run the open-source iroh-relay server (a ready config
and compose file are in [deploy/relay/](../deploy/relay/README.md)), put its address in **Relays** on
the main computer, and pair your devices again. Computers follow the main computer's relays; they
have no relay setting of their own.

**Computers paired under n0's relays need pairing again.** There is no migration: a pairing made
then remembers them, so pair each computer again to have it follow the current relay.

The relays in `remote_relays` also carry [guest notes](guest-notes.md): guests' browsers reach this
computer through them. A browser only connects to a relay with a certificate from a public authority
(Mnemosyne's relay has one), so a self-hosted relay used for guest notes needs one (Let's Encrypt, as
in [deploy/relay/](../deploy/relay/README.md)).

Also under Advanced: **Connect to a backend by address** (a backend URL and token, for a team server
or a machine on Tailscale you reach by name), and this computer's API token, which is made for you
the first time you add a device.

### From a terminal

`mnemosyne-link` (the `link/` crate, bundled with the app) also pairs and connects without the app:

```bash
mnemosyne-link pair '<invite>' --key-file ~/.config/mnemosyne/link.key --name Laptop
mnemosyne-link connect '<ticket printed by pair>' --key-file ~/.config/mnemosyne/link.key --listen 127.0.0.1:8009
```

## Troubleshooting

- **"Remote access could not be turned on"** when adding a device: the `mnemosyne-link` program is
  missing or did not start. The message says which; settings are put back as they were.
- **"This phone is not paired"**: the phone was removed, its browser data was cleared, or the address
  changed (each address keeps its own key). Pair it again.
- **"Pairing failed: This pairing code is invalid or has expired"**: codes work once and for 10
  minutes. Choose a new one.
- **The laptop browses but will not record into the main computer**: it was paired before the desktop
  app kept the pairing token. Disconnect and pair it again once.
- **The Tailscale address does not load**: check `tailscale serve status` on the main computer and
  that HTTPS certificates are enabled for the tailnet.
