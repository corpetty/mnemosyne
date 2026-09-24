"""Demo mode: a canned transcriber, diarizer and LLM provider.

Enabled with MNEMOSYNE_DEMO=1 (plus transcriber = "demo", diarizer = "demo",
default_provider = "demo" in the config). Used by the browser end-to-end tests
and for trying the UI without models or an LLM server. Output is deterministic.
"""

from __future__ import annotations

import json
import os

from .models.transcript import TranscriptSegment
from .transcription.engine import DiarizationResult, SpeakerTurn

# (speaker, start, end, text)
SCRIPT = [
    ("SPEAKER_00", 0.0, 3.0, "Morning everyone, let's go through the release."),
    ("SPEAKER_01", 3.2, 7.0, "The Waku migration is code complete."),
    ("SPEAKER_00", 7.2, 10.0, "Great, then we ship the migration in October."),
    ("SPEAKER_01", 10.2, 13.5, "I will update the docs before the release."),
    ("SPEAKER_00", 13.7, 16.0, "Who owns the mobile regression?"),
    ("SPEAKER_01", 16.2, 19.0, "Nobody yet, we should decide on Friday."),
]


def enabled() -> bool:
    return os.environ.get("MNEMOSYNE_DEMO", "") not in ("", "0", "false")


class DemoTranscriber:
    name = "demo"

    def __init__(self) -> None:
        self._loaded = False

    def is_loaded(self) -> bool:
        return self._loaded

    async def load(self) -> None:
        self._loaded = True

    async def unload(self) -> None:
        self._loaded = False

    async def transcribe(self, audio_path: str, language=None, progress=None):
        if progress is not None:
            progress(1.0)
        return [
            TranscriptSegment(text=text, speaker="UNKNOWN", start=start, end=end)
            for _, start, end, text in SCRIPT
        ]


class DemoDiarizer:
    name = "demo"

    def __init__(self) -> None:
        self._loaded = False

    def is_loaded(self) -> bool:
        return self._loaded

    async def load(self) -> None:
        self._loaded = True

    async def unload(self) -> None:
        self._loaded = False

    async def diarize(self, audio_path: str, min_speakers=None, max_speakers=None, progress=None):
        if progress is not None:
            progress(1.0)
        return DiarizationResult(
            turns=[SpeakerTurn(start=s, end=e, speaker=spk) for spk, s, e, _ in SCRIPT]
        )


# Words the demo embedder treats as the same, so semantic search can be shown and tested
# ("when do we launch" finds "ship the migration").
SYNONYMS = [{"ship", "launch", "release"}, {"docs", "documentation"}, {"bug", "regression"}]

DEMO_SUMMARY = {
    "title": "Release planning",
    "summary": "The team reviewed the release. The Waku migration is code complete.",
    "topics": ["release", "waku"],
    "decisions": ["Ship the Waku migration in October"],
    "action_items": [{"text": "Update the docs before the release", "owner": "SPEAKER_01"}],
    "open_questions": ["Who owns the mobile regression?"],
    "chapters": [
        {"start": "00:00", "title": "Release status"},
        {"start": "00:13", "title": "Mobile regression owner"},
    ],
}


class DemoProvider:
    """Answers each prompt type the app sends with a fixed, well-formed reply."""

    name = "demo"

    async def list_models(self) -> list[str]:
        return ["demo-model"]

    async def complete(self, system_prompt: str, user_prompt: str, model: str) -> str:
        if "Respond with ONLY a JSON object" in system_prompt:
            return json.dumps(DEMO_SUMMARY)
        if "numbered excerpts" in system_prompt:
            return "The migration ships in October [1]."
        if "where a topic stands" in system_prompt:
            return "## Where it stands\nThe migration ships in October."
        if "follow-up" in system_prompt:
            return (
                "Subject: Release planning follow-up\n\nThanks all. We ship the Waku "
                "migration in October.\n\nNext steps:\n- Update the docs before the release"
            )
        if "## Overview" in system_prompt:
            return "## Overview\nA demo week.\n\n## Themes\n- The Waku release"
        return user_prompt

    async def summarize(self, transcript: str, model: str, system_prompt: str) -> str:
        return await self.complete(system_prompt, transcript, model)
