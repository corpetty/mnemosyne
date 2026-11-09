"""The main computer this one uses (docs/plans/2026-11-09-connections-design.md, "Uses my main
computer").

The desktop shell connects to the main computer through remote access (src-tauri/src/remote.rs:
a tunnel on 127.0.0.1) and tells its own backend where that is with `PUT /api/system/main`, and
`DELETE` when it disconnects. The backend keeps it in <data_dir>/main_computer.json (mode 0600,
written atomically): the URL of the tunnel, the token the pairing returned (the same class of
secret as sync_peers.json's peer tokens: never returned by the API, not backed up) and the main
computer's name. Recordings made here go to it (the next task: streaming and the hand-off
outbox).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MainComputer:
    url: str  # the shell's tunnel to it, e.g. http://127.0.0.1:8009
    token: str  # the API token this computer uses there (a DESKTOP device's)
    name: str


def check_url(url: str) -> str:
    """The tunnel is always the shell's own loopback listener: http to 127.0.0.1 with a port.
    Anything else is refused (ValueError), so a local program cannot point this computer's
    recordings at another host."""
    url = url.strip().rstrip("/")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as e:
        raise ValueError("Not an address") from e
    if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost") or not port:
        raise ValueError("The main computer is reached through this computer's tunnel only")
    if parts.path or parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError("The address is the tunnel's origin only, e.g. http://127.0.0.1:8009")
    return url


class MainComputerStore:
    def __init__(self, path: Path):
        self.path = path
        self._main: MainComputer | None = None
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._main = MainComputer(
                url=str(raw["url"]), token=str(raw["token"]), name=str(raw.get("name") or "")
            )
        except FileNotFoundError:
            self._main = None
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("Could not read %s: no main computer", self.path, exc_info=True)
            self._main = None

    def get(self) -> MainComputer | None:
        return self._main

    def set(self, url: str, token: str, name: str) -> MainComputer:
        main = MainComputer(url=check_url(url), token=token.strip(), name=name.strip()[:80])
        if not main.token:
            raise ValueError("No token for the main computer")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(asdict(main), f, indent=2)
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)
        self._main = main
        return main

    def clear(self) -> bool:
        had = self._main is not None or self.path.exists()
        self.path.unlink(missing_ok=True)
        self._main = None
        return had
