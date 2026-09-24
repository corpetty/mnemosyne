"""User glossary: terms and names the recognisers get wrong.

Format (one entry per line, `#` starts a comment):

    Waku                      a term to spell exactly like this
    Nimbus
    walk you -> Waku          an explicit correction (case-insensitive, whole words)
    corey petty -> Corey Petty

Terms are hints (WhisperX prompt, LLM correction, summaries). Corrections are applied to
every final transcript.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from ..models.transcript import TranscriptSegment

logger = logging.getLogger(__name__)


@dataclass
class Glossary:
    terms: list[str] = field(default_factory=list)
    corrections: list[tuple[re.Pattern, str]] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.terms and not self.corrections


def parse_glossary(text: str) -> Glossary:
    g = Glossary()
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "->" in line:
            wrong, right = (part.strip() for part in line.split("->", 1))
            if wrong and right:
                pattern = re.compile(r"(?<!\w)" + re.escape(wrong) + r"(?!\w)", re.IGNORECASE)
                g.corrections.append((pattern, right))
                if right not in g.terms:
                    g.terms.append(right)
        elif line not in g.terms:
            g.terms.append(line)
    return g


def apply_corrections(
    segments: list[TranscriptSegment], glossary: Glossary
) -> tuple[list[TranscriptSegment], int]:
    """Apply explicit corrections to segment text. Returns (segments, lines changed)."""
    if not glossary.corrections:
        return segments, 0
    out, changed = [], 0
    for seg in segments:
        text = seg.text
        for pattern, right in glossary.corrections:
            text = pattern.sub(right, text)
        if text != seg.text:
            changed += 1
            out.append(seg.model_copy(update={"text": text}))
        else:
            out.append(seg)
    return out, changed


def initial_prompt(glossary: Glossary, max_chars: int = 600) -> str | None:
    """Whisper conditioning text: a short sentence naming the terms (Whisper reads it as
    preceding context, which biases spelling)."""
    if not glossary.terms:
        return None
    prompt = "Glossary: " + ", ".join(glossary.terms) + "."
    return prompt[:max_chars]


def glossary_instructions(glossary: Glossary) -> str:
    if not glossary.terms:
        return ""
    return "Spell these names and terms exactly as written: " + ", ".join(glossary.terms) + "."


LLM_SYSTEM = """\
You fix speech-recognition mistakes in meeting transcript lines, using the user's glossary.
Only fix words that are clearly a misrecognition of a glossary name or term (wrong spelling,
split or merged words, sound-alikes). Never rephrase, summarize, add punctuation style changes,
or fix grammar. If a line needs no fix, leave it out.
Respond with ONLY a JSON array of objects {"i": <line number>, "text": "<corrected line>"}.
Return [] when nothing needs fixing.
"""


def _parse_fixes(reply: str) -> list[dict]:
    start, end = reply.find("["), reply.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(reply[start : end + 1])
    except json.JSONDecodeError:
        return []
    return [d for d in data if isinstance(d, dict) and isinstance(d.get("i"), int)]


async def llm_correct(
    segments: list[TranscriptSegment],
    glossary: Glossary,
    complete,
    batch_size: int = 40,
) -> tuple[list[TranscriptSegment], int]:
    """Ask an LLM (`complete(system, user) -> str`) to fix glossary terms, in batches.
    Only lines that still share most of their words with the original are accepted, so a
    model that rewrites a line cannot slip it through."""
    if not glossary.terms or not segments:
        return segments, 0
    out = list(segments)
    changed = 0
    terms = "\n".join(f"- {t}" for t in glossary.terms)
    for start in range(0, len(out), batch_size):
        batch = out[start : start + batch_size]
        lines = "\n".join(f"{start + k}: {s.text}" for k, s in enumerate(batch))
        user = f"Glossary:\n{terms}\n\nTranscript lines:\n{lines}"
        try:
            reply = await complete(LLM_SYSTEM, user)
        except Exception:
            logger.warning("Glossary LLM correction failed for a batch", exc_info=True)
            continue
        for fix in _parse_fixes(reply):
            i, text = fix["i"], str(fix.get("text", "")).strip()
            if not (start <= i < start + len(batch)) or not text or text == out[i].text:
                continue
            if _similar_enough(out[i].text, text):
                out[i] = out[i].model_copy(update={"text": text})
                changed += 1
    return out, changed


def _similar_enough(before: str, after: str) -> bool:
    a = re.findall(r"\w+", before.lower())
    b = re.findall(r"\w+", after.lower())
    if not a or not b:
        return False
    common = len(set(a) & set(b))
    return common / max(len(set(a)), 1) >= 0.5 and 0.5 <= len(b) / len(a) <= 1.6
