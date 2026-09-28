"""A small page for recording on a phone and sending it here (server mode)."""

import os
import socket
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from ...config import Settings
from ...models.base import ApiModel
from ..context import AppContext, get_ctx

router = APIRouter(tags=["mobile"])

PAGE = Path(__file__).resolve().parents[1] / "static" / "mobile.html"


@router.get("/m", response_class=HTMLResponse, include_in_schema=False)
async def mobile_page():
    """Record (or pick a recording) on a phone and upload it to /api/audio/import. In server
    mode the phone first pairs: /m?pair=<one-time code> (routes/pairing.py)."""
    return HTMLResponse(PAGE.read_text(encoding="utf-8"))


class PhoneLink(ApiModel):
    reachable: bool  # a phone can connect: LAN server mode, or a phone_url is set
    pairing: bool  # phones pair with a one-time code (an API token is set)
    host: str
    port: int
    urls: list[str]  # candidate addresses of the phone page
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


def bind_address() -> tuple[str, int]:
    return (
        os.environ.get("MNEMOSYNE_BIND_HOST", "127.0.0.1"),
        int(os.environ.get("MNEMOSYNE_BIND_PORT", "8008")),
    )


def phone_page_urls(settings: Settings) -> list[str]:
    """Where a phone can open the page: the configured phone_url first, then the LAN
    addresses when the backend listens on the network."""
    host, port = bind_address()
    urls = [settings.phone_url.strip().rstrip("/") + "/m"] if settings.phone_url.strip() else []
    if host not in ("127.0.0.1", "localhost", "::1"):
        ips = [host] if host not in ("0.0.0.0", "::") else lan_addresses()
        urls += [f"http://{ip}:{port}/m" for ip in ips]
    return urls


@router.get("/api/server/phone", response_model=PhoneLink)
async def phone_link(ctx: AppContext = Depends(get_ctx)):
    host, port = bind_address()
    urls = phone_page_urls(ctx.settings)
    reachable = bool(urls)
    pairing = bool(ctx.settings.api_token)
    if not reachable:
        note = (
            "The backend only listens on this computer. Run it in server mode "
            "(--host 0.0.0.0, with an API token), or set a phone address such as a "
            "Tailscale HTTPS name, to record from a phone."
        )
    elif not pairing:
        note = (
            "Anyone who can reach this address can use Mnemosyne. Set an API token, "
            "then pair each phone with a one-time code."
        )
    else:
        note = (
            "Each phone pairs once with a code that works one time and expires in 10 "
            "minutes; remove a phone below to cut it off."
        )
    if reachable and not any(u.startswith("https://") for u in urls):
        note += (
            " Plain http: the page uses the phone's recorder app; "
            "an HTTPS address records in the page."
        )
    return PhoneLink(
        reachable=reachable, pairing=pairing, host=host, port=port, urls=urls, note=note
    )
