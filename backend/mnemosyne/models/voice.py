"""Narration voices and narrations (services/voices.py, services/narration.py)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from .base import ApiModel

NarrationSource = Literal["summary", "notes", "digest", "followup", "text"]


class VoiceClip(ApiModel):
    idx: int
    text: str  # what is said in it (the transcriber's, corrected by the person)
    seconds: float
    quality: float  # 0..1 from level, clipping and noise
    accepted: bool  # passed the checks and is the same person as the rest
    problem: str = ""  # why it was not accepted
    created_at: datetime


class Voice(ApiModel):
    id: str
    owner_id: str  # "" on a computer without sign-in
    name: str
    language: str
    clips: list[VoiceClip] = Field(default_factory=list)
    seconds: float  # of accepted clips
    consented: bool  # the live consent check passed
    consent_at: datetime | None = None
    ready: bool  # consented and enough accepted audio: can narrate
    needs: str = ""  # what is missing before it is ready, in words
    created_at: datetime
    updated_at: datetime


class ConsentChallenge(ApiModel):
    """What to read aloud, live, to prove the voice is yours. Valid for `expires_in` seconds."""

    text: str
    words: list[str]  # the random words in it, which must be heard
    expires_in: int


class ConsentResult(ApiModel):
    passed: bool
    heard: str  # what the transcriber heard
    text_match: float  # 0..1 of the statement's words heard
    voice_match: float  # cosine against the voice's clips
    problem: str = ""
    voice: Voice


class NarrationChunk(ApiModel):
    text: str
    start: float  # seconds into the narration
    end: float
    flagged: bool = False  # the round trip heard something else; re-synthesized once


class Narration(ApiModel):
    id: str
    voice_id: str | None
    voice_name: str
    user_id: str
    source_kind: NarrationSource
    source_id: str | None = None
    title: str
    engine: str
    model: str
    seconds: float
    chunks: list[NarrationChunk] = Field(default_factory=list)
    watermarked: bool
    created_at: datetime


class NarrationRequest(ApiModel):
    voice_id: str
    source_kind: NarrationSource
    source_id: str | None = None  # a meeting's id (summary, notes, followup) or a digest's
    text: str = ""  # for source_kind "text"
    title: str = ""
    check: bool = True  # transcribe the result and redo chunks that came out wrong


class VoiceEngine(ApiModel):
    """The narration engine on this computer, for Settings -> Voices."""

    engine: str  # kitten | demo | none
    installed: bool
    installable: bool  # uv is here (the install can run)
    install_bytes: int  # on disk now
    license_name: str
    license_url: str
    attribution: str
    can_check_voices: bool  # a speaker embedder runs here (same-person and consent checks)
