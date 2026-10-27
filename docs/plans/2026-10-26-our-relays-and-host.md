# Our relay and guest notes host

Status: proposal, 2026-10-26, nothing built. Drafted against main at 0.25.1. Asked for by Corey: stop
depending on n0's testing relays. Decided by Corey (2026-10-26): run it on his own high-availability
server on his enterprise line for now; one relay; no migration for already paired computers (he is the
only user and re-pairs); guest notes are copied to our host by default, so we can see some usage; our
host is preselected for publishing.

Why: every connection that leaves a machine goes through n0's public relays today: guest notes in a
browser (phase 10, labelled "through a free testing relay"), remote access and library sync between
Corey's computers, and phase 8's contacts. n0 documents them as for development and testing,
rate-limited, without guarantees. Guest links that should outlive the owner's computer, and hosted
publishing, need a publish host somebody runs; nobody does by default (`OUR_GUEST_HOST = ""`). The rule
agreed on 2026-09-28 still stands: **ours by default once they exist, not n0's**.

What exists already: deploy/relay/ (iroh-relay 1.3.0, open, Let's Encrypt, per-client limits) and
deploy/publish/ (publish-host behind Caddy: publications, guest rooms, quotas, mandatory expiry). This
plan runs both on one machine of Corey's and makes the app use them by default.

## What runs, and where

- **One machine, two addresses**: Corey's high-availability server on his enterprise line, which has
  two free static public addresses (Corey, 2026-10-26): one for the relay, one for the host, each
  with its own DNS name on petty.services, the domain of Corey's S corp consultancy, which runs
  them (Corey, 2026-10-26):
  `relay.petty.services` and `notes.petty.services`. Docker compose, one
  file (deploy/ours/compose.yaml) reusing the two existing setups, each service bound to its address.
- **The relay**: iroh-relay 1.3.0 (the version the app's iroh speaks), open to any iroh endpoint
  (browsers and sidecars use random endpoint ids, so there is nothing to allowlist), the existing
  limits: 20 new connections a second (burst 100), 1 MB/s per client. Most remote-access and sync
  traffic goes direct after the first packets; the relay carries guests' browsers (they are
  relay-only) and what cannot go direct.
- **The host**: publish-host at `notes.petty.services`: hosted publications (`/p/`, `/pub/`), guest rooms
  (`/n/`, `/live/`), the viewer and the guest page. Open writes as designed (first write claims an id,
  per-IP rate limits), quota 10 GiB to start, publications 90 days by default in the app and at most
  365, rooms at most 7 days. New: a per-client daily write budget (`--write-bytes-per-day`, 200 MiB per
  IP or IPv6 /64) so one client cannot fill the quota.
- **Ports, one address each**: the relay's address has 80/tcp and 443/tcp (iroh-relay's own TLS with
  Let's Encrypt, exactly as deploy/relay/) and 7842/udp (QUIC address discovery, which helps devices
  find a direct path); the host's address has 80/tcp and 443/tcp (+443/udp for HTTP/3) for Caddy. Each
  container publishes only on its own address (`<address>:443:443` in compose), so the two never
  compete for a port and neither needs a proxy in front of the other.
- **The guest page stays on GitHub Pages** (`https://corpetty.github.io/mnemosyne/n/`, static, survives an
  outage at home) and is also served by our host. Its CSP lists our relay next to n0's, which stay
  allowed so older links keep working (pages.yml, `guest_csp`, publish-host's defaults).
- **Usage we can see, and nothing more**: the host already knows how many rooms and publications exist.
  publish-host gains a few counters on its local metrics port (127.0.0.1 only): rooms created, guest
  connections, frames relayed, publications created and opened, bytes stored; iroh-relay has its own
  (connected clients, bytes). Counts only, no per-room or per-person records; read over SSH. Caddy and
  iroh-relay logs keep IP addresses for 7 days at most (log rotation). No analytics, no third-party
  scripts. A short privacy notice at `https://notes.petty.services/privacy`, naming the consultancy as the operator, linked from Settings and the guest
  page footer, says what the server sees (encrypted payloads, ids, sizes, times, IP addresses) and that
  guest notes are copied there by default.

## Operating it

- **deploy/ours/**: compose.yaml (iroh-relay, publish-host, Caddy), the Caddyfile for `notes.petty.services`,
  iroh-relay.toml for `relay.petty.services`, `.env.example` (domain, the two addresses, quota), a README with
  the exact steps: DNS records (A for each name, AAAA if the line has IPv6), the line's firewall
  (relay address: 80/tcp, 443/tcp, 7842/udp; host address: 80/tcp, 443/tcp+udp), first start, checks.
  The machine, the domain and the line are Corey's; nothing here creates paid resources.
- **Updates**: the guest page and viewer bundles on the host must match what released apps make. A
  `deploy/ours/update.sh` on the server pulls the latest release's `backend/mnemosyne/api/static/{guest,viewer}`
  from the tagged source archive into the mounted folders (no restart needed); run by a systemd timer
  every hour, so no SSH key for CI is needed. Relay and host images are bumped by hand in a PR to
  deploy/ours/ (pinned versions).
- **Watching it**: a scheduled GitHub Actions workflow (every 15 minutes) checks `/healthz` on the relay
  and `/health` on the host, and does one real round trip: a tiny publication created, read back and
  deleted on the host, and an iroh connection through the relay (a `mnemosyne-link relay-check` that
  dials an echo endpoint it starts itself through the given relay). On failure it opens or comments on
  one issue labelled `ours-down`; recovery closes it.
- **When home is down** (power, line): guest notes fall back to the owner's own tunnel; publishing to our
  host shows the error and offers the file; remote access on the same network still works (mDNS). The app
  never falls back to n0 silently.

## App changes

- **One module for our servers** (services/ours.py): `OUR_RELAYS = ["https://relay.petty.services"]`,
  `OUR_HOST = "https://notes.petty.services"`, read by services/link.py, guest_notes.py, publishing and the CSP
  builders (publishing.py `guest_csp`, pages.yml, publish-host defaults). A setting **Use Mnemosyne's
  servers** (`use_our_servers`): on by default; off on team servers and when `cloud_models` is off (the
  local-only posture); off means only what you configured yourself, and what needs a server says so.
- **Remote access, sync and contacts**: blank `remote_relays` means ours (`parse_relays`, the link
  sidecar's `Relays`, `Relays::for_ticket` treats ours like any custom relay). No migration (Corey,
  2026-10-26): after updating, pair your computers again, as docs/remote-access.md already says for a
  relay change. n0 is no longer a default anywhere.
- **Guest notes**: `auto` = iroh through our relay, plus the mirror on our host by default (Corey). The
  Invite guests menu: "Through Mnemosyne's relay" and "Also kept on Mnemosyne's server after the
  meeting"; `relay_kind` gains "ours". The tunnel fallback stays.
- **Publishing**: the publish dialog preselects "Mnemosyne's server" (Corey), 90 days by default, next to
  "This computer" and "A file".
- **Settings → Publishing / Server mode** show our relay and host as the defaults, with the privacy notice
  link, and keep the fields for your own.

## Tests

- deploy/ours/: a CI job runs the compose file with a local CA instead of Let's Encrypt, and the guest
  notes e2e (iroh and mirror) against it, replacing the dev relay with a real iroh-relay with TLS.
- Backend: `use_our_servers` off removes every default; team mode and local-only start with it off; the
  CSPs list our relay; the mirror is on by default and named in the menu.
- publish-host: the write budget; the counters.
- The health workflow runs on `workflow_dispatch` against the real names before the schedule is turned on.

## Order

1. Corey: the two DNS records (relay. and notes.petty.services, one free address each), the ports.
2. deploy/ours/ files and README; bring the services up; health workflow green.
3. publish-host write budget and counters; CSP lists; services/ours.py and `use_our_servers`.
4. The default switch for remote access, guest notes (mirror on) and publishing (preselected), menu
   wording, privacy notice.
5. update.sh and its timer.

## Open questions for Corey

None left: the domain is petty.services (Corey, 2026-10-26).
