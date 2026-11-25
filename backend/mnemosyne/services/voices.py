"""Narration voices: making one from a person's own recordings, with their live consent.

A voice is reference clips of one person (mono WAV at 24 kHz under <data_dir>/voices/<id>/,
sealed when encryption at rest is on), each with its exact transcript, plus the recording of a
live consent statement. It narrates (services/narration.py) only once it is `ready`: consent
passed and at least READY_SECONDS of accepted audio.

Every clip, recorded in the app or uploaded, goes through the same checks (`add_audio`):
- long audio is cut at its quietest moments into pieces of PIECE_MIN-PIECE_MAX seconds;
- level, clipping, noise and how much of it is speech (`measure`); a piece that fails is kept,
  shown with the reason, and never used;
- the same person: each piece's speaker embedding (the embedder voice profiles use) against the
  mean of the voice's accepted clips; a different voice is refused;
- what is said: the live transcriber's text, which the person can correct (engines condition on
  it, so it must be exact).

Consent (`challenge`, `check_consent`): the person reads a fixed statement with random words that
are only shown then, in the app, live. It passes when the transcriber heard the statement
(CONSENT_TEXT_MATCH of its words, every random word) and the voice matches the clips
(the space's profile threshold). An upload alone can never make a voice: someone holding another
person's recordings cannot read the random words in that person's voice.

Voices belong to their owner (`owner_id`, the signed-in user, '' without sign-in): only the owner
sees, changes and narrates with theirs; an admin may delete any. Nothing here travels.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from .. import access
from ..models.voice import ConsentChallenge, ConsentResult, Voice, VoiceClip

if TYPE_CHECKING:
    from ..api.context import AppContext
    from ..synthesis.engine import Synthesizer, VoicePrompt

logger = logging.getLogger(__name__)

RATE = 24000  # stored clips
PIECE_MIN, PIECE_MAX = 6.0, 20.0  # seconds; long uploads are cut into pieces this long
CLIP_MIN, CLIP_MAX = 3.0, 40.0  # a single take outside this is refused
UPLOAD_MAX_SECONDS = 15 * 60
READY_SECONDS = 20.0  # accepted audio a voice needs before it narrates
CHALLENGE_SECONDS = 300
CONSENT_TEXT_MATCH = 0.8
FRAME = 0.02  # seconds, for levels

STATEMENT = (
    "I am {name}. I agree that Mnemosyne may make a synthetic copy of my voice for my own use. "
    "My words today are {words}."
)
CHALLENGE_WORDS = (
    "amber anchor apple arrow autumn badger basket beacon breeze bridge candle canyon carpet "
    "castle cedar cherry cobalt comet copper cotton crystal dolphin ember falcon feather fiddle "
    "forest garden glacier granite harbor hazel honey island jasmine kettle lantern lemon maple "
    "marble meadow mirror monsoon nectar olive orbit orchid otter pebble pepper pillow planet "
    "quartz rabbit raven ribbon river saddle silver spruce summit thistle thunder tiger timber "
    "tulip velvet violin walnut willow winter yellow zebra"
).split()

GUIDED_SCRIPT = [
    "The quick brown fox jumps over the lazy dog near the riverbank.",
    "We will review the budget on Thursday and decide by the end of the month.",
    "Please call Stella and ask her to bring these things with her from the store.",
    "Six spoons of fresh snow peas, five thick slabs of blue cheese, and maybe a snack.",
    "Our meeting starts at nine thirty, so let's keep the agenda short and focused.",
    "Could you send me the notes from yesterday's call before lunch?",
    "The weather was warm, but the wind off the water made the evening feel cool.",
    "I think the main risk is timing, not cost, and we should plan for that.",
    "Numbers like forty two, one hundred and eight, and two thousand twenty six matter here.",
    "She asked whether the project would ship in June or slip into July.",
    "When the sunlight strikes raindrops in the air, they act as a prism and form a rainbow.",
    "Thanks, everyone. Let's pick this up again next week with the updated figures.",
    "A large, joyful crowd gathered quietly in the square to watch the parade go by.",
    "Is it really worth the effort? Yes, absolutely, if we do it carefully.",
    "He read the report twice, underlined three passages, and wrote a short reply.",
    "Good morning! I hope you had a restful weekend and are ready for a busy day.",
    "The engineer measured the voltage, checked the wiring, and replaced the fuse.",
    "Before we finish, does anyone have questions or anything else to add?",
]


class VoiceError(ValueError):
    """Refused (answered 400 unless `status` says otherwise)."""

    status = 400


class VoiceNotFound(VoiceError):
    status = 404


class VoiceForbidden(VoiceError):
    status = 403


class NoChecker(VoiceError):
    """No speaker embedder on this computer: the same-person and consent checks cannot run."""

    status = 409


# ---- who may use a voice -------------------------------------------------


def _owner() -> str:
    return access.user_id()


def get_own(app: AppContext, voice_id: str) -> dict:
    """The voice's row when the caller owns it; 404 for anyone else (ids reveal nothing)."""
    row = app.repo.get_voice_row(voice_id)
    if row is None or row["owner_id"] != _owner():
        raise VoiceNotFound("No such voice")
    return row


def deletable(app: AppContext, voice_id: str) -> dict:
    row = app.repo.get_voice_row(voice_id)
    if row is None:
        raise VoiceNotFound("No such voice")
    if row["owner_id"] != _owner() and not access.is_admin():
        raise VoiceNotFound("No such voice")
    return row


# ---- views ---------------------------------------------------------------


def view(app: AppContext, row: dict) -> Voice:
    clips = app.repo.voice_clips(row["id"])
    seconds = sum(c["seconds"] for c in clips if c["accepted"])
    consented = row["consent_at"] is not None
    needs = []
    if seconds < READY_SECONDS:
        needs.append(f"about {max(1, round(READY_SECONDS - seconds))} more seconds of recording")
    if not consented:
        needs.append("the consent check")
    return Voice(
        id=row["id"],
        owner_id=row["owner_id"],
        name=row["name"],
        language=row["language"],
        clips=[
            VoiceClip(
                idx=c["idx"],
                text=c["text"],
                seconds=round(c["seconds"], 2),
                quality=round(c["quality"], 2),
                accepted=c["accepted"],
                problem=c["problem"],
                created_at=c["created_at"],
            )
            for c in clips
        ],
        seconds=round(seconds, 1),
        consented=consented,
        consent_at=row["consent_at"],
        reference_idx=row["reference_idx"],
        ready=not needs,
        needs=" and ".join(needs),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def list_own(app: AppContext) -> list[Voice]:
    return [view(app, r) for r in app.repo.list_voice_rows(owner_id=_owner())]


def create(app: AppContext, name: str, language: str = "en") -> Voice:
    name = name.strip()
    if not name:
        raise VoiceError("Give the voice a name")
    row = app.repo.add_voice(name[:80], _owner(), (language or "en")[:8])
    folder(app, row["id"]).mkdir(parents=True, exist_ok=True)
    return view(app, row)


def folder(app: AppContext, voice_id: str) -> Path:
    return app.settings.voices_dir / voice_id


def delete(app: AppContext, voice_id: str) -> None:
    """The voice, every clip, its consent recording and prompts. Narrations made with it stay
    (they are the owner's files), with the voice's name kept."""
    deletable(app, voice_id)
    app.repo.delete_voice(voice_id)
    shutil.rmtree(folder(app, voice_id), ignore_errors=True)
    _challenges.pop(voice_id, None)
    logger.info("Voice %s deleted", voice_id)


# ---- audio checks --------------------------------------------------------


@dataclass
class Measure:
    seconds: float
    peak: float
    clipped: float  # fraction of samples at full scale
    level_db: float  # loud speech (90th percentile of 20 ms frame levels), dBFS
    noise_db: float  # the quiet floor (10th percentile)
    speech: float  # fraction of frames well above the floor

    @property
    def snr_db(self) -> float:
        return self.level_db - self.noise_db

    def problem(self) -> str:
        if self.seconds < CLIP_MIN:
            return f"Too short ({self.seconds:.1f} s): read a whole sentence"
        if self.level_db < -42:
            return "Too quiet: move closer to the microphone"
        if self.clipped > 0.001:
            return "Too loud: the recording clips, move back or lower the input level"
        if self.snr_db < 15:
            return "Too noisy: record somewhere quieter"
        if self.speech < 0.35:
            return "Mostly silence"
        return ""

    def quality(self) -> float:
        snr = min(1.0, max(0.0, (self.snr_db - 15) / 30))
        level = 1.0 - min(1.0, abs(self.level_db + 18) / 24)
        speech = min(1.0, self.speech / 0.7)
        return round(max(0.0, min(1.0, 0.5 * snr + 0.3 * level + 0.2 * speech)), 3)


def _frame_db(samples: np.ndarray, rate: int) -> np.ndarray:
    n = max(1, int(rate * FRAME))
    usable = (
        samples[: samples.size - samples.size % n].reshape(-1, n) if samples.size >= n else None
    )
    if usable is None:
        return np.array([-120.0])
    rms = np.sqrt(np.mean(usable.astype(np.float64) ** 2, axis=1))
    return 20 * np.log10(np.maximum(rms, 1e-6))


def measure(samples: np.ndarray, rate: int) -> Measure:
    db = _frame_db(samples, rate)
    level, noise = float(np.percentile(db, 90)), float(np.percentile(db, 10))
    floor = noise + max(10.0, (level - noise) * 0.4)
    return Measure(
        seconds=samples.size / rate,
        peak=float(np.max(np.abs(samples))) if samples.size else 0.0,
        clipped=float(np.mean(np.abs(samples) >= 0.999)) if samples.size else 0.0,
        level_db=level,
        noise_db=noise,
        speech=float(np.mean(db > floor)),
    )


def trim_silence(samples: np.ndarray, rate: int, pad: float = 0.25) -> np.ndarray:
    db = _frame_db(samples, rate)
    if db.size < 3:
        return samples
    floor = float(np.percentile(db, 10)) + 12
    loud = np.flatnonzero(db > floor)
    if loud.size == 0:
        return samples[:0]
    n = int(rate * FRAME)
    start = max(0, loud[0] * n - int(pad * rate))
    end = min(samples.size, (loud[-1] + 1) * n + int(pad * rate))
    return samples[start:end]


def split_pieces(samples: np.ndarray, rate: int) -> list[np.ndarray]:
    """Long audio cut at the quietest 20 ms frame between PIECE_MIN and PIECE_MAX seconds into
    each piece; a short remainder joins the last piece when that stays under CLIP_MAX."""
    if samples.size <= PIECE_MAX * rate:
        return [samples]
    db = _frame_db(samples, rate)
    n = int(rate * FRAME)
    pieces, start = [], 0
    while samples.size - start > PIECE_MAX * rate:
        lo = (start + int(PIECE_MIN * rate)) // n
        hi = (start + int(PIECE_MAX * rate)) // n
        cut = (lo + int(np.argmin(db[lo:hi]))) * n
        pieces.append(samples[start:cut])
        start = cut
    rest = samples[start:]
    if rest.size < PIECE_MIN * rate and pieces and (pieces[-1].size + rest.size) <= CLIP_MAX * rate:
        pieces[-1] = np.concatenate([pieces[-1], rest])
    else:
        pieces.append(rest)
    return pieces


def _cosine(a, b) -> float:
    a, b = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    if a.shape != b.shape or not a.size:
        return -1.0
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))


def centroid(clips: list[dict], space: str) -> np.ndarray | None:
    vectors = [
        c["embedding"] for c in clips if c["accepted"] and c["embedding"] and c["space"] == space
    ]
    if not vectors:
        return None
    return np.mean(np.asarray(vectors, dtype=np.float64), axis=0)


# ---- the models this needs (lazily, from the app's ModelService) -------------


async def _embedder(app: AppContext):
    emb = app.models.live_embedder
    if emb is not None and not emb.is_loaded():
        await emb.load()
    return emb


def can_check(app: AppContext) -> bool:
    from ..transcription.registry import resolve_speaker_embedder

    return app.settings.diarizer == "demo" or resolve_speaker_embedder(app.settings) != "none"


async def transcribe(app: AppContext, path: Path, language: str | None) -> str:
    transcriber = app.models.live_transcriber
    if not transcriber.is_loaded():
        await transcriber.load()
    segments = await transcriber.transcribe(str(path), language=language or None)
    return re.sub(r"\s+", " ", " ".join(s.text for s in segments)).strip()


def _threshold(app: AppContext, space: str) -> float:
    from ..transcription.speaker_embedders import thresholds

    return thresholds(space, app.settings).match


# ---- adding audio --------------------------------------------------------


def _store_wav(app: AppContext, dest: Path, samples: np.ndarray, rate: int) -> Path:
    """Write a WAV, sealed when encryption at rest is on. Returns the stored path."""
    from ..synthesis.workers import write_wav

    dest.parent.mkdir(parents=True, exist_ok=True)
    write_wav(dest, samples, rate)
    if app.settings.encrypt_at_rest and app.file_key is not None:
        from ..storage.crypto import encrypt_file

        dest = encrypt_file(dest, app.file_key)
    return dest


def rel(app: AppContext, path: Path) -> str:
    return path.relative_to(app.settings.data_dir).as_posix()


def stored(app: AppContext, relative: str) -> Path:
    path = (app.settings.data_dir / relative).resolve()
    if not path.is_relative_to(app.settings.data_dir.resolve()):
        raise ValueError("Not a voice file")
    return path


async def add_audio(
    app: AppContext, voice_id: str, source: Path, text: str = "", single: bool = False
) -> Voice:
    """Check `source` (any format ffmpeg reads) and add it to the voice as one clip, or as
    pieces when it is long and not `single`. `text`: what is said, when the person read a
    sentence from the guided script (one take); else the transcriber's."""
    from ..audio.mixer import decode_audio

    row = get_own(app, voice_id)
    if not can_check(app):
        raise NoChecker(
            "Making a voice needs the speaker model that recognises voices (Settings -> "
            "Transcription -> Diarizer: On the CPU)"
        )
    try:
        samples = await asyncio.to_thread(decode_audio, source, RATE)
    except Exception as e:
        raise VoiceError("Could not read that audio file") from e
    if samples.size > UPLOAD_MAX_SECONDS * RATE:
        raise VoiceError(f"Recordings up to {UPLOAD_MAX_SECONDS // 60} minutes")
    trimmed = trim_silence(samples, RATE)
    # Too quiet to tell speech from silence: keep it whole, so the checks say why.
    if trimmed.size >= CLIP_MIN * RATE or samples.size < CLIP_MIN * RATE:
        samples = trimmed
    if single and samples.size > CLIP_MAX * RATE:
        raise VoiceError(f"A take up to {int(CLIP_MAX)} seconds")
    pieces = [samples] if single else split_pieces(samples, RATE)
    emb = await _embedder(app)
    for piece in pieces:
        await _add_piece(app, row, piece, text if (single or len(pieces) == 1) else "", emb)
    return view(app, app.repo.get_voice_row(voice_id))


async def _add_piece(app: AppContext, row: dict, piece: np.ndarray, text: str, emb) -> None:
    voice_id = row["id"]
    m = measure(piece, RATE)
    problem = m.problem()
    space, vector = "", None
    if not problem and emb is not None:
        vector = await emb.embed(piece, RATE)
        space = emb.space
        if vector is None:
            problem = "Too short to recognise the voice"
        else:
            mean = centroid(app.repo.voice_clips(voice_id), space)
            if mean is not None and _cosine(vector, mean) < _threshold(app, space):
                problem = "This sounds like a different person from the other recordings"
    idx_hint = len(app.repo.voice_clips(voice_id))
    dest = folder(app, voice_id) / f"clip-{idx_hint:03d}-{secrets.token_hex(3)}.wav"
    path = await asyncio.to_thread(_store_wav, app, dest, piece, RATE)
    if not text and not problem:
        from ..storage.crypto import plaintext_async

        async with plaintext_async(path, app.file_key) as plain:
            text = await transcribe(app, plain, row["language"])
        if not text:
            problem = "No words were heard"
    app.repo.add_voice_clip(
        voice_id,
        rel(app, path),
        text,
        m.seconds,
        m.quality(),
        accepted=not problem,
        problem=problem,
        space=space,
        embedding=vector,
    )


def set_clip_text(app: AppContext, voice_id: str, idx: int, text: str) -> Voice:
    get_own(app, voice_id)
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        raise VoiceError("Write what is said in the recording")
    if not app.repo.update_voice_clip(voice_id, idx, text=text[:2000]):
        raise VoiceNotFound("No such recording")
    return view(app, app.repo.get_voice_row(voice_id))


def delete_clip(app: AppContext, voice_id: str, idx: int) -> Voice:
    row = get_own(app, voice_id)
    relative = app.repo.delete_voice_clip(voice_id, idx)
    if relative is None:
        raise VoiceNotFound("No such recording")
    stored(app, relative).unlink(missing_ok=True)
    if row["reference_idx"] == idx:
        app.repo.update_voice(voice_id, reference_idx=None)
    return view(app, app.repo.get_voice_row(voice_id))


def set_reference(app: AppContext, voice_id: str, idx: int) -> None:
    """Make clip `idx` the voice's reference (-1: let the engine pick again). The owner's ear
    beat every measure tried for this (plan, "Picking the reference")."""
    get_own(app, voice_id)
    if idx < 0:
        app.repo.update_voice(voice_id, reference_idx=None)
        return
    clip = next((c for c in app.repo.voice_clips(voice_id) if c["idx"] == idx), None)
    if clip is None:
        raise VoiceNotFound("No such recording")
    if not clip["accepted"]:
        raise VoiceError("That recording did not pass the checks, so it cannot be the reference")
    app.repo.update_voice(voice_id, reference_idx=idx)


# ---- consent -------------------------------------------------------------

_challenges: dict[str, tuple[str, list[str], float, str]] = {}  # voice -> text, words, until, who


def challenge(app: AppContext, voice_id: str, speaker_name: str = "") -> ConsentChallenge:
    row = get_own(app, voice_id)
    rng = secrets.SystemRandom()
    words = rng.sample(CHALLENGE_WORDS, 4)
    who = (speaker_name or "").strip() or _person_name(app) or row["name"]
    text = STATEMENT.format(name=who, words=", ".join(words))
    _challenges[voice_id] = (text, words, time.monotonic() + CHALLENGE_SECONDS, _owner())
    return ConsentChallenge(text=text, words=words, expires_in=CHALLENGE_SECONDS)


def _person_name(app: AppContext) -> str:
    p = access.principal()
    if p is not None and p.name:
        return p.name
    name = (app.settings.local_speaker_name or "").strip()
    return "" if name.lower() == "me" else name  # the default says nothing about who it is


_WORD = re.compile(r"[a-z0-9']+")


def _words(text: str) -> list[str]:
    return _WORD.findall((text or "").lower())


def text_match(expected: str, heard: str) -> float:
    """The fraction of the statement's words heard, in order (a longest common subsequence)."""
    want, got = _words(expected), _words(heard)
    if not want:
        return 0.0
    prev = [0] * (len(got) + 1)
    for w in want:
        cur = [0]
        for j, g in enumerate(got):
            cur.append(prev[j] + 1 if w == g else max(prev[j + 1], cur[j]))
        prev = cur
    return prev[-1] / len(want)


async def check_consent(app: AppContext, voice_id: str, source: Path) -> ConsentResult:
    """The live consent recording, against the challenge given and the voice's clips."""
    from ..audio.mixer import decode_audio

    row = get_own(app, voice_id)
    pending = _challenges.get(voice_id)
    if pending is None or pending[2] < time.monotonic() or pending[3] != _owner():
        raise VoiceError("Ask for the consent statement again: it is shown once, for 5 minutes")
    statement, words, _, _ = pending
    clips = app.repo.voice_clips(voice_id)
    emb = await _embedder(app)
    if emb is None and app.settings.diarizer != "demo":
        raise NoChecker("Checking consent needs the speaker model that recognises voices")
    mean = centroid(clips, emb.space) if emb is not None else None
    if mean is None and app.settings.diarizer != "demo":
        raise VoiceError("Record the voice first: consent is checked against those recordings")
    try:
        samples = await asyncio.to_thread(decode_audio, source, RATE)
    except Exception as e:
        raise VoiceError("Could not read that recording") from e
    samples = trim_silence(samples, RATE)
    dest = folder(app, voice_id) / f"consent-{secrets.token_hex(4)}.wav"
    path = await asyncio.to_thread(_store_wav, app, dest, samples, RATE)
    from ..storage.crypto import plaintext_async

    async with plaintext_async(path, app.file_key) as plain:
        heard = await transcribe(app, plain, row["language"])
    match = text_match(statement, heard)
    heard_words = set(_words(heard))
    missing = [w for w in words if w not in heard_words]
    voice_match = 1.0
    if emb is not None and mean is not None:
        vector = await emb.embed(samples, RATE)
        voice_match = _cosine(vector, mean) if vector is not None else -1.0
    problem = ""
    if missing:
        problem = f"Did not hear: {', '.join(missing)}. Read the statement as shown."
    elif match < CONSENT_TEXT_MATCH:
        problem = "The statement was not read as shown"
    elif emb is not None and voice_match < _threshold(app, emb.space):
        problem = "The voice does not match the recordings"
    passed = not problem
    if passed:
        old = row["consent_path"]
        app.repo.update_voice(
            voice_id,
            consent_path=rel(app, path),
            consent_at=time_now(),
            consent_text=statement,
            consent_score=round(voice_match, 3),
        )
        if old:
            stored(app, old).unlink(missing_ok=True)
        _challenges.pop(voice_id, None)
        logger.info("Consent recorded for voice %s (voice match %.2f)", voice_id, voice_match)
    else:
        path.unlink(missing_ok=True)
    return ConsentResult(
        passed=passed,
        heard=heard,
        text_match=round(match, 3),
        voice_match=round(voice_match, 3),
        problem=problem,
        voice=view(app, app.repo.get_voice_row(voice_id)),
    )


def time_now() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


# ---- prompts for an engine -------------------------------------------------


async def prompt_for(
    app: AppContext, voice_id: str, synth: Synthesizer, workdir: Path
) -> VoicePrompt:
    """The voice prepared for `synth`: from the table while its clips are unchanged, else made
    now. Clips are handed to the engine as plaintext copies in `workdir` (a scratch folder the
    caller removes), and so is the prompt."""
    from ..storage.crypto import encrypt_file, plaintext
    from ..synthesis.engine import RefClip, VoicePrompt

    row = app.repo.get_voice_row(voice_id)
    clips = [c for c in app.repo.voice_clips(voice_id) if c["accepted"]]
    chosen = [c for c in clips if c["idx"] == row["reference_idx"]]
    if chosen:  # the owner picked the clip that sounds like them: only that one
        clips = chosen
    extra = {"language": row["language"]}
    saved = app.repo.get_prompt_row(voice_id, synth.space)
    key = app.file_key
    if saved and stored(app, saved["path"]).exists():
        dest = workdir / Path(saved["path"]).name.removesuffix(".enc")
        with plaintext(stored(app, saved["path"]), key) as plain:
            shutil.copyfile(plain, dest)
        return VoicePrompt(synth.space, dest, saved["text"], extra)
    refs = []
    for c in clips:
        dest = workdir / f"ref-{c['idx']}.wav"
        with plaintext(stored(app, c["path"]), key) as plain:
            shutil.copyfile(plain, dest)
        refs.append(RefClip(dest, c["text"], c["seconds"], c["quality"]))
    prompt = await synth.prepare(refs, workdir / "prepared")
    prompt = VoicePrompt(prompt.space, prompt.path, prompt.text, {**prompt.extra, **extra})
    keep = folder(app, voice_id) / f"prompt-{synth.space}.wav"
    shutil.copyfile(prompt.path, keep)
    if app.settings.encrypt_at_rest and key is not None:
        keep = encrypt_file(keep, key)
    used = [c["idx"] for c in clips]  # adding, removing or editing a clip drops the prompt
    app.repo.set_prompt_row(voice_id, synth.space, rel(app, keep), prompt.text, used)
    logger.info("Voice %s (%s) prepared for %s", voice_id, row["name"] if row else "?", synth.name)
    return prompt


# ---- encryption at rest on/off (services/encryption.py) --------------------


def convert_files(app: AppContext, key: bytes | None) -> int:
    """Encryption turned on (key) or off (None): seal or open every voice and narration file."""
    from ..storage.crypto import decrypt_file, encrypt_file, is_encrypted

    done = 0
    for table, ident, relative in app.repo.voice_paths():
        path = stored(app, relative)
        if not path.exists():
            continue
        if key is not None and not is_encrypted(path):
            new = encrypt_file(path, key)
        elif key is None and is_encrypted(path):
            new = decrypt_file(path, app.file_key)
        else:
            continue
        app.repo.set_voice_path(table, ident, rel(app, new))
        done += 1
    return done
