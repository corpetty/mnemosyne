# Run your own publish host

Mnemosyne can publish a meeting as a read-only page: a curated copy (summary, decisions, action
items, chosen quotes and clips) that anyone with the link can open in a browser. The copy is
encrypted on your computer before it leaves, and the key lives only in the link
(`https://<host>/p/<id>#<key>`; the part after `#` is never sent to a server). This folder runs
the host that stores those encrypted copies and serves the viewer page. It is a small Rust
program ([`publish-host/`](../../publish-host)) behind [Caddy](https://caddyserver.com) for TLS.

## What it stores, and what it can see

- **Stores:** per publication, an id (16 random hex characters), a hash of the publisher's
  token, one encrypted manifest, and encrypted clip files named by their SHA-256. On disk:
  `<data>/<id>/{meta.json, manifest, blobs/<sha256>}`. Nothing else, and nothing readable.
- **Cannot see:** any text, title, name, clip, or the key. It has no way to decrypt, and a
  publication's contents are padded, so sizes say little.
- **Can see:** ids, padded sizes, upload and expiry times, and the IP addresses of whoever
  uploads or opens a link (the web server's access log, like any site).
- **Can do:** delete things, refuse service, or hand back an older version it kept a copy of.
  It cannot alter what a reader sees without the reader's browser rejecting it (every part is
  authenticated by the key).
- Unpublishing deletes the files at once. A copy someone already opened cannot be recalled.

## What you need

- A small server with a public IP address (any VPS) and a DNS name pointing at it, e.g.
  `publish.example.org`.
- Ports **80/tcp** and **443/tcp** open (HTTPS, and Let's Encrypt).
- Docker with the compose plugin, or `cargo build --release` in `publish-host/` and a service
  manager.
- The built viewer page (below).

## Set it up

1. Clone the repo on the server (compose builds the image from `../../publish-host`), or copy
   this folder and `publish-host/` there keeping that relative path.
2. `cp .env.example .env` and set `PUBLISH_DOMAIN`. Optionally set `PUBLISH_UPLOAD_TOKEN` (see
   "Open or closed").
3. Put the built viewer in `./viewer`: `index.html` and its `assets/` folder, exactly what the
   Mnemosyne repo's viewer build produces. It is mounted read-only, so updating the viewer is
   copying new files in; no restart needed.
4. Start it:

   ```bash
   docker compose up -d
   ```

5. Check it: `curl https://publish.example.org/health` answers `ok`, and
   `https://publish.example.org/p/anything` shows the viewer ("not found" until a link is real).
6. In Mnemosyne, Settings, put `https://publish.example.org` in **Publish host**
   (`publish_host`), and the upload token if you set one. Until a host is configured, a meeting
   can be published from this computer or as a file. Like the relay, a blank `publish_host`
   means "no hosted publishing".

### Behind a proxy you already run

Skip the `caddy` service. Point your proxy at the container (publish its port on 127.0.0.1 or put
both on one Docker network), pass the original client address in `X-Forwarded-For`, and keep
`PUBLISH_TRUST_PROXY=true`. Only set it when the host cannot be reached except through that proxy:
otherwise anyone can claim any address and dodge the rate limits. The host takes the **last**
`X-Forwarded-For` entry, the one your proxy added. Allow request bodies of at least 21 MB.

## Open or closed

By default anyone can publish, within the limits below: the first write to an unknown id claims
it for whoever sent it (only a hash of their token is kept), and nobody else can change it.
That is fine for a host you only share with people you trust, but anyone who finds the address can
fill the quota. To close it, set `PUBLISH_UPLOAD_TOKEN`: every first write must then also carry
`X-Upload-Token: <that value>`. Reading is always open, because the link is the permission.

## Limits (all configurable)

| What | Default | Flag / environment variable |
| --- | --- | --- |
| Manifest | 1 MiB | `--manifest-max-bytes` / `PUBLISH_MANIFEST_MAX_BYTES` |
| One blob (clip) | 20 MiB | `--blob-max-bytes` / `PUBLISH_BLOB_MAX_BYTES` |
| One publication, all parts held | 32 MiB | `--publication-max-bytes` / `PUBLISH_PUBLICATION_MAX_BYTES` |
| The whole store (507 when full) | 10 GiB | `--quota-bytes` / `PUBLISH_QUOTA_BYTES` |
| Furthest expiry | 365 days | `--max-days` / `PUBLISH_MAX_DAYS` |
| Writes per client | 20 at once, then 30 a minute | `--write-burst`, `--write-per-minute` |
| Reads per client | 120 at once, then 600 a minute | `--read-burst`, `--read-per-minute` |
| Answers 410 after delete or expiry | 30 days | `--tombstone-days` / `PUBLISH_TOMBSTONE_DAYS` |
| Keep a publication that never got a manifest | 24 hours | `--pending-hours` / `PUBLISH_PENDING_HOURS` |
| Sweep for expired publications | every 5 minutes | `--sweep-secs` / `PUBLISH_SWEEP_SECS` |

Every publication must expire (the publisher sends `X-Pub-Expires`, at most `--max-days` away);
the sweeper deletes expired ones. A client is an IP address (an IPv6 /64). `0` per minute turns a
rate limit off. Other settings: `--listen` (`0.0.0.0:8080` in the image), `--data-dir`,
`--viewer-dir`, `--trust-proxy`, `--upload-token`; run `mnemosyne-publish-host --help` for the list.

## The interface

Mnemosyne's backend implements the same read side for "this computer", so a viewer works against
either. Ids are 16 lowercase hex characters, hashes 64 lowercase hex (anything else is 400).

| Request | Answer |
| --- | --- |
| `GET /p/{id}` | the viewer's `index.html` (also for unknown ids; the viewer says "not found"), strict CSP, `no-store`. Files in the viewer folder's sub-folders, e.g. `assets/`, are under `/p/<folder>/...` |
| `GET /pub/{id}` | the current manifest, with `ETag` (its SHA-256), `X-Pub-Version`, `X-Pub-Expires`; 304 on `If-None-Match`; 404 unknown, 410 deleted or expired |
| `GET /pub/{id}/{sha256}` | a blob, cached as immutable; 404 if the current publication does not hold it |
| `PUT /pub/{id}/{sha256}` | upload a blob (its hash must match the path) |
| `PUT /pub/{id}` | swap in a manifest: `X-Pub-Version` (must rise), `X-Pub-Expires`, `X-Pub-Blobs` (comma-separated hashes it uses, all uploaded first, else 409). Blobs it no longer uses are deleted |
| `DELETE /pub/{id}` | delete everything; 410 for 30 days, then 404 |
| `GET /health` | `ok` |

Writes need `Authorization: Bearer <publisher token>`. Problems come back as JSON,
`{"error": "<code>", ...}`: 400 malformed, 401 no token, 403 wrong token (or no upload token on a
closed host), 409 version not rising or blobs missing (the body lists them), 413 too large,
429 rate limited (with `Retry-After`), 507 store full.

## Guest notes rooms

A guest notes room is a meeting's live notes, shared with guests through a link
(`https://<host>/n/<room>#<key>`). Several people edit the same notes at once, and each change
is encrypted in the browser before it leaves. The key is in the part after `#`, which is never
sent to a server, so the host cannot read the notes.

- **Stores:** the sealed frames (encrypted edits), a sealed snapshot of the notes, and room
  metadata: the room id, its start and end times, and sizes.
- **Sees:** room ids, sizes, times, and the IP addresses of the people who connect. It never sees
  text.
- **Limits:** a frame is at most 2 MiB. The log and the snapshot are each at most 8 MiB. A room
  takes at most 64 connections. A room lasts at most 7 days. It is deleted 24 hours after its
  last frame, unless it is ended earlier with a later expiry.
- **Guest page:** the files go in `./guest`, copied from `backend/mnemosyne/api/static/guest` in
  the Mnemosyne repo, the same way the viewer goes in `./viewer`. The folder is mounted read-only
  and needs no restart.
- **Relays:** the guest page connects to iroh relays; n0's public ones are always allowed in its
  Content-Security-Policy, and `PUBLISH_RELAY_ORIGINS` (comma separated hosts) adds your own. An entry
  starting with `http://` is kept as written (for a loopback dev relay, as in the e2e tests); a bare
  host becomes `https://` and `wss://`.
- **WebSockets:** Caddy passes them through without extra configuration.

If you run a public host for other people, tell them in your privacy notice: the host stores
encrypted notes and their metadata (room ids, times, sizes), keeps them for up to 7 days after a
room's last edit, and sees the IP addresses of the people who connect. The contents are encrypted
on the guests' computers, and the key never reaches the host.

## Operating it

- **Backups:** not needed for privacy, and a lost host only loses links (publishers can publish
  again). To keep them, copy the `data` volume while the host is stopped.
- **Disk use:** `du -sh` the data volume; the quota counts the same bytes.
- **Logs:** `docker compose logs publish` (set `RUST_LOG=debug` for more). Caddy's access log
  has the IP addresses.
- **Updating:** `git pull && docker compose up -d --build`.

Tested in CI with `cargo test` in `publish-host/` (in process, against a fixture viewer folder).
