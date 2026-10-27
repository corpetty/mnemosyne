# Images in the notes

Status: proposal, 2026-10-26, nothing built. Drafted against main at 0.25.1. Asked for by Corey after
comparing our editor with David's kb (hashseq web/kb.js, APP_NOTES 19 "Images: the value side store earns
its keep", 21 "A big image broke page load"): paste or drop a screenshot into the notes, encrypted for
guests, written to Obsidian. Decided by Corey (2026-10-26): guests may add images, from a phone camera
too; publications include the notes' images, chosen per image, all on by default; caps 2 MB an image,
50 MB a meeting, 25 MB a guest room.

Why: meetings show slides, whiteboards and screens. Today the notes hold only text; a screenshot has to
live somewhere else and loses its place in the meeting.

## The model: an image line, the bytes beside the document

- **An image is a line.** A new line kind `image`: the line's text is the caption (alt text, may be
  empty), and a closed `image` mark on the line's terminator carries `<sha256>.<ext>@<w>x<h>` of the
  bytes (e.g. `3f…a9.webp@1600x900`; over 14 bytes, so it travels as a 0xAF artifact frame beside the
  delta, as link URLs do; the size lets an image hold its place before it loads). Concurrent edits: the caption merges like any text; two people setting different images on
  one line keep both live marks and the newest wins in rendering (as a link conflict does, but shown).
- **The bytes never enter the CRDT.** kb put images inside the document as artifacts and every
  snapshot then carried every image (APP_NOTES 19, 21). Ours are content-addressed blobs kept next to
  the document: the hash is the name, so the same screenshot pasted twice is stored once, a blob can
  never change under its name, and anyone holding the document knows what to fetch.
- **Markdown**: `![caption](mnemosyne:image/<sha256>.<ext>@<w>x<h>)` on a line of its own (notes-core render and
  parse; the parser keeps other `![]()` literal, as today). `Session.notes` carries that line, so
  search finds the caption, and `plain_notes` gives "[image: caption]" to the summary, Ask and MCP.
  Changes notes-core, so the notes wasm is rebuilt by CI (the artifact loop).

## Making one

- **Paste** an image from the clipboard (screenshot tools put PNGs there), **drop** a file, or
  **/Image** in the menu (a file picker; on a phone it offers the camera, for a whiteboard).
- **In the browser, before anything is stored**: decode, downscale to at most 1600 px on the long edge,
  re-encode as WebP (quality 0.82; JPEG where the browser cannot encode WebP, Safari), which also
  **strips EXIF and GPS**. Over 2 MB after that: refused with a message. GIFs and SVGs are converted to a
  still image (an SVG is never stored as SVG: scripts).
- The line appears at once with the local image (an object URL) and "Uploading…", then the bytes go
  to the meeting's media store and the line is committed with its hash. A failed upload leaves the
  line with "Not uploaded, Retry / Remove".

## Where the bytes live

- **Desktop / team server**: `<data_dir>/notes_media/<session>/<sha256>.<ext>`, sealed like audio when
  encryption at rest is on (`<name>.enc`, read through `storage.crypto.plaintext`). Routes:
  `PUT /api/sessions/{id}/notes/media/{sha}` (body = bytes; the server checks the hash, the type by
  magic number — WebP or JPEG only — and the caps) and `GET …/media/{sha}` (cache immutable). Same
  access as editing / reading the notes. Caps: 2 MB an image, 50 MB a meeting.
- **Unused bytes**: a blob no line has referenced for 7 days (checked at fold time against the
  document, including undo-able deletions within that window) is deleted. A deleted meeting deletes
  its folder. Records and backups include the folder.
- **Library sync**: `SyncSession` lists the meeting's media hashes; a peer that lacks one pulls it
  (`GET /api/sync/media/{session}/{sha}` through the tunnel) after applying the meeting. Images are
  small, so unlike audio they travel.
- **Phase 8 shares**: a shared meeting whose notes are shared carries its media list; the recipient
  pulls the blobs from the owner like the meeting itself.

## Guests

- **Sealed with the room key**: a new guest_format kind `MEDIA` (raw bytes, padded to a 64 KiB step,
  AES-GCM with AAD `mnemosyne/guest/1|room|media|<sha256 of the plaintext>`). The plaintext hash stays
  the name in the document; the host only sees a ciphertext name (sha256 of the sealed bytes).
- **The room keeps media**: publish-host rooms gain `PUT/GET /live/{room}/media/{cipher-sha}` (room token
  required for both), counted in the room's caps (a new 25 MB media cap per room) and the per-client
  daily write budget, deleted with the room. The embedded loopback host (iroh rooms, tunnels) gets the
  same routes for free.
- **Owner → guests**: the bridge seals every blob a line references and uploads it to each of the
  meeting's rooms (primary and mirror) before it sends the frame that references it, so a guest never
  sees a line whose image cannot be fetched yet.
- **Guests → owner** (Corey: guests may add images, also from a phone camera): the guest page does the same downscale,
  seals and uploads to the room, then sends the line; the bridge fetches, opens, checks the hash and
  type, and stores it in the meeting's media store.
- The guest page's saved copy (IndexedDB) keeps the sealed blobs of the images it showed, until the
  link expires, like the snapshot.

## Showing it

- In the editor an image line renders as a block: the image (max width of the column, max height
  60 vh, rounded, a thin border), the caption under it as ordinary editable text with a muted
  "Add a caption" placeholder. The image itself is not editable; the caret moves over it as over one
  character. Click (or Enter on the focused line) opens it full size in a lightbox; Escape closes.
  Backspace at the start of an empty caption removes the image line (one undo restores it).
- Loading: a grey box of the image's size (the mark value carries it, see the model), so the text
  below does not jump while the bytes arrive.
- Live carets and comments work on the caption text; a comment on an image is a comment on its
  caption line.

## Elsewhere

- **Obsidian**: export writes each image the note uses to `<vault>/<folder>/attachments/<note stem>-<sha8>.<ext>`
  (decrypted) and renders `![[<that file>|caption]]`. Files of images no longer in the notes are
  removed at the next export (only files with our naming, only in our attachments folder). Vault
  edits to the notes section do not flow back (unchanged).
- **Publishing** (Corey): a publication includes the notes' images as encrypted blobs, like clips
  (content-addressed, AES-GCM under the publication key, counted in its 32 MiB cap); the publish dialog
  lists them with a checkbox each, all on by default; a left-out image shows as "[image: caption]".
- **Summary, Ask, search, MCP**: the caption only. Reading the image itself (vision, OCR) is not in v1.

## Tests

- notes-core: render/parse round trip of image lines, the mark value, conflicts; fold reports the
  media hashes in use (for cleanup and sync).
- Backend: upload checks (hash, magic number, caps), sealed storage, GC after 7 days, sync pull, share
  pull, Obsidian export and cleanup, plain_notes.
- publish-host: room media routes, token, caps, budget, deleted with the room.
- Frontend (Vitest): downscale and re-encode (canvas stubbed), EXIF gone, the paste/drop/menu paths,
  the upload states, the image block's caret behaviour.
- Playwright: paste an image in the desktop editor, it shows; a guest sees it; a guest's image reaches
  the owner; export writes the attachment.

## Not in v1

Resizing or cropping in the editor, several images per line, images inside table cells, video, reading
text from images, drawing.

## Open questions for Corey

None left (answered 2026-10-26).
