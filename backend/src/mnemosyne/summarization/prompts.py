"""Prompt templates for transcript summarization.

The model is asked for a JSON object with a markdown summary plus structured
fields. `parse_summary_response` is tolerant: if the model ignores the format,
its whole reply becomes the summary and the structured fields stay empty.
"""

from __future__ import annotations

import json
import re

from ..models.session import ActionItem, SummaryData

STYLES: dict[str, str] = {
    "meeting": (
        "You are a meeting summarizer for a small engineering team. Focus on what was "
        "discussed, what was decided, and who committed to what."
    ),
    "standup": (
        "You are summarizing a daily standup. For each person capture what they did, what "
        "they will do next, and any blockers. Keep it terse."
    ),
    "interview": (
        "You are summarizing an interview or user conversation. Capture the interviewee's "
        "key statements, insights, pain points and any follow-ups, staying faithful to their words."
    ),
    "lecture": (
        "You are summarizing a talk or lecture. Capture the main argument, key concepts with "
        "brief explanations, and any references or resources mentioned."
    ),
    "brainstorm": (
        "You are summarizing a brainstorming session. List every distinct idea raised, group "
        "related ones, and note which got traction or objections."
    ),
}

FORMAT_INSTRUCTIONS = """\
Respond with ONLY a JSON object, no prose before or after, with exactly these keys:
{
  "summary": "<markdown: 1-3 short paragraphs or bullet points covering the key discussion>",
  "topics": ["<3-8 short topic labels>"],
  "decisions": ["<decisions or agreements reached, one per item; empty if none>"],
  "action_items": [{"text": "<task>", "owner": "<speaker label or name, or null>"}],
  "open_questions": ["<unresolved questions or things to follow up; empty if none>"]
}
Rules:
- Use the speaker labels exactly as they appear in the transcript; do not invent names.
- Do not put action items or decisions inside the summary text; use the fields.
- If the transcript is short or trivial, keep everything proportionally brief.
- Output must be valid JSON (escape quotes and newlines inside strings).
"""


def get_system_prompt(segment_count: int, style: str = "meeting", extra: str = "") -> str:
    base = STYLES.get(style, STYLES["meeting"])
    brevity = " The transcript is short; be very brief." if segment_count < 10 else ""
    extra_block = (
        f"\nAdditional instructions from the user:\n{extra.strip()}\n" if extra.strip() else ""
    )
    return f"{base}{brevity}\n{extra_block}\n{FORMAT_INSTRUCTIONS}"


def format_transcript_for_llm(segments: list[dict]) -> str:
    lines = []
    for seg in segments:
        speaker = seg.get("speaker", "UNKNOWN")
        start = seg.get("start", 0)
        text = seg.get("text", "").strip()
        minutes = int(start // 60)
        seconds = int(start % 60)
        lines.append(f"[{minutes:02d}:{seconds:02d}] {speaker}: {text}")
    return "\n".join(lines)


_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json(text: str) -> dict | None:
    candidates = [m.group(1) for m in _FENCE.finditer(text)]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "summary" in obj:
            return obj
    return None


def _str_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for v in value:
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
        elif isinstance(v, dict):
            text = v.get("text") or v.get("item") or v.get("question") or v.get("decision")
            if isinstance(text, str) and text.strip():
                out.append(text.strip())
    return out


def _action_items(value) -> list[ActionItem]:
    out = []
    if not isinstance(value, list):
        return out
    for v in value:
        if isinstance(v, str) and v.strip():
            out.append(ActionItem(text=v.strip()))
        elif isinstance(v, dict):
            text = v.get("text") or v.get("task") or v.get("item")
            if isinstance(text, str) and text.strip():
                owner = v.get("owner") or v.get("assignee")
                owner = owner.strip() if isinstance(owner, str) and owner.strip() else None
                if owner and owner.lower() in ("null", "none", "n/a", "unknown", "unassigned"):
                    owner = None
                out.append(ActionItem(text=text.strip(), owner=owner))
    return out


def parse_summary_response(text: str, style: str = "meeting") -> tuple[str, SummaryData]:
    """Return (summary_markdown, structured). Never raises."""
    obj = _extract_json(text)
    if obj is None:
        return text.strip(), SummaryData(style=style)
    summary = obj.get("summary")
    if isinstance(summary, list):
        summary = "\n".join(f"- {s}" for s in _str_list(summary))
    if not isinstance(summary, str):
        summary = ""
    if "\\n" in summary and "\n" not in summary:
        # Some models double-escape newlines inside the JSON string.
        summary = summary.replace("\\n", "\n")
    data = SummaryData(
        style=style,
        topics=_str_list(obj.get("topics")),
        decisions=_str_list(obj.get("decisions")),
        action_items=_action_items(obj.get("action_items")),
        open_questions=_str_list(obj.get("open_questions")),
    )
    return summary.strip(), data
