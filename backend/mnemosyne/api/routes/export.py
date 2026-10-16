"""Export endpoints."""

import asyncio
import logging
import shutil
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from ... import access
from ...export.obsidian import ObsidianExporter
from ...jobs import Job
from ...models.base import ApiModel
from ...models.session import ActionItem
from ...services import history
from ...services.vault_sync import write_note
from ..context import AppContext, get_ctx

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["export"])


class ExportResponse(ApiModel):
    path: str
    message: str


def recorder_item_matcher(ctx: AppContext, session) -> Callable[[ActionItem], bool]:
    """Is an action item the recorder's own: owned by them (mine_matcher, so "Me" counts) or
    without an owner. For a session of nobody in particular (a preview with none) the
    desktop's local_speaker_name stands in."""
    from ...services.tasks import mine_matcher, recorder_names

    names, others = recorder_names(ctx, session)
    matches = mine_matcher(names, others)

    def mine(item: ActionItem) -> bool:
        return not (item.owner or "").strip() or matches(item.owner, True)

    return mine


def task_predicate(ctx: AppContext, session) -> Callable[[ActionItem], bool] | None:
    """Which action items of a meeting are checkboxes in its note (obsidian_task_scope): the
    recorder's and the ones followed, or None for all of them."""
    if ctx.settings.obsidian_task_scope == "all":
        return None
    mine = recorder_item_matcher(ctx, session)
    return lambda item: item.follow or mine(item)


def build_exporter(ctx: AppContext, vault_path: str, session=None) -> ObsidianExporter:
    """An exporter with the settings; a session's meeting type may choose the folder."""
    from ...export.vault_index import linkable_notes
    from ...services.meeting_types import available_types, obsidian_folder
    from ...services.vault_sync import vault_annex

    st = ctx.settings
    tags = [t.strip() for t in st.obsidian_tags.split(",") if t.strip()]
    root = Path(vault_path).expanduser()
    links = st.obsidian_link_notes and bool(vault_path) and vault_path != "."  # ".": no vault set
    own = {st.obsidian_subfolder} | {t.obsidian_folder for t in available_types(st)}
    return ObsidianExporter(
        vault_path,
        obsidian_folder(st, session) if session is not None else st.obsidian_subfolder,
        tags=tags or None,
        link_people=st.obsidian_link_people,
        include_transcript=st.obsidian_include_transcript,
        redact=st.redact_exports,
        is_mine=task_predicate(ctx, session) if session is not None else None,
        known_notes=(lambda: linkable_notes(root, own)) if links else None,
        annex=vault_annex(ctx, session.id) if session is not None else None,
    )


def _resources(ctx: AppContext, session, exporter: ObsidianExporter) -> list[str]:
    """The meeting's resources as note links; files are copied (decrypted) next to the notes,
    in attachments/."""
    import re

    from ...services.assets import file_path
    from ...storage.crypto import plaintext

    out = []
    for asset in session.assets:
        if asset.kind == "link":
            out.append(f"[{asset.title}]({asset.url})")
            continue
        relative, _ = ctx.repo.asset_file(asset.id)
        if not relative:
            continue
        name = re.sub(r"[\\/:*?\"<>|#^\[\]]", "_", f"{asset.id}-{asset.filename or 'file'}")
        target = exporter.vault_path / exporter.subfolder / "attachments" / name
        try:
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                with plaintext(file_path(ctx, relative), ctx.file_key) as plain:
                    shutil.copyfile(plain, target)
            out.append(f"[[{name}|{asset.title}]]")
        except Exception:
            logger.warning("Could not copy resource %s to the vault", asset.id, exc_info=True)
    return out


def before_export(ctx: AppContext, session, vault_path: str):
    """Runs before a meeting is written to the vault, and returns the session to write (read
    again): ticks and edits to the summary made in the note in Obsidian are taken back first,
    so exporting again never undoes them (services/vault_sync.py)."""
    from ...services.vault_sync import apply_vault_edits, apply_vault_ticks

    if not session.origin:  # a meeting recorded elsewhere has its note read there
        apply_vault_ticks(ctx, session, vault_path)
        apply_vault_edits(ctx, session, vault_path)
    # Read again in any case: taking edits may also have given the summary's items their ids.
    return ctx.sessions.get_session(session.id) or session


def write_decisions(ctx: AppContext, vault_path: str):
    """Rewrite Decisions.md (obsidian_decisions_log) from the meetings the caller can read."""
    from ...export.topic_notes import write_decisions_log

    meetings = []
    for summary in ctx.repo.list_summaries():  # newest first, only what the caller may read
        if not summary.has_summary:
            continue
        s = ctx.repo.get(summary.id)
        if s is not None and s.summary_data and s.summary_data.decisions:
            meetings.append((s, s.summary_data.decisions))
    exporter = build_exporter(ctx, vault_path)
    return write_decisions_log(
        exporter.vault_path.expanduser(),
        ctx.settings.obsidian_subfolder,
        meetings,
        known_notes=exporter.known_notes,
        redact=ctx.settings.redact_exports,
    )


def export_session(ctx: AppContext, session, vault_path: str, decisions: bool = True):
    """Write the meeting note and, when enabled, the notes of the people in it and the
    decisions log (`decisions=False`: the caller writes it once after many meetings)."""
    session = before_export(ctx, session, vault_path)
    exporter = build_exporter(ctx, vault_path, session)
    path = exporter.export(
        session,
        _resources(ctx, session, exporter),
        write=lambda note_path, text: write_note(ctx, session, note_path, text),
    )
    st = ctx.settings
    if st.obsidian_people_notes:
        from ...export.people_notes import write_person_notes

        names = set(session.participants) | set(session.attendees)
        if session.summary_data:
            names |= {a.owner for a in session.summary_data.action_items if a.owner}
        try:
            write_person_notes(
                ctx.repo,
                Path(vault_path).expanduser(),
                st.obsidian_subfolder,
                names,
                (st.local_speaker_name, st.remote_speaker_name),
                redact=st.redact_exports,
                task_scope=st.obsidian_task_scope,
            )
        except Exception:
            logger.warning("Person notes failed for session %s", session.id, exc_info=True)
    if st.obsidian_daily_notes:
        from ...export.daily_note import write_daily_entry
        from ...export.obsidian import note_stem

        try:
            write_daily_entry(
                Path(vault_path).expanduser(),
                session,
                note_stem(session),
                st.local_speaker_name,
                st.obsidian_daily_folder,
                redact=st.redact_exports,
                is_mine=recorder_item_matcher(ctx, session),
            )
        except Exception:
            logger.warning("Daily note failed for session %s", session.id, exc_info=True)
    if decisions and st.obsidian_decisions_log:
        try:
            write_decisions(ctx, vault_path)
        except Exception:
            logger.warning("Decisions log failed after session %s", session.id, exc_info=True)
    return path


@router.get("/sessions/{session_id}/export/markdown")
async def export_markdown(session_id: str, ctx: AppContext = Depends(get_ctx)):
    """The note as it would be written to the vault (for preview / clipboard)."""
    session = ctx.sessions.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    history.log_access(ctx, session_id, "exported")
    return {
        "markdown": build_exporter(ctx, ctx.settings.obsidian_vault_path or ".", session).render(
            session
        )
    }


@router.post("/sessions/{session_id}/export/obsidian", response_model=ExportResponse)
async def export_to_obsidian(session_id: str, ctx: AppContext = Depends(get_ctx)):
    vault_path = ctx.settings.obsidian_vault_path
    if not vault_path:
        raise HTTPException(status_code=400, detail="Obsidian vault path not configured")

    session = ctx.sessions.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    history.log_access(ctx, session_id, "exported")

    try:
        path = await asyncio.to_thread(export_session, ctx, session, vault_path)
        return ExportResponse(path=str(path), message="Exported successfully")
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("Export failed")
        raise HTTPException(status_code=500, detail=f"Export failed: {e}") from e


@router.post("/export/obsidian/all", response_model=Job)
async def export_all_to_obsidian(ctx: AppContext = Depends(get_ctx)):
    """Write every summarized meeting the caller may change to the vault again (an
    `obsidian_export_all` job), e.g. after changing how tasks are written. Its result is
    {exported, failed, total}."""
    vault_path = ctx.settings.obsidian_vault_path
    if not vault_path:
        raise HTTPException(status_code=400, detail="Obsidian vault path not configured")
    if any(j.kind == "obsidian_export_all" for j in ctx.jobs.list(active_only=True)):
        raise HTTPException(status_code=409, detail="The meetings are already being exported")
    wanted = [
        s
        for s in ctx.repo.list_summaries()
        if s.has_summary and not s.origin and access.can_write(s.owner_id)
    ]

    async def run(job) -> dict:
        total = len(wanted)
        exported = failed = 0
        for n, summary in enumerate(wanted, start=1):
            job.update(f"Exporting {summary.name} ({n} of {total})", progress=(n - 1) / total)
            try:
                session = ctx.sessions.get_session(summary.id)
                if session is None:
                    continue
                history.log_access(ctx, summary.id, "exported")
                await asyncio.to_thread(export_session, ctx, session, vault_path, False)
                exported += 1
            except FileNotFoundError as e:  # the vault is gone: every meeting would fail
                raise ValueError(str(e)) from e
            except Exception:
                failed += 1
                logger.warning("Export of session %s failed", summary.id, exc_info=True)
        if ctx.settings.obsidian_decisions_log:
            try:
                await asyncio.to_thread(write_decisions, ctx, vault_path)
            except Exception:
                logger.warning("Decisions log failed", exc_info=True)
        job.update(f"Exported {exported} of {total}", progress=1.0)
        return {"exported": exported, "failed": failed, "total": total}

    return ctx.jobs.submit("obsidian_export_all", run)
