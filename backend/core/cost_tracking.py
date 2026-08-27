"""Categorized LLM cost tracking for one application attempt."""

from __future__ import annotations

from typing import Any

from browser_use.llm.views import ChatInvokeUsage
from browser_use.tokens.custom_pricing import CUSTOM_MODEL_PRICING
from browser_use.tokens.service import TokenCost


# Official OpenAI standard prices per token. Browser Use's downloaded LiteLLM
# table can lag behind price changes, so keep Hunter's active GPT-5.6 models
# explicit. This shared table is also used by Agent's own history cost tracker.
_OPENAI_GPT_5_6_PRICING = {
    "gpt-5.6-sol": {
        "input_cost_per_token": 4.00 / 1_000_000,
        "cache_read_input_token_cost": 0.40 / 1_000_000,
        "cache_creation_input_token_cost": 5.00 / 1_000_000,
        "output_cost_per_token": 20.00 / 1_000_000,
    },
    "gpt-5.6-terra": {
        "input_cost_per_token": 2.00 / 1_000_000,
        "cache_read_input_token_cost": 0.20 / 1_000_000,
        "cache_creation_input_token_cost": 2.50 / 1_000_000,
        "output_cost_per_token": 12.00 / 1_000_000,
    },
    "gpt-5.6-luna": {
        "input_cost_per_token": 0.20 / 1_000_000,
        "cache_read_input_token_cost": 0.02 / 1_000_000,
        "cache_creation_input_token_cost": 0.25 / 1_000_000,
        "output_cost_per_token": 1.20 / 1_000_000,
    },
}

CUSTOM_MODEL_PRICING.update(_OPENAI_GPT_5_6_PRICING)


COST_CATEGORIES = (
    "classification",
    "cover_letter",
    "outreach",
    "cv",
    "application_agent",
    "judge",
    "memory_extract",
)


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _nested_integer(value: Any, key: str) -> int:
    if value is None:
        return 0
    if isinstance(value, dict):
        return _integer(value.get(key))
    return _integer(getattr(value, key, 0))


def response_usage(usage: Any) -> ChatInvokeUsage:
    """Normalize OpenAI Responses usage into Browser Use's usage model."""
    input_details = getattr(usage, "input_tokens_details", None)
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    prompt_tokens = _integer(
        getattr(usage, "input_tokens", None)
        or getattr(usage, "prompt_tokens", None)
    )
    completion_tokens = _integer(
        getattr(usage, "output_tokens", None)
        or getattr(usage, "completion_tokens", None)
    )
    cached_tokens = _nested_integer(input_details, "cached_tokens") or _nested_integer(
        prompt_details, "cached_tokens"
    )
    return ChatInvokeUsage(
        prompt_tokens=prompt_tokens,
        prompt_cached_tokens=cached_tokens,
        prompt_cache_creation_tokens=None,
        prompt_image_tokens=None,
        completion_tokens=completion_tokens,
        total_tokens=_integer(getattr(usage, "total_tokens", None))
        or prompt_tokens + completion_tokens,
    )


async def calculate_response_cost(model: str, usage: Any) -> float | None:
    """Calculate one OpenAI Responses call using Browser Use's price table."""
    if usage is None:
        return None
    tracker = TokenCost(include_cost=True)
    tracker.add_usage(model, response_usage(usage))
    summary = await tracker.get_usage_summary()
    return float(summary.total_cost)


class CategorizedCostTracker:
    """Wrap LLM instances and report costs grouped by application phase."""

    def __init__(self) -> None:
        self._trackers = {
            category: TokenCost(include_cost=True)
            for category in COST_CATEGORIES
            if category != "classification"
        }
        self._llms: dict[str, list[Any]] = {
            category: [] for category in self._trackers
        }

    def register_llm(self, category: str, llm: Any) -> Any:
        tracker = self._trackers.get(category)
        if tracker is None:
            raise ValueError(f"Unknown cost category: {category}")
        if not any(item is llm for item in self._llms[category]):
            self._llms[category].append(llm)
        return tracker.register_llm(llm)

    def add_response_usage(self, category: str, model: str, usage: Any) -> None:
        """Record one direct OpenAI Responses call under an application phase."""
        tracker = self._trackers.get(category)
        if tracker is None:
            raise ValueError(f"Unknown cost category: {category}")
        if usage is not None:
            tracker.add_usage(model, response_usage(usage))

    async def breakdown(self, classification_cost: float | None = None) -> dict[str, float]:
        values = {category: 0.0 for category in COST_CATEGORIES}
        values["classification"] = float(classification_cost or 0.0)
        for category, tracker in self._trackers.items():
            summary = await tracker.get_usage_summary()
            values[category] = float(summary.total_cost)
        return values

    async def token_breakdown(self) -> dict[str, dict[str, Any]]:
        """Return durable per-phase token counts for cost and cache diagnosis."""
        values: dict[str, dict[str, Any]] = {}
        for category, tracker in self._trackers.items():
            summary = await tracker.get_usage_summary()
            detailed = [
                details()
                for llm in self._llms[category]
                if callable(details := getattr(llm, "detailed_completion_usage", None))
            ]
            visible_completion_tokens = (
                sum(item["visible_completion_tokens"] for item in detailed)
                if detailed
                else int(summary.total_completion_tokens)
            )
            reasoning_tokens = (
                sum(item["reasoning_tokens"] for item in detailed)
                if detailed
                else None
            )
            values[category] = {
                "prompt_tokens": int(summary.total_prompt_tokens),
                "cached_prompt_tokens": int(summary.total_prompt_cached_tokens),
                "cache_write_tokens": int(summary.total_prompt_cache_creation_tokens),
                "completion_tokens": int(summary.total_completion_tokens),
                "visible_completion_tokens": visible_completion_tokens,
                "reasoning_tokens": reasoning_tokens,
                "total_tokens": int(summary.total_tokens),
                "requests": int(summary.entry_count),
                "models": {
                    model: {
                        "prompt_tokens": int(stats.prompt_tokens),
                        "completion_tokens": int(stats.completion_tokens),
                        "total_tokens": int(stats.total_tokens),
                        "requests": int(stats.invocations),
                    }
                    for model, stats in summary.by_model.items()
                },
            }
        return values


def total_cost(breakdown: dict[str, float]) -> float:
    return sum(float(breakdown.get(category) or 0.0) for category in COST_CATEGORIES)
