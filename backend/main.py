import argparse
import warnings

import uvicorn

# Suppress noisy torchcodec/pyannote FFmpeg warnings
warnings.filterwarnings("ignore", message=".*torchcodec.*")

from src.mnemosyne.api.app import create_app

app = create_app()


def main():
    parser = argparse.ArgumentParser(description="Mnemosyne Backend")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8008, help="Bind port")
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
