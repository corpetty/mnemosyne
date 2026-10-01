"""The people who use a team server (access.py): advisors, reviewers and admins.

Nobody has a password. An admin adds a person and sends them an invite link; opening it once
(within INVITE_TTL) gives that browser its own token. Someone on a second computer gets a second
invite; "sign out everywhere" drops all their tokens, and a disabled person's tokens stop working
at once.

Kept in <data_dir>/users.json (mode 0600) with tokens and invites as SHA-256 hashes, outside the
database like paired devices (services/pairing.py), so sign-in works while encrypted meetings
are locked. The file is re-read when it changes, so `mnemosyne-backend users ...` (cli.py) can
add the first admin while the server runs.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..access import ROLES

INVITE_TTL = 7 * 24 * 3600
SEEN_WRITE_INTERVAL = 300.0


class InvalidInvite(Exception):
    pass


@dataclass
class UserToken:
    hash: str
    device: str
    created_at: str
    last_seen_at: str | None = None


@dataclass
class User:
    id: str
    name: str
    email: str
    role: str
    created_at: str
    disabled: bool = False
    tokens: list[UserToken] = field(default_factory=list)

    @property
    def last_seen_at(self) -> str | None:
        seen = [t.last_seen_at for t in self.tokens if t.last_seen_at]
        return max(seen) if seen else None


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat()


class UserService:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = path
        self.clock = clock
        self._lock = threading.RLock()
        self._users: dict[str, User] = {}
        self._invites: dict[str, tuple[float, str]] = {}  # hash -> (expiry, user id)
        self._mtime = 0.0
        self._seen_written: dict[str, float] = {}
        self._load()

    # ---- storage ---------------------------------------------------------

    def _load(self) -> None:
        try:
            self._mtime = self.path.stat().st_mtime
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        users = {}
        for u in raw.get("users", []):
            tokens = [UserToken(**t) for t in u.pop("tokens", [])]
            users[u["id"]] = User(**u, tokens=tokens)
        self._users = users
        self._invites = {h: (exp, uid) for h, exp, uid in raw.get("invites", [])}

    def _fresh(self) -> None:
        """Pick up changes another process (the CLI) wrote."""
        try:
            if self.path.stat().st_mtime != self._mtime:
                self._load()
        except FileNotFoundError:
            pass

    def _save(self) -> None:
        now = self.clock()
        self._invites = {h: v for h, v in self._invites.items() if v[0] > now}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        data = {
            "users": [asdict(u) for u in self._users.values()],
            "invites": [[h, exp, uid] for h, (exp, uid) in self._invites.items()],
        }
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)
        self._mtime = self.path.stat().st_mtime

    # ---- people ----------------------------------------------------------

    def any(self) -> bool:
        with self._lock:
            self._fresh()
            return bool(self._users)

    def list(self) -> list[User]:
        with self._lock:
            self._fresh()
            return sorted(self._users.values(), key=lambda u: u.created_at)

    def get(self, user_id: str) -> User | None:
        with self._lock:
            self._fresh()
            return self._users.get(user_id)

    def add(self, name: str, email: str, role: str) -> User:
        if role not in ROLES:
            raise ValueError(f"Role must be one of {', '.join(ROLES)}")
        name, email = name.strip()[:80], email.strip().lower()[:120]
        if not name:
            raise ValueError("A name is needed")
        with self._lock:
            self._fresh()
            if email and any(u.email == email for u in self._users.values()):
                raise ValueError(f"{email} already has an account")
            user = User(
                id=secrets.token_hex(6),
                name=name,
                email=email,
                role=role,
                created_at=_iso(self.clock()),
            )
            self._users[user.id] = user
            self._save()
            return user

    def update(self, user_id: str, **changes) -> User:
        with self._lock:
            self._fresh()
            user = self._users.get(user_id)
            if user is None:
                raise KeyError(user_id)
            if "role" in changes and changes["role"] not in ROLES:
                raise ValueError(f"Role must be one of {', '.join(ROLES)}")
            for key in ("name", "email", "role", "disabled"):
                if changes.get(key) is not None:
                    setattr(user, key, changes[key])
            if not self._admins_left():
                self._load()  # undo: never leave a server nobody can administer
                raise ValueError("At least one admin must stay enabled")
            self._save()
            return user

    def _admins_left(self) -> bool:
        return any(u.role == "admin" and not u.disabled for u in self._users.values())

    def sign_out(self, user_id: str) -> None:
        with self._lock:
            self._fresh()
            user = self._users.get(user_id)
            if user is not None:
                user.tokens = []
                self._save()

    # ---- invites and tokens ------------------------------------------------

    def invite(self, user_id: str) -> tuple[str, float]:
        """A one-time code for this person's next browser, valid INVITE_TTL."""
        with self._lock:
            self._fresh()
            if user_id not in self._users:
                raise KeyError(user_id)
            code = secrets.token_urlsafe(18)
            expires = self.clock() + INVITE_TTL
            self._invites[_hash(code)] = (expires, user_id)
            self._save()
            return code, expires

    def redeem(self, code: str, device: str) -> tuple[User, str]:
        with self._lock:
            self._fresh()
            expires, user_id = self._invites.pop(_hash(code), (0.0, ""))
            user = self._users.get(user_id)
            if expires <= self.clock() or user is None or user.disabled:
                self._save()
                raise InvalidInvite(
                    "This invite link is invalid, used or expired: ask for a new one"
                )
            token = secrets.token_urlsafe(32)
            user.tokens.append(
                UserToken(
                    hash=_hash(token),
                    device=device.strip()[:80] or "Browser",
                    created_at=_iso(self.clock()),
                )
            )
            self._save()
            return user, token

    def verify(self, token: str) -> User | None:
        """The enabled user a token belongs to, noting that it was seen."""
        if not token:
            return None
        h = _hash(token)
        with self._lock:
            self._fresh()
            for user in self._users.values():
                for t in user.tokens:
                    if t.hash == h:
                        if user.disabled:
                            return None
                        now = self.clock()
                        t.last_seen_at = _iso(now)
                        if now - self._seen_written.get(h, 0.0) >= SEEN_WRITE_INTERVAL:
                            self._seen_written[h] = now
                            self._save()
                        return user
        return None

    def forget_token(self, token: str) -> None:
        """Sign this browser out."""
        h = _hash(token)
        with self._lock:
            self._fresh()
            for user in self._users.values():
                if any(t.hash == h for t in user.tokens):
                    user.tokens = [t for t in user.tokens if t.hash != h]
                    self._save()
                    return
