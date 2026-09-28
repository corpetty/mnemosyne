# Remote access

How to reach Mnemosyne on your home machine from a laptop or phone elsewhere. The recommended way
today is [Tailscale](https://tailscale.com): the backend stays on `127.0.0.1`, nothing is opened to
the internet, and phones get an HTTPS address, which lets the phone page record inside the browser.

Mnemosyne has no relay of its own yet; that is planned. This page is the no-code route until then.

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

## Troubleshooting

- **"This phone is not paired"**: the phone was removed, its browser data was cleared, or the address
  changed (each address keeps its own key). Pair it again.
- **"Pairing failed: This pairing code is invalid or has expired"**: codes work once and for 10
  minutes. Choose **New pairing code**.
- **The Tailscale address does not load**: check `tailscale serve status` on the home machine and
  that HTTPS certificates are enabled for the tailnet.
