# A team's Mnemosyne server

One machine runs Mnemosyne for a group of people; they open it in Chrome or Edge on their own
Windows, Mac or Linux computers and install nothing. Everyone signs in and has their own meetings;
reviewers can read everyone's. With `CLOUD_MODELS=false` (as in the unit file below) meetings,
transcripts, summaries and the AI models all stay on that machine: no cloud model can be used, the
providers are not even created.

This page is for whoever sets the machine up. `scripts/team-server-check.sh` checks the result.

## The machine

- An NVIDIA GPU with **16 GB** of memory holds the speech models and a 14B-class summary model at
  the same time (8 GB works if summaries may wait for the speech models to unload). Two or three
  people share one GPU comfortably: live transcription runs on the CPU, speaker labels take about
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
sudo -u mnemosyne git checkout vX.Y.Z   # the latest release tag (team mode needs 0.12.0 or later)
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

The unit file sets `DEFAULT_PROVIDER=ollama` and `DEFAULT_MODEL=qwen3:14b`. Summarize a meeting or two
before choosing a different model.

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
in. Keep it with your other recovery codes.

**A replacement machine.** Restore the latest backup (Settings → General → Backups), then turn the
recovery code back into the key and seal it to the new machine's TPM:

```bash
/opt/mnemosyne/backend/.venv/bin/python -c 'import base64, sys; from mnemosyne.storage.crypto import parse_recovery_code; print(base64.b64encode(parse_recovery_code(sys.argv[1])).decode())' 'XXXX-XXXX-...' > /root/mnemosyne-key
systemd-creds encrypt --name=mnemosyne-key --with-key=tpm2 /root/mnemosyne-key /etc/credstore.encrypted/mnemosyne-key
shred -u /root/mnemosyne-key && systemctl restart mnemosyne
```

### The service

```bash
cp deploy/team/mnemosyne.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now mnemosyne
```

The backend listens on `127.0.0.1:8008` only; HTTPS in front of it is what people reach.

## HTTPS

Browsers only let a page use the microphone and share a call's audio over HTTPS, so people need an
`https://` address with a certificate their browser accepts. Two ways:

**Tailscale (simplest).** Install Tailscale on the server and on each person's
computer, in your tailnet, with MagicDNS and HTTPS certificates on (admin console → DNS). Then
on the server:

```bash
tailscale serve --bg localhost:8008
```

`tailscale serve status` shows the address (`https://mnemosyne.tail1234.ts.net`). Only computers in
your tailnet can open it, from the office or from home. docs/remote-access.md has more.

**Your own domain.** Point a name such as `mnemosyne.example.com` at the server and put
[Caddy](https://caddyserver.com) in front with `deploy/team/Caddyfile` (edit the name). Caddy gets
and renews the certificate. For a name that only resolves inside the office, use Caddy's DNS
challenge for your DNS provider.

## People

Nobody has a password. The first admin comes from the server itself:

```bash
sudo -u mnemosyne MNEMOSYNE_DATA_DIR=/srv/mnemosyne/data \
  /opt/mnemosyne/backend/.venv/bin/mnemosyne-backend users add "Pat Lee" --email pat@example.com \
  --role admin --address https://mnemosyne.tail1234.ts.net
```

It prints an invite link. Opening it signs that browser in as that person; it works once and
expires after a week. From then on the admin adds everyone else in Settings → General → People and
access, and sends each their own link (directly, not in a shared channel: whoever opens it first
signs in as them). Someone on a second computer gets a second link.

- **Advisor**: sees and changes only their own meetings (the ones they recorded or imported).
- **Reviewer** (compliance): reads every meeting, changes only their own.
- **Admin**: everything, including people and settings.

Opening, playing and exporting a meeting is noted in that meeting's history with who did it.
"Disable" and "Sign out everywhere" take effect at once. The people list lives in
`/srv/mnemosyne/data/users.json` (tokens as hashes only).

## Recording

People record in the browser: New meeting → Record. With "Ask before every recording" on (Settings →
Recording → Consent), Mnemosyne first asks how the people in the meeting agreed to be recorded
(everyone told and agreed; in the room, everyone told; one-party consent where the law allows),
with a short script to read out; the answer and who gave it go into the meeting's history. Then
Chrome or Edge asks for the microphone the first time, and, when "The
call's audio" is ticked, what to share:

- **Zoom or Teams in their own app (Windows):** choose *Entire screen* and tick *Share system
  audio*. Only the sound is recorded, never the screen.
- **A call in a browser tab (Google Meet, Zoom or Teams on the web):** choose that tab and tick
  *Share tab audio*. This is also the way on a Mac, where Chrome shares a tab's sound but not the
  system's.
- **In the room:** untick the call's audio; the microphone records everyone. Put the laptop in
  the middle of the table.
- **Phone calls:** a softphone on the computer (Teams, Zoom Phone) is a call like any other. A
  desk or mobile phone's own recording can be added afterwards with Import.

The audio goes to the server as it is recorded, so the live transcript, the copilot and speaker
labels work as on the desktop app, and closing the laptop loses seconds, not the meeting: the
browser reconnects and carries on, and a recording no browser sends to for 10 minutes is saved
and stopped on its own. Keep the tab open while recording (it can be in the background).

## Records

For a team that keeps its meetings as records:

- **Nothing is overwritten.** Editing a transcript, renaming a speaker, transcribing or summarizing
  again keeps the earlier version (each meeting's Record card lists them, with who and when).
- **Sealed.** After each transcription, summary and edit, the meeting's content and the SHA-256 of
  each audio file are sealed, each seal chained to the one before. The Record card checks the chain
  against what is there now; anything changed outside the app (a database row, a swapped file)
  shows as "changed outside the app". Someone who can rewrite the whole database could rewrite the
  chain as well, which is why the chain head also goes into every export and backup.
- **Kept.** Settings → General → Records period (years): until then, deleting a meeting or its
  audio needs an admin and a reason, and audio retention leaves it alone. A reviewer or an admin
  can put a meeting on **legal hold**: then nobody can delete anything of it.
- **Deletions are logged** with who, when and why, and the log outlives the meeting (Settings →
  General → Records → the deletion log).
- **Export**: one meeting (its Record card) or every meeting in a date range (Settings
  → General → Records) as a zip: audio, transcripts, summaries, earlier versions, history
  (recordings, consent, who opened what), seals, the deletion log, and `SHA256SUMS` to check
  nothing changed. The zip holds the audio unencrypted: it can be downloaded once, and is removed
  from the server a day after it was made even if nobody fetches it.

## Backups

Settings → General → Backups: a folder on a second disk or a network share, every day, keeping at
least a week. Backups are encrypted with the same key, so the recovery code restores them too.

## Check it

```bash
/opt/mnemosyne/scripts/team-server-check.sh https://mnemosyne.tail1234.ts.net
```

It checks the GPU, the service, team mode, HTTPS with a valid certificate, the web app, Ollama and
the model, free disk, the clock (meeting records carry times) and the encryption credential.

## Updating

```bash
cd /opt/mnemosyne && sudo -u mnemosyne git fetch --tags && sudo -u mnemosyne git checkout vX.Y.Z
sudo -u mnemosyne bash -c 'cd backend && uv sync --frozen --extra gpu --extra onnx'
sudo -u mnemosyne bash -c 'pnpm install --frozen-lockfile && pnpm build'
systemctl restart mnemosyne
```

Not while someone is recording: the status bar in the app shows recordings in progress.
