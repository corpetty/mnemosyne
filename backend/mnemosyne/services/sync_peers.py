"""The computers this one syncs its library with (docs/plans/2026-10-12-hashseq-collaboration.md,
Phase 3), and pairing with them.

<data_dir>/sync_peers.json (mode 0600, written atomically) holds this machine's sync identity
and one record per peer, including the API token this machine uses on that peer: the same class
of secret as link.key, so it is never returned by the API and not backed up. A peer's `id` is
the id the other machine calls itself. Each peer is also a DESKTOP device in services/pairing.py
(the sidecar lets only listed endpoints in) and is dialed through the link sidecar
(services/link.py), which serves it on a loopback address kept as `listen`.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import time
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .link import LinkError

if TYPE_CHECKING:
    from ..api.context import AppContext
    from .link import LinkService

logger = logging.getLogger(__name__)


class SyncPairingError(Exception):
    """Pairing for sync was refused; the message says why, for the person."""


@dataclass
class SelfInfo:
    id: str
    name: str


@dataclass
class SyncPeer:
    id: str
    name: str
    endpoint_id: str
    ticket: str
    token: str  # the API token for this peer's backend
    created_at: str  # ISO 8601, UTC
    last_sync_at: str | None = None
    since: int = 0  # the peer's change counter this machine has pulled up to
    listen: str | None = None  # the sidecar's loopback address for it, when dialed


def now_iso() -> str:
    return datetime.fromtimestamp(time.time(), UTC).isoformat()


class SyncPeerStore:
    def __init__(self, path: Path):
        self.path = path
        self._self: SelfInfo
        self._peers: dict[str, SyncPeer] = {}
        self._load()

    def _load(self) -> None:
        raw: dict = {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        me = raw.get("self") or {}
        self._self = SelfInfo(
            id=me.get("id") or secrets.token_hex(6),
            name=me.get("name") or socket.gethostname(),
        )
        known = {f.name for f in fields(SyncPeer)}
        for p in raw.get("peers", []):
            peer = SyncPeer(**{k: v for k, v in p.items() if k in known})
            self._peers[peer.id] = peer
        if not me.get("id"):
            self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        data = {"self": asdict(self._self), "peers": [asdict(p) for p in self._peers.values()]}
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def self_info(self) -> SelfInfo:
        return SelfInfo(self._self.id, self._self.name)

    def set_name(self, name: str) -> None:
        name = name.strip()[:80]
        if name and name != self._self.name:
            self._self.name = name
            self.save()

    def peers(self) -> list[SyncPeer]:
        return sorted(self._peers.values(), key=lambda p: p.created_at)

    def get(self, peer_id: str) -> SyncPeer | None:
        return self._peers.get(peer_id)

    def find_by_endpoint(self, endpoint_id: str) -> SyncPeer | None:
        return next((p for p in self._peers.values() if p.endpoint_id == endpoint_id), None)

    def add(self, peer: SyncPeer) -> SyncPeer:
        """Record a peer, replacing one with the same id or the same endpoint (paired again)."""
        for old in [p for p in self._peers.values() if p.endpoint_id == peer.endpoint_id]:
            del self._peers[old.id]
        self._peers[peer.id] = peer
        self.save()
        return peer

    def update(self, peer_id: str, **changes) -> SyncPeer | None:
        peer = self._peers.get(peer_id)
        if peer is None:
            return None
        for key, value in changes.items():
            if not hasattr(peer, key) or key == "id":
                raise AttributeError(f"A sync peer has no field {key}")
            setattr(peer, key, value)
        self.save()
        return peer

    def remove(self, peer_id: str) -> SyncPeer | None:
        peer = self._peers.pop(peer_id, None)
        if peer is not None:
            self.save()
        return peer


# ---- the sidecar side: dial, hang up --------------------------------------------------------


async def dial(link: LinkService, store: SyncPeerStore, peer: SyncPeer) -> str:
    """Have the sidecar serve `peer` on a loopback address and keep it as `listen`. Dialing a
    peer already dialed returns the address it has."""
    reply = await link.request("dial", ticket=peer.ticket)
    listen = str(reply["listen"])
    if peer.listen != listen:
        store.update(peer.id, listen=listen)
    return listen


async def ensure_dialed(link: LinkService, store: SyncPeerStore, peer: SyncPeer) -> str:
    """The peer's loopback address, dialing first when there is none (before use)."""
    return peer.listen or await dial(link, store, peer)


async def dial_all(link: LinkService, store: SyncPeerStore) -> None:
    """Dial every peer again: the sidecar just started and its listeners are new."""
    for peer in store.peers():
        store.update(peer.id, listen=None)
        try:
            await dial(link, store, peer)
        except LinkError as e:
            logger.warning("Could not dial sync peer %s: %s", peer.name, e)


async def hang_up(link: LinkService, peer: SyncPeer) -> None:
    try:
        await link.request("hangup", peer=peer.endpoint_id)
    except LinkError as e:  # not running: nothing is dialed
        logger.info("Hang up on %s: %s", peer.name, e)


async def forget(ctx: AppContext, peer: SyncPeer) -> None:
    """Remove a peer, the DESKTOP devices for its endpoint and its dialed listener. What it
    synced stays."""
    ctx.sync_peers.remove(peer.id)
    for device in ctx.pairing.desktops_for(peer.endpoint_id):
        ctx.pairing.revoke(device.id)
    await hang_up(ctx.link, peer)


# ---- pairing --------------------------------------------------------------------------------


def check_can_pair(ctx: AppContext) -> None:
    st = ctx.settings
    if st.team_mode:
        raise SyncPairingError("Library sync is for personal computers, not a team server")
    if not st.remote_access or not ctx.link.status.running or not ctx.link.status.ticket:
        raise SyncPairingError("Turn on remote access first: syncing goes through it")
    if not st.api_token:
        raise SyncPairingError("Set an API token first: the other computer will use it to sync")


def split_invite(invite: str) -> tuple[str, str]:
    ticket, _, code = invite.strip().rpartition("#")
    if not ticket or not code:
        raise SyncPairingError("That is not a sync invite (expected <ticket>#<code>)")
    return ticket, code


async def fetch_peer_self(listen: str, token: str) -> dict:
    """Ask a dialed peer who it is (GET /api/sync/peers, its `self`)."""
    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"http://{listen}/api/sync/peers", headers={"Authorization": f"Bearer {token}"}
        )
        r.raise_for_status()
        return r.json()["self"]


async def pair_with_invite(
    ctx: AppContext, invite: str, name: str = "", *, fetch_self=None
) -> SyncPeer:
    """This machine is B: redeem the invite A showed. B trusts A's endpoint first (a DESKTOP
    device and the token A will use on B), then the sidecar pairs and hands A that token along
    with how to dial B back. A answers with the token for A and, when the sidecar relays it,
    A's sync identity; both sides then dial each other."""
    from .link import endpoint_id_from_ticket

    check_can_pair(ctx)
    ticket, _ = split_invite(invite)
    try:
        endpoint_id = endpoint_id_from_ticket(ticket)
    except ValueError as e:
        raise SyncPairingError("That is not a sync invite (expected <ticket>#<code>)") from e
    if endpoint_id == ctx.link.status.endpoint_id:
        raise SyncPairingError("That invite is from this computer: show it on the other one")
    if name.strip():
        ctx.sync_peers.set_name(name)
    me = ctx.sync_peers.self_info()
    device, token_for_a = ctx.pairing.add_desktop("Synced computer", endpoint_id)
    try:
        reply = await ctx.link.request(
            "pair",
            invite=invite.strip(),
            name=me.name,
            peer={
                "id": me.id,
                "name": me.name,
                "ticket": ctx.link.status.ticket,
                "token": token_for_a,
            },
        )
    except LinkError as e:
        ctx.pairing.revoke(device.id)
        raise SyncPairingError(f"Pairing failed: {e}") from e
    their = reply.get("sync") if isinstance(reply.get("sync"), dict) else {}
    peer = SyncPeer(
        id=str(their.get("id") or ""),
        name=str(their.get("name") or "Synced computer"),
        endpoint_id=endpoint_id,
        ticket=str(reply.get("ticket") or ticket),
        token=str(reply["token"]),
        created_at=now_iso(),
    )
    if not peer.id:
        # The sidecar did not carry A's identity back: ask A over the tunnel.
        try:
            listen = await dial(ctx.link, ctx.sync_peers, peer)
            found = await (fetch_self or fetch_peer_self)(listen, peer.token)
            peer.id, peer.name = found["id"], found["name"] or peer.name
        except Exception as e:  # noqa: BLE001 - paired anyway; fixed up at the next dial
            logger.warning("Could not ask the new sync peer who it is: %s", e)
    if not peer.id:
        peer.id = endpoint_id[:12]  # a stand-in until its sync id is known
    ctx.pairing.rename(device.id, peer.name)
    ctx.sync_peers.add(peer)
    try:
        await dial(ctx.link, ctx.sync_peers, peer)
    except LinkError as e:
        logger.warning("Could not dial sync peer %s yet: %s", peer.name, e)
    return ctx.sync_peers.get(peer.id) or peer


async def record_incoming(
    ctx: AppContext, *, device_name: str, endpoint_id: str, payload
) -> SyncPeer:
    """This machine is A: a sync code was just redeemed by B (`payload` is B's `peer` object,
    `endpoint_id` the endpoint the sidecar vouches for). Records B and dials it."""
    peer = SyncPeer(
        id=payload.id,
        name=(payload.name or device_name or "Synced computer")[:80],
        endpoint_id=endpoint_id,
        ticket=payload.ticket,
        token=payload.token,
        created_at=now_iso(),
    )
    ctx.sync_peers.add(peer)
    try:
        await dial(ctx.link, ctx.sync_peers, peer)
    except LinkError as e:
        logger.warning("Could not dial sync peer %s yet: %s", peer.name, e)
    return ctx.sync_peers.get(peer.id) or peer
