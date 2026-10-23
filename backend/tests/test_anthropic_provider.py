"""AnthropicProvider: thinking blocks, token limit, effort for Claude 5 models."""

import json

import httpx
import pytest

from mnemosyne.summarization.anthropic_provider import AnthropicProvider


def _patch(monkeypatch, reply):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=reply)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    return bodies


def _text(t):
    return {"type": "text", "text": t}


THINK = {"type": "thinking", "thinking": "hmm", "signature": "x"}


@pytest.mark.anyio
async def test_thinking_block_then_text(monkeypatch):
    _patch(monkeypatch, {"content": [THINK, _text("hello")], "stop_reason": "end_turn"})
    assert await AnthropicProvider("k").complete("s", "u", "claude-opus-5-5") == "hello"


@pytest.mark.anyio
async def test_text_blocks_joined(monkeypatch):
    reply = {
        "content": [THINK, _text("a"), {"type": "redacted_thinking", "data": "z"}, _text("b")],
        "stop_reason": "end_turn",
    }
    _patch(monkeypatch, reply)
    assert await AnthropicProvider("k").complete("s", "u", "claude-opus-5-5") == "ab"


@pytest.mark.anyio
async def test_request_body(monkeypatch):
    bodies = _patch(monkeypatch, {"content": [_text("ok")], "stop_reason": "end_turn"})
    p = AnthropicProvider("k")
    await p.complete("s", "u", "claude-sonnet-5-5")
    assert bodies[-1]["max_tokens"] == 16000
    assert "thinking" not in bodies[-1]
    assert "output_config" not in bodies[-1]
    await p.complete("s", "u", "claude-sonnet-5-5", think=False)
    assert bodies[-1]["output_config"] == {"effort": "low"}
    assert "thinking" not in bodies[-1]
    await p.complete("s", "u", "claude-fable-5-1", think=False)
    assert bodies[-1]["output_config"] == {"effort": "low"}
    for old in ("claude-haiku-4-5", "claude-sonnet-4-5", "claude-3-5-sonnet-latest"):
        await p.complete("s", "u", old, think=False)
        assert "output_config" not in bodies[-1]


@pytest.mark.anyio
async def test_only_thinking_at_token_limit(monkeypatch):
    _patch(monkeypatch, {"content": [THINK], "stop_reason": "max_tokens"})
    with pytest.raises(RuntimeError, match="token limit"):
        await AnthropicProvider("k").complete("s", "u", "claude-opus-5-5")


@pytest.mark.anyio
async def test_refusal(monkeypatch):
    _patch(monkeypatch, {"content": [], "stop_reason": "refusal"})
    with pytest.raises(RuntimeError, match="declined"):
        await AnthropicProvider("k").complete("s", "u", "claude-opus-5-5")


@pytest.mark.anyio
async def test_no_text_other_reason(monkeypatch):
    _patch(monkeypatch, {"content": [], "stop_reason": "end_turn"})
    with pytest.raises(RuntimeError, match="no text"):
        await AnthropicProvider("k").complete("s", "u", "claude-opus-5-5")


@pytest.mark.anyio
async def test_text_with_max_tokens_returned(monkeypatch):
    _patch(monkeypatch, {"content": [THINK, _text('{"a"')], "stop_reason": "max_tokens"})
    assert await AnthropicProvider("k").complete("s", "u", "claude-opus-5-5") == '{"a"'
