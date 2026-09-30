"""People on a firm's server (services/users.py, access.py): who am I, invites, and, for an
admin, adding people, changing roles, disabling and signing out. Redeeming an invite needs no
token (api/auth.py); the web app turns `?invite=<code>` into a token for this browser."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ... import access
from ...models.base import ApiModel
from ...services.users import InvalidInvite, User
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api/users", tags=["users"])


def admin_only() -> None:
    access.require_admin()


class Me(ApiModel):
    id: str  # "" for the desktop app or the admin token
    name: str
    email: str
    role: str
    firm_mode: bool


class UserDevice(ApiModel):
    device: str
    created_at: datetime
    last_seen_at: datetime | None


class UserInfo(ApiModel):
    id: str
    name: str
    email: str
    role: str
    disabled: bool
    created_at: datetime
    last_seen_at: datetime | None
    devices: list[UserDevice]  # an admin's view only; empty for everyone else


class NewUser(BaseModel):
    name: str
    email: str = ""
    role: str = access.ADVISOR


class UserChange(BaseModel):
    name: str | None = None
    email: str | None = None
    role: str | None = None
    disabled: bool | None = None


class Invite(ApiModel):
    code: str  # the web app links to <its address>/?invite=<code>
    expires_at: datetime
    user: UserInfo


class RedeemRequest(BaseModel):
    code: str
    device: str = ""


class Redeemed(ApiModel):
    token: str
    user: UserInfo


def _info(u: User, full: bool) -> UserInfo:
    return UserInfo(
        id=u.id,
        name=u.name,
        email=u.email if full else "",
        role=u.role,
        disabled=u.disabled,
        created_at=u.created_at,
        last_seen_at=u.last_seen_at,
        devices=[
            UserDevice(device=t.device, created_at=t.created_at, last_seen_at=t.last_seen_at)
            for t in u.tokens
        ]
        if full
        else [],
    )


@router.get("/me", response_model=Me)
async def me(ctx: AppContext = Depends(get_ctx)):
    p = access.principal()
    if p is None:
        return Me(id="", name="", email="", role=access.ADMIN, firm_mode=ctx.settings.firm_mode)
    user = ctx.users.get(p.user_id)
    return Me(
        id=p.user_id,
        name=p.name,
        email=user.email if user else "",
        role=p.role,
        firm_mode=ctx.settings.firm_mode,
    )


@router.get("", response_model=list[UserInfo])
async def list_users(ctx: AppContext = Depends(get_ctx)):
    """Everyone's name and role (a reviewer sees whose meeting is whose); details for admins."""
    full = access.is_admin()
    return [_info(u, full) for u in ctx.users.list()]


@router.post("", response_model=Invite, dependencies=[Depends(admin_only)])
async def add_user(body: NewUser, ctx: AppContext = Depends(get_ctx)):
    try:
        user = ctx.users.add(body.name, body.email, body.role)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    code, expires = ctx.users.invite(user.id)
    return Invite(code=code, expires_at=datetime.fromtimestamp(expires), user=_info(user, True))


@router.patch("/{user_id}", response_model=UserInfo, dependencies=[Depends(admin_only)])
async def change_user(user_id: str, body: UserChange, ctx: AppContext = Depends(get_ctx)):
    try:
        user = ctx.users.update(user_id, **body.model_dump(exclude_none=True))
    except KeyError as e:
        raise HTTPException(status_code=404, detail="No such person") from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return _info(user, True)


@router.post("/{user_id}/invite", response_model=Invite, dependencies=[Depends(admin_only)])
async def invite(user_id: str, ctx: AppContext = Depends(get_ctx)):
    try:
        code, expires = ctx.users.invite(user_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="No such person") from e
    user = ctx.users.get(user_id)
    return Invite(code=code, expires_at=datetime.fromtimestamp(expires), user=_info(user, True))


@router.post("/me/signout")
async def sign_out(request: Request, ctx: AppContext = Depends(get_ctx)):
    """Forget this browser's token."""
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    token = token or request.query_params.get("token", "")
    if token:
        ctx.users.forget_token(token)
    return {"ok": True}


@router.post("/{user_id}/signout", dependencies=[Depends(admin_only)])
async def sign_out_everywhere(user_id: str, ctx: AppContext = Depends(get_ctx)):
    if ctx.users.get(user_id) is None:
        raise HTTPException(status_code=404, detail="No such person")
    ctx.users.sign_out(user_id)
    return {"ok": True}


@router.post("/redeem", response_model=Redeemed)
async def redeem(body: RedeemRequest, ctx: AppContext = Depends(get_ctx)):
    try:
        user, token = ctx.users.redeem(body.code, body.device)
    except InvalidInvite as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return Redeemed(token=token, user=_info(user, False))
