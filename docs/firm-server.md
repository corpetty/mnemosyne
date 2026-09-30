# A firm's Mnemosyne server

One machine in the office runs Mnemosyne; the advisors open it in Chrome or Edge on their own
Windows or Mac computers and install nothing. Meetings, transcripts, summaries and the AI models all
stay on that machine: in firm mode no cloud model can be used (the providers are not even created).

This page is for whoever sets the machine up. `scripts/firm-server-check.sh` checks the result.

## The machine

- An NVIDIA GPU with **16 GB** of memory holds the speech models and a 14B-class summary model at
  the same time (8 GB works if summaries may wait for the speech models to unload). Two or three
  advisors share one GPU comfortably: live transcription runs on the CPU, speaker labels take about
  3% of the GPU per recorded channel, and final transcriptions queue.
- 8 CPU cores, 32 GB RAM, and disk for the recordings: about 100 MB per recorded hour (each
  channel and the mix, compressed), plus a second disk (or a network share) for backups.
- Ubuntu 24.04 LTS (or Fedora) with the NVIDIA driver, `ffmpeg`, `git`, Node.js 22 (to build the
  web app) and [uv](https://docs.astral.sh/uv/).
- A TPM 2.0 chip (most business desktops have one), which keeps the encryption key sealed to this
  machine.

## Install

As root:

```bash
useradd --system --home-dir /srv/mnemosyne --create-home --shell /usr/sbin/nologin mnemosyne
mkdir -p /opt/mnemosyne && chown mnemosyne: /opt/mnemosyne
sudo -u mnemosyne git clone https://github.com/corpetty/mnemosyne /opt/mnemosyne
cd /opt/mnemosyne
sudo -u mnemosyne git checkout vX.Y.Z   # the latest release tag (firm mode needs 0.12.0 or later)
sudo -u mnemosyne bash -c 'cd backend && uv sync --frozen --extra gpu --extra onnx'
sudo -u mnemosyne bash -c 'corepack enable --install-directory ~/.local/bin && pnpm install --frozen-lockfile && pnpm build'
```

`pnpm build` writes the web app to `/opt/mnemosyne/build`, which the backend serves (`WEB_DIR`).

### Voice profiles (optional)

Naming speakers by their saved voice uses pyannote's embedding model, which needs a free Hugging
Face token with the model's licence accepted (see the main README). Put it in
`/srv/mnemosyne/secrets.env` (owner mnemosyne, mode 0600) as `HF_TOKEN=hf_...`. Without it,
speakers are numbered and can be named by hand.

### The summary model

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen3:14b
```

The unit file sets `DEFAULT_PROVIDER=ollama` and `DEFAULT_MODEL=qwen3:14b`. Try the demo meeting's
summary before choosing a different model.

### Encryption

Meetings are encrypted at rest with a key that, on a server, lives in a systemd credential sealed
to this machine's TPM: copying the disk elsewhere does not copy a usable key.

```bash
head -c 32 /dev/urandom | base64 > /root/mnemosyne-key
mkdir -p /etc/credstore.encrypted
systemd-creds encrypt --name=mnemosyne-key --with-key=tpm2 /root/mnemosyne-key /etc/credstore.encrypted/mnemosyne-key
shred -u /root/mnemosyne-key
```

After the first start, an admin turns encryption on (Settings → General → Encryption) and **prints
the recovery code** it shows: with a new motherboard or TPM, the recovery code is the only way back
in. Keep it where the firm keeps its other recovery codes.

**A replacement machine.** Restore the latest backup (Settings → General → Backups), then turn the
recovery code back into the key and seal it to the new machine's TPM:

```bash
/opt/mnemosyne/backend/.venv/bin/python -c 'import base64, sys; from mnemosyne.storage.crypto import parse_recovery_code; print(base64.b64encode(parse_recovery_code(sys.argv[1])).decode())' 'XXXX-XXXX-...' > /root/mnemosyne-key
systemd-creds encrypt --name=mnemosyne-key --with-key=tpm2 /root/mnemosyne-key /etc/credstore.encrypted/mnemosyne-key
shred -u /root/mnemosyne-key && systemctl restart mnemosyne
```

### The service

```bash
cp deploy/firm/mnemosyne.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now mnemosyne
```

The backend listens on `127.0.0.1:8008` only; HTTPS in front of it is what the advisors reach.

## HTTPS

Browsers only let a page use the microphone and share a call's audio over HTTPS, so advisors need an
`https://` address with a certificate their browser accepts. Two ways:

**Tailscale (simplest for a pilot).** Install Tailscale on the server and on each advisor's
computer, in the firm's tailnet, with MagicDNS and HTTPS certificates on (admin console → DNS). Then
on the server:

```bash
tailscale serve --bg localhost:8008
```

`tailscale serve status` shows the address (`https://mnemosyne.tail1234.ts.net`). Only computers in
the firm's tailnet can open it, from the office or from home. docs/remote-access.md has more.

**The firm's own domain.** Point a name such as `mnemosyne.example-firm.com` at the server and put
[Caddy](https://caddyserver.com) in front with `deploy/firm/Caddyfile` (edit the name). Caddy gets
and renews the certificate. For a name that only resolves inside the office, use Caddy's DNS
challenge for the firm's DNS provider.

## Backups

Settings → General → Backups: a folder on a second disk or a network share, every day, keeping at
least a week. Backups are encrypted with the same key, so the recovery code restores them too.

## Check it

```bash
/opt/mnemosyne/scripts/firm-server-check.sh https://mnemosyne.tail1234.ts.net
```

It checks the GPU, the service, firm mode, HTTPS with a valid certificate, the web app, Ollama and
the model, free disk, the clock (meeting records carry times) and the encryption credential.

## Updating

```bash
cd /opt/mnemosyne && sudo -u mnemosyne git fetch --tags && sudo -u mnemosyne git checkout vX.Y.Z
sudo -u mnemosyne bash -c 'cd backend && uv sync --frozen --extra gpu --extra onnx'
sudo -u mnemosyne bash -c 'pnpm install --frozen-lockfile && pnpm build'
systemctl restart mnemosyne
```

Not while someone is recording: the status bar in the app shows recordings in progress.
