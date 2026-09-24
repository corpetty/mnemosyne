"""Post a message (a follow-up) to Slack or Matrix."""

from __future__ import annotations

import time
from urllib.parse import quote

import httpx

from .trackers import TrackerCheck, _error


class SlackPoster:
    name = "slack"

    def __init__(self, webhook_url: str, transport=None):
        self.webhook_url = webhook_url.strip()
        self._transport = transport

    def validate(self) -> str | None:
        if not self.webhook_url.startswith("https://hooks.slack.com/"):
            return "Set a Slack incoming webhook URL (https://hooks.slack.com/…) in Settings"
        return None

    async def check(self) -> TrackerCheck:
        # A webhook cannot be probed without posting; only the address is checked.
        problem = self.validate()
        return TrackerCheck(ok=problem is None, message=problem or "Webhook address looks right")

    async def post(self, text: str) -> None:
        async with httpx.AsyncClient(timeout=20, transport=self._transport) as c:
            r = await c.post(self.webhook_url, json={"text": text})
        if r.status_code >= 400:
            raise RuntimeError(f"Slack: {r.status_code} {r.text[:200]}")


class MatrixPoster:
    name = "matrix"

    def __init__(self, homeserver: str, token: str, room_id: str, transport=None):
        self.homeserver = homeserver.strip().rstrip("/")
        self.token = token.strip()
        self.room_id = room_id.strip()
        self._transport = transport

    def validate(self) -> str | None:
        if not self.homeserver.startswith(("https://", "http://")):
            return "Set the Matrix homeserver URL in Settings"
        if not self.token:
            return "Set a Matrix access token in Settings"
        if not self.room_id.startswith("!"):
            return "Set the Matrix room id (!…:server) in Settings"
        return None

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.homeserver,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=20,
            transport=self._transport,
        )

    async def check(self) -> TrackerCheck:
        if problem := self.validate():
            return TrackerCheck(ok=False, message=problem)
        async with self._client() as c:
            who = await c.get("/_matrix/client/v3/account/whoami")
            if who.status_code >= 400:
                return TrackerCheck(ok=False, message=f"Token rejected: {_error(who)}")
            rooms = await c.get("/_matrix/client/v3/joined_rooms")
        user = who.json().get("user_id", "?")
        if rooms.status_code < 400 and self.room_id not in rooms.json().get("joined_rooms", []):
            return TrackerCheck(ok=False, message=f"{user} has not joined {self.room_id}")
        return TrackerCheck(ok=True, message=f"Ready: posting as {user}")

    async def post(self, text: str) -> None:
        txn = f"mnemosyne-{time.time_ns()}"
        async with self._client() as c:
            r = await c.put(
                f"/_matrix/client/v3/rooms/{quote(self.room_id, safe='')}"
                f"/send/m.room.message/{txn}",
                json={"msgtype": "m.text", "body": text},
            )
        if r.status_code >= 400:
            raise RuntimeError(f"Matrix: {_error(r)}")
