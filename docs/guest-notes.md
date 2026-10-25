# Guest notes

Guest notes give the people in a meeting a link. Anyone holding it opens your meeting's live
notes in a browser, types into them, comments on them and sees the agenda, with no Mnemosyne, no
account and no pairing. Nothing else from the meeting goes out: not the transcript, the summary,
the audio, the action items, or comments on anything but the notes.

For how it works inside, see "Guest notes (phase 9)" in [architecture.md](architecture.md). To
publish a finished meeting instead, see [publishing.md](publishing.md).

## Starting them

- In the meeting's header choose **Invite guests**, or use the same button next to the Copilot on
  the recording screen.
- The panel shows the link. **Copy** it, or **Show QR code** for people in the room.
- A line shows who is here; it updates as guests come and go.
- If no host is available, the panel says so and offers Settings (see
  [Where it is relayed](#where-it-is-relayed)).

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

The link looks like `https://<host>/n/<room>#<key>`. Anyone who has it can type into the notes
until you end them. The key is the part after `#`; browsers never send it to a server. Treat the
link like the notes themselves, and send it only to the people in the meeting. If it went further
than you meant, press **New link** (below).

## Encryption, and what the host sees

Every change, comment, name and snapshot is sealed with AES-256-GCM in your app or the guest's
browser before it leaves, under keys derived from the link's key. The host relays and stores
ciphertext and cannot merge or read it.

The host can see room ids (random, not your meeting id), the sizes of what is sent, times, and the
IP address of everyone who connects, as any web server does. It cannot see text, names, the title,
the agenda or the key. It also cannot tell guests' edits from yours.

The trust note from publishing applies: the page that decrypts is code the host sends. Use a host
you run for anything sensitive. The page is served under a strict content-security policy.

## Where it is relayed

Your computer is behind a router, so guests and you meet at a room on a host both can reach. Which
one is chosen in this order:

1. `guest_host` in Settings, if set.
2. The publish host (`publish_host`), if set.
3. Our public guest host. **It does not run yet**, so today guest notes need one of the first two.

With none, starting answers "No host for guest notes". A host is the same small program that
serves publications ([deploy/publish/README.md](../deploy/publish/README.md)); its operator sees
only what is listed above. If your computer is offline, guests keep typing together through the
room, and your app catches up when it is back.

## Ending them

- **End guest notes**: nobody can type any more. Open pages turn read-only ("Ended"); guests can
  still read and copy the notes until the room expires, 24 hours later by default and at most 7
  days after it was made.
- **Automatically**: `guest_notes_end_hours` (default 12) after the recording stops. It never ends
  guest notes during a recording, and starting a new recording part cancels it.
- **Stop sharing the notes**: deletes the room on the host at once. Open pages lose it.
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

- One notes frame (a paste, say) is at most 2 MiB; the host's log and snapshot are each at most
  8 MiB. An owner offline for a long time while guests type can fill the log; new edits are then
  refused until you reconnect.
- A room takes at most 64 connections at once.
- A room lives at most 7 days.
- Guests need a current browser. Safari must be 15.4 or newer (the page connects to its own host
  only, which older Safari cannot do for WebSockets).
- Guests cannot see the transcript or summary or tick action items, and one guest cannot be
  removed alone.
- The notes need the link sidecar (`mnemosyne-link`); without it, guest notes are unavailable,
  as live shared notes are.
