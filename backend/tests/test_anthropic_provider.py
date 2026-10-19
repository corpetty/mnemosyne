import json

import httpx
import pytest

from mnemosyne.summarization.anthropic_provider import AnthropicProvider


@pytest.fixture
def replies(monkeypatch):
    """The Messages API answering with the queued bodies; requests are kept."""
    queued, bodies = [], []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=queued.pop(0))

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    return queued, bodies


@pytest.mark.anyio
async def test_text_after_thinking_block(replies):
    # Claude 5 models think by default: the answer is not the first block.
    queued, bodies = replies
    queued.append(
        {
            "stop_reason": "end_turn",
            "content": [
                {"type": "thinking", "thinking": "", "signature": "x"},
                {"type": "text", "text": '{"summary": '},
                {"type": "text", "text": '"ok"}'},
            ],
        }
    )
    text = await AnthropicProvider("key").complete("s", "u", "claude-sonnet-5-5")
    assert text == '{"summary": "ok"}'
    assert bodies[0]["max_tokens"] >= 16000  # room for thinking and a long JSON


@pytest.mark.anyio
async def test_no_text_is_an_error_not_an_empty_summary(replies):
    queued, _ = replies
    queued.append(
        {
            "stop_reason": "max_tokens",
            "content": [{"type": "thinking", "thinking": "", "signature": "x"}],
        }
    )
    with pytest.raises(RuntimeError, match="max_tokens"):
        await AnthropicProvider("key").complete("s", "u", "claude-haiku-5-5")
