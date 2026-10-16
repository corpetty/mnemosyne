# mnemosyne-link

Remote access to a Mnemosyne backend over [iroh](https://github.com/n0-computer/iroh): an end-to-end
encrypted tunnel that only paired computers can open. The backend runs `mnemosyne-link home` while
the `remote_access` setting is on (`backend/mnemosyne/services/link.py`); a paired computer runs the
client side (`pair`, then `connect`) to reach it from anywhere. See `docs/remote-access.md`.

```bash
cargo test                       # home + device on one machine: pairing, tunnelling, removal
cargo run -- home --help
```

Every iroh stream starts with one byte: `T` tunnels one TCP connection to the backend on
`127.0.0.1`, `P` carries a pairing request that the home side redeems with the backend on the
device's behalf, vouching for its endpoint id. Home re-reads `<data_dir>/paired_devices.json` for
each connection and stream, so removing a device in Settings cuts it off at once.

Local first: `Lan::Advertise` (home) announces the endpoint on the local network by mDNS and
`Lan::Discover` (devices) looks for it there, so machines on the same network connect directly
without a relay or any lookup service. Relays are the fallback.

## Control channel (sync peers)

`home` prints one ready line first, `{"endpoint_id": ..., "ticket": ...}`, then reads commands on
stdin, one JSON object per line, and answers each with one JSON line on stdout. Requests run
concurrently, so replies may come out of order: match them by `id`. Every reply echoes the request's
`id` and `op` (null when the line had none, e.g. invalid JSON) and has `ok`; a failure has `ok: false`
and `error` and changes nothing. Closing stdin stops nothing: home and every dialed peer keep running
until the process is terminated.

Everything runs on home's own endpoint (`link.key`), so a peer sees the same endpoint id it paired.

| Request | Reply on success |
| --- | --- |
| `{"id", "op": "dial", "ticket": <endpoint ticket>}` | `{"peer": <endpoint id>, "listen": "127.0.0.1:<port>"}` |
| `{"id", "op": "hangup", "peer": <endpoint id>}` | `{"peer", "closed": <whether it was dialed>}` |
| `{"id", "op": "pair", "invite": <ticket>#<code>, "name": <this machine>, "peer"?: {"name", "ticket", "token"}}` | `{"token", "ticket": <the invite's ticket>, "peer": <its endpoint id>}` |

- `dial` serves the peer on a new loopback port: each TCP connection there is tunnelled to the
  peer's backend, as `connect` does. Dialing a peer again returns the listener it already has.
  Home's relays are not changed by a dial: the peer's relay and direct addresses come from its
  ticket, and mDNS finds it on the local network when home was started with relays.
- `hangup` closes that listener and the connection under it (open tunnels are cut).
- `pair` is the `pair` command from home's endpoint. `peer`, when given, is passed unchanged to the
  other backend's `POST /api/pairing/redeem` body as `peer`, so it can dial this machine back.
