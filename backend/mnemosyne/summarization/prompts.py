"""Prompt templates for transcript summarization.

The model is asked for a JSON object with a markdown summary plus structured
fields. `parse_summary_response` is tolerant: if the model ignores the format,
its whole reply becomes the summary and the structured fields stay empty.
"""

from __future__ import annotations

import json
import re

from ..models.session import ActionItem, Chapter, SummaryData

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
  "title": "<3 to 7 word title for this conversation, no quotes or trailing punctuation>",
  "summary": "<markdown: 1-3 short paragraphs or bullet points covering the key discussion>",
  "topics": ["<3-8 short topic labels>"],
  "decisions": [{"text": "<a decision or agreement reached>", "at": "<MM:SS>"}],
  "action_items": [{"text": "<task>", "owner": "<speaker label, name or null>", "at": "<MM:SS>"}],
  "open_questions": [{"text": "<an unresolved question or thing to follow up>", "at": "<MM:SS>"}],
  "chapters": [{"start": "<MM:SS of the line where it begins>", "title": "<2 to 6 words>"}]
}
Rules:
- Use the speaker labels exactly as they appear in the transcript; do not invent names.
- Do not put action items or decisions inside the summary text; use the fields.
- If the transcript is short or trivial, keep everything proportionally brief.
- Decisions, action items and open questions may be empty lists. Each "at" is the timestamp
  of the transcript line where the item comes up, exactly as it appears in brackets.
- Chapters split the conversation by topic in time order, 3 to 8 of them (1 or 2 for a short
  one); the first starts at the first line. Use timestamps exactly as they appear in brackets.
- Output must be valid JSON (escape quotes and newlines inside strings).
"""


def get_system_prompt(segment_count: int, style: str = "meeting", extra: str = "") -> str:
    base = STYLES.get(style, STYLES["meeting"])
    brevity = " The transcript is short; be very brief." if segment_count < 10 else ""
    extra_block = (
        f"\nAdditional instructions from the user:\n{extra.strip()}\n" if extra.strip() else ""
    )
    return f"{base}{brevity}\n{extra_block}\n{FORMAT_INSTRUCTIONS}"


def transcript_lines(segments: list[dict]) -> list[str]:
    lines = []
    for seg in segments:
        speaker = seg.get("speaker", "UNKNOWN")
        text = seg.get("text", "").strip()
        lines.append(f"[{mmss(seg.get('start', 0))}] {speaker}: {text}")
    return lines


def mmss(seconds: float) -> str:
    return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}"


def format_transcript_for_llm(segments: list[dict]) -> str:
    return "\n".join(transcript_lines(segments))


def split_lines(lines: list[str], max_chars: int) -> list[tuple[int, int]]:
    """[start, end) line ranges whose joined text stays under max_chars (a single longer
    line gets a range of its own)."""
    ranges: list[tuple[int, int]] = []
    start, size = 0, 0
    for i, line in enumerate(lines):
        cost = len(line) + 1
        if i > start and size + cost > max_chars:
            ranges.append((start, i))
            start, size = i, 0
        size += cost
    if start < len(lines):
        ranges.append((start, len(lines)))
    return ranges


def partial_instructions(part: int, total: int, start: str, end: str) -> str:
    return (
        f"\nThis transcript is part {part} of {total} of a longer conversation "
        f"({start} to {end}). Summarize only this part; the parts are merged afterwards. "
        "Chapters must use timestamps from this part.\n"
    )


REDUCE_PROMPT = """\
You merge partial notes of one long conversation into a single summary. The input is a JSON
list of parts in time order, each with its own summary, topics, decisions, action items, open
questions and chapters (timestamps in MM:SS or H:MM:SS are from the full conversation).
Write one coherent summary of the whole conversation, remove duplicates across parts, keep
every distinct decision and action item, drop open questions that a later part resolved,
and merge chapters into 3 to 12 for the whole conversation, keeping their original timestamps.
Keep each item's "at" timestamp (the earliest one when you merge duplicates).
"""


def reduce_system_prompt(style: str = "meeting", extra: str = "") -> str:
    base = STYLES.get(style, STYLES["meeting"])
    extra_block = (
        f"\nAdditional instructions from the user:\n{extra.strip()}\n" if extra.strip() else ""
    )
    return f"{base}\n{REDUCE_PROMPT}{extra_block}\n{FORMAT_INSTRUCTIONS}"


def part_payload(part: int, start: str, end: str, summary: str, data: SummaryData) -> dict:
    return {
        "part": part,
        "from": start,
        "to": end,
        "summary": summary,
        "topics": data.topics,
        "decisions": _with_at(data.decisions, data.decision_at),
        "action_items": [
            {"text": a.text, "owner": a.owner, "at": _mmss_or_none(a.at)} for a in data.action_items
        ],
        "open_questions": _with_at(data.open_questions, data.question_at),
        "chapters": [{"start": mmss(c.start), "title": c.title} for c in data.chapters],
    }


def _mmss_or_none(seconds: float | None) -> str | None:
    return mmss(seconds) if seconds is not None else None


def _with_at(texts: list[str], ats: list[float | None]) -> list[dict]:
    return [
        {"text": t, "at": _mmss_or_none(ats[i] if i < len(ats) else None)}
        for i, t in enumerate(texts)
    ]


_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def extract_json(text: str) -> dict | None:
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


def _texts_and_times(value) -> tuple[list[str], list[float | None]]:
    """Items given as strings or {"text", "at"} objects: their texts and times (seconds)."""
    texts: list[str] = []
    times: list[float | None] = []
    if not isinstance(value, list):
        return texts, times
    for v in value:
        text = _str_list([v])
        if text:
            texts.append(text[0])
            times.append(parse_seconds(v.get("at", v.get("time"))) if isinstance(v, dict) else None)
    return texts, times


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
                at = parse_seconds(v.get("at", v.get("time")))
                out.append(ActionItem(text=text.strip(), owner=owner, at=at))
    return out


_TS = re.compile(r"^\s*(?:(\d+):)?(\d{1,3}):(\d{2})(?:\.\d+)?\s*$")


def parse_seconds(value) -> float | None:
    """Seconds from a number or an [H:]MM:SS string (None when neither)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return float(value)
    if isinstance(value, str):
        m = _TS.match(value.strip("[] "))
        if m:
            h, mnt, sec = int(m.group(1) or 0), int(m.group(2)), int(m.group(3))
            return float(h * 3600 + mnt * 60 + sec)
    return None


def _chapters(value) -> list[Chapter]:
    out: dict[float, Chapter] = {}
    if not isinstance(value, list):
        return []
    for v in value:
        if not isinstance(v, dict):
            continue
        start = parse_seconds(v.get("start", v.get("time")))
        title = v.get("title") or v.get("name")
        if start is None or not isinstance(title, str) or not title.strip():
            continue
        out.setdefault(start, Chapter(start=start, title=" ".join(title.split())[:80]))
    return sorted(out.values(), key=lambda c: c.start)


def snap_chapters(chapters: list[Chapter], starts: list[float]) -> list[Chapter]:
    """Move each chapter to the nearest line start; drop chapters past the end and
    duplicates that land on the same line."""
    if not starts:
        return []
    out: dict[float, Chapter] = {}
    last = max(starts)
    for c in chapters:
        if c.start > last + 30:
            continue
        nearest = min(starts, key=lambda s: abs(s - c.start))
        out.setdefault(nearest, Chapter(start=nearest, title=c.title))
    return sorted(out.values(), key=lambda c: c.start)


def snap_item_times(data: SummaryData, starts: list[float], tolerance: float = 20.0) -> None:
    """Snap the "at" of decisions, action items and open questions to the nearest line start;
    drop times that are not within `tolerance` seconds of any line (in place)."""

    def snap(t: float | None) -> float | None:
        if t is None or not starts:
            return None
        nearest = min(starts, key=lambda s: abs(s - t))
        return nearest if abs(nearest - t) <= tolerance else None

    data.decision_at = [snap(t) for t in data.decision_at]
    data.question_at = [snap(t) for t in data.question_at]
    for item in data.action_items:
        item.at = snap(item.at)


def parse_summary_response(text: str, style: str = "meeting") -> tuple[str, SummaryData]:
    """Return (summary_markdown, structured). Never raises."""
    obj = extract_json(text)
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
    title = obj.get("title")
    title = " ".join(title.split()).strip(" \"'.")[:80] if isinstance(title, str) else ""
    decisions, decision_at = _texts_and_times(obj.get("decisions"))
    questions, question_at = _texts_and_times(obj.get("open_questions"))
    data = SummaryData(
        title=title,
        style=style,
        topics=_str_list(obj.get("topics")),
        decisions=decisions,
        decision_at=decision_at,
        action_items=_action_items(obj.get("action_items")),
        open_questions=questions,
        question_at=question_at,
        chapters=_chapters(obj.get("chapters")),
    )
    return summary.strip(), data
