"""Library sync between computers: pairing and the paired machines (services/sync_peers.py),
and what a peer pulls from this one and how pulling from them goes (services/sync_service.py).

A peer calls these with the token this computer gave it at pairing (a DESKTOP device), so the
routes know which peer asks; the admin token works too, but then nothing is remembered as served.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ... import access
from ...models.sync import (
    SyncChanges,
    SyncClash,
    SyncContact,
    SyncContacts,
    SyncHello,
    SyncNowResponse,
    SyncPairRequest,
    SyncPairResponse,
    SyncPeerInfo,
    SyncPeers,
    SyncPeerStatus,
    SyncPerson,
    SyncPersonKey,
    SyncPokeResponse,
    SyncSelf,
    SyncStatus,
)
from ...services import contacts, sync_peers
from ...services.person import PersonKeys
from ...services.sync_peers import SyncPairingError, SyncPeer
from ..context import AppContext, get_ctx

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/sync", tags=["sync"])
ADMIN = [Depends(access.require_admin)]


def _self(ctx: AppContext) -> SyncSelf:
    me = ctx.sync_peers.self_info()
    return SyncSelf(
        id=me.id,
        name=me.name,
        person=SyncPerson(pid=ctx.person.pid, fingerprint=ctx.person.fingerprint),
    )


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
                waiting=st.waiting,
                clashes=[SyncClash(session_id=k, name=v) for k, v in st.clashes.items()],
                app_version=st.app_version,
                warning=st.warning,
            )
        )
    return SyncStatus(self=_self(ctx), peers=peers)


def _no_team(ctx: AppContext) -> None:
    if ctx.settings.team_mode:
        raise HTTPException(409, "Library sync is for personal computers, not a team server")


def _caller(request: Request, ctx: AppContext):
    return ctx.sync.calling_peer(getattr(request.state, "device_id", None))


def _sync_peer(request: Request, ctx: AppContext) -> SyncPeer:
    """The paired sync computer calling (its DESKTOP device's endpoint is a current sync peer),
    for what only one of this person's own computers may have. 403 for anything else, the admin
    token included: these routes carry the person's key and contacts between computers."""
    _no_team(ctx)
    peer = _caller(request, ctx)
    if peer is None:
        raise HTTPException(403, "Only a computer synced with this one may do this")
    return peer


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
    outcome: dict = {}
    try:
        peer = await sync_peers.pair_with_invite(ctx, body.invite, body.name, outcome=outcome)
    except SyncPairingError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return SyncPairResponse(peer=_info(peer), person_error=outcome.get("person_error"))


@router.delete("/peers/{peer_id}", dependencies=ADMIN)
async def remove_peer(peer_id: str, ctx: AppContext = Depends(get_ctx)):
    """Stop syncing with a computer. What it synced stays; it can pair again."""
    peer = ctx.sync_peers.get(peer_id)
    if peer is None:
        raise HTTPException(status_code=404, detail="Computer not found")
    await sync_peers.forget(ctx, peer)
    return {"message": "Computer removed"}


@router.get("/person-key", response_model=SyncPersonKey)
async def get_person_key(request: Request, ctx: AppContext = Depends(get_ctx)):
    """This person's private key, for the person's other computer that adopts it when pairing for
    library sync (docs/plans/2026-10-23-phase-8-tasks.md, C5). Only a computer synced with this
    one, over its paired tunnel."""
    _sync_peer(request, ctx)
    return SyncPersonKey(private=ctx.person.export_private())


@router.put("/person-key", response_model=SyncPerson)
async def put_person_key(body: SyncPersonKey, request: Request, ctx: AppContext = Depends(get_ctx)):
    """Take the key another computer of this person offers (the pairing computer, when only it
    had people data). Refused while this library has people data under a different key: that
    would cut the contacts off."""
    peer = _sync_peer(request, ctx)
    try:
        same = ctx.person.public_hex == PersonKeys.public_of(body.private)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if not same:
        if contacts.has_people_data(ctx):
            raise HTTPException(
                409,
                "This computer shares with other people under its own key: remove them first",
            )
        ctx.person.replace(body.private)
        contacts.on_key_changed(ctx)
        logger.info("Took the person key of %s", peer.name)
    return SyncPerson(pid=ctx.person.pid, fingerprint=ctx.person.fingerprint)


@router.get("/contacts", response_model=SyncContacts)
async def get_contacts(request: Request, ctx: AppContext = Depends(get_ctx)):
    """Every contact of this library, removed ones included, complete: card and shared secret too,
    because the computer asking is another of this person's (a paired sync peer). The runtime
    state of each contact (dialed machines, cursors) stays here. One step of every sync round."""
    _sync_peer(request, ctx)
    return SyncContacts(
        person=ctx.person.public_hex,
        has_people_data=contacts.has_people_data(ctx),
        contacts=[SyncContact(**row) for row in contacts.export_rows(ctx)],
    )
