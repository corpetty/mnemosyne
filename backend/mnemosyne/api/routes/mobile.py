"""A small page for recording on a phone and sending it here (server mode)."""

import os
import socket
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from ...models.base import ApiModel
from ..context import AppContext, get_ctx

router = APIRouter(tags=["mobile"])

PAGE = Path(__file__).resolve().parents[1] / "static" / "mobile.html"


@router.get("/m", response_class=HTMLResponse, include_in_schema=False)
async def mobile_page():
    """Record (or pick a recording) on a phone and upload it to /api/audio/import. In server
    mode open it as /m?token=<api_token>."""
    return HTMLResponse(PAGE.read_text(encoding="utf-8"))


class PhoneLink(ApiModel):
    reachable: bool  # bound to a non-loopback address, so a phone on the network can connect
    host: str
    port: int
    urls: list[str]  # candidate addresses of the phone page, token included
    note: str


def lan_addresses() -> list[str]:
    ips: list[str] = []
    try:  # the interface used for outbound traffic (no packet is sent)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ips.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.append(info[4][0])
    except OSError:
        pass
    return [ip for ip in dict.fromkeys(ips) if not ip.startswith("127.")]


@router.get("/api/server/phone", response_model=PhoneLink)
async def phone_link(ctx: AppContext = Depends(get_ctx)):
    host = os.environ.get("MNEMOSYNE_BIND_HOST", "127.0.0.1")
    port = int(os.environ.get("MNEMOSYNE_BIND_PORT", "8008"))
    reachable = host not in ("127.0.0.1", "localhost", "::1")
    token = ctx.settings.api_token
    suffix = f"/m?token={token}" if token else "/m"
    ips = [host] if reachable and host not in ("0.0.0.0", "::") else lan_addresses()
    urls = [f"http://{ip}:{port}{suffix}" for ip in ips]
    if not reachable:
        note = (
            "The backend only listens on this computer. Run it in server mode "
            "(--host 0.0.0.0, with an API token) to record from a phone."
        )
    elif not token:
        note = "Anyone on your network can use this address. Set an API token in Settings."
    else:
        note = (
            "Plain http: the page uses the phone's recorder app. "
            "Serve over HTTPS to record inside the page."
        )
    return PhoneLink(reachable=reachable, host=host, port=port, urls=urls, note=note)
