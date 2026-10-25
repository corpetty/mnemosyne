# Guest notes

Guest notes give the people in a meeting a link. Anyone holding it opens your meeting's live
notes in a browser, types into them, comments on them and sees the agenda, with no Mnemosyne, no
account and no pairing. Nothing else from the meeting goes out: not the transcript, the summary,
the audio, the action items, or comments on anything but the notes.

For how it works inside, see "Guest notes (phase 9)" and its phase 10 paragraph in
[architecture.md](architecture.md). To
publish a finished meeting instead, see [publishing.md](publishing.md).

## Starting them

- In the meeting's header choose **Invite guests**, or use the same button next to the Copilot on
  the recording screen.
- The panel shows the link. **Copy** it, or **Show QR code** for people in the room.
- A line shows who is here; it updates as guests come and go.
- Under the link the panel says how guests reach you: "Works while this computer is on", whether
  that is through a free testing relay, your own relay, or a temporary Cloudflare or Tailscale
  address, and "Also kept on <host> after the meeting" when a publish host holds a copy (see
  [How guests reach the notes](#how-guests-reach-the-notes)).
- If nothing can carry guest notes (no sidecar, no relay, no tunnel, no host), the panel says so
  and offers Settings.

You can start guest notes before, during or after the recording. One link exists per meeting at a
time. Meetings shared with you by someone else cannot be opened to guests.

## What guests see and do

- **First visit**: the page asks for a name, kept in that browser, and shows a consent line: what
  they type becomes part of your notes of the meeting. The page connects first, so the line names
  you.
- **Notes**: the same editor you have (headings, lists, bold, tables, paste, undo of their own
  edits). Everyone, you included, sees changes as they happen.
- **Comments**: select text and comment, reply, and edit or delete their own. They cannot resolve
  threads; that stays with you.
- **Agenda**: the meeting's title, date, your name and the agenda (the calendar event's
  description the copilot follows), with the items the copilot has marked covered ticked.
- **Presence**: who else is here and who is typing, and a state: Connecting, Live or Ended.

## The link is the permission

The link looks like `https://<page>/n/#2.<room>.<key>.<computer>.<relay>` (older links,
`https://<host>/n/<room>#<key>`, still work). Anyone who has it can type into the notes
until you end them. Everything after `#` is the key and where to find your computer; browsers never send it to a
server. Treat the
link like the notes themselves, and send it only to the people in the meeting. If it went further
than you meant, press **New link** (below).

## How guests reach the notes

By default there is nothing to set up. Which way is used follows `guest_transport` in Settings
(`auto`, `iroh`, `tunnel` or `host`; `auto` is the default and tries them in this order):

1. **Straight to this computer over iroh.** Guests' browsers connect to your computer through a
   relay: n0's free testing relay until ours runs, or the relays you set in `remote_relays` (see
   [remote-access.md](remote-access.md)). The panel says "Through a free testing relay" while n0's
   are used; they are rate-limited and for testing, with no guarantees. Each room has its own
   throwaway address on your computer, never the one your paired computers use.
2. **A tunnel to this computer**, when iroh is not possible: Tailscale Funnel if Tailscale is
   running, else a temporary Cloudflare address (`*.trycloudflare.com`, no account). The first
   time, Mnemosyne downloads `cloudflared` and checks it against a pinned SHA-256. The tunnel
   only reaches the guest notes, never the rest of the app, and closes with the last room.
3. **A publish host** (`guest_host`, else `publish_host`; see
   [deploy/publish/README.md](../deploy/publish/README.md)). It is the only way that keeps working
   when your computer is off or asleep. With a host configured, the notes are also kept there
   whichever way is used ("Also kept on ...") and the link names it; guests try your computer
   first and fall back to the host.

The guest page itself loads from GitHub Pages (or `guest_page_url`, if you host your own copy;
a tunnel or a host serves it from its own address). The page is code only and holds no notes:
everything arrives sealed through the relay, tunnel or host.

**It works while this computer is on.** Over iroh or a tunnel nobody can type or read until your
computer is online. The computer is kept from sleeping while guest notes are open (as it is while
recording). Close the laptop and guests lose the connection; they reconnect when you are back.

## What guests keep

A guest's browser keeps the last copy of the notes it saw, sealed (still encrypted, opened with
the key in the link each time), until the link expires. So a guest who already opened the link can
reread the notes when your computer is off, without you. It is removed when the link expires, when
the guest page learns the room is gone (you pressed **Stop sharing the notes** or **New link**), or
when the key does not open it. The page's name card says that a copy stays in the browser.

## Encryption, and what the relay, tunnel or host sees

Every change, comment, name and snapshot is sealed with AES-256-GCM in your app or the guest's
browser before it leaves, under keys derived from the link's key. Whatever carries it relays
ciphertext and cannot merge or read it.

- **A relay** (n0's, ours or yours) sees endpoint addresses, timing and the sizes of what is sent,
  and the IP address of everyone who connects. It cannot see text, names, the title, the agenda or
  the key.
- **A Tailscale Funnel or Cloudflare address**: Cloudflare terminates the TLS connection, but what
  it handles is only our ciphertext, plus addresses, timing and sizes. Tailscale sees the same
  for a Funnel.
- **A publish host** sees room ids (random, not your meeting id), sizes, times and IP addresses,
  as any web server does.
- GitHub Pages sees that the page was fetched, and by whom, as for any web page; the key is in the
  `#` part and never sent.

None of them can tell guests' edits from yours.

The trust note from publishing applies: the page that decrypts is code the page's host sends. Use
your own `guest_page_url` and relay or host for anything sensitive. The page is served under a
strict content-security policy that lists the relays it may talk to.

## Settings

- `guest_transport`: `auto` (default), `iroh`, `tunnel` or `host`.
- `guest_page_url`: where the guest page is loaded from. Blank is the copy on GitHub Pages.
- `guest_host`: a publish host just for guest notes; else `publish_host` is used.
- `remote_relays`: the relays for iroh, shared with remote access. Blank means n0's.
- `guest_notes_end_hours`: see below.

## Ending them

- **End guest notes**: nobody can type any more. Open pages turn read-only ("Ended"); guests can
  still read and copy the notes until the room expires, 24 hours later by default and at most 7
  days after it was made.
- **Automatically**: `guest_notes_end_hours` (default 12) after the recording stops. It never ends
  guest notes during a recording, and starting a new recording part cancels it.
- **Stop sharing the notes**: deletes the room at once, on this computer and on the publish host
  when it is mirrored there. Open pages lose it.
- **New link**: deletes the room and makes a new one with a new key; the old link stops working.
  Use it when a link leaked or you want to remove someone.

There is no way to remove one guest; it is everyone or a new link. What someone already copied
cannot be taken back.

## Authorship, honestly

A guest's name is whatever they typed; nothing proves it, and a guest can use someone else's. In
your meeting their typing and comments are stored as "<name> (guest)". Anything that reaches the
guests from your side (your typing, the copilot, people you shared the meeting with) shows to them
as yours.

## What stays in your meeting

Guest notes are part of the meeting, not a separate document:

- What guests typed is in the notes and their history, and is read by search, Obsidian export, the
  summary and the MCP server like the rest of the notes. Deleting it later is editing your notes.
- Their comments are ordinary comments on the notes: searchable, synced to your other computers,
  and in the records export.
- Each start, end and stop is a line in the meeting's history and seals a record version
  (`guest_notes.json` in the export lists them; the key and token are never in it).
- Deleting the meeting deletes its room.

With `require_consent` on, remind people that guests' typing becomes part of the record.

## Team servers

Guest notes are off on a team server until an admin turns on `allow_guest_notes`. Then a
meeting's owner or an admin can start them. See [team-server.md](team-server.md#guest-notes).

## Limits

- One notes frame (a paste, say) is at most 2 MiB; the room's log and snapshot are each at most
  8 MiB. An owner offline for a long time while guests type through a host can fill the log; new
  edits are then refused until you reconnect.
- A room takes at most 64 connections at once, and a room lives at most 7 days.
- Over iroh, one relay connection carries all guests: with n0's relays that is rate-limited, and
  ten guests each loading a 1 MB snapshot take about ten seconds in all.
- Presence (who is here, who is typing) is not shared between the guests who came through your
  computer and those who came through a host.
- A Cloudflare quick tunnel gets a new address every time it starts, so after a restart of the
  app the old link's tunnel address is dead; the iroh part of the link keeps working, and the
  panel shows the new link. Funnel addresses are stable.
- Guests need a current browser. Safari and other WebKit browsers are the least tested with iroh;
  they must be Safari 15.4 or newer, and a host path remains as the fallback.
- A self-hosted relay needs a certificate from a public authority for browsers to accept it.
- Guests cannot see the transcript or summary or tick action items, and one guest cannot be
  removed alone.
- The notes need the link sidecar (`mnemosyne-link`); without it, guest notes are unavailable,
  as live shared notes are.
