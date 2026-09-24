"""Mention alerts: keyword parsing, spotting, and live `mention` events."""

import pytest

from mnemosyne.transcription.live import LiveSource, LiveTranscriber
from mnemosyne.transcription.mentions import MentionSpotter, parse_keywords
from tests.test_live import RATE, DurationTranscriber, _append_seconds, growing_wav  # noqa: F401


def test_parse_keywords():
    assert parse_keywords("Corey, petty\n corey ,, Mnemosyne  team ") == [
        "Corey",
        "petty",
        "Mnemosyne team",
    ]
    assert parse_keywords("") == []


def test_spotter_whole_words_and_cooldown():
    sp = MentionSpotter(["Corey", "status go"], cooldown=20)
    assert sp.find("hey corey, can you", 1.0) == "Corey"
    assert sp.find("Corey again", 10.0) is None  # cooling down
    assert sp.find("and Corey", 25.0) == "Corey"
    assert sp.find("Coreys notes", 100.0) is None  # not a whole word
    assert sp.find("the Status   Go release", 100.0) == "status go"
    assert not MentionSpotter([])


class WordTranscriber(DurationTranscriber):
    """Like DurationTranscriber, but second 1 says the keyword."""

    async def transcribe(self, path, language=None):
        segs = await super().transcribe(path, language)
        for s in segs:
            if s.start == 1:
                s.text = "over to corey"
        return segs


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("kinds", "expect"),
    [(["system"], 1), (["mixed"], 1), (["mic", "system"], 0)],  # 2nd case: only the mic hears it
)
async def test_live_emits_mentions(growing_wav, tmp_path, kinds, expect):  # noqa: F811
    events = []
    sources = [LiveSource(path=growing_wav, speaker="Them", kind=kinds[0])]
    if len(kinds) > 1:
        # The keyword source is the mic here; add a silent second source.
        other = tmp_path / "other.wav"
        other.write_bytes(growing_wav.read_bytes())
        sources.append(LiveSource(path=other, speaker="Remote", kind=kinds[1]))
    live = LiveTranscriber(
        WordTranscriber(),
        sources,
        events.append,
        "s1",
        mentions=MentionSpotter(["Corey"]),
    )
    await live.transcriber.load()
    _append_seconds(growing_wav, 3.0)
    await live.tick(flush=True)
    mentions = [e for e in events if e["type"] == "mention"]
    assert len(mentions) == expect
    if expect:
        m = mentions[0]
        assert m["keyword"] == "Corey" and m["text"] == "over to corey"
        assert m["speaker"] == "Them" and m["start"] == 1.0 and m["session_id"] == "s1"
