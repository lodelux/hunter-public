from types import SimpleNamespace

import pytest
from browser_use.llm.views import ChatInvokeUsage
from browser_use.tokens.service import TokenCost

from backend.core.cost_tracking import CategorizedCostTracker


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "input_rate", "cached_rate", "write_rate", "output_rate"),
    [
        ("gpt-5.6-sol", 4.00, 0.40, 5.00, 20.00),
        ("gpt-5.6-terra", 2.00, 0.20, 2.50, 12.00),
        ("gpt-5.6-luna", 0.20, 0.02, 0.25, 1.20),
    ],
)
async def test_official_gpt_5_6_pricing_is_used_by_browser_use(
    model, input_rate, cached_rate, write_rate, output_rate
):
    usage = ChatInvokeUsage(
        prompt_tokens=800,
        prompt_cached_tokens=100,
        prompt_cache_creation_tokens=200,
        prompt_image_tokens=None,
        completion_tokens=50,
        total_tokens=1_050,
    )

    cost = await TokenCost(include_cost=True).calculate_cost(model, usage)

    assert cost is not None
    assert cost.new_prompt_cost == pytest.approx(700 * input_rate / 1_000_000)
    assert cost.prompt_read_cached_cost == pytest.approx(100 * cached_rate / 1_000_000)
    assert cost.prompt_cache_creation_cost == pytest.approx(200 * write_rate / 1_000_000)
    assert cost.completion_cost == pytest.approx(50 * output_rate / 1_000_000)


@pytest.mark.asyncio
async def test_categorized_tracker_uses_current_terra_pricing():
    tracker = CategorizedCostTracker()
    tracker._trackers["application_agent"].add_usage(
        "gpt-5.6-terra",
        ChatInvokeUsage(
            prompt_tokens=1_000_000,
            prompt_cached_tokens=0,
            prompt_cache_creation_tokens=None,
            prompt_image_tokens=None,
            completion_tokens=1_000_000,
            total_tokens=2_000_000,
        ),
    )

    breakdown = await tracker.breakdown()

    assert breakdown["application_agent"] == pytest.approx(14.00)


@pytest.mark.asyncio
async def test_categorized_tracker_records_direct_responses_usage():
    tracker = CategorizedCostTracker()
    tracker.add_response_usage(
        "memory_extract",
        "gpt-5.4-mini",
        SimpleNamespace(
            input_tokens=1_000,
            output_tokens=100,
            total_tokens=1_100,
            input_tokens_details=None,
            prompt_tokens_details=None,
        ),
    )

    breakdown = await tracker.breakdown()

    assert breakdown["memory_extract"] > 0


@pytest.mark.asyncio
async def test_categorized_tracker_exposes_token_and_cache_usage():
    tracker = CategorizedCostTracker()
    tracker._trackers["cv"].add_usage(
        "gpt-5.6-terra",
        ChatInvokeUsage(
            prompt_tokens=800,
            prompt_cached_tokens=100,
            prompt_cache_creation_tokens=200,
            prompt_image_tokens=None,
            completion_tokens=50,
            total_tokens=1_050,
        ),
    )

    usage = await tracker.token_breakdown()

    assert usage["cv"] == {
        "prompt_tokens": 800,
        "cached_prompt_tokens": 100,
        "cache_write_tokens": 200,
        "completion_tokens": 50,
        "visible_completion_tokens": 50,
        "reasoning_tokens": None,
        "total_tokens": 850,
        "requests": 1,
        "models": {
            "gpt-5.6-terra": {
                "prompt_tokens": 800,
                "completion_tokens": 50,
                "total_tokens": 850,
                "requests": 1,
            }
        },
    }
