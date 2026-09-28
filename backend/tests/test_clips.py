"""Share a quote: text and an audio clip of a range of transcript lines."""

import shutil
import subprocess
from datetime import datetime

import pytest

from mnemosyne.models.session import Session
from mnemosyne.models.transcript import TranscriptSegment

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def _session(ctx, tmp_path, audio=True):
    audio_file = None
    if audio:
        audio_file = tmp_path / "mixed.ogg"
        subprocess.run(  # 20 s of a quiet tone, never played
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=f=220:d=20",
                "-c:a",
                "libopus",
                str(audio_file),
            ],
            check=True,
        )
    lines = [
        ("Ana", 0.5, 4.0, "We ship Friday."),
        ("Bo", 4.5, 8.0, "Friday works."),
        ("Ana", 9.0, 12.0, "Great."),
    ]
    s = Session(
        name="Release sync",
        created_at=datetime(2026, 9, 25, 10, 0),
        audio_file=str(audio_file) if audio_file else None,
        transcript=[TranscriptSegment(text=t, speaker=sp, start=a, end=b) for sp, a, b, t in lines],
    )
    return ctx.repo.save(s)


@needs_ffmpeg
def test_quote_with_audio_clip(client, ctx, tmp_path):
    s = _session(ctx, tmp_path)
    body = client.post(f"/api/sessions/{s.id}/clip", json={"first_idx": 0, "last_idx": 1}).json()
    assert body["text"] == (
        "> **Ana** [00:00]: We ship Friday.\n> **Bo** [00:04]: Friday works.\n\n"
        "— *Release sync*, 2026-09-25"
    )
    clip = body["clip"]
    assert clip["id"] == "0-1" and clip["seconds"] == 7.5
    assert clip["filename"] == "Release sync 0m00s.ogg"
    got = client.get(f"/api/sessions/{s.id}/clips/0-1")
    assert got.status_code == 200 and got.headers["content-type"] == "audio/ogg"
    assert "Release%20sync%200m00s.ogg" in got.headers["content-disposition"]
    later = client.post(f"/api/sessions/{s.id}/clip", json={"first_idx": 2, "last_idx": 2}).json()
    assert later["clip"]["filename"] == "Release sync 0m09s.ogg"
    disposition = client.get(f"/api/sessions/{s.id}/clips/2-2").headers["content-disposition"]
    assert "Release%20sync%200m09s.ogg" in disposition
    assert (ctx.settings.recordings_dir / s.id / "clips" / "0-1.ogg").exists()

    target = tmp_path / "saved.ogg"
    saved = client.post(f"/api/sessions/{s.id}/clips/0-1/save", json={"path": str(target)})
    assert saved.status_code == 200 and target.read_bytes() == got.content


def test_quote_without_audio_and_bad_ranges(client, ctx, tmp_path):
    s = _session(ctx, tmp_path, audio=False)
    body = client.post(f"/api/sessions/{s.id}/clip", json={"first_idx": 2, "last_idx": 2}).json()
    assert body["clip"] is None and body["text"].startswith("> **Ana** [00:09]: Great.")
    for first, last in [(2, 1), (0, 3), (-1, 0)]:
        r = client.post(f"/api/sessions/{s.id}/clip", json={"first_idx": first, "last_idx": last})
        assert r.status_code == 400
    assert client.get(f"/api/sessions/{s.id}/clips/..%2Fmixed").status_code == 404
    assert client.get(f"/api/sessions/{s.id}/clips/0-0").status_code == 404
