"""This computer as a member of a team another computer hosts (docs/plans/
2026-11-10-team-members-in-the-app.md 1.2).

Joining redeems the team's app invite (`<host ticket>#<code>`, routes/users.py `app_invite`)
through the link sidecar's `pair`, from this machine's own endpoint: the host makes a MEMBER
device for this endpoint and answers with its token and `card = {"team": {name, user,
consent_required}}` (api/routes/pairing.py `_redeem_member`). The sidecar then `dial`s the
host, serving its backend on a loopback port (`listen`, new at every sidecar start: kept in
memory only and dialed again from `link.on_ready`). No `peer` is sent: the host is never given
a way back into this computer.

<data_dir>/team.json (mode 0600, written atomically) holds the membership: the host's ticket and
endpoint id, its name, the member token (the same class of secret as sync_peers.json's peer
tokens: never returned by the API, not backed up), who this person is there, the default work
mode for team recordings and, once the host refused the token, `signed_out_at`.

The window never holds the member token: api/routes/team_member.py proxies `/api/joined/...` to
`destination()` with it. One team per computer; not on a computer that uses a main computer,
hosts a team or is a team server.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from .. import config
from .link import LinkError, endpoint_id_from_ticket

if TYPE_CHECKING:
    from ..api.context import AppContext

logger = logging.getLogger(__name__)

WORK_MODES = ("here", "host")
PROBE_SECONDS = 5.0  # how long a reachability answer is reused (as satellite.py)
NOT_AN_INVITE = "That is not a team invite (it starts with “endpoint” and has a # in it)"


class TeamMemberError(Exception):
    """A join, leave or change refused; `status` is the HTTP status the route answers."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class Membership:
    host_name: str
    host_ticket: str
    host_endpoint: str
    token: str  # the MEMBER device's token on the host
    user: dict  # {id, name, role} on the host
    consent_required: bool = False  # the host's rule, as last seen (card, then /health)
    work: str = "here"  # where a team recording's work runs by default: here | host
    joined_at: str = ""
    signed_out_at: str | None = None  # the host refused the token: a new invite is needed


@dataclass(frozen=True)
class TeamDestination:
    """Where the team is reached, with the same three fields as main_computer.MainComputer."""

    url: str  # http://127.0.0.1:<the sidecar's dial port>
    token: str
    name: str


def _now_iso() -> str:
    return datetime.fromtimestamp(time.time(), UTC).isoformat()


class TeamStore:
    def __init__(self, path: Path):
        self.path = path
        self._m: Membership | None = None
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            user = raw.get("user") or {}
            self._m = Membership(
                host_name=str(raw.get("host_name") or "Team"),
                host_ticket=str(raw["host_ticket"]),
                host_endpoint=str(raw["host_endpoint"]),
                token=str(raw["token"]),
                user={
                    "id": str(user.get("id") or ""),
                    "name": str(user.get("name") or ""),
                    "role": str(user.get("role") or ""),
                },
                consent_required=bool(raw.get("consent_required")),
                work=raw.get("work") if raw.get("work") in WORK_MODES else "here",
                joined_at=str(raw.get("joined_at") or ""),
                signed_out_at=raw.get("signed_out_at") or None,
            )
        except FileNotFoundError:
            self._m = None
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            logger.warning("Could not read %s: not in a team", self.path, exc_info=True)
            self._m = None

    def get(self) -> Membership | None:
        return self._m

    def save(self, m: Membership) -> Membership:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(asdict(m), f, indent=2)
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)
        self._m = m
        return m

    def update(self, **changes) -> Membership | None:
        if self._m is None:
            return None
        current = asdict(self._m)
        for key in changes:
            if key not in current:
                raise AttributeError(f"A membership has no field {key}")
        return self.save(Membership(**{**current, **changes}))

    def clear(self) -> bool:
        had = self._m is not None or self.path.exists()
        self.path.unlink(missing_ok=True)
        self._m = None
        return had


@dataclass
class TeamMember:
    """The membership, its dial port and the HTTP client the proxy uses."""

    app: AppContext
    store: TeamStore
    # The sidecar's loopback address for the host ("127.0.0.1:<port>"), when dialed. Not
    # persisted: it changes at every sidecar start. Tests point it at a served host.
    listen: str | None = None
    _probe: tuple[float, bool] | None = field(default=None, repr=False)
    _client: httpx.AsyncClient | None = field(default=None, repr=False)

    @classmethod
    def build(cls, app: AppContext) -> TeamMember:
        return cls(app, TeamStore(app.settings.data_dir / "team.json"))

    # -- state

    @property
    def membership(self) -> Membership | None:
        return self.store.get()

    @property
    def joined(self) -> bool:
        return self.store.get() is not None

    @property
    def signed_out(self) -> bool:
        m = self.store.get()
        return m is not None and bool(m.signed_out_at)

    def destination(self) -> TeamDestination | None:
        """The team's backend through the sidecar with the member token, or None: not joined,
        signed out, or not dialed (yet)."""
        m = self.store.get()
        if m is None or m.signed_out_at or not self.listen or not m.token:
            return None
        return TeamDestination(url=f"http://{self.listen}", token=m.token, name=m.host_name)

    async def ensure_destination(self) -> TeamDestination | None:
        """`destination()`, dialing first when it is not dialed (the sidecar restarted, or the
        hook has not run yet)."""
        m = self.store.get()
        if m is not None and not m.signed_out_at and not self.listen:
            try:
                await self.dial()
            except LinkError as e:
                logger.info("Could not dial the team %s: %s", m.host_name, e)
        return self.destination()

    def client(self) -> httpx.AsyncClient:
        """One pooled client for everything sent to the host. No read timeout: an Ask answer
        or a records export takes as long as it takes. Never a proxy: the sidecar's loopback."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=None, write=60.0, pool=10.0),
                trust_env=False,
                follow_redirects=False,
            )
        return self._client

    # -- the sidecar

    async def dial(self) -> str | None:
        m = self.store.get()
        if m is None:
            return None
        reply = await self.app.link.request("dial", ticket=m.host_ticket)
        self.listen = str(reply["listen"])
        self._probe = None
        return self.listen

    async def redial(self) -> None:
        """The sidecar (re)started: its listeners are new (a `link.on_ready` hook)."""
        self.listen = None
        self._probe = None
        m = self.store.get()
        if m is None or m.signed_out_at:
            return
        try:
            await self.dial()
        except LinkError as e:
            logger.warning("Could not dial the team %s: %s", m.host_name, e)

    async def hang_up(self) -> None:
        m = self.store.get()
        self.listen = None
        self._probe = None
        if m is None:
            return
        # Another use of the same endpoint keeps its listener (a sync peer, a contact).
        if self.app.sync_peers.find_by_endpoint(m.host_endpoint) is not None:
            return
        try:
            await self.app.link.request("hangup", peer=m.host_endpoint)
        except LinkError as e:  # not running: nothing is dialed
            logger.info("Hang up on the team %s: %s", m.host_name, e)

    # -- reachability and sign-out

    async def reachable(self) -> bool:
        """Whether the host answers /health through the sidecar (cached PROBE_SECONDS). Also
        refreshes the host's consent rule from that answer."""
        if self._probe is not None and time.monotonic() - self._probe[0] < PROBE_SECONDS:
            return self._probe[1]
        dest = await self.ensure_destination()
        ok = False
        if dest is not None:
            try:
                r = await self.client().get(f"{dest.url}/health", timeout=3.0)
                ok = r.status_code == 200
                if ok:
                    rule = bool(r.json().get("consent_required"))
                    m = self.store.get()
                    if m is not None and m.consent_required != rule:
                        self.store.update(consent_required=rule)
            except (httpx.HTTPError, ValueError, AttributeError):
                ok = False
        self._probe = (time.monotonic(), ok)
        return ok

    def seen(self, ok: bool) -> None:
        self._probe = (time.monotonic(), ok)

    def mark_signed_out(self) -> None:
        m = self.store.get()
        if m is None or m.signed_out_at:
            return
        self.store.update(signed_out_at=_now_iso())
        self._probe = None
        logger.warning("Signed out of the team %s: its token was refused", m.host_name)

    async def check_signed_out(self) -> bool:
        """The host answered 401 (or closed a socket 4401): ask it once more whether the
        member token is still good (`GET /api/pairing/me`, which every device may call), and
        mark the membership signed out when it is not. A 401 is only ever the token's."""
        dest = self.destination()
        if dest is None:
            return self.signed_out
        try:
            r = await self.client().get(
                f"{dest.url}/api/pairing/me",
                headers={"Authorization": f"Bearer {dest.token}"},
                timeout=5.0,
            )
        except httpx.HTTPError:
            return False
        if r.status_code == 401:
            self.mark_signed_out()
            return True
        return False

    # -- joining and leaving

    def refusal(self) -> str | None:
        """Why this computer cannot join a team now, or None."""
        st = self.app.settings
        if config.dedicated_team_server(st):
            return "A team server cannot join another team"
        if config.hosts_team(st):
            return "This computer hosts a team: it cannot also join one"
        main = self.app.main_computer
        if main is not None:
            return (
                f"This computer uses {main.name or 'a main computer'}: its recordings go there, "
                "so it cannot join a team"
            )
        m = self.store.get()
        if m is not None and not m.signed_out_at:
            return f"This computer is already in {m.host_name}: leave it first"
        return None

    async def join(self, invite: str, name: str = "") -> tuple[Membership, str | None]:
        """Redeem a team's app invite. Returns the membership and the API token made for this
        computer now (None when it had one): the window must carry it. Raises TeamMemberError
        (409: cannot join here, or remote access cannot run; 400: the invite)."""
        from .pairing_prep import PairingUnavailable, prepare_pairing

        reason = self.refusal()
        if reason:
            raise TeamMemberError(409, reason)
        text = invite.strip()
        if text.lower().startswith(("http://", "https://")):
            raise TeamMemberError(
                400, "That is the link for a browser: paste the invite for the app instead"
            )
        ticket, _, code = text.rpartition("#")
        if not ticket or not code:
            raise TeamMemberError(400, NOT_AN_INVITE)
        try:
            endpoint = endpoint_id_from_ticket(ticket)
        except ValueError as e:
            raise TeamMemberError(400, NOT_AN_INVITE) from e
        try:
            prepared = await prepare_pairing(self.app, link=True)
        except PairingUnavailable as e:
            raise TeamMemberError(409, str(e)) from e
        if endpoint == self.app.link.status.endpoint_id:
            raise TeamMemberError(400, "That invite is from this computer")
        me = name.strip()[:80] or self.app.sync_peers.self_info().name
        try:
            # No `peer`: the host gets no way back into this computer (M9).
            reply = await self.app.link.request("pair", invite=f"{ticket}#{code}", name=me)
        except LinkError as e:
            raise TeamMemberError(400, f"Joining failed: {e}") from e
        card = reply.get("card")
        team = card.get("team") if isinstance(card, dict) else None
        user = team.get("user") if isinstance(team, dict) else None
        token = str(reply.get("token") or "")
        if not (isinstance(user, dict) and user.get("id") and token):
            logger.warning("Joining: the reply to %s is not a team's", endpoint[:12])
            raise TeamMemberError(400, "That invite is not a team invite")
        old = self.store.get()
        if old is not None:  # signed out of a team before: this one replaces it
            await self.hang_up()
        membership = self.store.save(
            Membership(
                host_name=str(team.get("name") or "Team")[:80],
                host_ticket=str(reply.get("ticket") or ticket),
                host_endpoint=str(reply.get("peer") or endpoint),
                token=token,
                user={
                    "id": str(user["id"]),
                    "name": str(user.get("name") or ""),
                    "role": str(user.get("role") or ""),
                },
                consent_required=bool(team.get("consent_required")),
                work=old.work if old is not None else "here",
                joined_at=_now_iso(),
            )
        )
        logger.info("Joined the team %s as %s", membership.host_name, membership.user["name"])
        try:
            await self.dial()
        except LinkError as e:
            logger.warning("Could not dial the team %s yet: %s", membership.host_name, e)
        return membership, prepared.new_api_token

    async def leave(self) -> bool:
        """Leave the team: tell the host (best effort: `DELETE /api/pairing/me` revokes this
        device only), hang up and forget the membership. False when not in one."""
        m = self.store.get()
        if m is None:
            return False
        dest = None if m.signed_out_at else await self.ensure_destination()
        if dest is not None:
            try:
                r = await self.client().delete(
                    f"{dest.url}/api/pairing/me",
                    headers={"Authorization": f"Bearer {dest.token}"},
                    timeout=5.0,
                )
                if r.status_code >= 400:
                    logger.info("Leaving %s: the host answered %s", m.host_name, r.status_code)
            except httpx.HTTPError as e:
                logger.info("Leaving %s: the host was not reached (%s)", m.host_name, e)
        await self.hang_up()
        self.store.clear()
        logger.info("Left the team %s", m.host_name)
        return True

    def set_work(self, work: str) -> Membership:
        if work not in WORK_MODES:
            raise TeamMemberError(400, "The work runs here or on the host")
        m = self.store.update(work=work)
        if m is None:
            raise TeamMemberError(404, "This computer is not in a team")
        return m

    # -- lifetime

    async def startup(self) -> None:
        """Dial the team when the sidecar is already up; otherwise its ready hook does."""
        status = self.app.link.status
        if self.joined and status.running and not self.listen:
            await self.redial()

    async def shutdown(self) -> None:
        if self.listen and self.app.link.status.running:
            await self.hang_up()
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()
