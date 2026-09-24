"""Answer questions across all meetings: retrieve passages, ask the LLM, cite sources."""

from __future__ import annotations

import re

from ..models.ask import Ask, Citation, Passage
from ..storage.sqlite import SessionRepository
from .summarization_service import SummarizationService

SYSTEM_PROMPT = """\
You answer questions about the user's past meetings using ONLY the numbered excerpts provided.
Rules:
- Cite every factual claim with the excerpt number in square brackets, e.g. [2] or [1][4].
- If the excerpts do not contain the answer, say so plainly; do not guess or use outside knowledge.
- Mention which meeting and roughly when, when it helps (e.g. "in the Sept 12 release sync [3]").
- Speaker labels like SPEAKER_01 are anonymous; use names only when they appear in the excerpts.
- Be concise. Use markdown (short paragraphs or bullets).
"""

NO_RESULTS = "I couldn't find anything in your meetings that matches this question."

_CITE = re.compile(r"\[(\d{1,3}(?:\s*,\s*\d{1,3})*)\]")


def _fmt_time(seconds: float) -> str:
    return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}"


def format_passages(passages: list[Passage], char_budget: int = 16000) -> tuple[str, list[Passage]]:
    """Numbered excerpt block for the prompt, trimmed to a character budget."""
    blocks: list[str] = []
    used: list[Passage] = []
    total = 0
    for p in passages:
        n = len(used) + 1
        when = p.created_at.strftime("%Y-%m-%d")
        if p.kind == "summary":
            body = p.text
            header = f'[{n}] Summary of "{p.session_name}" ({when})'
        else:
            body = "\n".join(f"{ln.speaker}: {ln.text}" for ln in p.lines)
            start = _fmt_time(p.lines[0].start) if p.lines else "00:00"
            header = f'[{n}] "{p.session_name}" ({when}) at {start}'
        block = f"{header}\n{body}"
        if used and total + len(block) > char_budget:
            break
        blocks.append(block)
        used.append(p)
        total += len(block)
    return "\n\n".join(blocks), used


def cited_numbers(answer: str) -> list[int]:
    seen: list[int] = []
    for m in _CITE.finditer(answer):
        for part in m.group(1).split(","):
            n = int(part.strip())
            if n not in seen:
                seen.append(n)
    return seen


def excerpt(p: Passage, max_len: int = 240) -> str:
    if p.kind == "summary":
        text = p.text
    else:
        focus = p.focus_line()
        lines = [ln for ln in p.lines if focus is None or ln.idx >= focus.idx]
        text = " ".join(f"{ln.speaker}: {ln.text}" for ln in lines)
    return text if len(text) <= max_len else text[: max_len - 1].rstrip() + "…"


def build_citations(answer: str, used: list[Passage]) -> list[Citation]:
    out = []
    for n in cited_numbers(answer):
        if 1 <= n <= len(used):
            p = used[n - 1]
            focus = p.focus_line()
            out.append(
                Citation(
                    n=n,
                    session_id=p.session_id,
                    session_name=p.session_name,
                    created_at=p.created_at,
                    idx=focus.idx if focus else None,
                    start=focus.start if focus else None,
                    excerpt=excerpt(p),
                )
            )
    return out


async def answer_question(
    repo: SessionRepository,
    summarizer: SummarizationService,
    question: str,
    provider_name: str,
    model: str,
    extra_instructions: str = "",
    index=None,
) -> Ask:
    from ..search.hybrid import hybrid_passages

    passages = hybrid_passages(repo, index, question)
    if not passages:
        return Ask(question=question, answer=NO_RESULTS)
    provider = summarizer.providers.get(provider_name)
    if provider is None:
        raise ValueError(
            f"Provider '{provider_name}' not available. Available: {list(summarizer.providers)}"
        )
    if not model:
        models = await provider.list_models()
        if not models:
            raise ValueError(f"No models available from provider '{provider_name}'")
        model = models[0]
    block, used = format_passages(passages)
    user = f"Question: {question}\n\nExcerpts:\n\n{block}"
    system = (
        f"{SYSTEM_PROMPT}\n{extra_instructions}".strip() if extra_instructions else SYSTEM_PROMPT
    )
    answer = (await provider.complete(system, user, model)).strip()
    return Ask(
        question=question,
        answer=answer,
        citations=build_citations(answer, used),
        provider=provider_name,
        model=model,
    )
