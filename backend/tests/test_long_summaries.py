"""Long meetings: summarize in parts, then merge."""

import json

import pytest

from mnemosyne.services.summarization_service import SummarizationService, merge_parts
from mnemosyne.summarization.prompts import split_lines


def test_split_lines():
    lines = ["a" * 9, "b" * 9, "c" * 9, "d" * 30]
    assert split_lines(lines, 20) == [(0, 2), (2, 3), (3, 4)]
    assert split_lines(lines, 1000) == [(0, 4)]
    assert split_lines([], 10) == []


def _segments(n):
    return [
        {
            "text": f"line {i} " + "word " * 10,
            "speaker": f"S{i % 2}",
            "start": i * 60.0,
            "end": i * 60.0 + 50,
        }
        for i in range(n)
    ]


def _part(i):
    return json.dumps(
        {
            "summary": f"part {i}",
            "topics": [f"t{i}", "shared"],
            "decisions": [f"decision {i}", "Ship in October"],
            "action_items": [{"text": f"task {i}", "owner": "S0"}, {"text": "Update the docs"}],
            "open_questions": [f"q{i}"],
            "chapters": [{"start": f"{i * 2:02d}:00", "title": f"Chapter {i}"}],
        }
    )


class ScriptedProvider:
    name = "scripted"

    def __init__(self, reduce_reply):
        self.reduce_reply = reduce_reply
        self.calls = []

    async def list_models(self):
        return ["m"]

    async def summarize(self, transcript, model, system_prompt):
        self.calls.append(("map", system_prompt, transcript))
        return _part(sum(1 for c in self.calls if c[0] == "map"))

    async def complete(self, system_prompt, user_prompt, model):
        self.calls.append(("reduce", system_prompt, user_prompt))
        return self.reduce_reply


@pytest.mark.anyio
async def test_map_reduce_with_progress():
    reduce_reply = json.dumps(
        {
            "summary": "whole meeting",
            "decisions": ["Ship in October"],
            "action_items": [{"text": "Update the docs"}],
            "chapters": [{"start": "02:00", "title": "Merged"}],
        }
    )
    svc = SummarizationService()
    svc.chunk_chars = 250
    provider = ScriptedProvider(reduce_reply)
    svc.providers = {"p": provider}
    progress = []
    out = await svc.summarize(_segments(6), "p", on_progress=progress.append)
    maps = [c for c in provider.calls if c[0] == "map"]
    assert len(maps) >= 2
    assert "part 1 of" in maps[0][1] and "Summarize only this part" in maps[0][1]
    assert maps[0][2].startswith("[00:00] S0: line 0")
    reduce_ = [c for c in provider.calls if c[0] == "reduce"]
    assert len(reduce_) == 1 and "merge partial notes" in reduce_[0][1]
    payload = json.loads(reduce_[0][2])
    assert payload[0]["part"] == 1 and payload[0]["from"] == "00:00"
    assert out["summary"] == "whole meeting"
    assert out["data"].decisions == ["Ship in October"]
    assert [c.title for c in out["data"].chapters] == ["Merged"]
    assert out["data"].chapters[0].start == 120.0  # snapped to the line at 2:00
    assert progress[0] == f"Summarizing part 1 of {len(maps)}"
    assert progress[-1] == f"Merging {len(maps)} parts"


@pytest.mark.anyio
async def test_merge_falls_back_when_reduce_is_not_json():
    svc = SummarizationService()
    svc.chunk_chars = 250
    svc.providers = {"p": ScriptedProvider("Sorry, I can't do that.")}
    out = await svc.summarize(_segments(6), "p")
    d = out["data"]
    assert out["summary"].startswith("**00:00–")
    assert d.decisions[:2] == ["decision 1", "Ship in October"]
    assert sum(1 for a in d.action_items if a.text == "Update the docs") == 1  # deduped
    assert [c.title for c in d.chapters][:2] == ["Chapter 1", "Chapter 2"]


@pytest.mark.anyio
async def test_short_transcripts_are_not_split():
    svc = SummarizationService()
    svc.chunk_chars = 100000
    provider = ScriptedProvider("unused")
    svc.providers = {"p": provider}
    await svc.summarize(_segments(3), "p")
    assert [c[0] for c in provider.calls] == ["map"]


def test_merge_parts_unit():
    parts = [
        {
            "part": 1,
            "from": "00:00",
            "to": "10:00",
            "summary": "a",
            "topics": ["x"],
            "decisions": ["d"],
            "action_items": [{"text": "t", "owner": None}],
            "open_questions": [],
            "chapters": [{"start": "00:00", "title": "A"}],
        },
        {
            "part": 2,
            "from": "10:00",
            "to": "20:00",
            "summary": "b",
            "topics": ["x", "y"],
            "decisions": ["d."],
            "action_items": [{"text": "t.", "owner": "S1"}],
            "open_questions": ["q"],
            "chapters": [{"start": "10:00", "title": "B"}],
        },
    ]
    summary, d = merge_parts(parts, "meeting")
    assert d.topics == ["x", "y"] and d.decisions == ["d"] and len(d.action_items) == 1
    assert [(c.start, c.title) for c in d.chapters] == [(0.0, "A"), (600.0, "B")]
