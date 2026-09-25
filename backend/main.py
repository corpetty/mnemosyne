"""Shim so `uvicorn main:app` (dev) and `python main.py` (release shell) keep working."""

from mnemosyne.cli import main, quiet_known_warnings

quiet_known_warnings()

if __name__ == "__main__":
    main()
else:  # uvicorn main:app
    from mnemosyne.api.app import create_app
    from mnemosyne.config import load_settings
    from mnemosyne.logs import setup_logging

    settings = load_settings()
    setup_logging(settings.data_dir)
    app = create_app(settings)
