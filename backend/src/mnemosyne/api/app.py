from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..config import Settings, load_settings
from .auth import TokenAuthMiddleware
from .context import AppContext
from .routes.ask import router as ask_router
from .routes.audio import router as audio_router
from .routes.devices import router as devices_router
from .routes.export import router as export_router
from .routes.jobs import router as jobs_router
from .routes.models import router as models_router
from .routes.search import router as search_router
from .routes.segments import router as segments_router
from .routes.sessions import router as sessions_router
from .routes.settings import router as settings_router
from .routes.speakers import router as speakers_router
from .routes.storage import router as storage_router
from .websocket import router as ws_router


def create_app(settings: Settings | None = None) -> FastAPI:
    ctx = AppContext.build(settings or load_settings())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await ctx.startup()
        yield
        await ctx.shutdown()

    app = FastAPI(title="Mnemosyne Backend", version="0.3.0", lifespan=lifespan)
    app.state.ctx = ctx
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.add_middleware(TokenAuthMiddleware, ctx=ctx)

    app.include_router(devices_router)
    app.include_router(audio_router)
    app.include_router(sessions_router)
    app.include_router(models_router)
    app.include_router(export_router)
    app.include_router(settings_router)
    app.include_router(jobs_router)
    app.include_router(speakers_router)
    app.include_router(segments_router)
    app.include_router(search_router)
    app.include_router(ask_router)
    app.include_router(storage_router)
    app.include_router(ws_router)

    @app.get("/health")
    async def health():
        import socket

        return {
            "status": "ok",
            "version": app.version,
            "host": socket.gethostname(),
            "auth_required": bool(ctx.settings.api_token),
        }

    return app
