# The shared desktop, finished; a first run that is ready when you are

Follows docs/plans/2026-10-01-start-anywhere.md (released as 0.14.0). Two parts: what a computer
shared with a team still lacks, and making the first run's downloads visible, optional where they are
big, and unnecessary with the offline AppImage.

## 1. The shared desktop, finished

### 1a. No certificate warning on a tailnet
- When `tailscale status --json` shows HTTPS certificates are enabled (`CertDomains`), the sharing card
  offers "Use my Tailscale name (no browser warning)". It is a button, not automatic: the name is
  published in certificate-transparency logs when Let's Encrypt issues the certificate.
- `tailscale cert --cert-file --key-file <name>` writes `<data_dir>/tls/tailscale.{crt,key}`. It
  needs root or the Tailscale operator; when refused, the card shows
  `sudo tailscale set --operator=$USER`.
- The listener serves both certificates through SNI: the Tailscale name gets the real certificate,
  everything else (LAN addresses, `.local`) the self-signed one. The Tailscale address is listed
  first. Setting: `team_tailscale_cert`.
- Renewed when it has less than 30 days left (certificates last 90), checked daily.

### 1b. Laptops that move
- While sharing, the addresses are checked every 30 s. When they change, the self-signed certificate
  is made again and loaded into the running listener (no restart: the listener binds 0.0.0.0, and
  the TLS context takes the new certificate for new connections).

### 1c. Keep sharing after quitting (an option)
- Setting `keep_sharing_after_quit` (default off), in the sharing card: "Keep sharing when I quit
  Mnemosyne". When on and sharing is on, quitting the app leaves the backend running: the shell
  does not stop it (it asks `/health`, which says `outlives_app`), and `api/app_watch.py` does not
  shut it down when the app is gone. A desktop notification says it is still shared; opening the
  app again takes the backend over as today (`/api/system/attach`); Stop sharing there ends it.
- An AppImage's files disappear when it exits (its mount goes away), so the AppImage runs the
  backend, the web app and the link sidecar from a copy in `<app data>/runtime/<version>/` (a few
  MB, made once per version, older versions removed). This also makes today's "recording outlives
  a crashed app by 15 minutes" safe on the AppImage.
- It still ends at logout or shutdown; a machine that must serve unattended is docs/team-server.md.

### 1d. Close minimizes to the tray (an option)
- A shell setting `close_to_tray` (default off), kept by the Rust shell in
  `<app config>/shell.json` (it is about this window, not the backend, which may be remote).
  Settings → General shows it only in the desktop app and only when the tray works (GNOME needs the
  AppIndicator extension; the shell says whether it built the tray).
- When on, closing the window hides it; the tray's Show brings it back and Quit quits. A recording
  goes on while hidden (the tray says "recording" and offers Stop), instead of the quit dialog.

### 1e. Do not sleep in the middle of it
- While anything is recording on this backend (the desktop's own recording or a teammate's in their
  browser), the backend holds a logind sleep inhibitor (`systemd-inhibit --what=sleep --mode=block`,
  released when the last recording ends). Optional: also while sharing
  (`keep_awake_while_sharing`, default off, in the sharing card), for a laptop that serves a team
  all day.
- Best effort: without systemd-inhibit (the Flatpak) nothing is held, and the card says so.

## 2. A first run that is ready when you are

Measured on 2026-10-02: the base install (Python 3.13 + packages, no torch) is about 0.4 GB and
took 18 s. The weight is elsewhere:

| What | Size | When |
|---|---|---|
| Parakeet int8 | 0.67 GB | first transcription or live transcript |
| Search model (potion-retrieval-32M) | 0.13 GB | first index |
| Speaker models (onnx diarizer) | 45 MB | first transcription without NVIDIA |
| GPU extra (torch, WhisperX, NeMo) | about 7.5 GB | right after the first start, NVIDIA only |
| WhisperX large-v3 + Nemotron | about 3.2 GB | first transcription on NVIDIA |

### 2a. Downloads say so
- `ModelService.ensure_loaded` reports download progress while a model loads (bytes added to the
  Hugging Face cache and `models_dir`, against the expected size per engine). A transcription shows
  "Downloading the speech model: 230 of 670 MB" instead of a long "Loading models...".

### 2b. Setup gets the models ready
- The wizard's last step starts a `prepare_models` job (`POST /api/system/prepare`): the chosen
  transcriber and diarizer, and the search model. Its progress shows in the wizard and, once the
  wizard is closed, in the status bar like any job. The first recording and the sample then start
  without waiting.

### 2c. GPU support is a choice, with its size
- On an NVIDIA machine the wizard says what GPU support is (faster transcription, better speaker
  labels) and what it costs (about 7.5 GB, in the background, CPU engines work meanwhile), with
  "Install" (the default) and "Not now". Setting `gpu_support` = `auto` | `off`.
- The shell no longer starts the GPU phase before setup is answered: on the first run it waits for
  the wizard (Tauri command `start_gpu_install`); after that it installs when `gpu_support` is
  `auto` (upgrades as today). Settings → Transcription offers "Install GPU support" when it is off.

### 2d. The offline AppImage works with no network at all
- It also carries the CPU models (Parakeet int8, the speaker models, the search model; about
  0.9 GB more, about 1.1 GB in all, under GitHub's 2 GB asset limit), fetched by
  `mnemosyne-backend prefetch <dir>` in scripts/build-offline-appimage.sh. On first launch the shell
  copies them into the Hugging Face cache and `models_dir` when absent.
- The built-in summary model stays a download (2.5 GB or more).

### 2e. Docs
- README: what the first run downloads, and when.

## Order
1e, 1d, 1c (shell work together), 1b, 1a; then 2a, 2b, 2c, 2d. One commit per item. Tests: backend
pytest with fakes (inhibitor runner, tailscale runner, cert reload, app watch), `cargo check`, svelte
check, e2e. Browser checks on the demo backend (`share-demo` launch config).
