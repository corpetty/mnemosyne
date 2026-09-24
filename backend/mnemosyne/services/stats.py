"""Talk time and turn statistics for one meeting, computed from its transcript."""

from __future__ import annotations

from ..models.base import ApiModel
from ..models.transcript import TranscriptSegment


class SpeakerStats(ApiModel):
    speaker: str
    talk_seconds: float
    share: float  # of all talk time, 0..1
    turns: int
    longest_turn_seconds: float
    words: int
    words_per_minute: float


class Turn(ApiModel):
    speaker: str
    start: float
    end: float
    first_idx: int  # transcript line where the turn starts


class MeetingStats(ApiModel):
    duration_seconds: float
    speech_seconds: float  # union of all segments, so overlapping speech counts once
    silence_seconds: float
    turns: int
    speakers: list[SpeakerStats]  # most talk time first
    timeline: list[Turn]


def _union_length(intervals: list[tuple[float, float]]) -> float:
    total, cur_start, cur_end = 0.0, None, None
    for start, end in sorted(intervals):
        if cur_end is None or start > cur_end:
            if cur_end is not None:
                total += cur_end - cur_start
            cur_start, cur_end = start, end
        else:
            cur_end = max(cur_end, end)
    if cur_end is not None:
        total += cur_end - cur_start
    return total


def meeting_stats(segments: list[TranscriptSegment]) -> MeetingStats:
    segs = [s for s in segments if s.end > s.start]
    timeline: list[Turn] = []
    for idx, s in enumerate(segments):
        if s.end <= s.start:
            continue
        if timeline and timeline[-1].speaker == s.speaker:
            timeline[-1].end = max(timeline[-1].end, s.end)
        else:
            timeline.append(Turn(speaker=s.speaker, start=s.start, end=s.end, first_idx=idx))

    talk: dict[str, float] = {}
    words: dict[str, int] = {}
    for s in segs:
        talk[s.speaker] = talk.get(s.speaker, 0.0) + (s.end - s.start)
        words[s.speaker] = words.get(s.speaker, 0) + len(s.text.split())
    total_talk = sum(talk.values()) or 1.0
    speakers = []
    for spk, secs in talk.items():
        turns = [t for t in timeline if t.speaker == spk]
        speakers.append(
            SpeakerStats(
                speaker=spk,
                talk_seconds=round(secs, 1),
                share=round(secs / total_talk, 3),
                turns=len(turns),
                longest_turn_seconds=round(max(t.end - t.start for t in turns), 1),
                words=words[spk],
                words_per_minute=round(words[spk] / (secs / 60), 1) if secs > 0 else 0.0,
            )
        )
    speakers.sort(key=lambda x: x.talk_seconds, reverse=True)
    duration = max((s.end for s in segs), default=0.0)
    speech = _union_length([(s.start, s.end) for s in segs])
    return MeetingStats(
        duration_seconds=round(duration, 1),
        speech_seconds=round(speech, 1),
        silence_seconds=round(max(0.0, duration - speech), 1),
        turns=len(timeline),
        speakers=speakers,
        timeline=timeline,
    )
