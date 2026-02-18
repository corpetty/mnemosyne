"""Application configuration loaded from environment variables."""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the backend directory
if getattr(sys, "frozen", False):
    # PyInstaller bundle: .env lives next to the executable
    _backend_dir = Path(sys.executable).resolve().parent
else:
    _backend_dir = Path(__file__).resolve().parents[2]
load_dotenv(_backend_dir / ".env")

# HuggingFace
HF_TOKEN: str = os.environ.get("HF_TOKEN", "")

# WhisperX
WHISPER_MODEL_SIZE: str = os.environ.get("WHISPER_MODEL_SIZE", "medium.en")
WHISPER_COMPUTE_TYPE: str = os.environ.get("WHISPER_COMPUTE_TYPE", "float16")
WHISPER_BATCH_SIZE: int = int(os.environ.get("WHISPER_BATCH_SIZE", "8"))


def _get_data_dir() -> Path:
    """Resolve data directory.

    When bundled via PyInstaller, the sidecar binary lives inside the Tauri
    resource directory (e.g. .../resources/backend/mnemosyne-backend).
    Data goes in a sibling 'data' directory next to the app binary.

    When running in dev, data goes in the project root's data/ directory.
    """
    if getattr(sys, "frozen", False):
        # PyInstaller: resources/backend/mnemosyne-backend -> resources/data
        return Path(sys.executable).resolve().parent.parent / "data"
    else:
        return Path(__file__).resolve().parents[2].parent / "data"


DATA_DIR: Path = _get_data_dir()
