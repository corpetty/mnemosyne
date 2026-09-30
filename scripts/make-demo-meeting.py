#!/usr/bin/env python3
"""Make a demo annual-review meeting for a firm's pilot: an advisor and a client in two
synthetic voices, about two minutes, written to a WAV file. Import it on the firm's server
(Import, name it "Annual review (demo)") to see the whole path on the firm's own machine and model
before any client is recorded: transcription, who said what, the advisory summary with client
facts and action items, and masked identifiers (the client says a made-up Social Security number).

    python3 scripts/make-demo-meeting.py [annual-review-demo.wav]

Needs espeak-ng and ffmpeg. Everyone and everything in it is fictional. Two voices, one male and
one female, because espeak's voices of the same kind sound too alike to a diarizer; real people
do not.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

# (voice, pitch 0-99, words per minute)
VOICES = {"Advisor": ("en-us+m3", 40, 160), "Maria": ("en-us+f4", 70, 150)}

SCRIPT = [
    ("Advisor", "Good morning Maria. Before we start, I'd like to record this review so I can keep "
     "accurate notes. It stays on our firm's own server. Is that all right?"),
    ("Maria", "Yes, that's fine."),
    ("Advisor", "Thank you. So, it's been a year since our last annual review. What has changed "
     "for you since then?"),
    ("Maria", "Quite a lot. I was promoted in March, so my salary went up to about one hundred "
     "and forty thousand. And our daughter Emma starts college in the fall of twenty twenty "
     "seven. We want to make sure the five twenty nine plan is on track."),
    ("Advisor", "Congratulations. Last time you said you'd like to retire at sixty five. Is that "
     "still the plan?"),
    ("Maria", "I'd like to stop at sixty two now, if the numbers work. And I'm less comfortable "
     "with big swings than I used to be. After last year's market drop I'd rather take a bit "
     "less risk."),
    ("Advisor", "That's helpful to know. I'll prepare two scenarios for retiring at sixty two, one "
     "with the current allocation and one more conservative, and send them by the end of the month."),
    ("Maria", "Also, I need to update the beneficiary on my IRA. It still names my brother. It "
     "should be my husband, David, and Emma as contingent."),
    ("Advisor", "Of course. I'll send you the beneficiary change form this week. For the form I'll "
     "need your Social Security number."),
    ("Maria", "Sure, it's one two three, four five, six seven eight nine."),
    ("Advisor", "Thank you. Anything else on your mind?"),
    ("Maria", "Should we think about long term care insurance? My mother needed care last year."),
    ("Advisor", "Good question. Let's look at that at our next meeting. Shall we meet again in six "
     "months, around April?"),
    ("Maria", "April works."),
    ("Advisor", "Great. To recap: your new salary, Emma's college in twenty twenty seven, "
     "retirement at sixty two, a bit less risk, the IRA beneficiary change, and long term care "
     "at our next meeting. Thank you, Maria."),
]  # fmt: skip


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "annual-review-demo.wav")
    for tool in ("espeak-ng", "ffmpeg"):
        if not shutil.which(tool):
            raise SystemExit(f"Needs {tool} (on Ubuntu: apt install {tool})")
    with tempfile.TemporaryDirectory() as tmp:
        frames, rate = b"", None
        for n, (who, text) in enumerate(SCRIPT):
            part = Path(tmp) / f"{n}.wav"
            voice, pitch, speed = VOICES[who]
            subprocess.run(
                ["espeak-ng", "-v", voice, "-p", str(pitch), "-s", str(speed), "-w", str(part), text],
                check=True,
            )
            with wave.open(str(part)) as w:
                rate = rate or w.getframerate()
                frames += w.readframes(w.getnframes()) + b"\x00\x00" * int(rate * 0.7)
        raw = Path(tmp) / "raw.wav"
        with wave.open(str(raw), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(frames)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw), "-ar", "16000", str(out)],
            check=True,
        )
    print(f"Wrote {out}. On the firm's server: Import it, name it \"Annual review (demo)\".")


if __name__ == "__main__":
    main()
