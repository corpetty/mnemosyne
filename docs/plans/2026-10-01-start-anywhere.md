# Start anywhere: any laptop, no extra installs, a team from one machine

Corey, 2026-10-01: "plan 1, 2 and 3, then build them" — getting people started immediately without
extra infrastructure when their machines can manage it. Today a laptop without an NVIDIA card fails
its first transcription, summaries need Ollama installed and a model pulled by hand, and a team needs
a systemd/TPM/HTTPS server set up from a shell.

Checks before every commit: in `backend/` `uv run ruff check . && uv run ruff format --check . &&
uv run pytest` (with ML packages hidden for new imports); `bash scripts/gen-api-types.sh --check`;
`pnpm check`; `pnpm test:e2e`. One commit per item (an item may take two if it splits cleanly).
Downloads (models, llama.cpp) are pinned by version and checked by SHA-256.

## 1. The first run works on any machine

- **Defaults that fit the machine.** `transcriber = "auto"` (new default): WhisperX when the GPU stack
  is installed and CUDA works, else Parakeet. `diarizer = "auto"`: Nemotron, else pyannote when torch
  is installed, else the new ONNX diarizer, else none. Saved explicit values are kept, but a saved
  engine this machine cannot run falls back to auto with a note in /api/system (no failed first
  transcription). The wizard pre-selects only options that work here.
- **Speakers told apart without a GPU.** A new `onnx` diarizer behind the Diarizer protocol:
  sherpa-onnx (Apache-2.0, ~14 MB of wheels, in the `onnx` extra) with pyannote segmentation-3.0 as
  ONNX (7 MB) and a speaker-embedding model (~30 MB), downloaded on first use from k2-fsa's releases
  (no Hugging Face token, no torch). Measured on AMI ES2004a against pyannote/Nemotron with per-word
  labels before choosing the embedding model.
- **A sample meeting.** A short clip (AMI, CC BY 4.0, credited) ships with the app; the wizard's last
  step offers "Try it on a sample" and opens the result: transcript, speakers, summary.

## 2. Summaries with nothing else installed

- **A built-in model.** Provider `local`: the backend downloads llama.cpp's `llama-server` (the
  Vulkan build when the machine has Vulkan, else the CPU build; ~17-31 MB, pinned release and
  SHA-256) and a GGUF model into the data folder, runs it on 127.0.0.1 on a free port while needed,
  and talks to it over its OpenAI-compatible API. Models: Qwen3-4B-Instruct-2507 Q4_K_M (2.5 GB,
  default) or Qwen3-30B-A3B-Instruct-2507 Q4_K_M (18.6 GB, offered with 32 GB of RAM or more), both
  Apache-2.0, pinned by revision and SHA-256. The server stops when idle, like the speech models.
- **The wizard sets it up.** The Summaries step finds what is there: Ollama with a model (use it),
  Ollama without one (offer to pull one sized to the machine, with progress), or nothing (offer the
  built-in model, with progress). A `model_download` job reports progress over the WebSocket.
- New installs default to `local` only once it is downloaded; until then the wizard's choice stands.

## 3. A team from one desktop install

- **Settings → General → "Share this computer with my team".** The backend opens a second listener on
  the local network (`team_port`, default 8443) with HTTPS from a certificate it makes for this
  machine's addresses (browsers ask once to accept it; recording in the browser needs HTTPS), and
  serves the web app there. The desktop app keeps using 127.0.0.1:8008.
- **You are the admin, nothing from a shell.** Turning it on creates an admin for you (the name on
  your microphone), switches on team mode, and gives your existing meetings to you. The desktop app's
  own requests on 127.0.0.1:8008 run as you (`team_owner_id`); people on the network sign in with
  invite links. Turning it off stops the listener and team mode; people and meetings stay.
- **Invite people from the app.** The People settings show each invite as a link to the network
  address and a QR code. The Team section shows the address, whether the port is reachable, and what
  to do when a firewall blocks it.
- **The web app ships with the desktop app.** The built web app is bundled as a resource (`web/`),
  and the Rust shell tells the backend where it is (`WEB_DIR`).
- docs/team-server.md gains a short "From your desktop" section; the full server setup stays for
  machines that must run unattended.
