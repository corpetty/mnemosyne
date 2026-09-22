"""Transcript editing: correct text, reassign speakers, merge, split, delete."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ...models.session import Session
from ...models.transcript import TranscriptSegment, WordSegment
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api/sessions/{session_id}/segments", tags=["segments"])


class SegmentUpdate(BaseModel):
    text: str | None = None
    speaker: str | None = None


class SplitRequest(BaseModel):
    offset: int  # character offset in the segment text


def _apply(ctx: AppContext, session_id: str, edit) -> Session:
    try:
        session = ctx.repo.edit_segments(session_id, edit)
    except IndexError as e:
        raise HTTPException(status_code=404, detail="Segment not found") from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    ctx.bus.publish({"type": "session", "session_id": session_id, "status": session.status.value})
    return session


def _check(segments: list[TranscriptSegment], idx: int) -> None:
    if idx < 0 or idx >= len(segments):
        raise IndexError(idx)


@router.patch("/{idx}", response_model=Session)
async def update_segment(
    session_id: str, idx: int, update: SegmentUpdate, ctx: AppContext = Depends(get_ctx)
):
    def edit(segments):
        _check(segments, idx)
        seg = segments[idx]
        changes = {}
        if update.text is not None:
            text = update.text.strip()
            if not text:
                raise ValueError("Text must not be empty (delete the segment instead)")
            changes["text"] = text
            if seg.words and text != seg.text:
                changes["words"] = None  # word timings no longer match the text
        if update.speaker is not None:
            speaker = update.speaker.strip()
            if not speaker:
                raise ValueError("Speaker must not be empty")
            changes["speaker"] = speaker
        segments[idx] = seg.model_copy(update=changes)
        return segments

    return _apply(ctx, session_id, edit)


@router.delete("/{idx}", response_model=Session)
async def delete_segment(session_id: str, idx: int, ctx: AppContext = Depends(get_ctx)):
    def edit(segments):
        _check(segments, idx)
        del segments[idx]
        return segments

    return _apply(ctx, session_id, edit)


@router.post("/{idx}/merge", response_model=Session)
async def merge_with_previous(session_id: str, idx: int, ctx: AppContext = Depends(get_ctx)):
    """Merge segment idx into the one before it (keeps the earlier speaker)."""

    def edit(segments):
        _check(segments, idx)
        if idx == 0:
            raise ValueError("First segment has nothing to merge into")
        prev, cur = segments[idx - 1], segments[idx]
        words = (prev.words or []) + (cur.words or []) if (prev.words or cur.words) else None
        segments[idx - 1] = prev.model_copy(
            update={
                "text": f"{prev.text} {cur.text}".strip(),
                "end": max(prev.end, cur.end),
                "words": words or None,
            }
        )
        del segments[idx]
        return segments

    return _apply(ctx, session_id, edit)


@router.post("/{idx}/split", response_model=Session)
async def split_segment(
    session_id: str, idx: int, request: SplitRequest, ctx: AppContext = Depends(get_ctx)
):
    """Split at a character offset; times split in proportion to text length."""

    def edit(segments):
        _check(segments, idx)
        seg = segments[idx]
        head, tail = seg.text[: request.offset].strip(), seg.text[request.offset :].strip()
        if not head or not tail:
            raise ValueError("Split point must leave text on both sides")
        frac = len(head) / max(len(seg.text), 1)
        mid = seg.start + (seg.end - seg.start) * frac
        head_words: list[WordSegment] | None = None
        tail_words: list[WordSegment] | None = None
        if seg.words:
            n = len(head.split())
            head_words, tail_words = seg.words[:n] or None, seg.words[n:] or None
            if head_words:
                mid = head_words[-1].end
        segments[idx : idx + 1] = [
            seg.model_copy(update={"text": head, "end": mid, "words": head_words}),
            seg.model_copy(update={"text": tail, "start": mid, "words": tail_words}),
        ]
        return segments

    return _apply(ctx, session_id, edit)
