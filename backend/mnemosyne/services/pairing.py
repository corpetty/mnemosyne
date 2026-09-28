"""Devices paired with this backend: phones using the /m page, and computers reaching it
through remote access (services/link.py).

A device gets its own token by redeeming a one-time pairing code shown in Settings, so the
shared API token never leaves this computer and each device can be removed on its own. A
phone's token only opens the few paths the phone page needs (see api/auth.py). A computer
pairs through the link sidecar, which vouches for its iroh endpoint id; its token opens the
whole API, and the sidecar only lets that endpoint in while the device is listed.

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


PHONE, DESKTOP = "phone", "desktop"


class InvalidPairingCode(Exception):
    pass


@dataclass
class PairedDevice:
    id: str
    name: str
    token_hash: str
    created_at: str  # ISO 8601, UTC
    last_seen_at: str | None = None
    kind: str = PHONE  # PHONE: the phone page's paths only; DESKTOP: the whole API
    endpoint_id: str | None = None  # a desktop device's iroh endpoint (services/link.py)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat()


class PairingService:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = path
        self.clock = clock
        self._codes: dict[str, tuple[float, str]] = {}  # code -> (expiry, device kind)
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

    def new_code(self, kind: str = PHONE) -> tuple[str, float]:
        now = self.clock()
        self._codes = {c: v for c, v in self._codes.items() if v[0] > now}
        code = secrets.token_urlsafe(16)
        expires = now + CODE_TTL_SECONDS
        self._codes[code] = (expires, kind)
        return code, expires

    def redeem(
        self, code: str, name: str, endpoint_id: str | None = None
    ) -> tuple[PairedDevice, str]:
        """Trade a pairing code (once, before it expires) for a device and its token. A
        desktop code needs the endpoint id the link sidecar vouches for; a phone code none."""
        expires, kind = self._codes.get(code, (0.0, PHONE))
        if expires <= self.clock():
            self._codes.pop(code, None)
            raise InvalidPairingCode("This pairing code is invalid or has expired")
        if (kind == DESKTOP) != bool(endpoint_id):
            raise InvalidPairingCode(
                "This code pairs a computer through remote access"
                if kind == DESKTOP
                else "This code pairs a phone: open it on the phone"
            )
        del self._codes[code]
        token = secrets.token_urlsafe(32)
        device = PairedDevice(
            id=secrets.token_hex(8),
            name=(name.strip() or ("Computer" if kind == DESKTOP else "Phone"))[:80],
            token_hash=_hash(token),
            created_at=_iso(self.clock()),
            kind=kind,
            endpoint_id=endpoint_id if kind == DESKTOP else None,
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
