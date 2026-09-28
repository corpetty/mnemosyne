# Remote access

How to reach Mnemosyne on your home machine from a laptop or phone elsewhere. The recommended way
today is [Tailscale](https://tailscale.com): the backend stays on `127.0.0.1`, nothing is opened to
the internet, and phones get an HTTPS address, which lets the phone page record inside the browser.

Mnemosyne's own remote access (below, experimental) needs no Tailscale: an end-to-end encrypted
iroh connection that only paired computers can open. It has no Settings UI yet.

## What you need

- Tailscale on the home machine and on each laptop or phone, signed in to the same tailnet.
- **MagicDNS** and **HTTPS certificates** turned on for the tailnet (admin console → DNS).
- An API token in Mnemosyne (Settings → General → Server mode). Everyone on your tailnet can reach a
  served address, so the token is what keeps it yours.

## Set up the home machine

1. In Mnemosyne, set an **API token** (Settings → General → Server mode) and save.
2. Serve the backend on your tailnet over HTTPS:

   ```bash
   tailscale serve --bg localhost:8008
   ```

   The address appears in `tailscale serve status`, e.g. `https://desk.tail1234.ts.net`. It stays up
   across reboots until you turn it off with `tailscale serve --https=443 off`.
3. Put that address in **Phone address** (same Settings section) and save.

The backend keeps listening on `127.0.0.1` only; Tailscale forwards tailnet requests to it. No
`--host 0.0.0.0` is needed.

## A phone

In Settings → Server mode → Record from a phone, choose **Pair a phone** and scan the QR code with
the phone. The code works once and expires after 10 minutes; the phone keeps its own key, and the API
token never leaves the computer. Because the address is HTTPS, the page records in the browser (keep
it open while recording); the phone's own recorder app works too.

Paired phones are listed under the QR code. **Remove** cuts one off at once. A phone's key only
allows uploading recordings, not reading your meetings.

## A laptop

On the other machine, open Mnemosyne → Settings → General → Connection, enter the Tailscale address as the
**Backend URL** and the API token, then Connect. The app now works with the home machine's
meetings: browse, search, ask, import and summarize. Recording, audio devices and echo cancellation
still act on the machine running the backend.

## On a trusted LAN without Tailscale

Run the backend with `--host 0.0.0.0` and an API token (see [development.md](development.md#server-mode-backend-on-another-machine)),
then pair phones the same way; they use `http://<lan address>:8008`. Plain http means the phone
records with its own recorder app rather than inside the page, and anyone on the network can see the
traffic, so keep this to networks you trust.

## Built-in remote access (experimental)

The `link/` crate builds `mnemosyne-link`. With the `remote_access` setting on (Settings UI to come;
for now `REMOTE_ACCESS=true` or `remote_access = true` in config.toml, plus an API token), the
backend runs `mnemosyne-link home`. It dials out to n0's public relays, so nothing listens on the
internet, and accepts connections only from computers paired with it. Relays forward encrypted
packets they cannot read; when both machines can reach each other directly, iroh switches to a
direct path.

Pairing and connecting from another computer, for now from a terminal:

```bash
cargo build --release --manifest-path link/Cargo.toml
# On the home machine: an invite for a computer (valid 10 minutes, once).
curl -s -X POST -H "Authorization: Bearer $API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"kind":"desktop"}' http://127.0.0.1:8008/api/pairing/codes
# On the other computer: pair (prints this computer's own API token and the home ticket) ...
mnemosyne-link pair '<invite>' --key-file ~/.config/mnemosyne/link.key --name Laptop
# ... then serve home on a local port and point Settings → Connection at it, with that token.
mnemosyne-link connect '<ticket>' --key-file ~/.config/mnemosyne/link.key --listen 127.0.0.1:8009
```

Paired computers are listed with the phones and removed the same way.

## Troubleshooting

- **"This phone is not paired"**: the phone was removed, its browser data was cleared, or the address
  changed (each address keeps its own key). Pair it again.
- **"Pairing failed: This pairing code is invalid or has expired"**: codes work once and for 10
  minutes. Choose **New pairing code**.
- **The Tailscale address does not load**: check `tailscale serve status` on the home machine and
  that HTTPS certificates are enabled for the tailnet.
