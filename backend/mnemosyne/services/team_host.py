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
import json
import logging
import os
import pwd
import shutil
import socket
import ssl
import subprocess
import time
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


def this_machine() -> list[str]:
    """The names and addresses the self-signed certificate covers."""
    host = socket.gethostname()
    return [host, f"{host}.local", *lan_addresses()]


# ---- Tailscale: a real certificate for the machine's tailnet name -----------------------------


def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def tailscale_name() -> str:
    """This machine's Tailscale name when the tailnet issues HTTPS certificates for it ("" when
    Tailscale is missing, stopped, or HTTPS certificates are off in its admin console)."""
    if not shutil.which("tailscale"):
        return ""
    try:
        out = _run(["tailscale", "status", "--json"], timeout=5)
        status = json.loads(out.stdout) if out.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return ""
    if status.get("BackendState") != "Running":
        return ""
    domains = status.get("CertDomains") or []
    name = (status.get("Self") or {}).get("DNSName", "").rstrip(".")
    return name if name in domains else (domains[0] if domains else "")


def tailscale_cert(folder: Path, name: str) -> tuple[Path, Path]:
    """`tailscale cert` for `name` into folder/tailscale.{crt,key}. Raises RuntimeError with what
    to do when Tailscale refuses (it needs root or this user as its operator)."""
    folder.mkdir(parents=True, exist_ok=True)
    cert, key = folder / "tailscale.crt", folder / "tailscale.key"
    try:
        out = _run(
            ["tailscale", "cert", "--cert-file", str(cert), "--key-file", str(key), name],
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"tailscale cert failed: {e}") from e
    if out.returncode != 0:
        detail = (out.stderr or out.stdout).strip().splitlines()
        message = detail[-1] if detail else f"exit {out.returncode}"
        if "access denied" in message.lower() or "permission" in message.lower():
            user = pwd.getpwuid(os.getuid()).pw_name
            message = (
                "Tailscale only gives certificates to its operator: run "
                f"`sudo tailscale set --operator={user}` once, then try again"
            )
        raise RuntimeError(message)
    key.chmod(0o600)
    return cert, key


def days_left(cert: Path) -> float:
    """Days until the certificate in this file expires (-1 when it cannot be read)."""
    from cryptography import x509

    try:
        loaded = x509.load_pem_x509_certificate(cert.read_bytes())
    except (OSError, ValueError):
        return -1
    return (loaded.not_valid_after_utc - dt.datetime.now(dt.UTC)).total_seconds() / 86400


RENEW_DAYS = 30  # Tailscale's certificates last 90 days


class TeamHost:
    """The network listener: a second uvicorn server on the same app.

    Its TLS context has the self-signed certificate for this machine's names and addresses; a
    second context, with the Tailscale certificate, answers connections that ask for the
    Tailscale name (SNI). While it runs, the addresses are checked every WATCH seconds and the
    self-signed certificate is made again and loaded into the running context when they change.
    """

    WATCH = 30.0
    RENEW_EVERY = 12 * 3600.0

    def __init__(self):
        self._server = None
        self._task: asyncio.Task | None = None
        self._watch: asyncio.Task | None = None
        self._ssl: ssl.SSLContext | None = None
        self._named: dict[str, ssl.SSLContext] = {}  # server name -> its own context
        self._hosts: list[str] = []
        self._folder: Path | None = None
        self._renewed_at = 0.0
        self.error = ""
        self.tailscale = ""  # the Tailscale name served with its own certificate
        self.tailscale_error = ""

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _sni(self, sslobj, server_name, _context) -> None:
        named = self._named.get((server_name or "").lower())
        if named is not None:
            sslobj.context = named

    async def start(self, app, ctx: AppContext) -> None:
        import uvicorn

        await self.stop()
        port = ctx.settings.team_port
        self._folder = ctx.settings.data_dir / "tls"
        self._hosts = this_machine()
        cert, key = await asyncio.to_thread(ensure_certificate, self._folder, self._hosts)
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
        config.load()  # makes the TLS context, which the watch and SNI then work on
        self._ssl = config.ssl
        self._ssl.sni_callback = self._sni
        if ctx.settings.team_tailscale_cert:
            try:
                await self.use_tailscale(renew=True)
            except RuntimeError as e:  # shared anyway, with the self-signed certificate only
                self.tailscale_error = str(e)
                logger.warning("No Tailscale certificate: %s", e)
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
        self._watch = asyncio.create_task(self._watch_loop(ctx))
        logger.info("Sharing on the network: https://%s:%d", self._hosts[-1], port)

    async def use_tailscale(self, renew: bool = False) -> None:
        """Serve the Tailscale name with its certificate, fetched (or renewed when it has less
        than RENEW_DAYS left, or `renew` and it cannot be read). Raises RuntimeError when
        Tailscale cannot give one and there is none to keep using."""
        assert self._folder is not None
        name = await asyncio.to_thread(tailscale_name)
        if not name:
            self.tailscale = ""
            raise RuntimeError(
                "Tailscale is not running here, or HTTPS certificates are off for your tailnet "
                "(Tailscale admin console → DNS → HTTPS Certificates)"
            )
        cert, key = self._folder / "tailscale.crt", self._folder / "tailscale.key"
        left = await asyncio.to_thread(days_left, cert)
        if left < RENEW_DAYS or not key.is_file():
            try:
                await asyncio.to_thread(tailscale_cert, self._folder, name)
                self.tailscale_error = ""
            except RuntimeError as e:
                self.tailscale_error = str(e)
                if left <= 0 or not key.is_file():
                    self.tailscale = ""
                    self._named.clear()
                    raise
                logger.warning("Renewing the Tailscale certificate failed: %s", e)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        self._named = {name.lower(): context}
        self.tailscale = name
        self._renewed_at = time.monotonic()
        logger.info("Serving %s with its Tailscale certificate", name)

    def drop_tailscale(self) -> None:
        self._named = {}
        self.tailscale = ""
        self.tailscale_error = ""

    async def check_addresses(self) -> bool:
        """Make the certificate again when this machine's addresses changed (a laptop on another
        network) and load it into the running listener. Returns whether they changed."""
        hosts = await asyncio.to_thread(this_machine)
        if hosts == self._hosts or self._ssl is None or self._folder is None:
            return False
        cert, key = await asyncio.to_thread(ensure_certificate, self._folder, hosts)
        self._ssl.load_cert_chain(cert, key)  # new connections get it; no restart
        logger.info("This computer's addresses changed: %s", ", ".join(hosts[2:]) or "none")
        self._hosts = hosts
        return True

    async def _watch_loop(self, ctx: AppContext) -> None:
        while True:
            await asyncio.sleep(self.WATCH)
            try:
                await self.check_addresses()
                if (
                    ctx.settings.team_tailscale_cert
                    and time.monotonic() - self._renewed_at > self.RENEW_EVERY
                ):
                    await self.use_tailscale()
            except Exception as e:
                logger.warning("Checking the shared address failed: %s", e)
                self._renewed_at = time.monotonic()  # try again in RENEW_EVERY, not at once

    async def _serve(self, server) -> None:
        try:
            await server.serve()
        except (OSError, SystemExit) as e:
            self.error = f"Could not listen on port {server.config.port}: {e}"
            logger.warning(self.error)

    async def stop(self) -> None:
        server, task, self._server, self._task = self._server, self._task, None, None
        watch, self._watch = self._watch, None
        if watch is not None:
            watch.cancel()
        if server is not None:
            server.should_exit = True
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                task.cancel()
        self._ssl = None
        self.drop_tailscale()


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


async def set_options(ctx: AppContext, **options: bool) -> None:
    """keep_sharing_after_quit, keep_awake_while_sharing, team_tailscale_cert: saved, and in
    effect at once. Turning the Tailscale certificate on fetches it first (RuntimeError when
    Tailscale refuses; then nothing is saved)."""
    from ..config import save_settings

    tailscale = options.get("team_tailscale_cert")
    if tailscale is True and ctx.team_host.running:
        await ctx.team_host.use_tailscale(renew=True)
    elif tailscale is False:
        ctx.team_host.drop_tailscale()
    settings = ctx.settings.model_copy(update=options)
    save_settings(settings)
    await ctx.apply_settings(settings)


async def turn_off(ctx: AppContext) -> None:
    from ..config import save_settings

    await ctx.team_host.stop()
    settings = ctx.settings.model_copy(update={"team_mode": False, "share_on_network": False})
    save_settings(settings)
    await ctx.apply_settings(settings)
