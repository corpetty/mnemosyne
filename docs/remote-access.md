# Remote access

How to reach Mnemosyne on your home machine from a laptop or phone elsewhere. The recommended way
today is [Tailscale](https://tailscale.com): the backend stays on `127.0.0.1`, nothing is opened to
the internet, and phones get an HTTPS address, which lets the phone page record inside the browser.

Mnemosyne's own remote access (below, experimental) needs no Tailscale: an end-to-end encrypted
iroh connection that only paired computers can open, through n0's relays or your own.

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

Mnemosyne's own remote access needs no Tailscale or VPN app: an end-to-end encrypted
[iroh](https://github.com/n0-computer/iroh) connection that only computers you pair can open.
Nothing on the home machine listens on the internet; it dials out to a relay.

1. **Home machine:** Settings → General → Server mode. Set an API token, tick **Remote access**,
   save. Once it says "On", choose **Pair a computer** and copy the invite (it works once, for
   10 minutes).
2. **Other computer** (desktop app): Settings → General → Connection → Remote access. Paste the
   invite, name the computer, and choose **Connect through remote access**. The app keeps its own
   key, runs a local tunnel (`127.0.0.1:8009`), and restarts it with the app.
3. Paired computers are listed under **Paired devices** on the home machine. **Remove** cuts one off
   at once. **Disconnect** on the other computer forgets the home machine.

A paired computer's key opens the whole API, as the API token does. Recording still happens on the
home machine.

### Local first

On the same network the two machines find each other directly: with remote access on, the home
machine announces itself on the local network (mDNS, under iroh's generic service name, carrying
only its endpoint ID and addresses), and a paired computer looks for it there before anything
else. Traffic then goes straight across the network, never through a relay. A paired computer only
listens; it does not announce itself on networks it visits.

### Relays

Relays are the fallback away from home. They only pass along packets they cannot read, and only
until the two machines find a direct path. By default Mnemosyne uses n0's free public relays, which are rate-limited and meant for
testing. To use your own, run the open-source iroh-relay server (a ready config and compose file are
in [deploy/relay/](../deploy/relay/README.md)), put its address in **Relays** under Remote access on
the home machine, and pair your computers again. Computers follow the home machine's relays; they
have no relay setting of their own. With your own relays, n0 is not involved at all.

### From a terminal

`mnemosyne-link` (the `link/` crate, bundled with the app) also pairs and connects without the app:

```bash
mnemosyne-link pair '<invite>' --key-file ~/.config/mnemosyne/link.key --name Laptop
mnemosyne-link connect '<ticket printed by pair>' --key-file ~/.config/mnemosyne/link.key --listen 127.0.0.1:8009
```

## Troubleshooting

- **"This phone is not paired"**: the phone was removed, its browser data was cleared, or the address
  changed (each address keeps its own key). Pair it again.
- **"Pairing failed: This pairing code is invalid or has expired"**: codes work once and for 10
  minutes. Choose **New pairing code**.
- **The Tailscale address does not load**: check `tailscale serve status` on the home machine and
  that HTTPS certificates are enabled for the tailnet.
