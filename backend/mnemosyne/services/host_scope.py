"""What leaves a computer that has a team owner (docs/plans/2026-11-09-team-hosting.md 2.4).

Let H be `team_owner_id`: the person whose computer this is (set while the desktop hosts a team
and kept after hosting is turned off, so members' meetings still in the database never start
flowing out). Without one (a plain main computer, a dedicated team server) nothing here limits
anything and the callers behave as before.

- The vault (Obsidian: auto-export, export-all, daily/people/topic/decision notes, Ask notes,
  the read-back of ticks and edits) gets the owner's meetings and those contacts shared with
  him (`access.PERSONAL`, then `shared_in_obsidian` as before), never a member's.
- The owner's Ask and digest see what he would as a member plus his own (`access.VIEW`): a
  member's meeting only when it was granted to him.
- Only the owner's digest is written to the vault. A dedicated server writes none (no one's
  vault is the server's), a member's digest on a host stays in the database.
- MCP requests (a header, `CLIENT_HEADER`) are held to the same view: an assistant is usually a
  cloud model, and the owner's tokenless local requests would otherwise see every member.

The limits are `access.limited_to` blocks: the repository applies them to every read inside.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from .. import access

if TYPE_CHECKING:
    from ..api.context import AppContext

# Sent by the MCP server (mcp_server.py): the request is an assistant's, not the owner's own.
CLIENT_HEADER = "x-mnemosyne-client"
CLIENT_MCP = "mcp"

NOT_IN_VAULT = "A team member's meeting is not written to this computer's vault"
OWNERS_VAULT = "Only the owner of this computer writes to its vault"


def owner(app: AppContext) -> str:
    """H, or "" on a computer without a team owner."""
    return getattr(app.settings, "team_owner_id", "") or ""


def vault(app: AppContext) -> access.ReadScope | None:
    """What may reach the vault, or None for no limit."""
    h = owner(app)
    return access.ReadScope(access.PERSONAL, h, h) if h else None


def view(app: AppContext) -> access.ReadScope | None:
    """What the owner works on (Ask, digest, MCP), or None for no limit."""
    h = owner(app)
    return access.ReadScope(access.VIEW, h, h) if h else None


def own(app: AppContext) -> access.ReadScope | None:
    """The owner's own meetings, or None for no limit."""
    h = owner(app)
    return access.ReadScope(access.OWN, h, h) if h else None


def is_owner(app: AppContext) -> bool:
    """Whether the current work is the owner's: nobody signed in (the desktop, the API token,
    the app's own jobs) or H himself. Without a team owner every caller is."""
    h = owner(app)
    p = access.principal()
    return not h or p is None or p.user_id == h


@contextmanager
def vault_only(app: AppContext) -> Iterator[None]:
    """Everything read inside is what may reach the vault, whoever asks."""
    with access.limited_to(vault(app)):
        yield


@contextmanager
def owner_view(app: AppContext) -> Iterator[None]:
    """Everything read inside is the owner's view when the owner is asking (his Ask, digest);
    anyone else keeps their own scope (a member's Ask is over what a member reads)."""
    h = owner(app)
    with access.limited_to(view(app), only_for=h):
        yield


def require_vault_owner(app: AppContext) -> None:
    """The vault of a computer with a team owner is the owner's: a member (or reviewer) asking
    for an export, a topic note or an Ask note gets a 403, not a write into it."""
    if not is_owner(app):
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail=OWNERS_VAULT)


def vault_allows(app: AppContext, meeting) -> bool:
    """May this meeting (anything with `owner_id` and `shared_from`) be written to the vault?
    The owner's and those shared with him (the `shared_in_obsidian` switch is the callers'),
    never a member's."""
    h = owner(app)
    if not h:
        return True
    return getattr(meeting, "owner_id", "") in ("", h) or bool(getattr(meeting, "shared_from", ""))


def digest_to_vault(app: AppContext) -> bool:
    """May the digest being made now be written to the vault? Only the owner's (a plain
    computer's, or H's on a computer with a team owner); a member's stays in the database and a
    dedicated team server has no owner whose vault it is."""
    if owner(app):
        return is_owner(app)
    return not getattr(app.settings, "team_mode", False)
