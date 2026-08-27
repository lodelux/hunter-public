from types import SimpleNamespace

import pytest

from memory.extractors import (
    ExtractedMemory,
    ExtractedMemoryBatch,
    _build_friction_timeline,
    extract_end_of_run_memories,
    store_extracted_memories,
)
from memory.store import MemoryStore


def _step(url, evaluation, actions=None, goal="", memory=""):
    return SimpleNamespace(
        state=SimpleNamespace(url=url),
        model_output=SimpleNamespace(
            evaluation_previous_goal=evaluation,
            next_goal=goal,
            memory=memory,
            action=actions or [],
        ),
    )


def _result(*steps):
    return SimpleNamespace(history=list(steps))


class FakeResponses:
    def __init__(self, parsed, usage=None):
        self.parsed = parsed
        self.usage = usage
        self.calls = []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_parsed=self.parsed, usage=self.usage)


class FakeClient:
    def __init__(self, parsed, usage=None):
        self.responses = FakeResponses(parsed, usage)


@pytest.fixture
def store(tmp_path):
    memory_store = MemoryStore(db_path=tmp_path / "memory_store.db")
    yield memory_store
    memory_store.close()


def test_friction_timeline_uses_current_browser_use_fields_and_action_names():
    result = _result(
        _step("https://jobs.ashbyhq.com/acme", "Success: form opened"),
        _step(
            "https://jobs.ashbyhq.com/acme",
            "Failure for ada@example.com: clicking the visible label did not select the radio.",
            [{"lookup_current_platform_memories": {"issue": "radio"}}],
            goal="Enter verification code: 123456 and <secret>password</secret>",
        ),
        _step(
            "https://jobs.ashbyhq.com/acme",
            "Success: the underlying radio input is verified checked.",
            [{"evaluate": {"code": "hidden and intentionally omitted"}}],
        ),
    )

    timeline = _build_friction_timeline(result)

    assert [step["step"] for step in timeline] == [1, 2, 3]
    assert timeline[1]["scope"] == "ashby"
    assert timeline[1]["actions"] == ["lookup_current_platform_memories"]
    assert "hidden and intentionally omitted" not in str(timeline)
    assert "ada@example.com" not in str(timeline)
    assert "123456" not in str(timeline)
    assert "password" not in str(timeline)


def test_friction_timeline_exposes_at_most_three_unique_marked_memories():
    result = _result(
        *[
            _step(
                "https://jobs.personio.de/acme",
                "Success: the behavior was verified.",
                memory=f"PLATFORM_INSIGHT: Reusable smooth discovery number {number} for Personio forms.",
            )
            for number in range(1, 5)
        ]
    )

    timeline = _build_friction_timeline(result)

    assert sum(bool(step["memory"]) for step in timeline) == 3
    assert "number 4" not in "\n".join(step["memory"] for step in timeline)


@pytest.mark.asyncio
async def test_smooth_run_skips_model_call_even_without_api_key():
    client = FakeClient(ExtractedMemoryBatch(memories=[]))
    result = _result(
        _step("https://jobs.personio.de/acme", "Success: form opened"),
        _step("https://jobs.personio.de/acme", "Success: application submitted"),
    )

    extracted = await extract_end_of_run_memories(
        result,
        existing_memories=[],
        api_key="",
        client=client,
    )

    assert extracted.memories == []
    assert client.responses.calls == []


@pytest.mark.asyncio
async def test_verified_smooth_insight_is_extracted_from_step_memory():
    parsed = ExtractedMemoryBatch(
        memories=[
            ExtractedMemory(
                platform_url="https://jobs.personio.de/acme",
                category="form_strategy",
                content=(
                    "On Personio, leaving the optional salary field blank keeps the "
                    "application form valid when no salary is requested."
                ),
                evidence_steps=[1, 2],
            )
        ]
    )
    client = FakeClient(parsed)
    result = _result(
        _step(
            "https://jobs.personio.de/acme",
            "Success: the application form is ready for validation.",
            goal="Validate the completed form.",
        ),
        _step(
            "https://jobs.personio.de/acme",
            "Success: validation completed with the optional salary field blank.",
            memory=(
                "The form is complete.\n"
                "PLATFORM_INSIGHT: On Personio, leave the optional salary field blank "
                "when no salary is requested."
            ),
        ),
    )

    extracted = await extract_end_of_run_memories(
        result,
        existing_memories=[],
        api_key="test-key",
        client=client,
    )

    assert len(extracted.memories) == 1
    prompt = client.responses.calls[0]["input"][1]["content"]
    assert "PLATFORM_INSIGHT:" in prompt
    assert "The form is complete" not in prompt
    assert '"memory": ""' in prompt


@pytest.mark.asyncio
async def test_unverified_smooth_insight_is_rejected():
    parsed = ExtractedMemoryBatch(
        memories=[
            ExtractedMemory(
                platform_url="https://jobs.personio.de/acme",
                category="navigation",
                content="On Personio, use an undocumented shortcut to bypass the review page.",
                evidence_steps=[1, 2],
            )
        ]
    )
    client = FakeClient(parsed)
    result = _result(
        _step(
            "https://jobs.personio.de/acme",
            "The application form is open.",
        ),
        _step(
            "https://jobs.personio.de/acme",
            "The shortcut has not been verified.",
            memory=(
                "PLATFORM_INSIGHT: On Personio, use an undocumented shortcut to "
                "bypass the review page."
            ),
        ),
    )

    extracted = await extract_end_of_run_memories(
        result,
        existing_memories=[],
        api_key="test-key",
        client=client,
    )

    assert extracted.memories == []


@pytest.mark.asyncio
async def test_extracts_verified_same_platform_recovery_and_tracks_usage():
    usage = SimpleNamespace(input_tokens=100, output_tokens=20, total_tokens=120)
    parsed = ExtractedMemoryBatch(
        memories=[
            ExtractedMemory(
                platform_url="https://jobs.ashbyhq.com/acme",
                category="element_interaction",
                content=(
                    "If Ashby label clicks leave a required radio unanswered, click the "
                    "underlying radio input and verify its checked state."
                ),
                evidence_steps=[2, 3],
            )
        ]
    )
    client = FakeClient(parsed, usage)
    result = _result(
        _step("https://jobs.ashbyhq.com/acme", "Success: form opened"),
        _step(
            "https://jobs.ashbyhq.com/acme",
            "Failure: clicking the visible option did not clear validation.",
        ),
        _step(
            "https://jobs.ashbyhq.com/acme",
            "Success: the underlying radio input is verified checked.",
        ),
    )

    extracted = await extract_end_of_run_memories(
        result,
        existing_memories=[],
        api_key="test-key",
        client=client,
    )

    assert len(extracted.memories) == 1
    assert extracted.memories[0]["scope"] == "ashby"
    assert extracted.memories[0]["website_domain"] == "ashbyhq.com"
    assert extracted.usage is usage
    call = client.responses.calls[0]
    assert call["model"] == "gpt-5.6-luna"
    assert call["reasoning"] == {"effort": "low"}
    assert call["store"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "candidate",
    [
        ExtractedMemory(
            platform_url="https://jobs.ashbyhq.com/acme",
            category="failure_recovery",
            content="Retry the same action until the transient error eventually disappears.",
            evidence_steps=[1, 2],
        ),
        ExtractedMemory(
            platform_url="https://evil.example/form",
            category="failure_recovery",
            content="Use this unvisited platform-specific recovery on future application forms.",
            evidence_steps=[1, 2],
        ),
        ExtractedMemory(
            platform_url="https://jobs.ashbyhq.com/acme",
            category="form_strategy",
            content="Enter the candidate email ada@example.com into the hidden application field.",
            evidence_steps=[1, 2],
        ),
    ],
)
async def test_rejects_unresolved_unvisited_or_sensitive_candidates(candidate):
    client = FakeClient(ExtractedMemoryBatch(memories=[candidate]))
    result = _result(
        _step(
            "https://jobs.ashbyhq.com/acme",
            "Failure: submission returned an unresolved error.",
        ),
        _step(
            "https://jobs.ashbyhq.com/acme",
            "Failure: retrying did not resolve the error.",
        ),
        _step(
            "https://jobs.ashbyhq.com/acme",
            "Success: a different field was selected.",
        ),
    )

    extracted = await extract_end_of_run_memories(
        result,
        existing_memories=[],
        api_key="test-key",
        client=client,
    )

    assert extracted.memories == []


@pytest.mark.asyncio
async def test_existing_same_scope_memories_are_supplied_for_reinforcement():
    client = FakeClient(ExtractedMemoryBatch(memories=[]))
    result = _result(
        _step("https://boards.greenhouse.io/acme", "Failure: location had no options."),
        _step("https://boards.greenhouse.io/acme", "Success: ArrowDown opened options."),
    )

    await extract_end_of_run_memories(
        result,
        existing_memories=[
            {
                "id": 7,
                "website_domain": "greenhouse.io",
                "ats_platform": "greenhouse",
                "category": "element_interaction",
                "content": "Press ArrowDown to open an unfiltered location list.",
                "success": True,
            },
            {
                "id": 8,
                "website_domain": "linkedin.com",
                "ats_platform": "linkedin",
                "category": "navigation",
                "content": "Unrelated memory.",
                "success": True,
            },
        ],
        api_key="test-key",
        client=client,
    )

    prompt = client.responses.calls[0]["input"][1]["content"]
    assert "Press ArrowDown" in prompt
    assert "Unrelated memory" not in prompt


def test_store_extracted_memories_records_source_and_reinforces(store):
    memories = [
        {
            "platform_url": "https://jobs.ashbyhq.com/acme",
            "website_domain": "ashbyhq.com",
            "ats_platform": "ashby",
            "scope": "ashby",
            "category": "element_interaction",
            "content": "Click the underlying radio input when the visible label leaves validation active.",
            "evidence_steps": [2, 3],
        }
    ]

    first = store_extracted_memories(store, memories, source_attempt_id="attempt-1")
    second = store_extracted_memories(store, memories, source_attempt_id="attempt-2")

    assert first == (1, 0, {"ashby"})
    assert second == (0, 1, {"ashby"})
    [row] = store.export_all()
    assert row["source_attempt_id"] == "attempt-2"
    assert row["agent_marked_critical"] is False
