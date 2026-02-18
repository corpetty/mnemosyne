"""Prompt templates for transcript summarization."""

SYSTEM_PROMPT = """\
You are a meeting summarizer. Given a transcript with speaker labels and timestamps, \
produce a clear, well-organized summary in markdown format.

Include:
- **Key Discussion Points**: Main topics discussed
- **Decisions Made**: Any decisions or agreements reached
- **Action Items**: Tasks assigned, with the responsible person if identifiable
- **Notable Quotes**: Important statements worth highlighting (optional)

Guidelines:
- Use speaker labels (e.g., SPEAKER_00) as-is; do not invent real names
- Be concise but comprehensive
- Use bullet points for readability
- If the transcript is short or trivial, keep the summary proportionally brief
"""

COMPACT_SYSTEM_PROMPT = """\
You are a meeting summarizer. Given a short transcript, produce a brief summary \
in markdown with key points and any action items. Be concise.
"""


def format_transcript_for_llm(segments: list[dict]) -> str:
    """Format transcript segments into a readable text for LLM input."""
    lines = []
    for seg in segments:
        speaker = seg.get("speaker", "UNKNOWN")
        start = seg.get("start", 0)
        text = seg.get("text", "").strip()
        minutes = int(start // 60)
        seconds = int(start % 60)
        lines.append(f"[{minutes:02d}:{seconds:02d}] {speaker}: {text}")
    return "\n".join(lines)


def get_system_prompt(segment_count: int) -> str:
    """Select appropriate prompt based on transcript length."""
    if segment_count < 10:
        return COMPACT_SYSTEM_PROMPT
    return SYSTEM_PROMPT
