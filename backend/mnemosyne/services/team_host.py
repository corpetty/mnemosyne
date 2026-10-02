"""A team from one desktop install: "Share this computer with my team" (Settings → General).

Turning it on makes the person at this computer the first admin (`team_owner_id`), gives them the
meetings that belong to nobody yet, switches on team mode, and opens a second listener on the local
network (`team_port`, all interfaces) with HTTPS from a certificate made for this machine's
addresses: browsers ask once to accept it, and recording in a browser needs HTTPS. That listener
serves the web app (`web_dir`, bundled with the desktop app) and the API; everyone there signs in
with an invite link. The desktop app keeps using 127.0.0.1 on the main port, where requests
without a token run as the owner (api/auth.py).

Turning it off closes the listener and team mode; the people and their meetings stay.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import logging
import os
import pwd
import shutil
import socket
from pathlib import Path
from typing import TYPE_CHECKING

from .. import access

if TYPE_CHECKING:
    from ..api.context import AppContext

logger = logging.getLogger(__name__)


def lan_addresses() -> list[str]:
    """This machine's IPv4 addresses on its networks (not loopback), the default route's first."""
    found: list[str] = []
    try:  # the address used to reach the outside: no packet is sent for a UDP connect
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in found:
                found.append(ip)
    except OSError:
        pass
    return [ip for ip in found if not ipaddress.ip_address(ip).is_loopback]


def owner_name() -> str:
    """The person at this computer, from the system account."""
    try:
        entry = pwd.getpwuid(os.getuid())
        return entry.pw_gecos.split(",")[0].strip() or entry.pw_name
    except KeyError:
        return "Owner"


def ensure_certificate(folder: Path, hosts: list[str]) -> tuple[Path, Path]:
    """A self-signed certificate for these names and addresses, made again when they change."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    folder.mkdir(parents=True, exist_ok=True)
    cert_path, key_path, names_path = folder / "team.crt", folder / "team.key", folder / "names"
    wanted = ",".join(sorted(hosts))
    if cert_path.is_file() and key_path.is_file() and names_path.is_file():
        if names_path.read_text() == wanted:
            return cert_path, key_path
    key = ec.generate_private_key(ec.SECP256R1())
    sans: list[x509.GeneralName] = []
    for h in hosts:
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(h)))
        except ValueError:
            sans.append(x509.DNSName(h))
    now = dt.datetime.now(dt.UTC)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"Mnemosyne on {hosts[0]}")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    names_path.write_text(wanted)
    return cert_path, key_path


def firewall_hint(port: int) -> str:
    """How to let others reach the port, for the firewall this machine has ("" when none)."""
    if shutil.which("firewall-cmd"):
        return f"sudo firewall-cmd --add-port={port}/tcp --permanent && sudo firewall-cmd --reload"
    if shutil.which("ufw"):
        return f"sudo ufw allow {port}/tcp"
    return ""


class TeamHost:
    """The network listener: a second uvicorn server on the same app."""

    def __init__(self):
        self._server = None
        self._task: asyncio.Task | None = None
        self.error = ""

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self, app, ctx: AppContext) -> None:
        import uvicorn

        await self.stop()
        port = ctx.settings.team_port
        hosts = [socket.gethostname(), f"{socket.gethostname()}.local", *lan_addresses()]
        cert, key = await asyncio.to_thread(
            ensure_certificate, ctx.settings.data_dir / "tls", hosts
        )
        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=port,
            ssl_certfile=str(cert),
            ssl_keyfile=str(key),
            lifespan="off",  # the app is already running: this is only another door into it
            log_level="warning",
            ws_max_size=16 * 1024 * 1024,
        )
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None  # the main server owns the signals
        self._server = server
        self.error = ""
        self._task = asyncio.create_task(self._serve(server))
        for _ in range(50):  # until it listens, or gives up (a port in use)
            await asyncio.sleep(0.1)
            if server.started or self._task.done():
                break
        if not server.started:
            await self.stop()
            raise RuntimeError(self.error or f"Could not listen on port {port}")
        logger.info("Sharing on the network: https://%s:%d", hosts[-1], port)

    async def _serve(self, server) -> None:
        try:
            await server.serve()
        except (OSError, SystemExit) as e:
            self.error = f"Could not listen on port {server.config.port}: {e}"
            logger.warning(self.error)

    async def stop(self) -> None:
        server, task, self._server, self._task = self._server, self._task, None, None
        if server is not None:
            server.should_exit = True
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                task.cancel()


async def turn_on(ctx: AppContext, app, name: str = "") -> str:
    """Share this computer: the owner (made admin the first time), their meetings, team mode
    and the listener. Returns the owner's user id."""
    from ..config import save_settings

    owner = ctx.users.get(ctx.settings.team_owner_id) if ctx.settings.team_owner_id else None
    if owner is None:
        owner = ctx.users.add(name.strip() or owner_name(), "", access.ADMIN)
    adopted = ctx.repo.adopt_unowned(owner.id)
    if adopted:
        logger.info("Gave %d meetings without an owner to %s", adopted, owner.name)
    settings = ctx.settings.model_copy(
        update={"team_mode": True, "share_on_network": True, "team_owner_id": owner.id}
    )
    save_settings(settings)
    await ctx.apply_settings(settings)
    await ctx.team_host.start(app, ctx)
    return owner.id


async def turn_off(ctx: AppContext) -> None:
    from ..config import save_settings

    await ctx.team_host.stop()
    settings = ctx.settings.model_copy(update={"team_mode": False, "share_on_network": False})
    save_settings(settings)
    await ctx.apply_settings(settings)
