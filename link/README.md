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
