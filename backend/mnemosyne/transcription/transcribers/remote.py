"""Transcriber for any OpenAI-compatible `/audio/transcriptions` server.

Works with OpenAI, earheart-stt, speaches, whisper.cpp server, vLLM, and others.
Asks for `verbose_json` with segment and word timestamps; servers that only
return `text` still work but produce a single untimed segment.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from ...models.transcript import TranscriptSegment, WordSegment

logger = logging.getLogger(__name__)


class RemoteTranscriber:
    name = "remote"

    def __init__(
        self,
        base_url: str,
        model: str = "whisper-1",
        api_key: str = "",
        timeout: float = 600.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self._transport = transport
        self._loaded = False

    def is_loaded(self) -> bool:
        return self._loaded

    async def load(self) -> None:
        self._loaded = True

    async def unload(self) -> None:
        self._loaded = False

    async def transcribe(
        self, audio_path: str, language: str | None = None, progress=None
    ) -> list[TranscriptSegment]:
        path = Path(audio_path)
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        data: dict[str, str | list[str]] = {
            "model": self.model,
            "response_format": "verbose_json",
            "timestamp_granularities[]": ["segment", "word"],
        }
        if language:
            data["language"] = language

        async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client:
            with path.open("rb") as f:
                resp = await client.post(
                    f"{self.base_url}/audio/transcriptions",
                    headers=headers,
                    data=data,
                    files={"file": (path.name, f, "application/octet-stream")},
                )
            resp.raise_for_status()
            body = resp.json()
        return parse_verbose_json(body)


def parse_verbose_json(body: dict) -> list[TranscriptSegment]:
    segments = body.get("segments") or []
    words = body.get("words") or []
    if not segments:
        text = (body.get("text") or "").strip()
        if not text:
            return []
        end = float(body.get("duration") or 0.0)
        return [TranscriptSegment(text=text, speaker="UNKNOWN", start=0.0, end=end)]

    out = []
    for s in segments:
        start, end = float(s.get("start", 0.0)), float(s.get("end", 0.0))
        seg_words = None
        if words:
            inside = [w for w in words if start <= float(w.get("start", -1)) < end]
            if inside:
                seg_words = [
                    WordSegment(
                        word=str(w.get("word", "")).strip(),
                        start=float(w.get("start", start)),
                        end=float(w.get("end", end)),
                        score=float(w.get("probability", w.get("score", 0.0)) or 0.0),
                    )
                    for w in inside
                ]
        text = (s.get("text") or "").strip()
        if text:
            out.append(
                TranscriptSegment(
                    text=text, speaker="UNKNOWN", start=start, end=end, words=seg_words
                )
            )
    return out
