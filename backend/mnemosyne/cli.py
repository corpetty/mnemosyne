"""Command-line entry point: `mnemosyne-backend --host 127.0.0.1 --port 8008`."""

import argparse
import warnings


def main() -> None:
    # Silence noisy torchcodec/pyannote FFmpeg warnings on import.
    warnings.filterwarnings("ignore", message=".*torchcodec.*")
    import uvicorn

    from .api.app import create_app

    parser = argparse.ArgumentParser(description="Mnemosyne backend")
    parser.add_argument(
        "--host", default="127.0.0.1", help="Bind host (0.0.0.0 for server mode; set API_TOKEN)"
    )
    parser.add_argument("--port", type=int, default=8008, help="Bind port")
    args = parser.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
