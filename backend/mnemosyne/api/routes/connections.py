"""What is connected to this computer, in one list (docs/plans/2026-11-09-connections-design.md).

Phones, computers, library-sync peers and other people's machines are stored apart (paired
devices, `sync_peers.json`, contacts); a sync peer is also a DESKTOP paired device. This merges
them so each shows once, with a short phrase for what it can do and the DELETE that removes it.
"""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends

from ... import access
from ...config import personal_allowed
from ...models.base import ApiModel
from ...services.contacts import my_or_card_name
from ...services.pairing import DESKTOP, PHONE
from ..auth import require_owner_channel
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api/connections", tags=["connections"])
ADMIN = [Depends(require_owner_channel), Depends(access.require_admin)]

CAN = {
    "phone": "Sends recordings",
    "computer": "Uses this computer",
    "sync": "Keeps its own library in sync",
    "contact": "Shared meetings",
}


class ConnectedDevice(ApiModel):
    id: str  # the paired device's, the sync peer's, or (contact) the person key
    kind: Literal["phone", "computer", "sync", "contact"]
    name: str
    created_at: datetime
    last_seen_at: datetime | None
    can: str  # one short phrase: what this device may do here
    remove: str  # DELETE this path to remove it


def _when(t: str | datetime | None) -> datetime | None:
    return datetime.fromisoformat(t) if isinstance(t, str) and t else t or None


def _later(*times: str | datetime | None) -> datetime | None:
    found = [t for t in map(_when, times) if t is not None]
    return max(found) if found else None


def _row(
    id_: str, kind: str, name: str, created: str, seen: str | datetime | None, remove: str
) -> ConnectedDevice:
    return ConnectedDevice(
        id=id_,
        kind=kind,
        name=name,
        created_at=datetime.fromisoformat(created),
        last_seen_at=_when(seen),
        can=CAN[kind],
        remove=remove,
    )


def connected_devices(ctx: AppContext) -> list[ConnectedDevice]:
    """Oldest first. Sync peers and contacts are left out on a team server, which refuses both."""
    team = not personal_allowed(ctx.settings)
    out: list[ConnectedDevice] = []
    peers = {} if team else {p.endpoint_id: p for p in ctx.sync_peers.peers()}
    sync_seen: dict[str, datetime | None] = {}  # peer id -> when its paired device was last used
    for d in ctx.pairing.devices():
        if d.kind == PHONE:
            out.append(_row(d.id, "phone", d.name, d.created_at, d.last_seen_at, _remove(d.id)))
        elif d.kind == DESKTOP and d.endpoint_id in peers:
            peer = peers[d.endpoint_id]  # also a sync peer: shown once, as that
            sync_seen[peer.id] = _later(sync_seen.get(peer.id), d.last_seen_at)
        elif d.kind == DESKTOP:
            out.append(_row(d.id, "computer", d.name, d.created_at, d.last_seen_at, _remove(d.id)))
    for peer in peers.values():
        seen = _later(sync_seen.get(peer.id), peer.last_sync_at)
        path = f"/api/sync/peers/{peer.id}"
        out.append(_row(peer.id, "sync", peer.name, peer.created_at, seen, path))
    if not team:
        for row in ctx.repo.contacts():
            machines = ctx.pairing.contacts_for(row["person"])
            seen = _later(*(m.last_seen_at for m in machines))
            path = f"/api/contacts/{row['person']}"
            out.append(
                _row(row["person"], "contact", my_or_card_name(row), row["created_at"], seen, path)
            )
    return sorted(out, key=lambda r: r.created_at)


def _remove(device_id: str) -> str:
    return f"/api/pairing/devices/{device_id}"


@router.get("/devices", response_model=list[ConnectedDevice], dependencies=ADMIN)
async def list_connected_devices(ctx: AppContext = Depends(get_ctx)):
    return connected_devices(ctx)
