"""Demo mode's recipient side of sharing (phase 8, task 18): one contact, Ada Lovelace, and one
meeting she shared, so a single-backend browser test can show a meeting shared with me.

Everything is fixed and offline: Ada's key comes from a constant seed (so her person key and pid
are the same on every run), her card names one machine whose ticket points nowhere, and the
meeting is signed by her key exactly as services/shares.py signs one. The notes and the
transcript are not shared, which keeps the seed independent of the notes sidecar. Demo mode
only, never on a team server, and idempotent (an existing Ada and meeting are left alone).
Started and stopped by the demo-only routes in api/routes/demo.py, never at startup, so other
browser specs
see an empty library.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from ..models.contact import Card, CardMachine
from ..models.session import ActionItem, Chapter, SummaryData
from ..models.shared_meeting import SharedMeeting, SharedOwner, ShareParts
from . import contacts, shares
from .person import PersonKeys

if TYPE_CHECKING:
    from ..api.context import AppContext

logger = logging.getLogger(__name__)

ADA_SEED = bytes.fromhex("ada10ce0" * 8)  # 32 bytes: Ada's private key (a demo, not a secret)
ADA_NAME = "Ada Lovelace"
ADA_MACHINE = "Ada's laptop"
ADA_ENDPOINT = "ad" * 32  # no such computer: nothing answers
ADA_SECRET = "5e" * 32
CARD_AT = "2026-10-20T09:00:00+00:00"
MEETING_ID = "ada000000001"
MEETING_NAME = "Shared: Waku roadmap"
CREATED = datetime(2026, 10, 20, 14, 0)
UPDATED = datetime(2026, 10, 20, 15, 0)
SUMMARY = (
    "Ada walked through the Waku roadmap for the quarter: the store protocol comes first, "
    "light clients second."
)
DECISIONS = ["Ship the store protocol before light clients", "Review the roadmap every month"]
QUESTIONS = ["Who owns the light client benchmarks?"]
ITEMS = [  # (id, text, owner; None = the local demo user)
    ("ada0000000a1", "Write up the store protocol milestones", "Ada"),
    ("ada0000000a2", "Send Ada the benchmark numbers", None),
]
CHAPTERS = [(0.0, "Roadmap overview"), (120.0, "Light clients")]


def _ada_private():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return Ed25519PrivateKey.from_private_bytes(ADA_SEED)


def ada_person() -> str:
    return PersonKeys.public_of(ADA_SEED.hex())


def _ticket() -> str:
    raw = b"\x00" + bytes.fromhex(ADA_ENDPOINT) + b"\x00"
    return "endpoint" + base64.b32encode(raw).decode().lower().rstrip("=")


def _sign(data: bytes) -> str:
    return _ada_private().sign(data).hex()


def seed_demo(app: AppContext) -> bool:
    """Make Ada and her shared meeting. False when nothing was done (not demo mode, a team
    server, locked, or already there). Never raises."""
    from ..demo import enabled

    if not enabled() or app.settings.team_mode or app.locked:
        return False
    try:
        person = ada_person()
        row = app.repo.contact(person)
        have_contact = row is not None and not row["removed_at"]
        if have_contact and app.repo.get(MEETING_ID) is not None:
            return False
        card = Card(
            person=person,
            name=ADA_NAME,
            machines=[CardMachine(name=ADA_MACHINE, ticket=_ticket())],
            at=CARD_AT,
            sig="",
        )
        card.sig = _sign(PersonKeys.canonical(card.model_dump(exclude={"sig"})))
        if not have_contact:
            contacts.upsert_contact(app, card, ADA_SECRET)

        me = (app.settings.local_speaker_name or "").strip() or "Me"
        data = SummaryData(
            title=MEETING_NAME,
            style="meeting",
            decisions=list(DECISIONS),
            decision_at=[None] * len(DECISIONS),
            decision_ids=[f"ada0000000d{i + 1}" for i in range(len(DECISIONS))],
            open_questions=list(QUESTIONS),
            question_at=[None] * len(QUESTIONS),
            question_ids=["ada0000000e1"],
            action_items=[ActionItem(id=i, text=t, owner=owner or me) for i, t, owner in ITEMS],
            chapters=[Chapter(start=s, title=t) for s, t in CHAPTERS],
        )
        sm = SharedMeeting(
            id=MEETING_ID,
            name=MEETING_NAME,
            created_at=CREATED,
            updated_at=UPDATED,
            owner=SharedOwner(person=person, name=ADA_NAME),
            parts=ShareParts(transcript=False, notes=False),
            summary=SUMMARY,
            summary_data=data,
            attendees=["Ada", me],
            participants=["Ada", me],
            shared_with=[me],
        )
        sm.digest = shares.digest_of(sm)
        sm.sig = _sign(shares._canonical(sm, {"sig"}))
        app.repo.apply_shared(sm, person)
        return True
    except Exception:
        logger.warning("Could not seed the demo share", exc_info=True)
        return False


async def remove_demo(app: AppContext) -> None:
    """Take the seed away again: the meeting through the normal delete path, then the contact."""
    from .. import access

    with access.internally():
        app.sessions.delete_session(MEETING_ID)
    await contacts.forget_contact(app, ada_person())
