import pytest
from types import SimpleNamespace

from core import linkedin_auth


class FakeBrowserSession:
    cdp_url = "http://127.0.0.1:9222"

    def __init__(self):
        self.browser_profile = SimpleNamespace(allowed_domains=None)


@pytest.mark.asyncio
async def test_logged_in_session_does_not_start_agent(monkeypatch):
    async def session_state(_cdp_url):
        return "logged_in"

    monkeypatch.setattr(linkedin_auth, "_linkedin_session_state", session_state)
    monkeypatch.setattr(
        linkedin_auth,
        "Agent",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("agent should not start")),
    )

    restored = await linkedin_auth.ensure_linkedin_login(
        FakeBrowserSession(),
        llm_factory=lambda: object(),
    )

    assert restored is False


@pytest.mark.asyncio
async def test_logged_out_session_uses_browser_agent_and_verifies(monkeypatch):
    states = iter(("logged_out", "logged_in"))
    captured = {}

    async def session_state(_cdp_url):
        return next(states)

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run(self, max_steps):
            captured["max_steps"] = max_steps
            captured["allowed_domains"] = captured["browser_session"].browser_profile.allowed_domains

    monkeypatch.setenv("LINKEDIN_EMAIL", "person@example.com")
    monkeypatch.setenv("LINKEDIN_PASSWORD", "secret")
    monkeypatch.setattr(linkedin_auth, "_linkedin_session_state", session_state)
    monkeypatch.setattr(linkedin_auth, "Agent", FakeAgent)

    restored = await linkedin_auth.ensure_linkedin_login(
        FakeBrowserSession(),
        llm_factory=lambda: "llm",
    )

    assert restored is True
    assert captured["llm"] == "llm"
    assert captured["sensitive_data"] == {
        "linkedin_email": "person@example.com",
        "linkedin_password": "secret",
    }
    assert captured["max_steps"] == 12
    assert captured["allowed_domains"] == ["*.linkedin.com"]
    assert captured["browser_session"].browser_profile.allowed_domains is None


@pytest.mark.asyncio
async def test_missing_credentials_is_fatal_when_logged_out(monkeypatch):
    async def session_state(_cdp_url):
        return "logged_out"

    monkeypatch.delenv("LINKEDIN_EMAIL", raising=False)
    monkeypatch.delenv("LINKEDIN_PASSWORD", raising=False)
    monkeypatch.setattr(linkedin_auth, "_linkedin_session_state", session_state)

    with pytest.raises(linkedin_auth.LinkedInAuthenticationError, match="missing") as exc:
        await linkedin_auth.ensure_linkedin_login(
            FakeBrowserSession(),
            llm_factory=lambda: object(),
        )

    assert exc.value.fatal_automation is True
