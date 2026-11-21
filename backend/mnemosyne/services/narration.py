"""Narrating text in an enrolled voice: the `narrate` job (plan: 2026-11-20-narration-voices.md).

1. The text: a meeting's summary, own notes or follow-up draft, a digest, or text given
   (`source_text`), made speakable (synthesis/text.py) with the person's pronunciations.
2. Chunks of a few sentences, each spoken by the engine in the voice (services/voices.py
   `prompt_for`), joined with short pauses and fades, loudness evened out.
3. The round trip (`check`): each chunk is transcribed and compared with what it should say; a
   chunk that came out wrong (skipped or invented words, the usual failure of cloning models) is
   spoken once more and the better take kept, flagged when both are poor.
4. OGG/Opus under <data_dir>/narrations/, tagged SYNTHETIC=mnemosyne (and watermarked by the
   engine: AudioSeal for KittenTTS), sealed when encryption at rest is on, with each chunk's
   times so playback highlights what is being read.

Every narration is logged (`narrations`, and the meeting's history for a meeting's text) and is
its maker's alone. Nothing is ever played here: narrations are files.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import numpy as np

from .. import access
from ..models.voice import Narration, NarrationChunk, NarrationRequest
from . import voices

if TYPE_CHECKING:
    from ..api.context import AppContext
    from ..jobs import JobContext
    from ..synthesis.engine import Synthesizer

logger = logging.getLogger(__name__)

OUT_RATE = 48000
PAUSE = 0.35  # seconds between chunks
PARAGRAPH_PAUSE = 0.6
FADE = 0.01
TARGET_RMS_DB = -20.0
CHECK_MATCH = 0.85  # a chunk whose words are heard less than this is spoken again
MAX_TEXT = 60_000  # characters; a whole digest is ~10k
SYNTHETIC_TAG = "SYNTHETIC=mnemosyne"


class NarrationError(ValueError):
    status = 400


class NoEngine(NarrationError):
    status = 409


# ---- the engine (one per app, built lazily, stopped with the speech models) ---


_UNBUILT = object()


def engine(app: AppContext) -> Synthesizer | None:
    """This app's narration engine (kept on the app, built on first use; None: none here)."""
    synth = getattr(app, "_narration_engine", _UNBUILT)
    if synth is _UNBUILT:
        from ..synthesis.registry import build_synthesizer

        synth = build_synthesizer(app.settings)
        app._narration_engine = synth
    return synth


def forget_engine(app: AppContext) -> None:
    """The setting changed, or the engine was installed or removed: build again next time.
    A running engine is stopped (its worker ends with it when there is no event loop)."""
    synth = getattr(app, "_narration_engine", None)
    app._narration_engine = _UNBUILT
    if synth not in (None, _UNBUILT) and synth.is_loaded():
        try:
            asyncio.get_running_loop().create_task(synth.unload())
        except RuntimeError:
            pass


# ---- the text --------------------------------------------------------------


def source_text(app: AppContext, request: NarrationRequest) -> tuple[str, str]:
    """(title, markdown) of what to narrate. Meetings are read through the session service, so
    the caller's access rules apply (someone else's meeting is not found)."""
    kind = request.source_kind
    if kind == "text":
        text = (request.text or "").strip()
        if not text:
            raise NarrationError("Write something to narrate")
        return (request.title.strip() or text.split("\n", 1)[0][:60]), text
    if kind == "digest":
        digest = app.repo.get_digest(request.source_id or "")
        if digest is None:
            raise NarrationError("No such digest")
        return request.title.strip() or f"Digest {digest.label}", digest.markdown
    if not request.source_id:
        raise NarrationError("Say which meeting")
    try:
        session = app.sessions.get_session(request.source_id)
    except access.Forbidden:
        session = None
    if session is None:
        raise NarrationError("No such meeting")
    name = request.title.strip() or session.name
    if kind == "summary":
        if not session.summary.strip():
            raise NarrationError("This meeting has no summary yet")
        return f"{name}: summary", session.summary
    if kind == "notes":
        from .notes_chips import plain_notes
        from .notes_sections import own_notes

        notes = own_notes(app.repo, session.id, session.notes or "")
        if not notes.strip():
            raise NarrationError("This meeting has no notes")
        return f"{name}: notes", plain_notes(notes)
    if kind == "followup":
        draft = session.summary_data.followup if session.summary_data else ""
        if not (draft or "").strip():
            raise NarrationError("This meeting has no follow-up draft")
        return f"{name}: follow-up", draft
    raise NarrationError(f"Cannot narrate {kind}")


def plan_chunks(app: AppContext, markdown: str) -> list[tuple[str, bool]]:
    """(chunk, ends a paragraph) to speak, respellings applied."""
    from ..synthesis.text import chunks, parse_pronunciations, respell, speakable

    spoken = speakable(markdown)
    if len(spoken) > MAX_TEXT:
        raise NarrationError(f"Narrations up to {MAX_TEXT:,} characters")
    rules = parse_pronunciations(app.settings.narration_pronunciations)
    out = []
    for paragraph in (p for p in spoken.split("\n") if p.strip()):
        pieces = chunks(paragraph)
        out += [(respell(c, rules), i == len(pieces) - 1) for i, c in enumerate(pieces)]
    if not out:
        raise NarrationError("There is nothing to read aloud in that text")
    return out


# ---- audio -----------------------------------------------------------------


def _resample(samples: np.ndarray, rate: int, to: int) -> np.ndarray:
    if rate == to or samples.size == 0:
        return samples.astype(np.float32)
    n = int(round(samples.size * to / rate))
    x = np.linspace(0, samples.size - 1, n)
    return np.interp(x, np.arange(samples.size), samples).astype(np.float32)


def _even(samples: np.ndarray) -> np.ndarray:
    """Trim the edges' silence, fade them, and bring speech to TARGET_RMS_DB."""
    samples = voices.trim_silence(samples, OUT_RATE, pad=0.05)
    if samples.size == 0:
        return samples
    rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))) or 1e-6
    gain = 10 ** ((TARGET_RMS_DB - 20 * np.log10(rms)) / 20)
    samples = np.clip(samples * min(gain, 10.0), -0.98, 0.98).astype(np.float32)
    n = min(int(FADE * OUT_RATE), samples.size // 2)
    if n:
        ramp = np.linspace(0, 1, n, dtype=np.float32)
        samples[:n] *= ramp
        samples[-n:] *= ramp[::-1]
    return samples


def encode(samples: np.ndarray, dest: Path, title: str) -> Path:
    """OGG/Opus at 48 kHz, tagged as synthetic (title, artist, and SYNTHETIC=mnemosyne)."""
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = ["ffmpeg", "-y", "-f", "s16le", "-ar", str(OUT_RATE), "-ac", "1", "-i", "-"]
    args += ["-c:a", "libopus", "-b:a", "64k"]
    args += ["-metadata", f"title={title[:200]}", "-metadata", "comment=" + SYNTHETIC_TAG]
    args += ["-metadata", "SYNTHETIC=mnemosyne", str(dest)]
    result = subprocess.run(args, input=pcm, capture_output=True, timeout=3600)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg encode failed: {result.stderr.decode()[-500:]}")
    return dest


def is_tagged_synthetic(path: Path) -> bool:
    """The file says it is a Mnemosyne narration (its tags). Cheap; anyone can remove tags."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format_tags:stream_tags", "-of", "json"]
            + [str(path)],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    import json

    try:
        info = json.loads(result.stdout or b"{}")
    except ValueError:
        return False
    tags: dict = dict(info.get("format", {}).get("tags", {}))
    for stream in info.get("streams", []):
        tags.update(stream.get("tags", {}))
    for key, value in tags.items():
        if key.lower() == "synthetic" and str(value).lower() == "mnemosyne":
            return True
        if key.lower() == "comment" and SYNTHETIC_TAG in str(value):
            return True
    return False


# ---- the job ---------------------------------------------------------------


async def _speak_checked(app, synth, prompt, text, language, check, workdir):
    """(samples at OUT_RATE, watermarked, flagged) for one chunk."""
    speech = await synth.speak(prompt, text)
    samples = _resample(speech.samples, speech.sample_rate, OUT_RATE)
    if not check:
        return samples, speech.watermarked, False
    score = await _heard(app, samples, text, language, workdir)
    if score >= CHECK_MATCH:
        return samples, speech.watermarked, False
    again = await synth.speak(prompt, text)
    samples2 = _resample(again.samples, again.sample_rate, OUT_RATE)
    score2 = await _heard(app, samples2, text, language, workdir)
    if score2 > score:
        return samples2, again.watermarked, score2 < CHECK_MATCH
    return samples, speech.watermarked, True


async def _heard(app, samples, text, language, workdir) -> float:
    from ..synthesis.workers import write_wav

    path = workdir / f"check-{uuid4().hex[:6]}.wav"
    await asyncio.to_thread(write_wav, path, samples, OUT_RATE)
    try:
        heard = await voices.transcribe(app, path, language)
    except Exception:
        logger.warning("Narration check could not transcribe a chunk", exc_info=True)
        return 1.0  # no transcriber: keep what was made rather than fail the narration
    finally:
        path.unlink(missing_ok=True)
    return voices.text_match(text, heard)


async def run(app: AppContext, request: NarrationRequest, job: JobContext) -> dict:
    """The `narrate` job's work. Returns {"narration_id": ...}."""
    row = voices.get_own(app, request.voice_id)
    voice = voices.view(app, row)
    if not voice.ready:
        raise NarrationError(f"The voice is not ready: it needs {voice.needs}")
    synth = engine(app)
    if synth is None:
        raise NoEngine("No narration engine is installed (Settings -> Voices)")
    title, markdown = source_text(app, request)
    planned = plan_chunks(app, markdown)
    job.update(f"Preparing {voice.name}'s voice", 0.02)
    if not synth.is_loaded():
        job.update("Starting the voice engine (the first time takes a minute)", 0.03)
        await synth.load()
    scratch = Path(tempfile.mkdtemp(prefix="narrate-", dir=app.settings.data_dir))
    try:
        prompt = await voices.prompt_for(app, voice.id, synth, scratch)
        parts: list[np.ndarray] = []
        chunks: list[NarrationChunk] = []
        marked = True
        t = 0.0
        for i, (text, paragraph_end) in enumerate(planned):
            job.update(f"Reading part {i + 1} of {len(planned)}", 0.05 + 0.9 * i / len(planned))
            samples, wm, flagged = await _speak_checked(
                app, synth, prompt, text, row["language"], request.check, scratch
            )
            marked = marked and wm
            samples = _even(samples)
            start = t
            t += samples.size / OUT_RATE
            chunks.append(
                NarrationChunk(text=text, start=round(start, 3), end=round(t, 3), flagged=flagged)
            )
            pause = np.zeros(
                int(OUT_RATE * (PARAGRAPH_PAUSE if paragraph_end else PAUSE)), np.float32
            )
            parts += [samples, pause]
            t += pause.size / OUT_RATE
        audio = np.concatenate(parts[:-1]) if parts else np.zeros(1, np.float32)
        job.update("Saving", 0.97)
        narration_id = uuid4().hex[:12]
        dest = app.settings.narrations_dir / f"{narration_id}.ogg"
        await asyncio.to_thread(encode, audio, dest, title)
        if app.settings.encrypt_at_rest and app.file_key is not None:
            from ..storage.crypto import encrypt_file

            dest = await asyncio.to_thread(encrypt_file, dest, app.file_key)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    text_hash = hashlib.sha256("\n".join(c for c, _ in planned).encode()).hexdigest()
    app.repo.add_narration(
        {
            "id": narration_id,
            "voice_id": voice.id,
            "voice_name": voice.name,
            "user_id": access.user_id(),
            "source_kind": request.source_kind,
            "source_id": request.source_id,
            "title": title,
            "text_hash": text_hash,
            "engine": synth.name,
            "model": synth.model,
            "path": voices.rel(app, dest),
            "seconds": round(audio.size / OUT_RATE, 2),
            "chunks": [c.model_dump() for c in chunks],
            "watermarked": marked,
        }
    )
    if request.source_kind in ("summary", "notes", "followup") and request.source_id:
        from . import history

        history.log(
            app,
            request.source_id,
            "narrated",
            narration=narration_id,
            what=request.source_kind,
            voice=voice.name,
            by=access.principal().name if access.principal() else "",
        )
    flagged = sum(c.flagged for c in chunks)
    job.update(
        f"Narrated {len(chunks)} parts" + (f", {flagged} may not read right" if flagged else ""),
        1.0,
    )
    return {"narration_id": narration_id}


def view(row: dict) -> Narration:
    return Narration(
        id=row["id"],
        voice_id=row["voice_id"],
        voice_name=row["voice_name"],
        user_id=row["user_id"],
        source_kind=row["source_kind"],
        source_id=row["source_id"],
        title=row["title"],
        engine=row["engine"],
        model=row["model"],
        seconds=row["seconds"],
        chunks=[NarrationChunk(**c) for c in row["chunks"]],
        watermarked=row["watermarked"],
        created_at=row["created_at"],
    )


def get_own(app: AppContext, narration_id: str) -> dict:
    row = app.repo.get_narration_row(narration_id)
    if row is None or row["user_id"] != access.user_id():
        raise voices.VoiceNotFound("No such narration")
    return row


def delete(app: AppContext, narration_id: str) -> None:
    get_own(app, narration_id)
    relative = app.repo.delete_narration(narration_id)
    if relative:
        voices.stored(app, relative).unlink(missing_ok=True)
