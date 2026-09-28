"""Learn from transcript corrections: suggest glossary entries, and add them."""

from fastapi import APIRouter, Depends, HTTPException

from ...config import save_settings
from ...models.base import ApiModel
from ...transcription.glossary import (
    add_correction,
    apply_corrections,
    parse_glossary,
    suggest_corrections,
)
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api/glossary", tags=["glossary"])


class SuggestRequest(ApiModel):
    before: str  # a transcript line as it was
    after: str  # and as the user corrected it


class GlossarySuggestion(ApiModel):
    heard: str  # what the recognizer wrote
    correct: str  # what it should be


class AddCorrection(ApiModel):
    heard: str
    correct: str
    session_id: str | None = None  # also fix the rest of this meeting's transcript


class AddCorrectionResult(ApiModel):
    glossary: str
    fixed_lines: int  # lines changed in the meeting given


@router.post("/suggest", response_model=list[GlossarySuggestion])
async def suggest(request: SuggestRequest, ctx: AppContext = Depends(get_ctx)):
    glossary = parse_glossary(ctx.settings.glossary)
    return [
        GlossarySuggestion(heard=h, correct=c)
        for h, c in suggest_corrections(request.before, request.after, glossary)
    ]


@router.post("/corrections", response_model=AddCorrectionResult)
async def add(request: AddCorrection, ctx: AppContext = Depends(get_ctx)):
    """Add `heard -> correct` to the glossary (every later transcript gets it right) and,
    with a session, fix the lines of that meeting that still have it wrong."""
    heard, correct = request.heard.strip(), request.correct.strip()
    if not heard or not correct or "->" in heard or "->" in correct or "\n" in heard + correct:
        raise HTTPException(status_code=400, detail="Give the misheard words and the right ones")
    new = ctx.settings.model_copy(
        update={"glossary": add_correction(ctx.settings.glossary, heard, correct)}
    )
    save_settings(new)
    await ctx.apply_settings(new)

    fixed = 0
    if request.session_id:
        one = parse_glossary(f"{heard} -> {correct}")

        def edit(segments):
            nonlocal fixed
            segments, fixed = apply_corrections(segments, one)
            return segments

        if ctx.repo.edit_segments(request.session_id, edit) is None:
            raise HTTPException(status_code=404, detail="Session not found")
        if fixed:
            ctx.bus.publish(
                {"type": "session", "session_id": request.session_id, "status": "completed"}
            )
    return AddCorrectionResult(glossary=new.glossary, fixed_lines=fixed)
