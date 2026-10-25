# mnemosyne-link

Remote access to a Mnemosyne backend over [iroh](https://github.com/n0-computer/iroh): an end-to-end
encrypted tunnel that only paired computers can open. The backend runs `mnemosyne-link home` while
the `remote_access` setting is on (`backend/mnemosyne/services/link.py`); a paired computer runs the
client side (`pair`, then `connect`) to reach it from anywhere. See `docs/remote-access.md`.

```bash
cargo test                       # home + device on one machine: pairing, tunnelling, removal
cargo run -- home --help
```

Tunnelled connections (and the pairing redeem) reach the backend from `127.0.0.2`
(`TUNNEL_SOURCE`), so it can tell them from this machine's own, which come from `127.0.0.1`. If
`127.0.0.2` cannot be bound (not Linux, loopback /8 not routed) the sidecar falls back to
`127.0.0.1` and logs a warning once; the backend then cannot tell the difference.

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
| `{"id", "op": "pair", "invite": <ticket>#<code>, "name": <this machine>, "peer"?: {"name", "ticket", "token"}, "card"?: <JSON>}` | `{"token", "sync"?, "card"?, "ticket": <the invite's ticket>, "peer": <its endpoint id>}` |

- `dial` serves the peer on a new loopback port: each TCP connection there is tunnelled to the
  peer's backend, as `connect` does. Dialing a peer again returns the listener it already has.
  Home's relays are not changed by a dial: the peer's relay and direct addresses come from its
  ticket, and mDNS finds it on the local network when home was started with relays.
- `hangup` closes that listener and the connection under it (open tunnels are cut).
- `pair` is the `pair` command from home's endpoint. `peer`, when given, is passed unchanged to the
  other backend's `POST /api/pairing/redeem` body as `peer`, so it can dial this machine back.
  `card`, when given, is an opaque signed JSON object forwarded as the top-level `card` field of that
  body (absent otherwise); a `card` in the backend's redeem reply comes back as `card` in the reply
  (null when none). The `pair` command prints it too when present.

## Guest rooms (`rooms`)

`mnemosyne-link rooms --data-dir D --guest-dir G --viewer-dir V --upload-token-file F [--relay URL]...`
serves guest-notes rooms over iroh without remote access being on (`src/guest.rs`). It starts the
embedded publish host on `127.0.0.1:<port>` (`src/host.rs`, data under `D/guest_rooms/host`); only
the token in `F` creates rooms there (`X-Upload-Token`). `F` must exist, be non-empty and not be
world-readable, or the command exits with an error. The ready line is
`{"listen": "127.0.0.1:<port>", "relay_kind": "n0"|"custom"}` ("custom" when `--relay` is given),
then stdin takes the commands below with the same reply rules as `home` (`id`, `op`, `ok`,
`error`). Closing stdin stops nothing; SIGTERM closes every room endpoint and keeps their keys.

| Request | Reply on success |
| --- | --- |
| `{"id", "op": "room_start", "room": <16 hex>, "relay"?: <relay URL>}` | `{"endpoint_id": <room endpoint id>, "relay": <relay URL>}` |
| `{"id", "op": "room_stop", "room"}` | `{"closed": <whether it was served>}` |
| `{"id", "op": "room_list"}` | `{"rooms": [{"room", "endpoint_id", "relay"}]}` |

- Each room has its own endpoint and key, `D/guest_rooms/keys/<room>.key` (0600), so a restart
  serves the same endpoint id and the guest link stays valid. `room_stop` deletes the key: a later
  `room_start` is a new endpoint id. Starting a room that is served returns it unchanged.
- `relay` absent: one relay is picked per process, the only `--relay` given, or (several, or n0's
  when none is given) the home relay a throwaway endpoint connects to within 10 s; no relay
  reachable is an error. Pass the `relay` from an earlier reply to keep the link's relay. A room
  endpoint uses that one relay only, speaks only `mnemosyne/guest/1` (never the `T`/`P` streams of
  `mnemosyne/link/1`) and is not announced by mDNS.
- A guest opens one bi stream of frames `u32 BE len || tag (0 text, 1 binary) || payload` (`len`
  counts the tag; payload at most 2 MiB + 64 KiB), proxied to `ws://127.0.0.1:<port>/live/<room>`:
  text frames are WebSocket text messages, binary frames binary ones, both ways. The host's close
  `(code, reason)` (4401, 4403, 4409, 4410, 4413, 4429, 1001, ...) becomes the connection's
  application close, after the stream is finished so every frame before it arrives. The guest
  finishing its stream closes the WebSocket with 1000 and the connection with the host's answer
  (1000). The proxy's own closes: 4413 `frame_too_large` (after `{"type":"error","code":
  "frame_too_large"}`) for a frame over the cap either way, 1002 for a bad tag or a truncated
  frame, 1007 for text that is not UTF-8, 1011 when the host is not reachable or lost, 4401
  `join_timeout` when no stream is opened within 15 s.
- `home` answers these ops with an error, and `rooms` answers `dial`, `hangup` and `pair` with one.
