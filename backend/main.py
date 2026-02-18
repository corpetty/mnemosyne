import warnings

import uvicorn

# Suppress noisy torchcodec/pyannote FFmpeg warnings
warnings.filterwarnings("ignore", message=".*torchcodec.*")

from src.mnemosyne.api.app import create_app

app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8008)
