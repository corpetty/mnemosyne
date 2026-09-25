"""Backend logging: the console, as before, plus a rotating file in the data dir.

The Tauri shell inherits the backend's stdout/stderr, so an app started from the desktop kept no
backend log at all. `<data_dir>/logs/backend.log` is what Settings → Copy diagnostics includes.
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FILE = "backend.log"
FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# Libraries that log every request or file lock at INFO.
_NOISY = ("httpx", "httpcore", "urllib3", "huggingface_hub", "filelock", "multipart", "asyncio")


def log_path(data_dir: Path) -> Path:
    return Path(data_dir) / "logs" / LOG_FILE


def setup_logging(data_dir: Path, level: int = logging.INFO) -> Path | None:
    """Log INFO and up to the console and to `<data_dir>/logs/backend.log` (2 x 2 MB).
    Safe to call twice. Returns the log file, or None when it cannot be written."""
    root = logging.getLogger()
    formatter = logging.Formatter(FORMAT)
    if not any(getattr(h, "_mnemosyne", False) for h in root.handlers):
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        console._mnemosyne = True  # type: ignore[attr-defined]
        root.addHandler(console)
    root.setLevel(level)
    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.captureWarnings(True)  # warnings.warn() goes to the log too (filters still apply)

    path = log_path(data_dir)
    for h in root.handlers:
        if isinstance(h, RotatingFileHandler) and Path(h.baseFilename) == path:
            return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        file = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    except OSError:
        logging.getLogger(__name__).warning("Cannot write the log file %s", path, exc_info=True)
        return None
    file.setFormatter(formatter)
    root.addHandler(file)
    # uvicorn's own loggers do not propagate; keep its startup messages and errors too.
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).addHandler(file)
    return path


def tail(path: Path, lines: int = 200) -> list[str]:
    """The last `lines` lines of a text file (empty if it does not exist)."""
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 256_000))
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return data.splitlines()[-lines:]
