# Run your own relay

Mnemosyne's remote access (docs/remote-access.md) connects a paired computer to your home
machine over an end-to-end encrypted iroh connection. When the two cannot reach each other
directly, a relay passes the encrypted packets along. By default that is n0's free public
relays, which are rate-limited and meant for testing. This folder runs your own: the open-source
[iroh-relay](https://github.com/n0-computer/iroh/tree/main/iroh-relay) server, and nothing else.

- **Open, no accounts.** Any iroh endpoint may use it. There is no token, sign-up or extra
  service to run; the relay cannot read what it forwards and stores nothing.
- **Bounded.** `iroh-relay.toml` limits new connections and each client's bandwidth, so a busy
  or misbehaving client cannot run up the hosting bill.
- **Mostly idle.** Computers usually switch to a direct connection once the relay has helped them
  find each other; the relay carries traffic only when they cannot.

## What you need

- A small server with a public IP address (any VPS).
- A DNS name pointing at it, e.g. `relay.example.org`.
- Ports **80/tcp** and **443/tcp** open (HTTPS, and Let's Encrypt), and **7842/udp** (QUIC address
  discovery, which helps devices find a direct path).
- Docker with the compose plugin, or the `iroh-relay` binary.

## Set it up

1. Copy this folder to the server.
2. In `iroh-relay.toml`, set `hostname` to your DNS name and `contact` to your email address.
3. Start it:

   ```bash
   docker compose up -d
   ```

   Without Docker: `cargo install iroh-relay --features server`, then
   `iroh-relay --config-path iroh-relay.toml` under your service manager.
4. Check it: `curl https://relay.example.org/healthz` should answer.
5. On the home machine, in Mnemosyne → Settings → General → Server mode, with Remote access on,
   put `https://relay.example.org` in **Relays** and save. Several relays, e.g. one per region,
   go in comma-separated.
6. Pair your computers again (Pair a computer): they learn the relay from the new invite and need
   no setting of their own.

From then on n0 is not involved at all: not for relaying, and not for looking up where your home
machine is.

## Tuning

- `limits.client.rx.bytes_per_second` is per connected endpoint. 1 MB/s is plenty for Mnemosyne
  (meetings are 64 kbps audio); lower it to save bandwidth, raise it for faster audio seeking.
- `accept_conn_limit` / `accept_conn_burst` cap how fast new connections are accepted.
- Metrics (Prometheus) are served on `127.0.0.1:9090` on the server.

Tested locally: the backend's sidecar, a paired computer and this config (TLS section removed,
`iroh-relay --dev`) connected through the relay with iroh-relay 1.3.0.
