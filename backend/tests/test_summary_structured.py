"""Structured summaries: parsing, styles, endpoint, and the richer export."""

from datetime import datetime

from src.mnemosyne.export.obsidian import ObsidianExporter
from src.mnemosyne.models.session import ActionItem, Session, SummaryData
from src.mnemosyne.models.transcript import TranscriptSegment
from src.mnemosyne.summarization.prompts import (
    STYLES,
    get_system_prompt,
    parse_summary_response,
)


def test_parse_clean_json():
    raw = (
        '{"summary": "We planned the release.", "topics": ["release", "docs"],'
        ' "decisions": ["Ship Friday"], "action_items": [{"text": "Update docs", "owner": "Alice"},'
        ' {"text": "Tag release", "owner": null}], "open_questions": ["Who reviews?"]}'
    )
    summary, data = parse_summary_response(raw, style="meeting")
    assert summary == "We planned the release."
    assert data.topics == ["release", "docs"]
    assert data.decisions == ["Ship Friday"]
    assert data.action_items == [
        ActionItem(text="Update docs", owner="Alice"),
        ActionItem(text="Tag release", owner=None),
    ]
    assert data.open_questions == ["Who reviews?"]
    assert data.style == "meeting"


def test_parse_fenced_and_messy_json():
    raw = (
        "Sure! Here you go:\n```json\n"
        '{"summary": ["a", "b"], "action_items": ["Do X"], "decisions": [{"text": "Y"}]}'
        "\n```\nHope that helps."
    )
    summary, data = parse_summary_response(raw)
    assert summary == "- a\n- b"
    assert data.action_items == [ActionItem(text="Do X")]
    assert data.decisions == ["Y"]
    assert data.topics == []


def test_parse_fallback_to_plain_text():
    summary, data = parse_summary_response("## Meeting\n- talked about stuff")
    assert summary.startswith("## Meeting")
    assert data == SummaryData()


def test_system_prompt_styles_and_instructions():
    p = get_system_prompt(50, style="standup", extra="Always mention Waku.")
    assert "standup" in p and "Always mention Waku." in p and '"action_items"' in p
    assert "short" in get_system_prompt(3)
    assert get_system_prompt(50, style="nope").startswith(STYLES["meeting"])


def test_summarize_endpoint_stores_structured(client, ctx, fake_provider, transcribed_session):
    fake_provider.summary = (
        '{"summary": "Body", "topics": ["t"], "decisions": ["d"],'
        ' "action_items": [{"text": "a", "owner": "SPEAKER_00"}], "open_questions": []}'
    )
    sid = transcribed_session["id"]
    resp = client.post(
        f"/api/sessions/{sid}/summarize", json={"provider": "fake", "style": "standup"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] == "Body"
    assert body["data"]["style"] == "standup"
    assert body["data"]["action_items"] == [{"text": "a", "owner": "SPEAKER_00"}]
    assert body["data"]["provider"] == "fake"
    assert "standup" in fake_provider.calls[0]["system_prompt"]

    session = client.get(f"/api/sessions/{sid}").json()
    assert session["summary"] == "Body"
    assert session["data" if "data" in session else "summary_data"]["decisions"] == ["d"]
    assert client.post(f"/api/sessions/{sid}/summarize", json={"style": "bogus"}).status_code == 400
    assert client.get("/api/summary-styles").json()[0]["id"] == "meeting"


def test_summary_instructions_setting_used(client, ctx, fake_provider, transcribed_session):
    # Set directly: PUT /api/settings rebuilds the summarizer and would drop the fake.
    ctx.settings.summary_instructions = "Mention the moon."
    client.post(f"/api/sessions/{transcribed_session['id']}/summarize", json={"provider": "fake"})
    assert "Mention the moon." in fake_provider.calls[-1]["system_prompt"]


def _session():
    return Session(
        name="Weekly Sync",
        created_at=datetime(2026, 9, 22, 10, 0),
        participants=["Me", "Alice", "SPEAKER_02"],
        transcript=[TranscriptSegment(text="hi", speaker="Alice", start=0, end=125.0)],
        summary="Body text",
        summary_data=SummaryData(
            topics=["release"],
            decisions=["Ship Friday"],
            action_items=[ActionItem(text="Update docs", owner="Alice"), ActionItem(text="Tag")],
            open_questions=["Who reviews?"],
        ),
        notes="my notes",
    )


def test_render_structured_note(tmp_path):
    md = ObsidianExporter(str(tmp_path), tags=["meeting", "logos"]).render(_session())
    assert 'participants: ["Me", "Alice", "SPEAKER_02"]' in md
    assert 'people: ["[[Me]]", "[[Alice]]"]' in md  # labels never become links
    assert 'topics: ["release"]' in md
    assert "tags: [meeting, logos]" in md
    assert "duration_minutes: 2" in md
    assert "**Participants:** [[Me]], [[Alice]], SPEAKER_02" in md
    assert "## Decisions\n\n- Ship Friday" in md
    assert "- [ ] Update docs ([[Alice]])\n- [ ] Tag" in md
    assert "## Open Questions\n\n- Who reviews?" in md
    assert "## Transcript" in md and "**[00:00] Alice:** hi" in md


def test_render_without_links_or_transcript(tmp_path):
    md = ObsidianExporter(str(tmp_path), link_people=False, include_transcript=False).render(
        _session()
    )
    assert "[[" not in md
    assert "## Transcript" not in md
    assert "- [ ] Update docs (Alice)" in md


def test_markdown_preview_endpoint(client, ctx, transcribed_session):
    client.put(
        "/api/settings", json={"obsidian_tags": "x, y", "obsidian_include_transcript": False}
    )
    md = client.get(f"/api/sessions/{transcribed_session['id']}/export/markdown").json()["markdown"]
    assert "tags: [x, y]" in md and "## Transcript" not in md
