"""Mnemosyne's own servers (docs/plans/2026-10-26-our-relays-and-host.md): one iroh relay and one
publish host that the app uses by default, so nothing depends on n0's testing relays.

Every place that needs a relay or a host asks here, so the `use_our_servers` setting (and what
the user configured) is read in one spot:

- `relays(settings)`: the `remote_relays` setting; else ours; else [] = NO relay (never n0's);
- `publish_host(settings)`: `publish_host`; else ours; else "" (a meeting can then only be
  published from this computer);
- `guest_host(settings)`: `guest_host`, else the publish host, so the same chain with ours last.

`use_our_servers` is on unless the settings say otherwise: a team server and the local-only
posture (`cloud_models` off) start with it off, and a value the person set always wins.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from ..config import resolve_use_our_servers

OUR_RELAYS = ["https://relay.petty.services"]
OUR_HOST = "https://notes.petty.services"
PRIVACY_URL = OUR_HOST + "/privacy"


def parse_relays(text: str) -> list[str]:
    """The `remote_relays` setting: comma or space separated URLs ("" = none configured)."""
    return [u for u in (text or "").replace(",", " ").split() if u]


def enabled(settings: Any) -> bool:
    """Whether Mnemosyne's servers are used for what is not configured: the setting, or its
    default (on, except on team servers and with cloud models off)."""
    return resolve_use_our_servers(
        getattr(settings, "use_our_servers", None),
        team_mode=bool(getattr(settings, "team_mode", False)),
        cloud_models=bool(getattr(settings, "cloud_models", True)),
    )


def relays(settings: Any) -> list[str]:
    """The iroh relays to use: those configured, else ours, else none (direct connections and
    the local network only; guest notes then use a tunnel)."""
    configured = parse_relays(getattr(settings, "remote_relays", "") or "")
    if configured:
        return configured
    return list(OUR_RELAYS) if enabled(settings) else []


def publish_host(settings: Any) -> str:
    """The publish host: the configured one, else ours, else "" (none). No trailing slash."""
    url = (getattr(settings, "publish_host", "") or "").strip().rstrip("/")
    if url:
        return url
    return OUR_HOST if enabled(settings) else ""


def guest_host(settings: Any) -> str:
    """The host guest notes rooms are made or mirrored on: `guest_host`, else `publish_host`,
    else ours; "" when there is none. No trailing slash."""
    url = (getattr(settings, "guest_host", "") or "").strip().rstrip("/")
    return url or publish_host(settings)


def relay_hostnames() -> list[str]:
    """The host names of our relays (for `relay_kind`)."""
    return [h for u in OUR_RELAYS if (h := urlsplit(u).hostname)]


def is_our_host(url: str) -> bool:
    """Whether a publish or guest host address is ours."""
    return url.strip().rstrip("/").lower() == OUR_HOST
