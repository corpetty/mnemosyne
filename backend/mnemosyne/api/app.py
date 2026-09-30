from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .. import access
from ..config import Settings, load_settings
from .app_watch import watch_app
from .auth import LockedMiddleware, TokenAuthMiddleware
from .context import AppContext
from .routes.ask import router as ask_router
from .routes.assets import router as assets_router
from .routes.audio import router as audio_router
from .routes.backup import router as backup_router
from .routes.bookmarks import router as bookmarks_router
from .routes.calendar import router as calendar_router
from .routes.clips import router as clips_router
from .routes.devices import router as devices_router
from .routes.digests import router as digests_router
from .routes.encryption import router as encryption_router
from .routes.export import router as export_router
from .routes.external_notes import router as external_notes_router
from .routes.glossary import router as glossary_router
from .routes.history import router as history_router
from .routes.integrations import router as integrations_router
from .routes.jobs import router as jobs_router
from .routes.mobile import router as mobile_router
from .routes.models import router as models_router
from .routes.pairing import router as pairing_router
from .routes.people import router as people_router
from .routes.record import router as record_router
from .routes.search import router as search_router
from .routes.segments import router as segments_router
from .routes.sessions import router as sessions_router
from .routes.settings import router as settings_router
from .routes.speakers import router as speakers_router
from .routes.storage import router as storage_router
from .routes.system import router as system_router
from .routes.tasks import router as tasks_router
from .routes.topics import router as topics_router
from .routes.users import router as users_router
from .websocket import router as ws_router


def create_app(settings: Settings | None = None, keystore=None) -> FastAPI:
    ctx = AppContext.build(settings or load_settings(), keystore)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await ctx.startup()
        watching = watch_app(ctx)
        yield
        if watching is not None:
            watching[1].cancel()
        await ctx.shutdown()

    app = FastAPI(title="Mnemosyne Backend", version="0.11.0", lifespan=lifespan)
    app.state.ctx = ctx
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(access.Forbidden)
    async def forbidden(_request, exc: access.Forbidden):
        status = 404 if isinstance(exc, access.Hidden) else 403
        return JSONResponse(status_code=status, content={"detail": str(exc)})

    app.add_middleware(LockedMiddleware, ctx=ctx)
    app.add_middleware(TokenAuthMiddleware, ctx=ctx)

    app.include_router(devices_router)
    app.include_router(audio_router)
    app.include_router(record_router)
    app.include_router(sessions_router)
    app.include_router(clips_router)
    app.include_router(encryption_router)
    app.include_router(models_router)
    app.include_router(export_router)
    app.include_router(settings_router)
    app.include_router(jobs_router)
    app.include_router(speakers_router)
    app.include_router(segments_router)
    app.include_router(bookmarks_router)
    app.include_router(assets_router)
    app.include_router(external_notes_router)
    app.include_router(glossary_router)
    app.include_router(search_router)
    app.include_router(ask_router)
    app.include_router(digests_router)
    app.include_router(tasks_router)
    app.include_router(people_router)
    app.include_router(topics_router)
    app.include_router(mobile_router)
    app.include_router(pairing_router)
    app.include_router(users_router)
    app.include_router(system_router)
    app.include_router(storage_router)
    app.include_router(history_router)
    app.include_router(backup_router)
    app.include_router(calendar_router)
    app.include_router(integrations_router)
    app.include_router(ws_router)

    @app.get("/health")
    async def health():
        import os
        import socket

        return {
            "status": "ok",
            "version": app.version,
            "host": socket.gethostname(),
            "auth_required": bool(ctx.settings.api_token) or ctx.settings.firm_mode,
            "firm_mode": ctx.settings.firm_mode,
            # Recording needs a word on how the people in it agreed (routes/audio.py).
            "consent_required": ctx.settings.require_consent or ctx.settings.firm_mode,
            # For the desktop shell, which finds this backend already running after a crash.
            "pid": os.getpid(),
            "recording": any(r.is_recording for r in ctx.active_recordings.values()),
        }

    if ctx.settings.web_dir:
        from .web import mount_web

        mount_web(app, ctx.settings.web_dir)  # last: every other route wins
    return app
