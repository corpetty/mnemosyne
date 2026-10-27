# Mnemosyne's own relay and notes host

This folder runs the servers Mnemosyne uses by default (design: `docs/plans/2026-10-26-our-relays-and-host.md`):

- **`relay.petty.services`**: an [iroh-relay](https://github.com/n0-computer/iroh/tree/main/iroh-relay) 1.3.0. Open
  to any iroh endpoint, limits as in `deploy/relay/`. Carries guests' browsers (relay-only) and whatever
  cannot go direct.
- **`notes.petty.services`**: publish-host behind Caddy (`deploy/publish/`): hosted publications (`/p/`, `/pub/`),
  guest notes rooms (`/n/`, `/live/`), the viewer and guest page, and the privacy notice at `/privacy`.

Both run on one machine with two static public IPv4 addresses, one per name. Operator: petty.services.
Read `deploy/relay/README.md` and `deploy/publish/README.md` for what each part does and sees.

## What you need

- A machine with Docker and the compose plugin, `curl`, GNU `tar` and `rsync` (for the update script).
- Two static public IPv4 addresses on it: `RELAY_ADDR` and `HOST_ADDR`.
- The repo cloned on it (compose builds the host from `../../publish-host`).

## DNS

Plain A records, no CNAMEs, a low TTL (300) while you set up:

| Name | Type | Value |
| --- | --- | --- |
| `relay.petty.services` | A | `<RELAY_ADDR>` |
| `notes.petty.services` | A | `<HOST_ADDR>` |

Add an AAAA record for a name only if the line gives that address its own IPv6 address and the
firewall below is open for it too. Without AAAA, only IPv4 is used, which is fine. Let's Encrypt needs
the A records to be right before the first start.

## Firewall

Open exactly these, each on its own address:

| Address | Port | Used for |
| --- | --- | --- |
| `RELAY_ADDR` | 80/tcp | Let's Encrypt, redirect |
| `RELAY_ADDR` | 443/tcp | relay (its own TLS) |
| `RELAY_ADDR` | 7842/udp | QUIC address discovery |
| `HOST_ADDR` | 80/tcp | Let's Encrypt, redirect |
| `HOST_ADDR` | 443/tcp | Caddy: host, guest page, privacy notice |
| `HOST_ADDR` | 443/udp | HTTP/3 |

Nothing else is published. The metrics ports and the host's port 8080 stay inside Docker.
Docker's published ports bypass `ufw`/`firewalld` rules written for the host's INPUT chain, so set the
rules on the line's firewall (or in the `DOCKER-USER` chain).

## First start

```bash
cd deploy/ours
cp .env.example .env        # set RELAY_ADDR and HOST_ADDR to the two public addresses
./update.sh                 # fetches the guest page and viewer of the latest release into ./guest, ./viewer
docker compose up -d --build
```

The first start asks Let's Encrypt for both certificates; give it a minute.

## Checks

```bash
curl https://relay.petty.services/healthz        # the relay answers
curl https://notes.petty.services/health         # ok
curl https://notes.petty.services/privacy        # the privacy notice
```

Open `https://notes.petty.services/n/` in a browser: the guest page loads (it says the room does not
exist). `https://notes.petty.services/p/0123456789abcdef` shows the viewer ("not found").

## Keep the guest page and viewer current

Their versions must match what released apps make. `update.sh` looks up the latest GitHub release of
`corpetty/mnemosyne`, downloads that tag's source archive and copies
`backend/mnemosyne/api/static/{guest,viewer}` into `./guest` and `./viewer` (it does nothing when the tag is
unchanged). No restart is needed: the folders are mounted read-only and replaced in place. To run it every hour:

```bash
# Edit User= and the paths in ours-update.service first (it assumes /opt/mnemosyne, user mnemosyne).
sudo cp ours-update.service ours-update.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ours-update.timer
systemctl list-timers ours-update.timer
journalctl -u ours-update.service -n 20
```

## Reading the metrics

Counts only, for your eyes only. Both stay on the containers' loopback, so read them from the server by
joining the container's network namespace:

```bash
# the relay (127.0.0.1:9090)
docker run --rm --network container:$(docker compose ps -q relay) curlimages/curl -s http://127.0.0.1:9090/metrics
# the host: rooms, guest connections, publications, bytes. The port is in publish-host/README.md.
docker run --rm --network container:$(docker compose ps -q publish) curlimages/curl -s http://127.0.0.1:9091/metrics
```

The host's image has no shell or curl, which is why the helper container is used. Run the commands over SSH on the server; the ports are not reachable from outside the containers on
purpose.

## Logs

`docker compose logs relay|publish|caddy`. The logs hold IP addresses, and the privacy notice promises at
most 7 days. `compose.yaml` makes Docker rotate each service's log at 10 MB, keeping 5 files; at normal
traffic that rolls over well inside 7 days. If a service logs more than about 7 MB a day, lower `max-size`
(and check `docker inspect --format '{{.LogPath}}' <container>`; the oldest line must be under 7 days old).

## Updating

Images are pinned by tag (`n0computer/iroh-relay:v1.3.0`, `caddy:2.10-alpine`); when deploying, pin them
by digest as well (`image: name@sha256:...`). To bump one, change the tag in a PR to this folder, then:

```bash
git pull
docker compose pull
docker compose up -d --build     # --build also rebuilds publish-host from the checked-out source
```

The guest page and viewer need none of this: the timer does it.

## Fill in before it is live

- `RELAY_ADDR` and `HOST_ADDR` in `.env`.
- `ops@petty.services` in `iroh-relay.toml` (Let's Encrypt contact) and `site/privacy.html`: confirm the mailbox exists.
- `User=` and the paths in `ours-update.service`.
- Optionally `PUBLISH_UPLOAD_TOKEN`: empty is an open host, as decided; the write budget
  (`PUBLISH_WRITE_BYTES_PER_DAY`, 200 MiB per client) and the 10 GiB quota bound it.
