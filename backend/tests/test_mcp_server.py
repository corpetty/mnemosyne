"""MCP tools against the real API in-process (httpx ASGI transport)."""

import json
from datetime import datetime, timedelta

import httpx
import pytest

from mnemosyne.mcp_server import (
    Backend,
    ask_meetings,
    build_server,
    get_action_items,
    get_meeting,
    list_meetings,
    search_meetings,
)
from mnemosyne.models.session import ActionItem, Session, SummaryData
from mnemosyne.models.transcript import TranscriptSegment


@pytest.fixture
def backend(app):
    return Backend(url="http://test", token="", transport=httpx.ASGITransport(app=app))


@pytest.fixture
def seeded(ctx):
    s = ctx.sessions.create_session("Release sync")
    ctx.sessions.set_transcript(
        s.id,
        [
            TranscriptSegment(
                text="The Waku migration ships in October", speaker="Alice", start=65, end=70
            ),
            TranscriptSegment(text="I will update the docs", speaker="Bob", start=71, end=74),
        ],
    )
    ctx.sessions.set_summary(
        s.id,
        "We agreed on October.",
        SummaryData(
            decisions=["Ship in October"],
            action_items=[ActionItem(text="Update docs", owner="Bob")],
        ),
    )
    return s


@pytest.mark.anyio
async def test_list_search_get(backend, seeded):
    out = await list_meetings(backend)
    assert "Release sync" in out and f"id={seeded.id}" in out and "summary" in out
    assert await list_meetings(backend, days=0) != ""

    out = await search_meetings(backend, "migration")
    assert "## Release sync" in out and "[01:05] Alice:" in out and "**migration**" in out
    assert "No matches" in await search_meetings(backend, "zebra")

    out = await get_meeting(backend, seeded.id)
    assert "# Release sync" in out and "## Decisions\n- Ship in October" in out
    assert "- [ ] Update docs (Bob)" in out
    assert "[01:05] Alice: The Waku migration ships in October" in out
    paged = await get_meeting(backend, seeded.id, offset=0, max_lines=1)
    assert "more: call again with offset=1" in paged


@pytest.mark.anyio
async def test_action_items(backend, seeded):
    out = await get_action_items(backend)
    assert "## Release sync" in out and "- [ ] Update docs (Bob)" in out


@pytest.mark.anyio
async def test_ask_polls_job(backend, seeded, ctx, fake_provider):
    ctx.settings.default_provider = "fake"
    fake_provider.reply = "It ships in October [1]."
    out = await ask_meetings(backend, "When does the Waku migration ship?")
    assert out.startswith("It ships in October [1].")
    assert "Sources:\n[1] Release sync" in out and "at 01:05" in out


@pytest.mark.anyio
async def test_token_sent(app, ctx, seeded):
    ctx.settings.api_token = "tok"
    anon = Backend(url="http://test", token="", transport=httpx.ASGITransport(app=app))
    with pytest.raises(httpx.HTTPStatusError):
        await list_meetings(anon)
    authed = Backend(url="http://test", token="tok", transport=httpx.ASGITransport(app=app))
    assert "Release sync" in await list_meetings(authed)


@pytest.mark.anyio
async def test_server_exposes_tools(backend, seeded):
    server = build_server(backend)
    names = {t.name for t in await server.list_tools()}
    assert names == {
        "list_meetings",
        "search_meetings",
        "ask_meetings",
        "get_meeting",
        "get_action_items",
    }
    result = await server.call_tool("search_meetings", {"query": "docs"})
    text = json.dumps(result.model_dump())
    assert "Release sync" in text


@pytest.mark.anyio
async def test_local_only_meetings_are_not_shared(backend, ctx, seeded, fake_provider):
    from mnemosyne.mcp_server import LOCAL_ONLY_NOTE

    ctx.repo.update_fields(seeded.id, local_only=True)
    assert "Release sync" not in await list_meetings(backend)
    assert "Release sync" not in await search_meetings(backend, "migration")
    assert await get_meeting(backend, seeded.id) == LOCAL_ONLY_NOTE
    assert "Update docs" not in await get_action_items(backend)
    # Ask leaves it out even though the configured provider is local.
    ctx.settings.default_provider = "fake"
    out = await ask_meetings(backend, "When does the migration ship?")
    assert "October" not in json.dumps(fake_provider.calls)
    assert "Release sync" not in out


@pytest.mark.anyio
async def test_action_items_linked_task_listed_once(backend, ctx):
    for day, text in ((1, "Rotate keys"), (8, "Rotate all the keys")):
        ctx.repo.save(
            Session(
                name=f"Infra {day}",
                created_at=datetime.now().replace(microsecond=0) - timedelta(days=10 - day),
                summary="s",
                summary_data=SummaryData(
                    action_items=[ActionItem(id="i", text=text, owner="Bob", task_id="t1")]
                ),
            )
        )
    out = await get_action_items(backend, days=30)
    assert out.count("Rotate") == 1
    assert "- [ ] Rotate all the keys (Bob) (also in 1 earlier meeting)" in out
    assert "## Infra 8" in out and "## Infra 1" not in out


@pytest.mark.anyio
async def test_get_meeting_lists_open_comment_threads(backend, ctx, seeded):
    from mnemosyne.models.comment import CommentCreate
    from mnemosyne.services import comments

    root = comments.create(
        ctx,
        seeded.id,
        CommentCreate(text="Is October firm?", target="body", exact="We agreed on October."),
    )
    comments.create(ctx, seeded.id, CommentCreate(text="Yes, it is", parent_id=root.id))
    done = comments.create(
        ctx, seeded.id, CommentCreate(text="Old question", target="body", exact="agreed on October")
    )
    comments.resolve(ctx, seeded.id, done.id)

    out = await get_meeting(backend, seeded.id)
    section = out.split("## Open comments\n", 1)[1]
    assert section.startswith("- ")
    assert "- " in section.splitlines()[0] and "Is October firm?" in section.splitlines()[0]
    assert "(on “We agreed on October.”)" in section.splitlines()[0]
    assert "\n  - " in section and "Yes, it is" in section
    assert "Old question" not in out


@pytest.mark.anyio
async def test_get_meeting_prints_the_notes_chips_as_text(backend, ctx, seeded):
    ctx.sessions.update_notes(
        seeded.id,
        "At [01:05](mnemosyne:t/0/65) [@Bob](mnemosyne:person/Bob) said yes;"
        " see [Kickoff](mnemosyne:meeting/0123456789ab) [site](https://example.com)",
    )
    out = await get_meeting(backend, seeded.id)
    assert "## Notes\nAt 01:05 @Bob said yes; see Kickoff [site](https://example.com)" in out
    assert "mnemosyne:" not in out
