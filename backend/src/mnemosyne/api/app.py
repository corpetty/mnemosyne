from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routes.audio import router as audio_router
from .routes.devices import router as devices_router
from .routes.export import router as export_router
from .routes.models import router as models_router
from .routes.sessions import router as sessions_router
from .websocket import router as ws_router


def create_app() -> FastAPI:
    app = FastAPI(title="Mnemosyne Backend", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(devices_router)
    app.include_router(audio_router)
    app.include_router(sessions_router)
    app.include_router(models_router)
    app.include_router(export_router)
    app.include_router(ws_router)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app
