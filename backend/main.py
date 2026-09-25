"""Shim so `uvicorn main:app` (dev) and `python main.py` (release shell) keep working."""

from mnemosyne.cli import main, quiet_known_warnings

quiet_known_warnings()

from mnemosyne.api.app import create_app  # noqa: E402

app = create_app()

if __name__ == "__main__":
    main()
