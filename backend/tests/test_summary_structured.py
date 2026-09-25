"""Structured summaries: parsing, styles, endpoint, and the richer export."""

from datetime import datetime

from mnemosyne.export.obsidian import ObsidianExporter
from mnemosyne.models.session import ActionItem, Session, SummaryData
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.summarization.prompts import (
    STYLES,
    get_system_prompt,
    parse_summary_response,
)
from tests.conftest import run_summarize


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


def test_parse_unescapes_double_escaped_newlines():
    raw = '{"summary": "- one\\\\n- two", "topics": []}'
    summary, _ = parse_summary_response(raw)
    assert summary == "- one\n- two"


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
    job = run_summarize(client, sid, {"provider": "fake", "style": "standup"})
    assert job["status"] == "completed"
    assert job["result"]["provider"] == "fake"
    assert "standup" in fake_provider.calls[0]["system_prompt"]

    session = client.get(f"/api/sessions/{sid}").json()
    assert session["summary"] == "Body"
    data = session["summary_data"]
    assert data["style"] == "standup" and data["provider"] == "fake"
    assert data["decisions"] == ["d"]
    assert data["action_items"] == [
        {"text": "a", "owner": "SPEAKER_00", "issue_url": None, "done": False, "live": False}
    ]
    assert client.post(f"/api/sessions/{sid}/summarize", json={"style": "bogus"}).status_code == 400
    assert client.get("/api/summary-styles").json()[0]["id"] == "meeting"


def test_summary_instructions_setting_used(client, ctx, fake_provider, transcribed_session):
    # Set directly: PUT /api/settings rebuilds the summarizer and would drop the fake.
    ctx.settings.summary_instructions = "Mention the moon."
    run_summarize(client, transcribed_session["id"], {"provider": "fake"})
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


TITLED = (
    '{"title": "  \\"Release planning sync.\\" ", "summary": "Body", "topics": [],'
    ' "decisions": [], "action_items": [], "open_questions": []}'
)


def test_title_parsed_cleaned_and_truncated():
    _, data = parse_summary_response(TITLED)
    assert data.title == "Release planning sync"
    long = '{"title": "' + "word " * 40 + '", "summary": "x"}'
    assert len(parse_summary_response(long)[1].title) <= 80
    assert parse_summary_response('{"summary": "x"}')[1].title == ""
    assert parse_summary_response('{"title": 5, "summary": "x"}')[1].title == ""


def test_summarize_names_untitled_session(client, ctx, fake_provider, fake_engine):
    fake_provider.summary = TITLED
    sid = client.post("/api/sessions", json={}).json()["id"]
    assert client.get(f"/api/sessions/{sid}").json()["name"] == "Untitled Session"
    ctx.sessions.set_transcript(sid, [TranscriptSegment(text="hi", speaker="S", start=0, end=1)])
    job = run_summarize(client, sid, {"provider": "fake"})
    assert job["result"]["title"] == "Release planning sync"
    session = client.get(f"/api/sessions/{sid}").json()
    assert session["name"] == "Release planning sync"
    assert session["summary_data"]["title"] == "Release planning sync"


def test_summarize_keeps_custom_name(client, ctx, fake_provider, transcribed_session):
    fake_provider.summary = TITLED
    sid = transcribed_session["id"]  # named "Transcribed" by the fixture
    run_summarize(client, sid, {"provider": "fake"})
    assert client.get(f"/api/sessions/{sid}").json()["name"] == "Transcribed"


def test_auto_name_can_be_disabled(client, ctx, fake_provider):
    ctx.settings.auto_name_sessions = False
    fake_provider.summary = TITLED
    sid = client.post("/api/sessions", json={}).json()["id"]
    ctx.sessions.set_transcript(sid, [TranscriptSegment(text="hi", speaker="S", start=0, end=1)])
    run_summarize(client, sid, {"provider": "fake"})
    assert client.get(f"/api/sessions/{sid}").json()["name"] == "Untitled Session"


def test_summary_goes_stale_when_transcript_changes(
    client, ctx, fake_provider, transcribed_session
):
    sid = transcribed_session["id"]
    run_summarize(client, sid, {"provider": "fake"})
    s = client.get(f"/api/sessions/{sid}").json()
    assert s["summary_stale"] is False and s["summary_data"]["source_hash"]
    client.patch(f"/api/sessions/{sid}/segments/0", json={"speaker": "Alice"})
    assert client.get(f"/api/sessions/{sid}").json()["summary_stale"] is True
    run_summarize(client, sid, {"provider": "fake"})
    assert client.get(f"/api/sessions/{sid}").json()["summary_stale"] is False
    # no summary -> never stale
    other = client.post("/api/sessions", json={}).json()
    assert other["summary_stale"] is False


def test_auto_export_after_summary(client, ctx, fake_provider, transcribed_session, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    ctx.settings.obsidian_vault_path = str(vault)
    ctx.settings.obsidian_subfolder = "m"
    job = run_summarize(client, transcribed_session["id"], {"provider": "fake"})
    assert job["result"]["exported"] is None  # off by default
    assert not (vault / "m").exists()
    ctx.settings.obsidian_auto_export = True
    job = run_summarize(client, transcribed_session["id"], {"provider": "fake"})
    assert job["result"]["exported"].endswith(".md")
    assert "## Summary" in open(job["result"]["exported"]).read()
    # a missing vault never fails the summary
    ctx.settings.obsidian_vault_path = str(tmp_path / "gone")
    job = run_summarize(client, transcribed_session["id"], {"provider": "fake"})
    assert job["status"] == "completed" and job["result"]["exported"] is None
