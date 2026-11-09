"""Who is asking, for a team server where each member sees their own meetings.

The auth middleware (api/auth.py) puts the signed-in user in `current` for the request; it
follows the request into threads, background tasks and the jobs it starts (they copy the
context when created). `None` means unrestricted: the desktop app, the admin token, and work
the backend starts on its own (retention, auto-record, the weekly digest).

Roles: a member sees and changes only their own meetings; a reviewer sees every
meeting but changes only their own; an admin does everything, including users and settings.
The session repository applies this (storage/sqlite.py, `read_scope`), so every route and search
gets it. On a main computer that hosts a team (config.hosts_team) a reviewer sees every TEAM
meeting but only the owner's personal ones granted to them (docs/plans/2026-11-09-team-hosting.md
2.3), and a contact (phase 8, "p:<16 hex>") reads only what was shared with them in person, never
a team grant to everyone.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace

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


CONTACT_PREFIX = "p:"  # a contact's principal id (services/contacts.py `pid_of`)

# What a caller may read (ReadScope.kind):
ALL = "all"  # everything: unrestricted, an admin, a reviewer off a hosting computer
OWN_AND_GRANTED = "member"  # a member: own meetings + team grants to them or to everyone ("*")
TEAM_WIDE = "team_wide"  # a reviewer on a hosting computer: team meetings + grants to them
PERSONAL_SHARES = "contact"  # a contact: only their own live peer shares
# Limits for the work that copies or processes data on a computer with a team owner H
# (`team_owner_id`; docs/plans/2026-11-09-team-hosting.md 2.4). They are asked for by
# services/host_scope.py, never by who the caller is:
OWN = "own"  # the owner's own meetings: owner '' or H, not shared with H by a contact
PERSONAL = "personal"  # OWN plus the meetings contacts shared with H: what may reach the vault
VIEW = "view"  # OWN, team meetings granted to H, shared_from: what H would see as a member


@dataclass(frozen=True)
class ReadScope:
    """Which meetings the caller may read; the repository turns it into SQL
    (storage/sqlite.py `_sees`). `host` is the owner of a computer that hosts a team."""

    kind: str
    user_id: str = ""
    host: str = ""
    # Further scopes the caller must be inside of too (`within`): a limit on top of who they are.
    also: tuple[ReadScope, ...] = ()

    @property
    def everything(self) -> bool:
        return self.kind == ALL and not self.also

    def within(self, limit: ReadScope) -> ReadScope:
        """This scope cut down to what `limit` allows as well (the intersection)."""
        if limit.everything or limit == self or limit in self.also:
            return self
        if self.everything:
            return limit
        return replace(self, also=(*self.also, limit))


EVERYTHING = ReadScope(ALL)


@dataclass(frozen=True)
class Limit:
    """A scope the current block of work is held to, on top of who the caller is. `only_for`:
    None for everyone, else only for the owner (a caller with that user id, or nobody signed in:
    the desktop, the API token), so a member's own meetings are never cut by the owner's limit."""

    scope: ReadScope
    only_for: str | None = None


limits: ContextVar[tuple[Limit, ...]] = ContextVar("mnemosyne_limits", default=())


@contextmanager
def limited_to(scope: ReadScope | None, only_for: str | None = None) -> Iterator[None]:
    """Hold the block (and the threads and jobs it starts) to `scope` as well as to the caller's
    own: a vault write sees no team member's meeting, an owner's Ask only what he could read as
    a member. `scope` None changes nothing (a computer without a team owner)."""
    if scope is None:
        yield
        return
    token = limits.set((*limits.get(), Limit(scope, only_for)))
    try:
        yield
    finally:
        limits.reset(token)


def read_scope(host_owner: str = "") -> ReadScope:
    """The current caller's read scope, held to the limits in force (`limited_to`).

    `host_owner`: the owner of this computer while it hosts
    a team (`SessionRepository.host_owner`), else "". On a host, every non-member who is not the
    owner (a reviewer; an admin other than the owner, which rule I2 forbids anyway) reads
    team-wide: team meetings and the owner's personal ones granted to them, never a contact's."""
    scope = _callers_scope(host_owner)
    p = current.get()
    for limit in limits.get():
        if limit.only_for is None or p is None or p.user_id == limit.only_for:
            scope = scope.within(limit.scope)
    return scope


def _callers_scope(host_owner: str) -> ReadScope:
    p = current.get()
    if p is None:
        return EVERYTHING
    if p.user_id.startswith(CONTACT_PREFIX):
        return ReadScope(PERSONAL_SHARES, p.user_id)
    if p.role == MEMBER:
        return ReadScope(OWN_AND_GRANTED, p.user_id)
    if host_owner and p.user_id != host_owner:
        return ReadScope(TEAM_WIDE, p.user_id, host_owner)
    return EVERYTHING


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
    if p.user_id.startswith(CONTACT_PREFIX):  # a team grant to everyone is not for contacts
        return p.user_id in shared_with
    return p.user_id in shared_with or "*" in shared_with


def is_admin() -> bool:
    p = current.get()
    return p is None or p.role == ADMIN


def require_admin() -> None:
    """FastAPI dependency (and plain check) for what only an admin may do."""
    if not is_admin():
        raise Forbidden("Only an admin can do this")
