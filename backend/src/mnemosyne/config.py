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

    Precedence:
    1. MNEMOSYNE_DATA_DIR environment variable (used by tests and custom setups).
    2. PyInstaller bundle: a sibling 'data' directory next to the app binary
       (the sidecar lives in .../resources/backend/mnemosyne-backend).
    3. Dev: the project root's data/ directory.
    """
    override = os.environ.get("MNEMOSYNE_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        # PyInstaller: resources/backend/mnemosyne-backend -> resources/data
        return Path(sys.executable).resolve().parent.parent / "data"
    else:
        return Path(__file__).resolve().parents[2].parent / "data"


DATA_DIR: Path = _get_data_dir()
