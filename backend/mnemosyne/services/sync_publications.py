"""Publications and library sync (docs/plans/2026-10-14-library-sync-rules.md, "Publications").

The rows travel in `SyncSession.publications` and are merged by `models.sync_session.
merge_publications`; this module is what a machine does with its own files once the rows have
changed under it. The files in `<data_dir>/publications/<id>/` are this machine's copy of what
it publishes or has published; a "this computer" publication is served from the machine that
made it only, a hosted one lives on the publish host and any machine may hold a copy.
"""

from __future__ import annotations

import logging
import shutil
from typing import TYPE_CHECKING

from . import publishing

if TYPE_CHECKING:
    from ..api.context import AppContext

logger = logging.getLogger(__name__)


def served_elsewhere(row: dict) -> bool:
    """A "this computer" publication whose files are on another of the person's computers."""
    return row["place"] == "local" and bool(row.get("origin"))


def settle(ctx: AppContext, session_id: str, before: list[dict]) -> list[dict]:
    """After a meeting's publications were written by sync (`before`: the rows we had), make
    this machine's files agree with the rows:

    - a publication that is now unpublished loses its files here (a tombstone stays, so a
      reader of this machine gets 410);
    - one served by another computer, or hosted and now at a version we have no files for,
      loses its stale files (an update from here fetches what it needs from the host);
    - a publication that lost to another one published for the same meeting (two machines
      published separately) is returned when it is hosted, to be deleted from the host.
    """
    repo = ctx.repo
    after = repo.session_publications(session_id)
    was = {r["id"]: r for r in before}
    another_live = any(not r["unpublished_at"] for r in after)
    lost: list[dict] = []
    for row in after:
        folder = publishing.pub_dir(ctx, row["id"])
        meta = publishing.read_meta(ctx, row["id"]) if folder.exists() else None
        if row["unpublished_at"]:
            if folder.exists() and not (meta or {}).get("gone"):
                publishing.bury(ctx, row["id"], "unpublished", session_id)
            prev = was.get(row["id"])
            # Newly dead here (it was live, or is new to us) next to a live one: it may have
            # lost a merge, with its hosted copy still up. Deleting a copy that is gone is fine.
            if (prev is None or not prev["unpublished_at"]) and another_live:
                if row["place"] == "host" and row["host_url"]:
                    lost.append(row)
            continue
        if not folder.exists():
            continue
        stale = (
            meta is None
            or meta.get("gone")
            or served_elsewhere(row)
            or (row["place"] == "host" and meta.get("version") != row["version"])
        )
        if stale:
            shutil.rmtree(folder, ignore_errors=True)
    return lost


async def delete_hosted(ctx: AppContext, row: dict) -> None:
    await publishing.delete_quietly(ctx, row)
