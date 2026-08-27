from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import BaseModel

from core.openai_cached_chat import (
    CachedChatOpenAI,
    _CACHE_WARMUP_MESSAGE,
    _CachedCompletions,
    _messages_with_system_cache_breakpoint,
)
from browser_use.llm.messages import (
    ContentPartImageParam,
    ImageURL,
    SystemMessage,
    UserMessage,
)
from browser_use.tokens.service import TokenCost


def test_system_cache_breakpoint_is_added_without_mutating_messages():
    messages = [
        {"role": "system", "content": "Stable instructions"},
        {"role": "user", "content": "Changing browser state"},
    ]
    original = deepcopy(messages)

    cached = _messages_with_system_cache_breakpoint(messages)

    assert messages == original
    assert cached[0]["content"] == [
        {
            "type": "text",
            "text": "Stable instructions",
            "prompt_cache_breakpoint": {"mode": "explicit"},
        }
    ]
    assert cached[1] == messages[1]


@pytest.mark.asyncio
async def test_cached_completions_adds_explicit_only_request_options():
    calls = []
    expected = object()

    class FakeCompletions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return expected

    result = await _CachedCompletions(FakeCompletions(), "hunter:test").create(
        model="gpt-5.6-terra",
        messages=[
            {"role": "system", "content": "Stable instructions"},
            {"role": "user", "content": "Changing browser state"},
        ],
        extra_body={"existing": True},
    )

    assert result is expected
    assert calls[0]["prompt_cache_key"] == "hunter:test"
    assert calls[0]["extra_body"] == {
        "existing": True,
        "prompt_cache_options": {"mode": "explicit", "ttl": "30m"},
    }
    assert calls[0]["messages"][0]["content"][0]["prompt_cache_breakpoint"] == {
        "mode": "explicit"
    }


@pytest.mark.asyncio
async def test_warmup_failure_does_not_block_or_repeat_the_real_request():
    calls = []
    expected = object()

    class FakeCompletions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise RuntimeError("temporary warmup failure")
            return expected

    completions = _CachedCompletions(
        FakeCompletions(),
        "hunter:test",
        warmup_on_first_image=True,
    )
    kwargs = {
        "model": "gpt-5.6-terra",
        "messages": [
            {"role": "system", "content": "Stable instructions"},
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": "data:image/png"}}],
            },
        ],
    }

    first = await completions.create(**kwargs)
    second = await completions.create(**kwargs)

    assert first is expected
    assert second is expected
    assert len(calls) == 3
    assert calls[0]["messages"][-1]["content"] == _CACHE_WARMUP_MESSAGE
    assert calls[1]["messages"][-1]["content"][0]["type"] == "image_url"
    assert calls[2]["messages"][-1]["content"][0]["type"] == "image_url"


@pytest.mark.asyncio
async def test_cached_chat_sends_documented_request_shape_through_installed_sdk():
    requests = []

    class Output(BaseModel):
        ok: bool

    async def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-5.6-terra",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": '{"ok":true}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1_000,
                    "completion_tokens": 10,
                    "total_tokens": 1_010,
                    "prompt_tokens_details": {
                        "cached_tokens": 0,
                        "cache_write_tokens": 200,
                    },
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        llm = CachedChatOpenAI(
            model="gpt-5.6-terra",
            api_key="test",
            http_client=client,
            prompt_cache_key="hunter:test",
        )
        result = await llm.ainvoke(
            [
                SystemMessage(content="Stable instructions"),
                UserMessage(content="Changing browser state"),
            ],
            output_format=Output,
        )

    body = requests[0]
    assert result.completion.ok is True
    assert result.usage.prompt_cache_creation_tokens == 200
    assert body["prompt_cache_key"] == "hunter:test"
    assert body["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
    assert body["messages"][0]["content"][0]["prompt_cache_breakpoint"] == {
        "mode": "explicit"
    }


@pytest.mark.asyncio
async def test_first_vision_request_is_warmed_once_and_usage_is_included():
    requests = []

    class Output(BaseModel):
        ok: bool

    async def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        is_warmup = body["messages"][-1]["content"] == _CACHE_WARMUP_MESSAGE
        prompt_tokens = 1_000 if is_warmup else 1_200
        completion_tokens = 2 if is_warmup else 10
        return httpx.Response(
            200,
            json={
                "id": f"chatcmpl-{len(requests)}",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-5.6-terra",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": '{"ok":true}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                    "prompt_tokens_details": {
                        "cached_tokens": 0 if is_warmup else 800,
                        "cache_write_tokens": 800 if is_warmup else 0,
                    },
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        llm = CachedChatOpenAI(
            model="gpt-5.6-terra",
            api_key="test",
            http_client=client,
            prompt_cache_key="hunter:test",
        )
        llm.enable_vision_cache_warmup()
        messages = [
            SystemMessage(content="Stable instructions"),
            UserMessage(
                content=[
                    ContentPartImageParam(
                        image_url=ImageURL(
                            url="data:image/png;base64,iVBORw0KGgo=",
                            detail="low",
                        )
                    )
                ]
            ),
        ]

        first = await llm.ainvoke(messages, output_format=Output)
        second = await llm.ainvoke(messages, output_format=Output)

    assert len(requests) == 3
    assert requests[0]["messages"][-1] == {
        "role": "user",
        "content": _CACHE_WARMUP_MESSAGE,
    }
    assert all(
        part.get("type") != "image_url"
        for message in requests[0]["messages"]
        for part in message["content"]
        if isinstance(message.get("content"), list)
    )
    assert requests[1]["messages"][-1]["content"][0]["type"] == "image_url"
    assert requests[0]["response_format"] == requests[1]["response_format"]
    assert requests[0]["prompt_cache_key"] == requests[1]["prompt_cache_key"]
    assert requests[0]["prompt_cache_options"] == requests[1]["prompt_cache_options"]
    assert first.usage.prompt_tokens == 1_400
    assert first.usage.prompt_cached_tokens == 800
    assert first.usage.prompt_cache_creation_tokens == 800
    assert first.usage.completion_tokens == 12
    assert first.usage.total_tokens == 2_212
    assert second.usage.prompt_tokens == 1_200
    assert second.usage.prompt_cached_tokens == 800
    assert second.usage.prompt_cache_creation_tokens is None


@pytest.mark.asyncio
async def test_cache_write_usage_is_separated_for_accurate_costing():
    llm = CachedChatOpenAI(model="gpt-5.6-terra", api_key="test")
    response = SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=1_000,
            completion_tokens=50,
            total_tokens=1_050,
            prompt_tokens_details=SimpleNamespace(
                cached_tokens=100,
                cache_write_tokens=200,
            ),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=30),
        )
    )

    usage = llm._get_usage(response)

    assert usage.prompt_tokens == 800
    assert usage.prompt_cached_tokens == 100
    assert usage.prompt_cache_creation_tokens == 200
    assert usage.total_tokens == 1_050
    assert llm.detailed_completion_usage() == {
        "visible_completion_tokens": 20,
        "reasoning_tokens": 30,
    }

    tracker = TokenCost(include_cost=True)
    tracker._initialized = True
    tracker._pricing_data = {
        "test-cache-model": {
            "input_cost_per_token": 2 / 1_000_000,
            "cache_read_input_token_cost": 0.2 / 1_000_000,
            "cache_creation_input_token_cost": 2.5 / 1_000_000,
            "output_cost_per_token": 12 / 1_000_000,
        }
    }

    cost = await tracker.calculate_cost("test-cache-model", usage)

    assert cost.new_prompt_cost == pytest.approx(700 * 2 / 1_000_000)
    assert cost.prompt_read_cached_cost == pytest.approx(100 * 0.2 / 1_000_000)
    assert cost.prompt_cache_creation_cost == pytest.approx(200 * 2.5 / 1_000_000)
