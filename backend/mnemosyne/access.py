"""Who is asking, for a team server where each member sees their own meetings.

The auth middleware (api/auth.py) puts the signed-in user in `current` for the request; it
follows the request into threads, background tasks and the jobs it starts (they copy the
context when created). `None` means unrestricted: the desktop app, the admin token, and work
the backend starts on its own (retention, auto-record, the weekly digest).

Roles: a member sees and changes only their own meetings; a reviewer sees every
meeting but changes only their own; an admin does everything, including users and settings.
The session repository applies this (storage/sqlite.py), so every route and search gets it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

MEMBER, REVIEWER, ADMIN = "member", "reviewer", "admin"
ROLES = (MEMBER, REVIEWER, ADMIN)


@dataclass(frozen=True)
class Principal:
    user_id: str
    name: str
    role: str


current: ContextVar[Principal | None] = ContextVar("mnemosyne_principal", default=None)


internal: ContextVar[bool] = ContextVar("mnemosyne_internal", default=False)


@contextmanager
def internally() -> Iterator[None]:
    """Mark the block as the app's own writer (sync, vault sync, jobs), not a request acting for
    a person. A later task makes repository writes to meetings shared with you refuse anything
    that is not internal."""
    token = internal.set(True)
    try:
        yield
    finally:
        internal.reset(token)


def is_internal() -> bool:
    return internal.get()


class Forbidden(PermissionError):
    """The signed-in user may not do this (answered 403)."""


class Hidden(Forbidden):
    """Someone else's meeting the user may not even see (answered 404, as if it were not
    there, so ids of other people's meetings reveal nothing)."""


def principal() -> Principal | None:
    return current.get()


def user_id() -> str:
    """The signed-in user's id, "" when unrestricted."""
    p = current.get()
    return p.user_id if p else ""


def read_owner() -> str | None:
    """Only meetings of this owner are visible, or None for all."""
    p = current.get()
    return p.user_id if p is not None and p.role == MEMBER else None


def can_write(owner_id: str) -> bool:
    p = current.get()
    return p is None or p.role == ADMIN or owner_id == p.user_id


def can_edit_notes(owner_id: str, shared_with: set[str]) -> bool:
    """May they type into the meeting's live shared notes (services/shared_notes.py)? Its
    owner, an admin, and anyone it is shared with, alone or with everyone ("*"); other readers
    (a reviewer reading someone else's meeting) only read. Unrestricted (the desktop) edits."""
    p = current.get()
    if p is None or p.role == ADMIN or owner_id == p.user_id:
        return True
    return p.user_id in shared_with or "*" in shared_with


def is_admin() -> bool:
    p = current.get()
    return p is None or p.role == ADMIN


def require_admin() -> None:
    """FastAPI dependency (and plain check) for what only an admin may do."""
    if not is_admin():
        raise Forbidden("Only an admin can do this")
