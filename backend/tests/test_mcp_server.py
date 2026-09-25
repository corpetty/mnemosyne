"""MCP tools against the real API in-process (httpx ASGI transport)."""

import json

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
from mnemosyne.models.session import ActionItem, SummaryData
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
