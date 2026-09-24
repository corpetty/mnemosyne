"""Chapters in structured summaries: parsing, snapping, export."""

import json

from mnemosyne.export.templates import render_meeting_note
from mnemosyne.models.session import Chapter
from mnemosyne.summarization.prompts import (
    get_system_prompt,
    parse_summary_response,
    snap_chapters,
)


def test_prompt_asks_for_chapters():
    assert '"chapters"' in get_system_prompt(20)


def test_parse_chapters_accepts_formats_and_drops_junk():
    raw = json.dumps(
        {
            "summary": "s",
            "chapters": [
                {"start": "[01:05]", "title": "Budget"},
                {"start": "00:00", "title": "  Intro   and   hello "},
                {"start": "1:02:03", "title": "Late"},
                {"start": 90, "title": "Numeric"},
                {"start": "soon", "title": "Bad time"},
                {"start": "00:00", "title": "Duplicate start"},
                {"title": "No start"},
                "not a dict",
            ],
        }
    )
    _, data = parse_summary_response(raw)
    assert [(c.start, c.title) for c in data.chapters] == [
        (0.0, "Intro and hello"),
        (65.0, "Budget"),
        (90.0, "Numeric"),
        (3723.0, "Late"),
    ]


def test_snap_to_line_starts():
    chapters = [
        Chapter(start=0, title="A"),
        Chapter(start=64, title="B"),
        Chapter(start=66, title="B again"),
        Chapter(start=900, title="Past end"),
    ]
    snapped = snap_chapters(chapters, [0.4, 30.0, 65.2, 120.0])
    assert [(c.start, c.title) for c in snapped] == [(0.4, "A"), (65.2, "B")]
    assert snap_chapters(chapters, []) == []


def test_summarize_job_stores_snapped_chapters(client, transcribed_session, fake_provider):
    from tests.conftest import run_summarize

    fake_provider.summary = json.dumps(
        {
            "summary": "s",
            "chapters": [
                {"start": "00:01", "title": "Greetings"},
                {"start": "00:03", "title": "Start"},
            ],
        }
    )
    job = run_summarize(client, transcribed_session["id"], {"provider": "fake"})
    assert job["status"] == "completed", job["error"]
    s = client.get(f"/api/sessions/{transcribed_session['id']}").json()
    # FAKE_SEGMENTS start at 0.0, 1.5 and 3.2
    assert s["summary_data"]["chapters"] == [
        {"start": 1.5, "title": "Greetings"},
        {"start": 3.2, "title": "Start"},
    ]


def test_export_lists_chapters():
    from datetime import datetime

    from mnemosyne.models.session import SummaryData

    md = render_meeting_note(
        title="t",
        date=datetime(2026, 9, 24),
        participants=[],
        transcript_segments=[],
        summary="s",
        notes="",
        summary_data=SummaryData(chapters=[Chapter(start=75, title="Budget")]),
    )
    assert "## Chapters\n\n- 01:15 Budget" in md
