"""Devices paired with this backend in server mode (for now: phones using the /m page).

A device gets its own token by redeeming a one-time pairing code, shown as a QR code in
Settings, so the shared API token never leaves this computer and each device can be removed
on its own. A device token only opens the few paths a phone needs (see api/auth.py).

Tokens are kept as SHA-256 hashes in <data_dir>/paired_devices.json (mode 0600), outside the
database, so they can still be checked while encrypted meetings are locked.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

CODE_TTL_SECONDS = 600
# last_seen is written to disk at most this often per device; it is kept in memory in between.
SEEN_WRITE_INTERVAL = 300.0


class InvalidPairingCode(Exception):
    pass


@dataclass
class PairedDevice:
    id: str
    name: str
    token_hash: str
    created_at: str  # ISO 8601, UTC
    last_seen_at: str | None = None


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat()


class PairingService:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = path
        self.clock = clock
        self._codes: dict[str, float] = {}  # code -> expiry (clock time)
        self._devices: dict[str, PairedDevice] = {}  # by token hash
        self._seen_written: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        for d in raw.get("devices", []):
            device = PairedDevice(**d)
            self._devices[device.token_hash] = device

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        data = {"devices": [asdict(d) for d in self._devices.values()]}
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def new_code(self) -> tuple[str, float]:
        now = self.clock()
        self._codes = {c: exp for c, exp in self._codes.items() if exp > now}
        code = secrets.token_urlsafe(16)
        expires = now + CODE_TTL_SECONDS
        self._codes[code] = expires
        return code, expires

    def redeem(self, code: str, name: str) -> tuple[PairedDevice, str]:
        """Trade a pairing code (once, before it expires) for a device and its token."""
        expires = self._codes.pop(code, None)
        if expires is None or expires <= self.clock():
            raise InvalidPairingCode("This pairing code is invalid or has expired")
        token = secrets.token_urlsafe(32)
        device = PairedDevice(
            id=secrets.token_hex(8),
            name=(name.strip() or "Phone")[:80],
            token_hash=_hash(token),
            created_at=_iso(self.clock()),
        )
        self._devices[device.token_hash] = device
        self._save()
        return device, token

    def verify(self, token: str) -> PairedDevice | None:
        """The device a token belongs to, noting that it was seen."""
        device = self._devices.get(_hash(token)) if token else None
        if device is not None:
            now = self.clock()
            device.last_seen_at = _iso(now)
            if now - self._seen_written.get(device.id, 0.0) >= SEEN_WRITE_INTERVAL:
                self._seen_written[device.id] = now
                self._save()
        return device

    def devices(self) -> list[PairedDevice]:
        return sorted(self._devices.values(), key=lambda d: d.created_at)

    def get(self, device_id: str) -> PairedDevice | None:
        return next((d for d in self._devices.values() if d.id == device_id), None)

    def revoke(self, device_id: str) -> bool:
        device = self.get(device_id)
        if device is None:
            return False
        del self._devices[device.token_hash]
        self._save()
        return True
