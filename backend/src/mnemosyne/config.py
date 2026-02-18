"""Application configuration loaded from environment variables."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the backend directory
_backend_dir = Path(__file__).resolve().parents[2]
load_dotenv(_backend_dir / ".env")

# HuggingFace
HF_TOKEN: str = os.environ.get("HF_TOKEN", "")

# WhisperX
WHISPER_MODEL_SIZE: str = os.environ.get("WHISPER_MODEL_SIZE", "large-v2")
WHISPER_COMPUTE_TYPE: str = os.environ.get("WHISPER_COMPUTE_TYPE", "float16")
WHISPER_BATCH_SIZE: int = int(os.environ.get("WHISPER_BATCH_SIZE", "16"))

# Data directory
DATA_DIR: Path = Path(__file__).resolve().parents[2].parent / "data"
