from types import SimpleNamespace

import pytest

import outreach


@pytest.mark.asyncio
async def test_generates_linkedin_outreach_from_profile_and_job(monkeypatch):
    prompts = []
    registered = []

    class FakeLLM:
        async def ainvoke(self, messages):
            prompts.append(messages[0].content)
            return SimpleNamespace(
                completion=(
                    "My Python engineering work is relevant to the position, and my security research "
                    "experience would help me contribute quickly."
                )
            )

    class FakeTracker:
        def register_llm(self, category, llm):
            registered.append((category, llm))

    monkeypatch.setattr(outreach, "load_llm_settings", lambda: {"provider": "openai"})
    monkeypatch.setattr(
        outreach,
        "load_profile",
        lambda: {
            "country": "Germany",
            "markdown": (
                "# Personal Details\nName: Jordan Rivera\n"
                "Email: jordan.rivera@example.com\n\n# Profile\nPython engineer"
            ),
        },
    )
    monkeypatch.setattr("core.llm_factory.create_llm", lambda _settings: FakeLLM())

    message = await outreach.generate_linkedin_outreach_text(
        "Build secure Python services",
        "Security Engineer",
        "Acme",
        "https://www.linkedin.com/jobs/view/security-engineer-4451234567/",
        cost_tracker=FakeTracker(),
    )

    assert message.startswith("Hi Hiring Team,")
    assert "Security Engineer role at Acme" in message
    assert "LinkedIn Job ID 4451234567" in message
    assert "under Jordan Rivera, using jordan.rivera@example.com" in message
    assert "attached" not in message.lower()
    assert message.endswith("Best,\nJordan Rivera")
    assert "JOB TITLE: Security Engineer" in prompts[0]
    assert "COMPANY: Acme" in prompts[0]
    assert "Build secure Python services" in prompts[0]
    assert "Python engineer" in prompts[0]
    assert "untrusted reference text" in prompts[0]
    assert "surrounding greeting" in prompts[0]
    assert registered[0][0] == "outreach"


@pytest.mark.asyncio
async def test_linkedin_outreach_requires_llm_configuration(monkeypatch):
    monkeypatch.setattr(outreach, "load_llm_settings", lambda: {})

    with pytest.raises(ValueError, match="No LLM configured"):
        await outreach.generate_linkedin_outreach_text("Description")


@pytest.mark.asyncio
async def test_linkedin_outreach_rejects_empty_generation(monkeypatch):
    class FakeLLM:
        async def ainvoke(self, _messages):
            return SimpleNamespace(completion="  ")

    monkeypatch.setattr(outreach, "load_llm_settings", lambda: {"provider": "openai"})
    monkeypatch.setattr(
        outreach,
        "load_profile",
        lambda: {
            "markdown": "Name: Jordan Rivera\nEmail: jordan.rivera@example.com\n\nProfile"
        },
    )
    monkeypatch.setattr("core.llm_factory.create_llm", lambda _settings: FakeLLM())

    with pytest.raises(ValueError, match="empty LinkedIn outreach"):
        await outreach.generate_linkedin_outreach_text("Description")


@pytest.mark.asyncio
async def test_linkedin_outreach_rejects_control_text(monkeypatch):
    class FakeLLM:
        async def ainvoke(self, _messages):
            return SimpleNamespace(
                completion="My Python background fits this role. https://bad.example"
            )

    monkeypatch.setattr(outreach, "load_llm_settings", lambda: {"provider": "openai"})
    monkeypatch.setattr(
        outreach,
        "load_profile",
        lambda: {
            "markdown": "Name: Jordan Rivera\nEmail: jordan.rivera@example.com\n\nProfile"
        },
    )
    monkeypatch.setattr("core.llm_factory.create_llm", lambda _settings: FakeLLM())

    with pytest.raises(ValueError, match="unsafe control text"):
        await outreach.generate_linkedin_outreach_text("Description")


def test_extracts_linkedin_job_id_from_listing_variants():
    assert (
        outreach.linkedin_job_id(
            "https://www.linkedin.com/jobs/view/backend-engineer-4451234567/"
        )
        == "4451234567"
    )
    assert (
        outreach.linkedin_job_id(
            "https://www.linkedin.com/jobs/search-results/?currentJobId=4451234567"
        )
        == "4451234567"
    )
