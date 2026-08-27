"""OpenAI chat adapter with explicit caching for Hunter's stable system prompt."""

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from browser_use.llm import ChatOpenAI
from browser_use.llm.views import ChatInvokeUsage


logger = logging.getLogger(__name__)

_CACHE_WARMUP_MESSAGE = (
    "THIS IS A CACHE-WARMING REQUEST. DO NOT INFER OR PERFORM ANY REAL BROWSER ACTION. "
    "RETURN THE SMALLEST VALID RESPONSE THAT MATCHES THE REQUIRED RESPONSE SCHEMA."
)


def _messages_with_system_cache_breakpoint(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a copy with an explicit breakpoint at the end of the system message."""
    cached_messages = deepcopy(messages)
    for message in cached_messages:
        if message.get("role") != "system":
            continue

        content = message.get("content")
        if isinstance(content, str):
            message["content"] = [
                {
                    "type": "text",
                    "text": content,
                    "prompt_cache_breakpoint": {"mode": "explicit"},
                }
            ]
            return cached_messages

        if isinstance(content, list):
            for part in reversed(content):
                if isinstance(part, dict) and part.get("type") == "text":
                    part["prompt_cache_breakpoint"] = {"mode": "explicit"}
                    return cached_messages
        break

    return cached_messages


def _has_image(messages: list[dict[str, Any]]) -> bool:
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        if any(
            isinstance(part, dict) and part.get("type") in {"image_url", "input_image"}
            for part in content
        ):
            return True
    return False


def _warmup_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """Keep the marked stable prefix and append one schema-valid warmup request."""
    for index, message in enumerate(messages):
        if message.get("role") != "system":
            continue
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(part, dict) and part.get("prompt_cache_breakpoint")
            for part in content
        ):
            return deepcopy(messages[: index + 1]) + [
                {"role": "user", "content": _CACHE_WARMUP_MESSAGE}
            ]
        break
    return None


@dataclass
class _CacheWarmupState:
    attempted: bool = False
    response: Any | None = None


class _CachedCompletions:
    def __init__(
        self,
        completions: Any,
        cache_key: str,
        warmup_on_first_image: bool = False,
        warmup_state: _CacheWarmupState | None = None,
    ) -> None:
        self._completions = completions
        self._cache_key = cache_key
        self._warmup_on_first_image = warmup_on_first_image
        self._warmup_state = warmup_state or _CacheWarmupState()

    async def create(self, **kwargs: Any) -> Any:
        kwargs["messages"] = _messages_with_system_cache_breakpoint(list(kwargs["messages"]))
        kwargs["prompt_cache_key"] = self._cache_key
        extra_body = dict(kwargs.get("extra_body") or {})
        extra_body["prompt_cache_options"] = {"mode": "explicit", "ttl": "30m"}
        kwargs["extra_body"] = extra_body

        warmup_response = None
        if (
            self._warmup_on_first_image
            and not self._warmup_state.attempted
            and _has_image(kwargs["messages"])
        ):
            self._warmup_state.attempted = True
            messages = _warmup_messages(kwargs["messages"])
            if messages is not None:
                warmup_kwargs = dict(kwargs)
                warmup_kwargs["messages"] = messages
                try:
                    warmup_response = await self._completions.create(**warmup_kwargs)
                    logger.info("Primed the OpenAI prompt cache before the first vision request")
                except Exception as exc:
                    logger.warning("OpenAI prompt-cache warmup failed; continuing normally: %s", exc)

        response = await self._completions.create(**kwargs)
        if warmup_response is not None:
            self._warmup_state.response = warmup_response
        return response


class _CachedChat:
    def __init__(
        self,
        chat: Any,
        cache_key: str,
        warmup_on_first_image: bool,
        warmup_state: _CacheWarmupState,
    ) -> None:
        self._chat = chat
        self.completions = _CachedCompletions(
            chat.completions,
            cache_key,
            warmup_on_first_image,
            warmup_state,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._chat, name)


class _CachedOpenAIClient:
    def __init__(
        self,
        client: Any,
        cache_key: str,
        warmup_on_first_image: bool,
        warmup_state: _CacheWarmupState,
    ) -> None:
        self._client = client
        self.chat = _CachedChat(
            client.chat,
            cache_key,
            warmup_on_first_image,
            warmup_state,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


@dataclass
class CachedChatOpenAI(ChatOpenAI):
    """Browser Use OpenAI model with a stable explicit system-prompt cache."""

    prompt_cache_key: str = "hunter-openai-v1"
    warmup_on_first_image: bool = False
    _warmup_state: _CacheWarmupState = field(
        default_factory=_CacheWarmupState,
        init=False,
        repr=False,
    )
    _visible_completion_tokens: int = field(default=0, init=False, repr=False)
    _reasoning_tokens: int = field(default=0, init=False, repr=False)

    def enable_vision_cache_warmup(self) -> None:
        """Prime the stable text prefix before this model's first vision request."""
        self.warmup_on_first_image = True

    def get_client(self) -> Any:
        return _CachedOpenAIClient(
            super().get_client(),
            self.prompt_cache_key,
            self.warmup_on_first_image,
            self._warmup_state,
        )

    def _single_usage(self, response: Any) -> ChatInvokeUsage | None:
        usage = super()._get_usage(response)
        if usage is None or response.usage is None:
            return usage

        completion_details = getattr(
            response.usage, "completion_tokens_details", None
        )
        reasoning_tokens = int(
            getattr(completion_details, "reasoning_tokens", 0) or 0
        )
        self._reasoning_tokens += reasoning_tokens
        self._visible_completion_tokens += max(
            0, int(usage.completion_tokens) - reasoning_tokens
        )

        details = response.usage.prompt_tokens_details
        write_tokens = int(getattr(details, "cache_write_tokens", 0) or 0)
        if write_tokens:
            # Browser Use's TokenCost expects cache-creation tokens to be
            # separate from prompt_tokens, while OpenAI includes them there.
            usage.prompt_tokens = max(0, usage.prompt_tokens - write_tokens)
            usage.prompt_cache_creation_tokens = write_tokens
        return usage

    def detailed_completion_usage(self) -> dict[str, int]:
        """Return provider-reported visible output and hidden reasoning totals."""
        return {
            "visible_completion_tokens": self._visible_completion_tokens,
            "reasoning_tokens": self._reasoning_tokens,
        }

    def _get_usage(self, response: Any):
        warmup_response = self._warmup_state.response
        self._warmup_state.response = None
        usage = self._single_usage(response)
        if warmup_response is None:
            return usage

        warmup_usage = self._single_usage(warmup_response)
        if usage is None:
            return warmup_usage
        if warmup_usage is None:
            return usage

        def optional_sum(left: int | None, right: int | None) -> int | None:
            if left is None and right is None:
                return None
            return (left or 0) + (right or 0)

        return ChatInvokeUsage(
            prompt_tokens=usage.prompt_tokens + warmup_usage.prompt_tokens,
            prompt_cached_tokens=optional_sum(
                usage.prompt_cached_tokens,
                warmup_usage.prompt_cached_tokens,
            ),
            prompt_cache_creation_tokens=optional_sum(
                usage.prompt_cache_creation_tokens,
                warmup_usage.prompt_cache_creation_tokens,
            ),
            prompt_image_tokens=optional_sum(
                usage.prompt_image_tokens,
                warmup_usage.prompt_image_tokens,
            ),
            completion_tokens=usage.completion_tokens + warmup_usage.completion_tokens,
            total_tokens=usage.total_tokens + warmup_usage.total_tokens,
        )
