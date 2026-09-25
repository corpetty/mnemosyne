"""Summarization service managing providers and model selection."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

from ..config import Settings
from ..models.session import ActionItem, SummaryData
from ..summarization.anthropic_provider import AnthropicProvider
from ..summarization.ollama import OllamaProvider
from ..summarization.openai_provider import OpenAIProvider
from ..summarization.prompts import (
    extract_json,
    get_system_prompt,
    mmss,
    parse_summary_response,
    part_payload,
    partial_instructions,
    reduce_system_prompt,
    snap_chapters,
    split_lines,
    transcript_lines,
)
from ..summarization.provider import SummarizationProvider
from ..summarization.vllm import VLLMProvider

logger = logging.getLogger(__name__)


class SummarizationService:
    chunk_chars = 0  # 0: never split (see Settings.summary_chunk_chars)
    # Names to hide from cloud providers when cloud_redaction is on; set by AppContext.
    name_source: Callable[[], list[str]] | None = None

    def __init__(self, settings: Settings | None = None):
        self.providers: dict[str, SummarizationProvider] = {}
        self.chunk_chars = settings.summary_chunk_chars if settings is not None else 0
        if settings is not None:
            self._init_providers(settings)

    def _init_providers(self, settings: Settings) -> None:
        self.providers["ollama"] = OllamaProvider(base_url=settings.ollama_url)
        self.providers["vllm"] = VLLMProvider(base_url=settings.vllm_url)
        if settings.openai_api_key:
            self.providers["openai"] = OpenAIProvider(api_key=settings.openai_api_key)
        if settings.anthropic_api_key:
            self.providers["anthropic"] = AnthropicProvider(api_key=settings.anthropic_api_key)
        if settings.cloud_redaction:
            from ..summarization.privacy import CLOUD_PROVIDERS, RedactingProvider

            for name in CLOUD_PROVIDERS & set(self.providers):
                self.providers[name] = RedactingProvider(
                    self.providers[name], lambda: self.name_source() if self.name_source else []
                )
        from .. import demo

        if demo.enabled():
            self.providers = {"demo": demo.DemoProvider()}

    async def list_all_models(self) -> list[dict]:
        results = []
        for name, provider in self.providers.items():
            models = await provider.list_models()
            results.append({"provider": name, "models": models})
        return results

    async def resolve_model(self, provider_name: str, model: str = "") -> str:
        """The model to use: `model` itself, or the provider's first one."""
        provider = self.providers.get(provider_name)
        if provider is None:
            raise ValueError(f"Provider '{provider_name}' not available")
        if model:
            return model
        models = await provider.list_models()
        if not models:
            raise ValueError(f"No models available from provider '{provider_name}'")
        return models[0]

    async def complete(self, system: str, user: str, provider_name: str, model: str = "") -> str:
        """One chat turn with a configured provider (first model if none given)."""
        model = await self.resolve_model(provider_name, model)
        return await self.providers[provider_name].complete(system, user, model)

    async def summarize(
        self,
        segments: list[dict],
        provider_name: str = "ollama",
        model: str = "",
        style: str = "meeting",
        instructions: str = "",
        on_progress: Callable[[str], None] | None = None,
    ) -> dict:
        provider = self.providers.get(provider_name)
        if provider is None:
            available = list(self.providers.keys())
            raise ValueError(f"Provider '{provider_name}' not available. Available: {available}")

        if not model:
            models = await provider.list_models()
            if not models:
                raise ValueError(f"No models available from provider '{provider_name}'")
            model = models[0]

        lines = transcript_lines(segments)
        starts = [float(seg["start"]) for seg in segments]
        ranges = (
            split_lines(lines, self.chunk_chars)
            if self.chunk_chars and sum(len(x) + 1 for x in lines) > self.chunk_chars
            else [(0, len(lines))]
        )
        logger.info(
            "Summarizing with %s/%s style=%s (%d segments, %d part(s))",
            provider_name,
            model,
            style,
            len(segments),
            len(ranges),
        )
        if len(ranges) == 1:
            system_prompt = get_system_prompt(len(segments), style=style, extra=instructions)
            raw = await provider.summarize("\n".join(lines), model, system_prompt)
            summary, data = parse_summary_response(raw, style=style)
        else:
            summary, data = await self._summarize_in_parts(
                provider, model, segments, lines, ranges, style, instructions, on_progress
            )
        data.chapters = snap_chapters(data.chapters, starts)
        data.provider, data.model = provider_name, model
        return {"summary": summary, "data": data, "provider": provider_name, "model": model}

    async def _summarize_in_parts(
        self, provider, model, segments, lines, ranges, style, instructions, on_progress
    ) -> tuple[str, SummaryData]:
        """Map: summarize each part on its own. Reduce: merge the partial JSONs."""
        parts = []
        total = len(ranges)
        for n, (a, b) in enumerate(ranges, start=1):
            if on_progress:
                on_progress(f"Summarizing part {n} of {total}")
            start, end = mmss(segments[a]["start"]), mmss(segments[b - 1]["end"])
            system = get_system_prompt(
                b - a,
                style=style,
                extra=f"{instructions}\n{partial_instructions(n, total, start, end)}",
            )
            raw = await provider.summarize("\n".join(lines[a:b]), model, system)
            summary, data = parse_summary_response(raw, style=style)
            parts.append(part_payload(n, start, end, summary, data))
        return await self._merge(provider, model, parts, style, instructions, on_progress)

    async def _merge(
        self, provider, model, parts, style, instructions, on_progress
    ) -> tuple[str, SummaryData]:
        """Merge partial notes into one summary. When the notes themselves exceed the
        budget (very long meetings), merge them in groups first, then merge the groups."""
        groups = _group_parts(parts, self.chunk_chars) if self.chunk_chars else [parts]
        if len(groups) > 1:
            merged = []
            for n, group in enumerate(groups, start=1):
                if len(group) == 1:
                    merged.append({**group[0], "part": n})
                    continue
                summary, data = await self._reduce_once(
                    provider, model, group, style, instructions, on_progress
                )
                merged.append(part_payload(n, group[0]["from"], group[-1]["to"], summary, data))
            return await self._merge(provider, model, merged, style, instructions, on_progress)
        return await self._reduce_once(provider, model, parts, style, instructions, on_progress)

    async def _reduce_once(
        self, provider, model, parts, style, instructions, on_progress
    ) -> tuple[str, SummaryData]:
        if on_progress:
            on_progress(f"Merging {len(parts)} parts")
        raw = await provider.complete(
            reduce_system_prompt(style, instructions),
            json.dumps(parts, ensure_ascii=False, indent=1),
            model,
        )
        summary, data = parse_summary_response(raw, style=style)
        if extract_json(raw) is None or not summary:
            logger.warning("Merge step returned no usable JSON; merging parts directly")
            return merge_parts(parts, style)
        if not data.chapters:
            data.chapters = merge_parts(parts, style)[1].chapters
        return summary, data


def _group_parts(parts: list[dict], budget: int) -> list[list[dict]]:
    """Consecutive groups whose JSON stays under `budget`, each with at least two parts
    when possible, so every round of merging shrinks the list."""
    groups: list[list[dict]] = [[]]
    size = 0
    for p in parts:
        cost = len(json.dumps(p, ensure_ascii=False, indent=1))
        if groups[-1] and size + cost > budget and len(groups[-1]) >= 2:
            groups.append([])
            size = 0
        groups[-1].append(p)
        size += cost
    return groups


def merge_parts(parts: list[dict], style: str) -> tuple[str, SummaryData]:
    """Deterministic fallback: concatenate the partial notes, dropping duplicates."""
    from .tasks import same_item

    def unique(items: list[str]) -> list[str]:
        out: list[str] = []
        for x in items:
            if not any(same_item(x, y) for y in out):
                out.append(x)
        return out

    summary = "\n\n".join(f"**{p['from']}–{p['to']}.** {p['summary']}" for p in parts)
    actions: list[ActionItem] = []
    for p in parts:
        for a in p["action_items"]:
            if not any(same_item(a["text"], b.text) for b in actions):
                actions.append(ActionItem(text=a["text"], owner=a.get("owner")))
    raw_chapters = json.dumps(
        {"summary": "x", "chapters": [c for p in parts for c in p["chapters"]]}
    )
    return summary, SummaryData(
        style=style,
        topics=unique([t for p in parts for t in p["topics"]])[:8],
        decisions=unique([d for p in parts for d in p["decisions"]]),
        action_items=actions,
        open_questions=unique([q for p in parts for q in p["open_questions"]]),
        chapters=parse_summary_response(raw_chapters)[1].chapters,
    )
