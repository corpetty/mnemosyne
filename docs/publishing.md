# Publishing a meeting

Publishing turns a meeting into a read-only page that anyone with a link can open in a browser,
without Mnemosyne and without an account. It is a curated copy, not the meeting: you choose what
it shows, and it is encrypted on your computer before it goes anywhere.

For how it works inside, see "Publishing" in [architecture.md](architecture.md); for the HTTP
interface, [api-reference.md](api-reference.md) (Publishing).

## What a publication is, and what it never contains

A publication is a snapshot of parts of the meeting: the title, date and length, the summary,
decisions, open questions, action items (with owner, due date, done), chapters, topics, the
attendees you list, the meeting's live notes, and quotes. Each of those is a tick box; everything
except the notes and quotes is on by default.

It **never contains the transcript**. The only transcript text in it is the quotes you pick one by
one (the lines inside the stretch you mark), and there is no setting that adds the rest. The
audio is never in it either, except a short clip for each quote you choose to attach one to.

## Choosing what readers see

- **Parts**: tick the parts above. A preview next to the choices is drawn by the same viewer your
  readers will use, so what you see is what they get.
- **Quotes and clips**: mark a stretch of the meeting; the transcript lines whose middle falls
  inside it become a quote. With "clip", the audio of that stretch (a third of a second of padding either
  side) goes with it. Listen to each clip in the dialog before you publish.
- **Names**: as they are, first names only, or "Person 1, Person 2…". The attendees you tick are
  the people listed; anyone of the meeting you do not list is replaced by "Person N" in every
  text (summary, quotes, owners), not only in the attendee list. Speaker labels the diarizer
  never named ("SPEAKER_01") are never shown as a name or owner.
- **Identifiers**: on by default, text is scrubbed of identifiers such as social security,
  account and card numbers, **and e-mail addresses and phone numbers**. Turn it off to publish
  them as they are.
- **Clips cannot be redacted.** Names and numbers spoken in a clip are in the audio. That is why
  the dialog makes you listen first; if in doubt, leave the clip out and keep the text quote.

## Where it goes

| Where | Who can open it | Notes |
| --- | --- | --- |
| **This computer** | Anyone who can reach this computer's web address (below) | Served by the app while it runs. Expires after 90 days by default (`publish_default_expiry_days`), or never. |
| **A file** | Whoever you give the file to | One `.html` file with the viewer and the encrypted copy inside; works offline, no server. Not a publication: nothing to update or unpublish, never expires. Optional password. |
| **A publish host** | Anyone with the link | An always-on server you or your team run ([running your own](#running-your-own-publish-host)). Appears in the dialog once `publish_host` is set. Must expire, at most 365 days. |

**This computer: who can reach it.** The link's address depends on how others can reach your
machine, and the dialog says which case you are in (`local_reach`):

- *team*: "Share this computer with my team" is on, so the link uses that listener (Tailscale name
  first, else a LAN address). Everyone who can reach that port can open it.
- *phone_url*: you set the phone address in Settings; the link uses it.
- *this device*: neither is set. The link only works on this computer (127.0.0.1); nobody else can
  open it. Turn on team sharing, or use a file or a publish host.

**A file** with no password carries its key inside: anyone holding the file can read it. With a
password (at least 8 characters) the key is derived from it (PBKDF2-SHA-256, 600,000 rounds) and
the file alone is not enough; send the password through another channel. The dialog saves the file
through the desktop app's save dialog, or downloads it in a browser.

## What readers see

The link looks like `https://<host>/p/<id>#<key>`. The part after `#` is the key; browsers never
send it to a server. The page fetches the encrypted copy and decrypts it in the browser
(WebCrypto): title, date, summary, decisions, questions, action items, chapters, topics, notes and
the quotes, each with a play button when it has a clip. Readers cannot write, comment or see
anything you did not tick. A wrong or shortened key, a deleted publication and an expired one each
get their own plain message.

## What the host can see

Whoever serves the copy (this computer, a team server, or a publish host) holds only ciphertext.
A publish host can see publication ids, the **padded** sizes of the parts (the manifest is rounded
up to 4 KiB, each clip to 64 KiB), upload, update and expiry times, and the IP address of whoever
uploads or opens a link, as any web server does. It cannot see the text, the title, the names, the
clips or the key, and cannot alter what a reader sees without the reader's browser rejecting it.

## Revoking, honestly

- **Unpublish** deletes what the host holds and your computer's files at once; the link then
  answers "gone". The meeting itself is untouched.
- A copy someone **already opened** cannot be taken back: they may have saved the page, copied the
  text, or kept the file. Nothing deletes what a reader's browser or disk holds. Publish only what
  you are content to have out there.
- A link that was shared is a capability: anyone who gets it can read the copy until it expires or
  you unpublish. Treat it like the contents.
- **Expiry** ends hosted and this-computer links on their own (the host's sweeper deletes them;
  this computer's files go on the next sweep and readers get "gone" in the meantime). A file never
  expires, and cannot be recalled.

## Updating behind the same link

Publishing a meeting again, from the same dialog, makes a new version under **the same link**:
same id and key, so nobody needs a new address. Clips whose stretch did not change are reused.
You can change what is shown, move it from this computer to a host (the old copy is deleted), or
change the expiry.

After you re-summarize or edit the summary, notes or a quoted line, the meeting shows **"published version
is behind"** whenever publishing again would show something different. Nothing updates by itself.
Each version is recorded in the meeting's history (`published`, `publication_updated`,
`unpublished`).

## Team servers

On a team server publishing is off until an admin turns on `allow_publishing`. Then a meeting's
owner or an admin can publish, update and unpublish it. Everyone else who can read the meeting
sees **that it is published**, not the link. Links to a team server (`/p/<id>`) need no sign-in:
the key in the link is the permission. See [team-server.md](team-server.md#publishing).

## Two computers (library sync)

When two of your computers share a library ([remote-access.md](remote-access.md)), the publication
travels with the meeting, key included (the computers are one person's; library sync is refused on
team servers).

- **Hosted publications** can be updated or unpublished from either computer. A computer without
  the clip files fetches the sealed clips from the host; a clip that is new needs the audio, which
  only the recording computer has, so that update answers `409` "The audio is on <name>".
- **This-computer publications** are served by the computer that made them, and only by it. On
  the other one the meeting shows "served by <name>" with no link, and changing or unpublishing it
  answers `409` "Published from <name>". Do that on the computer that serves it.
- Two computers publishing the same meeting separately end up as one: the later one stays and the
  other is unpublished (its hosted copy deleted). Two computers publishing the same new version of
  a hosted publication at once: the second sees the host's copy is not its own and asks you to
  publish again.

## Records

Publishing is a disclosure, so each publish, update and unpublish seals a record version of the
meeting (reasons "published", "publication updated", "unpublished"), even when nothing else
changed; the chain shows when. A records export has `publications.json` per published meeting: who
published which version when, what it showed (parts, names, whether identifiers were hidden, the
number of quotes and clips), where it went, and when it ended. **The key and token are never in
it.** A legal hold does not stop publishing. A file export is logged and sealed the same way.

## Running your own publish host

A small Rust program ([`publish-host/`](../publish-host)) behind Caddy stores the encrypted copies
and serves the viewer page. It has no way to decrypt. Setup, limits, open or closed uploads
(`PUBLISH_UPLOAD_TOKEN`, entered in Settings as the publish host token) and operation are in
[deploy/publish/README.md](../deploy/publish/README.md).

## The trust note

The encryption protects the copy from whoever stores it, but the page that decrypts it is code
the server sends. **Whoever serves the viewer page is trusted for its code**: a dishonest host
could serve a viewer that sends the key (from the address bar) somewhere. The page is served under
a strict content-security policy (no outside requests), and the viewer is built reproducibly and
committed to this repository, so it can be checked, but a reader cannot see that for a given
server. To avoid trusting a third party, publish from **this computer** (your own backend serves
the page) or send a **file** (the viewer is inside it and makes no requests at all). Use a host
you run yourself for anything sensitive.
