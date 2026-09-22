# Mnemosyne

Linux desktop app for recording meetings (mic + system audio via PipeWire), transcribing
and diarizing them locally, summarizing with an LLM, and exporting to Obsidian.

Three parts, one repo:

- `src/` SvelteKit + Svelte 5 runes + Tailwind 4 frontend (static SPA, no SSR).
- `src-tauri/` Tauri v2 Rust shell. Only spawns/supervises the backend and owns the window.
- `backend/` Python 3.13 FastAPI service on `127.0.0.1:8008`. All real logic lives here.

Read `docs/architecture.md` before changing data flow. `docs/api-reference.md` is the HTTP/WS contract.

## Commands

```bash
pnpm tauri dev                 # full app: Vite + Tauri window + backend (spawned by Rust)
pnpm check                     # svelte-check, must stay at 0 errors
pnpm test:backend              # backend pytest (no GPU or network needed)
pnpm lint:backend              # ruff check + format --check
cd backend && uv run pytest    # same as test:backend
cd backend && uv run ruff format .   # auto-format before committing
```

Backend setup: `cd backend && uv sync --extra gpu --group dev`. The `gpu` extra pulls torch
(CUDA 12.8 index, pinned in pyproject) and WhisperX. `uv.lock` is committed; do not
`uv pip install` things by hand, add them to `pyproject.toml` and re-lock.

## Conventions

- Backend imports use `src.mnemosyne...` (the package is not installed; `main.py` and pytest
  add `backend/` to the path). Keep that consistent until the package layout is fixed.
- ML code is imported lazily inside functions so the API starts without torch. Keep it that way;
  tests rely on it (`tests/fakes.py` provides `FakeEngine` / `FakeProvider`).
- Engines and providers implement the Protocols in `transcription/engine.py` and
  `summarization/provider.py`. New implementations go behind those, not into routes.
- `MNEMOSYNE_DATA_DIR` overrides the data directory; tests set it to a temp dir in `conftest.py`.
- Frontend state is class-based rune stores in `src/lib/stores/*.svelte.ts`. Cross-store
  communication is via callbacks, not imports, to avoid cycles.
- Secrets and machine-specific config live in `backend/.env` (gitignored). Never commit it.

## Gotchas

- Wayland + WebKitGTK needs `WEBKIT_DISABLE_DMABUF_RENDERER=1` (already in `pnpm dev:app`).
- System Python is 3.14; the backend pins 3.13 via `backend/.python-version`. WhisperX
  does not support 3.14 yet.
- Diarization needs `HF_TOKEN` with the pyannote model licenses accepted.
- `src-tauri/binaries/`, `src-tauri/target/` and `backend/.venv/` are multi-GB build
  artifacts, all gitignored. Do not try to read or grep them.
- The WebSocket `transcribe` handler runs the whole pipeline inline; nothing else on that
  socket is processed until it finishes. Known limitation, see the roadmap.

## Roadmap (agreed 2026-09-22)

0. Hygiene: lockfile, GPU extra, ruff, pytest with fakes, this file.
1. Service refactor: job runner + event stream, SQLite sessions, persisted config, keep
   per-source audio separate, stop passing file paths from the UI.
2. Engine interface: WhisperX behind it, pyannote community-1, remote/OpenAI-compatible engine.
3. Live mode: chunked capture into a streaming engine with post-stop refinement.
4. Packaging: ship the app without torch (engine installed via uv on first run, or remote).
