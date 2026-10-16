"""Library sync between computers: pairing and the paired machines (services/sync_peers.py),
and what a peer pulls from this one and how pulling from them goes (services/sync_service.py).

A peer calls these with the token this computer gave it at pairing (a DESKTOP device), so the
routes know which peer asks; the admin token works too, but then nothing is remembered as served.
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ... import access
from ...models.sync import (
    SyncChanges,
    SyncClash,
    SyncHello,
    SyncNowResponse,
    SyncPairRequest,
    SyncPairResponse,
    SyncPeerInfo,
    SyncPeers,
    SyncPeerStatus,
    SyncPokeResponse,
    SyncSelf,
    SyncStatus,
)
from ...services import sync_peers
from ...services.sync_peers import SyncPairingError, SyncPeer
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api/sync", tags=["sync"])
ADMIN = [Depends(access.require_admin)]


def _self(ctx: AppContext) -> SyncSelf:
    me = ctx.sync_peers.self_info()
    return SyncSelf(id=me.id, name=me.name)


def _info(p: SyncPeer) -> SyncPeerInfo:
    # Never the token.
    return SyncPeerInfo(
        id=p.id,
        name=p.name,
        endpoint_id=p.endpoint_id,
        created_at=p.created_at,
        last_sync_at=p.last_sync_at,
        listen=p.listen,
    )


@router.get("/peers", response_model=SyncPeers, dependencies=ADMIN)
async def list_peers(ctx: AppContext = Depends(get_ctx)):
    return SyncPeers(self=_self(ctx), peers=[_info(p) for p in ctx.sync_peers.peers()])


@router.get("/status", response_model=SyncStatus, dependencies=ADMIN)
async def status(ctx: AppContext = Depends(get_ctx)):
    return _status(ctx)


def _status(ctx: AppContext) -> SyncStatus:
    peers = []
    for p in ctx.sync_peers.peers():
        st = ctx.sync.state(p.id)
        peers.append(
            SyncPeerStatus(
                id=p.id,
                name=p.name,
                endpoint=p.endpoint_id[:10],
                listen=p.listen,
                last_sync_at=p.last_sync_at,
                state=st.state,
                error=st.error,
                done=st.done,
                total=st.total,
                received=st.received,
                updated=st.updated,
                merged=st.merged,
                deleted=st.deleted,
                clashes=[SyncClash(session_id=k, name=v) for k, v in st.clashes.items()],
                app_version=st.app_version,
            )
        )
    return SyncStatus(self=_self(ctx), peers=peers)


def _no_team(ctx: AppContext) -> None:
    if ctx.settings.team_mode:
        raise HTTPException(409, "Library sync is for personal computers, not a team server")


def _caller(request: Request, ctx: AppContext):
    return ctx.sync.calling_peer(getattr(request.state, "device_id", None))


@router.get("/hello", response_model=SyncHello, dependencies=ADMIN)
async def hello(request: Request, ctx: AppContext = Depends(get_ctx)):
    """Who this computer is and which sync it speaks; a peer asks first in every round."""
    from ...services.sync_service import PROTOCOL
    from ...storage.sqlite import SCHEMA_VERSION

    _no_team(ctx)
    me = ctx.sync_peers.self_info()
    return SyncHello(
        protocol=PROTOCOL,
        schema_version=SCHEMA_VERSION,
        app_version=request.app.version,
        id=me.id,
        name=me.name,
        seq=ctx.repo.current_sync_seq(),
    )


@router.get("/changes", response_model=SyncChanges, dependencies=ADMIN)
async def changes(
    request: Request,
    since: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    tombstones_since: str = "",
    ctx: AppContext = Depends(get_ctx),
):
    """The meetings changed after `since` (this computer's change counter), whole, oldest
    change first, and the meetings deleted after `tombstones_since` (ISO time, this computer's
    clock). Each document is remembered as served to the calling peer: the base of a merge of
    its later edits."""
    _no_team(ctx)
    peer = _caller(request, ctx)
    return await asyncio.to_thread(ctx.sync.serve_changes, peer, since, limit, tombstones_since)


@router.post("/poke", response_model=SyncPokeResponse, dependencies=ADMIN)
async def poke(request: Request, ctx: AppContext = Depends(get_ctx)):
    """The calling peer has news: pull from it in a moment (pokes in between make one pull)."""
    _no_team(ctx)
    peer = _caller(request, ctx)
    return SyncPokeResponse(scheduled=peer is not None and ctx.sync.poke(peer.id))


@router.post("/now", response_model=SyncNowResponse, dependencies=ADMIN)
async def sync_now(ctx: AppContext = Depends(get_ctx)):
    """Pull from every paired computer now, in the background; `sync` events and
    GET /api/sync/status follow it."""
    _no_team(ctx)
    return SyncNowResponse(started=ctx.sync.sync_now())


@router.post("/pair", response_model=SyncPairResponse, dependencies=ADMIN)
async def pair(body: SyncPairRequest, ctx: AppContext = Depends(get_ctx)):
    """Pair with the computer that showed this invite (Settings > New sync invite there)."""
    try:
        peer = await sync_peers.pair_with_invite(ctx, body.invite, body.name)
    except SyncPairingError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return SyncPairResponse(peer=_info(peer))


@router.delete("/peers/{peer_id}", dependencies=ADMIN)
async def remove_peer(peer_id: str, ctx: AppContext = Depends(get_ctx)):
    """Stop syncing with a computer. What it synced stays; it can pair again."""
    peer = ctx.sync_peers.get(peer_id)
    if peer is None:
        raise HTTPException(status_code=404, detail="Computer not found")
    await sync_peers.forget(ctx, peer)
    return {"message": "Computer removed"}
