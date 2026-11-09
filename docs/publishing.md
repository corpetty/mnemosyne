# Publishing a meeting

Publishing turns a meeting into a read-only page that anyone with a link can open in a browser,
without Mnemosyne and without an account. It is a curated copy, not the meeting: you choose what
it shows, and it is encrypted on your computer before it goes anywhere.

For how it works inside, see "Publishing" in [architecture.md](architecture.md); for the HTTP
interface, [api-reference.md](api-reference.md) (Publishing).

## What a publication is, and what it never contains

A publication is a snapshot of parts of the meeting: the title, date and length, the summary,
decisions, open questions, action items (with owner, due date, done), chapters, topics, the
attendees you list, the meeting's live notes (with the pictures in them), and quotes. Each of those is a tick box; everything
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
- **Images**: with the notes ticked, the dialog lists the notes' images, each with a thumbnail and a tick, all
  ticked. A ticked image is encrypted and published like a clip; one you leave out reads `[image: caption]`
  to readers. Images and clips share one limit: 20 MiB together (padded to 64 KiB steps); a publish over it is refused. An image is not redacted: text in a
  screenshot is published as it is, so look at each one. (EXIF and GPS are already removed when an image is added to the notes.)
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
| **A publish host** | Anyone with the link | An always-on server you or your team run ([running your own](#running-your-own-publish-host)). Mnemosyne's own server (`notes.petty.services`) is the default and is preselected as "Mnemosyne's server" while `use_our_servers` is on; your own appears once `publish_host` is set. Must expire, at most 365 days. |

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
(WebCrypto): title, date, summary, decisions, questions, action items, chapters, topics, notes (with their images)
and the quotes, each with a play button when it has a clip. Readers cannot write or see
anything you did not tick. (On a publish host with comments on, readers can comment: see "Comments from
readers".) A wrong or shortened key, a deleted publication and an expired one each
get their own plain message.

## What the host can see

Whoever serves the copy (this computer, a team server, or a publish host) holds only ciphertext.
A publish host can see publication ids, the **padded** sizes of the parts (the manifest is rounded
up to 4 KiB, each clip and image to 64 KiB), upload, update and expiry times, and the IP address of whoever
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
is behind"** whenever publishing again would show something different. Nothing updates by itself, unless you turned on "Keep the link up to date" (below).
Each version is recorded in the meeting's history (`published`, `publication_updated`,
`unpublished`).

## Kept up to date

Tick **Keep the link up to date** in the Publish dialog and the link follows the meeting. When the meeting
changes (a re-summarize, an edit of the summary, the notes or a quoted line, a speaker renamed), your computer
publishes a new version by itself, under the same link, and readers who have the page open see it replace the
old one without reloading. The footer says "Updated 14:32".

- **Only what you ticked goes out.** The new version is made from the choices you saved, the same way an
  Update does it: the same parts, the same names and hidden identifiers, the same attendee list ("Person N"
  for everyone else), the same quotes. A decision you left unticked stays out however often the meeting
  changes. Pictures are the one thing that could grow: when you turn this on, the notes' pictures you saw in
  the dialog are fixed as the list, so a picture pasted in later is not published until you tick it at the
  next Update.
- **How often.** A change counts once the meeting has been quiet for 10 seconds, or once the first change
  waiting is a minute old if it never goes quiet (typing in the notes). There are at least 30 seconds between
  two automatic versions, and a version identical to the one out is never sent.
- **What it costs you.** A wrong speaker rename or a hasty summary edit is public within about a minute.
  Untick the switch or Unpublish to stop it.
- **Records.** A version made by itself is a line in the meeting's history (`publication_auto_updated`), and
  is listed in `publications.json` with `auto: true`. It does not seal a record version; Publish, Update (where
  you can turn this on or off) and Unpublish do, as before.
- **What readers see.** "Kept up to date as the meeting changes" in the page when live updates reach them. A
  host that cannot push (an older one) or a reader who cannot connect gets "Checks for updates every
  minute": the page asks the host once a minute and swaps in a newer version it finds. The dialog shows the
  state of your side: live, with how many are reading, or not connected to the host, or that this host
  cannot push (readers poll).
- **Your computer must be on.** The host keeps the last version without it, and readers can still open it;
  nothing new appears until your computer is back. Only the computer that recorded the meeting makes
  automatic versions; the other computer of a library-sync pair can still Update by hand.

**Where it applies.** Hosted publications only. A "this computer" publication has no room, so neither
switch is offered for it, and the one-file export never updates ("This file does not update"). A meeting
someone shared with you cannot be live either.

## Comments from readers

Tick **Readers may comment** and anyone with the link can comment on the published page, under a name they
type the first time. They can comment on a decision, a question, an action item, a quote, a passage of the
summary, a passage of the published notes, or the meeting as a whole. They can edit and delete their own comments. Nothing proves a name: it is
what the person typed, and your app shows it as "Ana (reader)" with a line saying it came from the published
link.

- **What readers see.** Only threads a reader started, with every reply in them, yours included. Your own
  threads, and threads on a part you did not publish, never go out. Your replies are passed through the same
  names and identifier hiding as the rest of the publication. A comment you delete shows as "removed".
  Readers see each other's threads a moment after they are written, not the comments as they are typed.
- **Your replies.** Reply in the comments panel as for any thread; the reader sees it in theirs. Resolve and
  delete work as for guests.
- **Approve or Hide.** Tick **Approve comments before other readers see them** (needs comments on) and a
  reader's first comment, and each reply, reaches the other readers only once you press **Approve** on it.
  The writer sees their own comment at once, marked "Waiting for the owner". Comments made before you turned
  this on stay shown. **Hide** takes a comment away from readers whether the switch is on or not; "Show to
  readers" brings it back. Both are for the meeting's owner or an admin.
- **A cleared browser is a new reader.** A reader's identity is a key in their browser. Clear the site's
  data, use another device or close a private window, and they are someone new: their old comments stay, but
  they can no longer edit them. You can still delete them.
- **The notes.** When the notes are published, a reader can select a passage of them and comment. Your app finds
  that passage in the live notes and marks it like a comment of your own, so the thread shows against the same
  words for you and for guests. If the passage cannot be found any more, or this computer has no notes engine, the
  thread stays on the meeting as a whole with the passage quoted ("On: ...").
- **Caps.** A comment is at most 4,000 characters, a name 80, and a reader can make 20 changes in 10
  minutes; all readers together 60 in 10 minutes. More are ignored.
- **How they travel.** Comments travel as encrypted frames: other readers cannot read a comment from
  the room before you let it out, or edit one that is not theirs. Reader comments are ordinary comments of
  the meeting: they are searchable, sync with your other computer and are in the records export.
- **Where it applies.** Hosted publications only, as above.

### Blocking a reader

On a reader's comment in the comments panel, **Block** turns comments off for that reader: their later comments, edits and
deletes are dropped, their page hides the comment buttons and says the owner has turned comments off for them.
Optionally their existing comments are hidden from the other readers; **Unblock** turns comments on again (hidden
comments stay hidden until you show them). The history names the reader's id and name.

What it does not do. It does not stop anyone reading: the reader still holds the link and can read every
version. It identifies a browser, not a person: a reader who clears the site's data, uses another browser or
device, or opens a private window is someone new and can comment again. Blocking is for a reader who is being a
nuisance, not for cutting someone off.

**Replace the link** is the way to cut readers off. It unpublishes the publication and publishes it again with the
same choices: a new link and new keys, the old link answers "gone", and every block and reader identity is
gone with it. You must send the new link to the readers you still want. If the host fails between the two steps the
meeting is left unpublished (the history and the dialog say so); publish it again.

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
  the clip files fetches the sealed clips and images from the host. A clip that is new needs the audio, which
  only the recording computer has, so that update answers `409` "The audio is on <name>". An image that is new needs its bytes: until sync has brought them the update fails
  with "An image of the notes is not on this computer yet"; leave it out or try again later.
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

## Mnemosyne's server

With no `publish_host`, the dialog offers Mnemosyne's server and preselects it (90 days by default). It
holds the same ciphertext any host does and has the same limits plus a per-client write budget of 200 MiB
per rolling 24 hours (429 `write_budget`). Operator, what is kept and for how long:
[privacy notice](https://notes.petty.services/privacy), run from [deploy/ours/](../deploy/ours/README.md).
The upload token for your own host is never sent to it. Turn **Use Mnemosyne's servers** off in Settings
and a meeting can be published only from this computer or as a file, unless you set `publish_host`.

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
