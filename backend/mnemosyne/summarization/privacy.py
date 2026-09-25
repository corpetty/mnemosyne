"""Keeping meetings away from cloud LLMs: local-only meetings and redaction.

`CLOUD_PROVIDERS` are the ones that send text off this machine to a third party. Ollama and
vLLM are treated as local (they may run on another machine you control)."""

from __future__ import annotations

import re
from collections.abc import Callable

CLOUD_PROVIDERS = frozenset({"openai", "anthropic"})

LOCAL_ONLY_ERROR = (
    "This meeting is marked local-only, so it is never sent to {provider}. "
    "Use Ollama or vLLM, or turn off local-only for this meeting."
)


def is_cloud(provider: str) -> bool:
    return provider in CLOUD_PROVIDERS


_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
# 9+ digits with the usual separators; avoids timestamps like 12:30 and short numbers.
_PHONE = re.compile(r"(?<![\w:])\+?\d[\d ()./-]{7,}\d(?![\w:])")
_DATE = re.compile(r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$|^\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}$")
_GROUPED = re.compile(r"^\d{1,3}(?:[ .]\d{3})+$")  # 1 000 000, 1.000.000


def _is_phone(candidate: str) -> bool:
    """A 9 to 15 digit number that is not a date or a thousands-grouped amount."""
    digits = sum(c.isdigit() for c in candidate)
    c = candidate.strip()
    return 9 <= digits <= 15 and not _DATE.match(c) and not _GROUPED.match(c)


class Redactor:
    """Swap emails, phone numbers and known names for placeholders, and back again.

    One Redactor covers one exchange (prompt out, reply in) so the placeholders in the reply
    map to the same values."""

    def __init__(self, names: list[str]):
        full: set[str] = set()  # "Jakub Sokołowski": matched in any case
        single: set[str] = set()  # "Will", "Jakub": matched as written, so "will" stays
        for n in names:
            n = " ".join(n.split())
            if len(n) < 2:
                continue
            parts = n.split(" ")
            if len(parts) > 1:
                full.add(n)
                single.update(p for p in parts if len(p) >= 3 and p[0].isupper())
            else:
                single.add(n)

        def pattern(words: set[str], flags: int = 0):
            if not words:
                return None
            alts = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
            return re.compile(r"(?<!\w)(" + alts + r")(?!\w)", flags)

        self._full_re = pattern(full, re.IGNORECASE)
        self._single_re = pattern(single)
        self._forward: dict[str, str] = {}
        self._back: dict[str, str] = {}
        self._counts: dict[str, int] = {}

    def _placeholder(self, kind: str, value: str) -> str:
        key = f"{kind}:{value.casefold()}"
        if key not in self._forward:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            ph = f"[{kind}_{self._counts[kind]}]"
            self._forward[key] = ph
            self._back[ph] = value
        return self._forward[key]

    def redact(self, text: str) -> str:
        text = _EMAIL.sub(lambda m: self._placeholder("EMAIL", m.group(0)), text)
        text = _PHONE.sub(
            lambda m: (
                self._placeholder("PHONE", m.group(0)) if _is_phone(m.group(0)) else m.group(0)
            ),
            text,
        )
        for name_re in (self._full_re, self._single_re):
            if name_re is not None:
                text = name_re.sub(lambda m: self._placeholder("PERSON", m.group(0)), text)
        return text

    def restore(self, text: str) -> str:
        if not self._back:
            return text
        return re.sub(
            r"\[(?:EMAIL|PHONE|PERSON)_\d+\]",
            lambda m: self._back.get(m.group(0), m.group(0)),
            text,
        )


class RedactingProvider:
    """Wraps a cloud provider: every prompt is redacted, every reply restored."""

    def __init__(self, inner, names: Callable[[], list[str]]):
        self.inner = inner
        self.name = getattr(inner, "name", "")
        self._names = names

    async def list_models(self) -> list[str]:
        return await self.inner.list_models()

    async def complete(self, system_prompt: str, user_prompt: str, model: str) -> str:
        r = Redactor(self._names())
        reply = await self.inner.complete(r.redact(system_prompt), r.redact(user_prompt), model)
        return r.restore(reply)

    async def summarize(self, transcript: str, model: str, system_prompt: str) -> str:
        r = Redactor(self._names())
        reply = await self.inner.summarize(r.redact(transcript), model, r.redact(system_prompt))
        return r.restore(reply)

    def __getattr__(self, item):
        return getattr(self.inner, item)
