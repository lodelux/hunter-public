from types import SimpleNamespace

import pytest

from memory.ranker import (
    CriticalMemoryChoice,
    CriticalMemoryRanking,
    ScopeRanking,
    rank_critical_memories,
    rerank_critical_scopes,
)


@pytest.mark.asyncio
async def test_ranker_uses_private_strict_structured_output():
    calls = []
    parsed = CriticalMemoryRanking(
        rankings=[
            ScopeRanking(
                scope="greenhouse",
                choices=[CriticalMemoryChoice(memory_id=7, reason="Prevents a lost form")],
            )
        ]
    )

    class FakeResponses:
        async def parse(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(output_parsed=parsed)

    client = SimpleNamespace(responses=FakeResponses())
    result = await rank_critical_memories(
        [{
            "id": 7,
            "scope": "greenhouse",
            "category": "failure_recovery",
            "content": "Reload once after the upload timeout.",
            "confidence": 0.9,
            "success": True,
            "agent_marked_critical": True,
        }],
        api_key="test-key",
        client=client,
    )

    assert result == parsed
    assert calls[0]["model"] == "gpt-5.6-luna"
    assert calls[0]["reasoning"] == {"effort": "low"}
    assert calls[0]["store"] is False
    assert calls[0]["text_format"] is CriticalMemoryRanking
    assert "untrusted observations" in calls[0]["input"][0]["content"]


@pytest.mark.asyncio
async def test_ranker_requires_saved_key():
    with pytest.raises(ValueError, match="saved OpenAI API key"):
        await rank_critical_memories([], api_key="")


@pytest.mark.asyncio
async def test_rerank_critical_scopes_updates_only_requested_scope(monkeypatch):
    import memory.ranker as ranker

    calls = []

    class FakeStore:
        @staticmethod
        def memory_scope(memory):
            return memory["scope"]

        @staticmethod
        def export_all():
            return [
                {"id": 1, "scope": "greenhouse", "success": True},
                {"id": 2, "scope": "lever", "success": True},
            ]

        @staticmethod
        def apply_critical_ranking(rankings, **kwargs):
            calls.append((rankings, kwargs))
            return {"selected": 1, "scopes": 1, "estimated_tokens": 20}

    async def fake_rank(memories, **kwargs):
        assert [memory["id"] for memory in memories] == [1]
        assert kwargs["max_count"] == 7
        assert kwargs["max_tokens"] == 900
        return CriticalMemoryRanking(rankings=[ScopeRanking(
            scope="greenhouse",
            choices=[CriticalMemoryChoice(memory_id=1, reason="important")],
        )])

    monkeypatch.setattr(ranker, "rank_critical_memories", fake_rank)

    result = await rerank_critical_scopes(
        FakeStore(),
        {"greenhouse"},
        api_key="test-key",
        max_count=7,
        max_tokens=900,
    )

    assert result["selected"] == 1
    assert calls[0][1]["scopes"] == {"greenhouse"}
