"""The web app, served by the backend itself (`web_dir`), for browsers with nothing installed:
a team opens the server's address (docs/team-server.md).

It is the same static SPA the desktop app bundles. Its index.html gets a marker telling the UI
that its backend is the server it was loaded from, instead of this computer's 127.0.0.1:8008.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

logger = logging.getLogger(__name__)

MARKER = '<meta name="mnemosyne-backend" content="same-origin" />'


def mount_web(app: FastAPI, web_dir: str) -> None:
    root = Path(web_dir).expanduser().resolve()
    index_file = root / "index.html"
    if not index_file.is_file():
        logger.error("web_dir %s has no index.html (run `pnpm build`); not serving the app", root)
        return
    cached: dict[str, object] = {"mtime": None, "html": ""}

    def index() -> str:
        """index.html with the marker, read again when it changes: a rebuilt web app (an update)
        names new asset files, and the old index would point at ones that are gone."""
        try:
            mtime = index_file.stat().st_mtime
        except OSError:
            return str(cached["html"])
        if mtime != cached["mtime"]:
            html = index_file.read_text(encoding="utf-8")
            cached["html"] = html.replace("<head>", "<head>\n\t\t" + MARKER, 1)
            cached["mtime"] = mtime
        return str(cached["html"])

    @app.get("/{path:path}", include_in_schema=False)
    async def web(path: str):
        if path.startswith("api/") or path == "ws":
            raise HTTPException(status_code=404)
        if path:
            f = (root / path).resolve()
            if f.is_file() and f.is_relative_to(root):
                # SvelteKit's hashed assets never change; everything else is revalidated.
                headers = {}
                if path.startswith("_app/immutable/"):
                    headers["Cache-Control"] = "public, max-age=31536000, immutable"
                return FileResponse(f, headers=headers)
        # The SPA routes itself: any other path is the app.
        return HTMLResponse(index(), headers={"Cache-Control": "no-cache"})
