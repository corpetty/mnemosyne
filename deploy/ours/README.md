# Mnemosyne's own relay and notes host

This folder runs the servers Mnemosyne uses by default (design: `docs/plans/2026-10-26-our-relays-and-host.md`):

- **`relay.petty.services`**: an [iroh-relay](https://github.com/n0-computer/iroh/tree/main/iroh-relay) 1.3.0. Open
  to any iroh endpoint, limits as in `deploy/relay/`. Carries guests' browsers (relay-only) and whatever
  cannot go direct.
- **`notes.petty.services`**: publish-host behind Caddy (`deploy/publish/`): hosted publications (`/p/`, `/pub/`),
  guest notes rooms (`/n/`, `/live/`), the viewer and guest page, and the privacy notice at `/privacy`.

Both run on one machine with two addresses, one per name. Operator: petty.services.
Read `deploy/relay/README.md` and `deploy/publish/README.md` for what each part does and sees.

## What you need

- A machine with rootful Docker and the compose plugin, `curl`, GNU `tar` and `rsync` (for the update
  script). Rootless Docker or Podman hides client addresses behind a proxy, and the rate limits and
  QUIC address discovery need them.
- Two static public IPv4 addresses, and two addresses on the machine for `RELAY_ADDR` and `HOST_ADDR`:
  the public ones if the machine holds them, else the LAN ones (next section).
- The repo cloned on it (compose builds the host from `../../publish-host`).

### Behind a router

Forward each public address to its own LAN address, with the ports from "Firewall" below. The machine
holds both, and neither may move, so reserve them on the router or keep them out of its DHCP pool. To
add the second one on Ubuntu, in `/etc/netplan/60-ours.yaml`, with `eno1` the interface's id in your
existing netplan file:

```yaml
network:
  version: 2
  ethernets:
    eno1:
      addresses: [<second LAN address>/24]
```

```bash
sudo chmod 600 /etc/netplan/60-ours.yaml
sudo netplan try      # reverts by itself unless you confirm, in case it cuts your SSH session
ip -4 -br addr show eno1
```

Other services on the machine are fine as long as nothing listens on 80, 443 or 7842 on all addresses
(`sudo ss -ltnup | grep -E ':(80|443|7842)\b'`); listening on another specific address is no conflict.
Inside the network, point both names at the LAN addresses with a local DNS record on the router, or
test from outside: looping back through the router's public address is unreliable.

## DNS

Plain A records, no CNAMEs, a low TTL (300) while you set up. On Cloudflare, **DNS only** (the grey
cloud): through the proxy UDP never arrives and the relay's Let's Encrypt validation fails.

| Name | Type | Value |
| --- | --- | --- |
| `relay.petty.services` | A | `<RELAY_ADDR>` |
| `notes.petty.services` | A | `<HOST_ADDR>` |

Add an AAAA record for a name only if the line gives that address its own IPv6 address and the
firewall below is open for it too. Without AAAA, only IPv4 is used, which is fine. Let's Encrypt needs
the A records to be right before the first start. This must print your addresses, not Cloudflare's:

```bash
dig +short @1.1.1.1 relay.petty.services notes.petty.services
```

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

Let's Encrypt allows 5 failed validations per name per hour, and the relay retries within seconds. So
check the path first, and start the relay and Caddy only once it works.

```bash
cd deploy/ours
cp .env.example .env        # set RELAY_ADDR and HOST_ADDR
./update.sh                 # fetches the guest page and viewer of the latest release into ./guest, ./viewer
docker compose up -d --build publish     # publishes no port, so this exposes nothing
```

Open the firewall or the router's rules, then check each path with a throwaway listener on the
machine:

```bash
sudo python3 -m http.server 443 --bind <RELAY_ADDR>
```

and from **outside the network** (a phone on mobile data, another server):

```bash
curl -m 5 -I http://relay.petty.services:443/
```

Any HTTP answer means DNS, the router and the machine's firewall all let it through. Stop the listener
and repeat with `<HOST_ADDR>` and `notes.petty.services`. Then start the rest; certificates usually take
under a minute:

```bash
docker compose up -d
docker compose logs -f relay caddy
```

A real failure on the relay reads `ERROR acme: ... error: Order(...)`, and on Caddy `tls.obtain` with
level `error`. If one keeps coming, stop the service before the limit is reached (`docker compose stop
relay caddy`): it retries on its own, and stays stopped across reboots until the next `up`.
`RELAY_LOG=info` in `.env` shows more, client addresses included, so put it back afterwards.

Ignore the relay's `TLS[acme] handshake` errors (`NoCipherSuitesInCommon`, `Tls12NotOffered`, `Connection
reset by peer`) and `Connection did not reach established state`. Those are other machines failing to
connect, mostly scanners: a new certificate is public within seconds. They carry no addresses.
`docker compose stop` takes 10 seconds for the relay and shows exit 137, because it ignores SIGTERM.

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

`docker compose logs relay|publish|caddy`. They hold no client addresses, and the privacy notice says so:
Caddy has no access log and drops `http.log.error`, iroh-relay logs at `warn` (its connection spans carry
the address from `info`), and publish-host logs none. Client addresses stay in memory, for the rate limits
and the write budget. Keep it that way when debugging: `RELAY_LOG=info` or a `log` directive in the
Caddyfile changes it. `compose.yaml` makes Docker rotate each service's log at 10 MB, keeping 5 files.

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
- `User=` and the paths in `ours-update.service`.
- Optionally `PUBLISH_UPLOAD_TOKEN`: empty is an open host, as decided; the write budget
  (`PUBLISH_WRITE_BYTES_PER_DAY`, 200 MiB per client) and the 10 GiB quota bound it.
