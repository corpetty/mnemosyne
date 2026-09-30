"""Kinds of meeting (a standup, a 1:1, a customer call), each with its own summary style and
instructions, Obsidian folder, local-only and auto-record. A meeting gets its type from its
title when it is named (the calendar, a rename, the summary's title), unless one was chosen;
"none" means the user chose no type.

Besides the user's own types (settings.meeting_types), a built-in pack for financial advisors
is offered when settings.advisor_meeting_types is on.
"""

from __future__ import annotations

from ..config import MeetingType, Settings
from ..models.session import Session

NONE = "none"

_NO_ADVICE = "Record what was said; add no recommendations of your own."

ADVISOR_MEETING_TYPES: list[MeetingType] = [
    MeetingType(
        name="Discovery meeting",
        match="discovery, intro meeting, introductory meeting, prospect",
        summary_style="advisory",
        instructions=(
            "A first meeting with a prospective client. Capture why they came, their goals, "
            "family and work situation, the accounts, insurance and estate documents they "
            "described, how they talk about risk, their concerns, and the agreed next steps. "
            + _NO_ADVICE
        ),
    ),
    MeetingType(
        name="Annual review",
        match="annual review, yearly review, portfolio review, semi-annual review",
        summary_style="advisory",
        instructions=(
            "An annual review with an existing client. Capture what changed since the last "
            "review (family, work, income, health, goals, risk tolerance), progress on their "
            "goals as discussed, changes the client asked for or agreed to, beneficiary and "
            "insurance updates mentioned, and the date of the next review. " + _NO_ADVICE
        ),
    ),
    MeetingType(
        name="Onboarding",
        match="onboarding, account opening, new account",
        summary_style="advisory",
        instructions=(
            "Onboarding a new client. Capture the accounts to open or transfer, beneficiaries "
            "named, documents and signatures still needed and who provides each, and what "
            "happens next and when. " + _NO_ADVICE
        ),
    ),
    MeetingType(
        name="Plan presentation",
        match="plan presentation, financial plan, plan delivery",
        summary_style="advisory",
        instructions=(
            "The advisor presents a financial plan. Capture what the advisor presented, the "
            "client's questions and reactions, what the client accepted, declined or wants to "
            "think about, and the follow-ups. " + _NO_ADVICE
        ),
    ),
    MeetingType(
        name="Service call",
        match="service call, service request, client check-in",
        summary_style="advisory",
        instructions=(
            "A short service call. Capture what the client asked for (a withdrawal, transfer, "
            "address change, a question), what the advisor said will happen and by when, and "
            "anything the client mentioned about changes in their situation. " + _NO_ADVICE
        ),
    ),
]


def available_types(settings: Settings) -> list[MeetingType]:
    """The user's types, then the advisor pack when it is on (a user type of the same name
    replaces the built-in one)."""
    own = list(settings.meeting_types)
    if not (settings.advisor_meeting_types or settings.firm_mode):  # a firm's server: on
        return own
    names = {t.name.lower() for t in own}
    return own + [t for t in ADVISOR_MEETING_TYPES if t.name.lower() not in names]


def match_type(title: str, types: list[MeetingType]) -> MeetingType | None:
    """The first type with a word or phrase that the title contains (any case)."""
    t = title.lower()
    for kind in types:
        words = [w.strip().lower() for w in kind.match.split(",") if w.strip()]
        if any(w in t for w in words):
            return kind
    return None


def session_type(settings: Settings, session: Session) -> MeetingType | None:
    """The type a meeting has (chosen, or given by its title when it was named)."""
    if not session.meeting_type or session.meeting_type == NONE:
        return None
    wanted = session.meeting_type.lower()
    return next((t for t in available_types(settings) if t.name.lower() == wanted), None)


def summary_style(settings: Settings, session: Session) -> str:
    kind = session_type(settings, session)
    return (kind.summary_style if kind else "") or settings.summary_style


def summary_instructions(settings: Settings, session: Session) -> str:
    kind = session_type(settings, session)
    extra = kind.instructions if kind else ""
    return f"{settings.summary_instructions}\n{extra}".strip()


def obsidian_folder(settings: Settings, session: Session) -> str:
    kind = session_type(settings, session)
    return (kind.obsidian_folder if kind else "") or settings.obsidian_subfolder
