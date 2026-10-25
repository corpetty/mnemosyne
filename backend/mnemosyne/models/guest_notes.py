"""Guest notes rooms (phase 9) as the API shows them."""

from typing import Literal

from .base import ApiModel


class GuestRoom(ApiModel):
    # The full link with `#key`; filled only for the owner and admins, and only while the room
    # is live or ended (a stopped room's link is dead). Null for readers.
    url: str | None
    host_url: str
    state: Literal["live", "ended", "stopped"]
    created_at: str
    ended_at: str = ""
    expires_at: str = ""
    started_by: str = ""
    guests_present: list[str] = []
    # How guests reach the room (services/guest_notes.py) and where: the relay's host, what kind
    # it is, the public address of a tunnel, the mirror host, the guest page's origin.
    transport: Literal["iroh", "tunnel", "host"] = "host"
    relay: str = ""
    relay_kind: Literal["n0", "ours", "custom", "none"] = "none"
    public_url: str = ""
    mirror_host_url: str = ""
    page_origin: str = ""
