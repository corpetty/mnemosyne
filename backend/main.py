"""Shim so `uvicorn main:app` (dev) and `python main.py` (release shell) keep working."""

import warnings

warnings.filterwarnings("ignore", message=".*torchcodec.*")

from mnemosyne.api.app import create_app  # noqa: E402
from mnemosyne.cli import main  # noqa: E402

app = create_app()

if __name__ == "__main__":
    main()
