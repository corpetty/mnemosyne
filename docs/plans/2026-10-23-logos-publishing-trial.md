# Trial: publishing an encrypted meeting through Logos

Status: written trial, nothing run, nothing built. Date: 2026-10-23.
Follows "Phase 7 design" in `2026-10-12-hashseq-collaboration.md`, which ships encrypted snapshots on
"this computer", as a file, and on our own publish host. Logos is not part of phase 7; this is the
experiment CLAUDE.md calls "later" (Logos Messaging). Basecamp's spaces module is only in David Rusu's
personal registry, so this trial uses the official modules only.

Source for every name below: the Logos Module Atlas (`0.1.202610141851`, checked against Basecamp
`0.3.2`, 2026-10-14) contracts for `storage_module` and `delivery_module`. Nothing here is taken from
memory; where a payload shape is not in a contract it says so.

## 1. What Logos offers that fits

A publication is a padded JSON manifest plus Ogg Opus clip blobs, all AES-256-GCM ciphertext already.
That is exactly "opaque files addressed by hash", which is what Logos Storage holds.

**storage_module 3.0.2** (C++, bundled in Basecamp 0.3.2, builder generation 0.3.2-era, protocol 0.9.0;
linux-amd64/arm64, darwin-arm64, windows). Contract `storage_module.lidl`:

- `init(cfg: tstr) -> bool`, `start() -> bool`, `stop() -> result`, `isRunning() -> bool`. `cfg` is JSON
  (`network`, `data-dir`, `listen-port`, `bootstrap-node`, `storage-quota`, `block-ttl` ...).
  Default `block-ttl` in the documented example is "30d": check what that means for how long a
  published blob stays alive (open question 4).
- `uploadUrl(filePath: tstr, chunkSize: int, advertise: bool) -> result`: absolute file path, returns a
  session id; `storageUploadProgress`, then `storageUploadDone` with `{success, sessionId, cid, error}`.
  `advertise=false` neither announces to the DHT nor serves peers. Streaming variant:
  `uploadInit` / `uploadChunk` / `uploadFinalize` / `uploadCancel`.
- `downloadToUrl(cid, filePath, local, chunkSize, isPrivate, advertise) -> result`: session id = CID;
  `storageDownloadDone`. `isPrivate=true` tunnels over Mix; the contract says full privacy also needs
  `advertise=false`. Also `downloadChunks`, `downloadManifest`, `fetch` (background, no completion event).
- `exists(cid)`, `manifests()`, `space()`, `getAdvertise`/`setAdvertise`, `remove(cid)` (async,
  `storageRemoveDone`), `network()`, `peerId()`, `spr()`, `connect(peerId, peerAddresses)`.

**delivery_module 0.3.2** (C++, installable from the default catalog, no dependencies). Contract
`delivery_module.lidl`:

- `createNode(cfg: tstr) -> result`, `start()`, `stop()`, `getConnectionStatus()`.
- `send(contentTopic: tstr, payload: bstr) -> result` returns a request id; events `messageQueued`,
  `messagePropagated`, `messageSent`, `messageError`.
- `subscribe(contentTopic: tstr)` / `unsubscribe`; event
  `messageReceived(messageHash, contentTopic, payload, source, timestamp)` with `source` "live" or
  "history". Duplicates are suppressed only briefly, so readers dedupe by `messageHash`.
- `storeQuery(jsonQuery: tstr, peerAddr: tstr, timeoutMs: int)`: history by content topic and time
  range; the contract marks it unstable ("subject to change at any point"). Needs a store peer address.
- `channelCreate`/`channelSend` (reliable channels) exist; not needed here.

**Who holds keys.** Neither module holds an encryption key for our content: Storage stores whatever
bytes it is given, so confidentiality is entirely our AES-256-GCM key, which stays in the link
fragment. Each module has a node identity (`net-privkey` in the storage config; a node key in the
delivery node) that identifies the peer, not the reader. Not a key-custody feature: no keystore is
involved. A CID reveals the ciphertext's address and size to anyone who sees the link or the DHT
announcement, so use `advertise=false`/private where it works, and keep the manifest padded as designed.

**chat_module 0.3.0** (Rust, depends on delivery_module >=0.3.0) is ruled out: it offers end-to-end
encrypted 1:1 and group conversations (`create_conversation`, `send_message`, ...) with an ephemeral
identity per `init`. That is a messenger for people with accounts, not a way to serve a document to
whoever holds a link, and it has no blob transfer.

## 2. What is missing for us

- **No web reader.** Reading a CID needs a Logos Core host (Basecamp or logoscore) running
  storage_module. A browser, the phone page and the `file://` viewer cannot fetch from Logos Storage.
  Our link model (`https://host/p/<id>#<key>`) has no equivalent: a reader must install Basecamp.
- **No Python or HTTP entry.** Modules are C++/Rust/Nim plugins (QML UIs) called through LogosAPI
  inside a Logos Core host. The Mnemosyne backend cannot call `uploadUrl` directly; it would need a
  sidecar module or a headless logoscore process (like `mnemosyne-link` for iroh).
- **No official spaces module.** The atlas search for "space" only finds `storage_module.space()`
  (quota info). Basecamp spaces live in David's personal registry, so nothing to depend on.
- **No deletion guarantee.** `remove(cid)` removes from local storage; copies on peers that fetched
  the blob are not revoked. Same as our own "copies already opened cannot be revoked", but there is no
  expiry we control either.
- Not verified from the contracts, to find out in the trial: whether a CID can be fetched without the
  uploader staying online, and the exact string form of `<network>` (`network()` returns it; the
  `logos:<network>:<cid>` scheme below is our own convention, not a Logos URI).

## 3. Manual trial

Needs two machines (or two users) with Basecamp 0.3.2, each with storage_module running on the same
network. No Mnemosyne code changes; the "export as files" step uses what phase 7's "a file" target
and `/pub/...` already write. If that is not built yet, hand-write a manifest.json and two small
clip blobs with a test key; the trial does not need real meeting content.

1. **Export a publication as files.** Take a publication (manifest ciphertext plus clip blobs named by
   ciphertext SHA-256) into one directory, plus the viewer's `single.html`-less pieces. Note the key
   separately. Expect: files are opaque, the manifest is padded to its bucket size, no plaintext in
   the directory (grep for a known title). Teaches: whether the product's output is already
   storage-ready, and how many files one meeting is.
2. **Upload from Basecamp A.** `init` with the trial network, `start`, check `network()` and
   `peerId()`, then `uploadUrl(<abs path>, 65536, false)` per file; read the CID from
   `storageUploadDone`. Try `advertise=true` on one file to compare. Expect: one CID per file, success
   events, `exists(cid)` true. Teaches: upload ergonomics (per-file CIDs, no directory CID), time and
   quota cost (`space()`), and whether the manifest must carry blob CIDs (we address clips by
   ciphertext SHA-256, Logos by its own CID: the manifest needs a mapping, or a CID index file).
3. **Share the link.** Compose `logos:<network>:<manifest cid>#<key>`; send the clip CID list inside
   the manifest or a sibling index. Teaches: whether one CID plus a key is enough, or whether the
   index must be a second CID.
4. **Download on Basecamp B.** `init`/`start` on the same network, optionally `connect(peerId, ...)`
   to A, then `downloadToUrl(<cid>, <dest>, false, 65536, false, false)` for the manifest and each
   clip. Run a second pass with `isPrivate=true` and `advertise=false`. Expect: `storageDownloadDone`,
   byte-identical files (compare SHA-256 with A). Teaches: whether B finds content with A online, with
   A offline after B fetched (does B now serve it), and Mix speed ("KB/s" per the stack doc).
5. **Open with the viewer.** Put the downloaded files next to the viewer build and open it from
   `file://` with the key in the fragment. Expect: the viewer decrypts and plays clips; a wrong key
   fails cleanly. Teaches: that the viewer is transport-agnostic (the reader only needs files), and
   how clumsy the manual "download, then open" hand-off is for a non-technical reader.
6. **Optional: announce versions.** A: `createNode`, `start`, `send("/mnemosyne/1/pub-<id>/proto",
   <manifest cid + version bytes>)`. B: `createNode`, `start`, `subscribe` the same topic, watch
   `messageReceived`; then restart B and use catch-up (`source: "history"`) or `storeQuery` against a
   store peer. Expect: update notice arrives live, replays after restart (dedupe by `messageHash`).
   Teaches: whether "the link stays the same, new version appears" works, without a polling host. The
   topic string and payload layout are ours to invent; the contracts only fix the call shapes.
7. **Remove.** `remove(cid)` on A, then try B again (not cached) and C (never fetched). Teaches: what
   "unpublish" can truthfully promise.

## 4. Making it a product feature

Roughly: (a) a "Logos" publish target next to "this computer / file / our host" that writes the files
and calls a local sidecar; (b) the sidecar, a headless Logos Core host with storage_module (and
delivery_module for announcements), managed like `services/link.py` manages `mnemosyne-link`, or a
small Rust/Nim module using the contracts; (c) a link scheme and a manifest field mapping clip hashes
to CIDs; (d) the Publish dialog saying readers need Basecamp, and unpublish saying copies persist;
(e) a reader path: a Basecamp UI module embedding our viewer, since no browser entry exists. Cost is
mostly (b) and (e). Without a web reader this stays a power-user target, not the default.

Questions for David:

1. Is there a supported way to read a CID from a web page (gateway, WASM node, light client)? Planned?
2. Is there or will there be an official spaces module, and what would its read/write model be?
3. Is there a supported headless or HTTP-callable way to drive storage_module from outside a Logos Core
   host (logoscore CLI, local socket)?
4. What guarantees keep a blob available: `block-ttl`, who pins, what happens when the uploader goes
   offline?
5. How should "unpublish" work for content other peers cached?
6. What is the stable form of a network/CID reference for links (the `logos:<network>:<cid>` here is
   our guess)?
7. Is `storeQuery` (marked unstable) the intended way to read missed announcements, and is there an
   official store peer for the test network?

## 5. Results

(Fill after the trial.)

- Date, machines, Basecamp and module versions actually used:
- Step 1:
- Step 2:
- Step 3:
- Step 4:
- Step 5:
- Step 6:
- Step 7:
- Decision (drop / keep as written / build the sidecar):
