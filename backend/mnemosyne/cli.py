"""Command-line entry point: `mnemosyne-backend --host 127.0.0.1 --port 8008`."""

import argparse
import os
import warnings


def quiet_known_warnings() -> None:
    """Silence warnings that are expected and harmless here.

    pyannote warns on import that torchcodec (its own audio decoder) cannot load, e.g. on
    Fedora 44 whose FFmpeg 8 torchcodec 0.7 does not support. We never use that decoder:
    audio reaches pyannote as in-memory waveforms. The message starts with a newline, so
    the pattern must allow leading whitespace ('.*' does not cross a newline).
    """
    warnings.filterwarnings("ignore", message=r"\s*torchcodec is not installed correctly")
    # pyannote turns TF32 off for reproducibility and says so on every run.
    warnings.filterwarnings("ignore", message=r"\s*TensorFloat-32 \(TF32\) has been disabled")
    # pyannote's statistics pooling on a one-frame chunk (very short speech turns).
    warnings.filterwarnings("ignore", message=r"\s*std\(\): degrees of freedom is <= 0")


def main() -> None:
    quiet_known_warnings()
    import uvicorn

    from .api.app import create_app
    from .config import load_settings
    from .logs import setup_logging

    parser = argparse.ArgumentParser(description="Mnemosyne backend")
    parser.add_argument(
        "--host", default="127.0.0.1", help="Bind host (0.0.0.0 for server mode; set API_TOKEN)"
    )
    parser.add_argument("--port", type=int, default=8008, help="Bind port")
    args = parser.parse_args()
    # The phone page (routes/mobile.py) tells whether other devices can reach us.
    os.environ["MNEMOSYNE_BIND_HOST"] = args.host
    os.environ["MNEMOSYNE_BIND_PORT"] = str(args.port)
    settings = load_settings()
    setup_logging(settings.data_dir)
    uvicorn.run(create_app(settings), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
