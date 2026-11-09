"""Export endpoints."""

import asyncio
import logging
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from ... import access
from ...export.obsidian import ObsidianExporter
from ...jobs import Job
from ...models.base import ApiModel
from ...models.session import ActionItem
from ...services import history, host_scope, shared_with_me
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


def continued_notes(ctx: AppContext, session) -> dict[str, str]:
    """Action item id -> the note of the later meeting that mentions the same task (the head
    of its task object, services/task_links.py), for the items of `session` (the summary's and
    the ones typed in its notes) that another meeting continues. {} when none; never raises."""
    from ...export.obsidian import stem_of
    from ...services import task_links

    data = session.summary_data
    items = [*(data.action_items if data is not None else []), *session.notes_items]
    if not any(a.task_id and a.id for a in items):
        return {}
    try:
        objects = task_links.index(ctx.repo, ctx.repo.visible_ids())
        out: dict[str, str] = {}
        for item in items:
            obj = objects.get(item.task_id) if item.task_id and item.id else None
            if obj is None or not any(m.session_id == session.id for m in obj.mentions):
                continue
            head = obj.mentions[0]
            if head.session_id != session.id:
                out[item.id] = stem_of(head.session_name, head.created_at)
        return out
    except Exception:
        logger.warning("Could not find the later mentions of tasks of %s", session.id,
                       exc_info=True)  # fmt: skip
        return {}


def build_exporter(ctx: AppContext, vault_path: str, session=None) -> ObsidianExporter:
    """An exporter with the settings; a session's meeting type may choose the folder."""
    from ...export.vault_index import linkable_notes
    from ...services.meeting_types import available_types, obsidian_folder
    from ...services.notes_chips import exported_meeting_note
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
        continued=continued_notes(ctx, session) if session is not None else None,
        meeting_note=lambda mid: exported_meeting_note(ctx.repo, mid),
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


_ATTACHMENT_CHARS = re.compile(r"[\\/:*?\"<>|#^\[\]]")


def _tag(sha: str) -> str:
    """8 hex digits of `sha` for a file name: the first 8 that are not all digits, which
    `redact_exports` would read as an account number and rewrite in the embed."""
    return next((sha[i : i + 8] for i in range(0, 64, 8) if not sha[i : i + 8].isdigit()), sha[:8])


def _images(
    ctx: AppContext, session, exporter: ObsidianExporter
) -> Callable[[str, str], str | None]:
    """The images of the meeting's notes, written (decrypted) to
    `<vault>/<folder>/attachments/<note stem>-<sha8>.<ext>` when missing or of another size, and
    the callback `(sha, ext) -> file name` for the note (None: that image's bytes are not here,
    so it renders as `[image: caption]`). Files of this stem that look like ours
    (`<stem>-<8 hex>.webp|jpeg`) and that the notes no longer use are removed; nothing else in
    attachments/ is touched, and no other note's file."""
    from ...export.obsidian import note_stem
    from ...services import notes_media

    root = exporter.vault_path
    if not root.is_dir():  # no vault: the export itself fails with its own message
        return lambda sha, ext: None
    folder = root / exporter.subfolder / "attachments"
    stem = _ATTACHMENT_CHARS.sub("_", note_stem(session))
    names: dict[str, str] = {}  # sha -> file name, for every image the notes use
    for value in notes_media.values_in(session.notes):
        sha, ext = notes_media.parse_value(value)[:2]
        name = f"{stem}-{_tag(sha)}.{ext}"
        if sha not in names and name not in names.values():  # a clash of 8 hex digits: first wins
            names[sha] = name
    available: dict[str, str] = {}
    for sha, name in names.items():
        target = folder / name
        try:
            data = notes_media.read(ctx, session.id, sha)
            if not (target.is_file() and target.stat().st_size == len(data)):
                folder.mkdir(parents=True, exist_ok=True)
                part = target.with_name(target.name + ".part")
                part.write_bytes(data)
                os.replace(part, target)
            available[sha] = name
        except FileNotFoundError:
            logger.debug("Image %s of %s is not on this computer", sha, session.id)
        except Exception:
            logger.warning("Could not copy image %s to the vault", sha, exc_info=True)
    if folder.is_dir():
        ours = re.compile(re.escape(stem) + r"-[0-9a-f]{8}\.(webp|jpeg)")
        try:
            for file in folder.iterdir():
                if ours.fullmatch(file.name) and file.name not in names.values():
                    if file.is_file() and not file.is_symlink():
                        try:
                            file.unlink()
                        except OSError:
                            logger.warning("Could not remove %s", file, exc_info=True)
        except OSError:
            logger.warning("Could not list %s", folder, exc_info=True)
    return lambda sha, ext: available.get(sha)


def before_export(ctx: AppContext, session, vault_path: str):
    """Runs before a meeting is written to the vault, and returns the session to write (read
    again): ticks and edits to the summary made in the note in Obsidian are taken back first,
    so exporting again never undoes them (services/vault_sync.py)."""
    from ...services.shared_notes import fold_blocking
    from ...services.vault_sync import apply_vault_edits, apply_vault_ticks

    fold_blocking(ctx, session.id)  # live notes typed so far (a no-op when all are folded)
    # A meeting recorded elsewhere has its note read there; one shared with me is the owner's
    # (its note is only written here, never read back).
    if not session.origin and not session.shared_from:
        apply_vault_ticks(ctx, session, vault_path)
        apply_vault_edits(ctx, session, vault_path)
    # Read again in any case: taking edits may also have given the summary's items their ids.
    return ctx.sessions.get_session(session.id) or session


def write_decisions(ctx: AppContext, vault_path: str):
    """Rewrite Decisions.md (obsidian_decisions_log) from the meetings the caller can read
    (and that may reach the vault: never a team member's, services/host_scope.py)."""
    with host_scope.vault_only(ctx):
        return _write_decisions(ctx, vault_path)


def _write_decisions(ctx: AppContext, vault_path: str):
    from ...export.topic_notes import write_decisions_log

    meetings = []
    shared = ctx.settings.shared_in_obsidian
    for summary in ctx.repo.list_summaries():  # newest first, only what the caller may read
        if not summary.has_summary or (summary.shared_from and not shared):
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
        index=_decision_index(ctx),
    )


def _decision_index(ctx: AppContext):
    """The decision objects the caller can read (services/decision_links.py), or None when
    they cannot be worked out: the log is then written without superseded chains."""
    from ...services import decision_links

    try:
        return decision_links.index(ctx.repo, ctx.repo.visible_ids())
    except Exception:
        logger.warning("Could not read the decision links", exc_info=True)
        return None


def export_session(ctx: AppContext, session, vault_path: str, decisions: bool = True):
    """Write the meeting note and, when enabled, the notes of the people in it and the
    decisions log (`decisions=False`: the caller writes it once after many meetings). A team
    member's meeting is refused (Forbidden) on a computer with a team owner, and everything
    read for the other notes is held to what may reach the vault (services/host_scope.py)."""
    if not host_scope.vault_allows(ctx, session):
        raise access.Forbidden(host_scope.NOT_IN_VAULT)
    with host_scope.vault_only(ctx):
        return _export_session(ctx, session, vault_path, decisions)


def _export_session(ctx: AppContext, session, vault_path: str, decisions: bool):
    session = before_export(ctx, session, vault_path)
    exporter = build_exporter(ctx, vault_path, session)
    exporter.image_file = _images(ctx, session, exporter)
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
                skip=shared_with_me.excluded(ctx, "shared_in_obsidian"),
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
    host_scope.require_vault_owner(ctx)
    vault_path = ctx.settings.obsidian_vault_path
    if not vault_path:
        raise HTTPException(status_code=400, detail="Obsidian vault path not configured")

    session = ctx.sessions.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if not host_scope.vault_allows(ctx, session):
        raise HTTPException(status_code=403, detail=host_scope.NOT_IN_VAULT)
    if session.shared_from and not ctx.settings.shared_in_obsidian:
        raise HTTPException(
            status_code=409,
            detail="Meetings shared with you are not written to Obsidian"
            " (Settings: shared_in_obsidian)",
        )
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
    host_scope.require_vault_owner(ctx)
    vault_path = ctx.settings.obsidian_vault_path
    if not vault_path:
        raise HTTPException(status_code=400, detail="Obsidian vault path not configured")
    if any(j.kind == "obsidian_export_all" for j in ctx.jobs.list(active_only=True)):
        raise HTTPException(status_code=409, detail="The meetings are already being exported")
    shared = ctx.settings.shared_in_obsidian  # meetings shared with me: only when asked for
    wanted = [
        s
        for s in ctx.repo.list_summaries()
        if s.has_summary
        and not s.origin
        and access.can_write(s.owner_id)
        and host_scope.vault_allows(ctx, s)
        and (shared or not s.shared_from)
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
