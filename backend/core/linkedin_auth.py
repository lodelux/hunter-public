"""Verified LinkedIn session recovery using Hunter's Browser Use profile."""

from __future__ import annotations

import os
import random
from collections.abc import Callable
from urllib.parse import urlsplit

from browser_use import Agent
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

try:
    from core.browser_stealth import apply_stealth_async
except ImportError:
    from backend.core.browser_stealth import apply_stealth_async


_LINKEDIN_HOSTS = {"linkedin.com", "www.linkedin.com"}
_AUTH_PATH_PREFIXES = ("/login", "/checkpoint", "/uas/", "/authwall")


class LinkedInAuthenticationError(RuntimeError):
    """LinkedIn authentication could not be restored safely."""

    fatal_automation = True


def is_linkedin_url(url: str) -> bool:
    return (urlsplit(str(url or "")).hostname or "").casefold() in _LINKEDIN_HOSTS


def linkedin_url_requires_authentication(url: str) -> bool:
    parsed = urlsplit(str(url or ""))
    path = parsed.path.casefold()
    return (
        (parsed.hostname or "").casefold() in _LINKEDIN_HOSTS
        and (
            path.startswith(_AUTH_PATH_PREFIXES)
            or "challenge" in path
            or "captcha" in path
        )
    )


def _credentials() -> tuple[str, str]:
    return (
        os.environ.get("LINKEDIN_EMAIL", "").strip(),
        os.environ.get("LINKEDIN_PASSWORD", ""),
    )


async def _linkedin_session_state(cdp_url: str) -> str:
    """Return ``logged_in``, ``logged_out``, or ``blocked`` from live state."""
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(cdp_url)
            if not browser.contexts:
                raise LinkedInAuthenticationError(
                    "Browser Use did not provide a browser context for LinkedIn login"
                )
            context = browser.contexts[0]
            await apply_stealth_async(context)
            page = await context.new_page()
            try:
                await page.goto(
                    "https://www.linkedin.com/feed/",
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                await page.wait_for_timeout(round(random.uniform(800, 1_800)))
                current_url = page.url
                cookies = await context.cookies(["https://www.linkedin.com/"])
            finally:
                await page.close()
    except LinkedInAuthenticationError:
        raise
    except PlaywrightError as exc:
        raise LinkedInAuthenticationError(
            "LinkedIn login status could not be verified"
        ) from exc

    if linkedin_url_requires_authentication(current_url):
        path = urlsplit(current_url).path.casefold()
        if "/checkpoint" in path or "challenge" in path or "captcha" in path:
            return "blocked"
        return "logged_out"
    has_session_cookie = any(
        cookie.get("name") == "li_at" and cookie.get("value")
        for cookie in cookies
    )
    return "logged_in" if has_session_cookie and is_linkedin_url(current_url) else "logged_out"


async def ensure_linkedin_login(
    browser_session,
    *,
    llm_factory: Callable[[], object],
    report: Callable[[str], None] | None = None,
) -> bool:
    """Restore a logged-out LinkedIn profile once and verify the result."""
    notify = report or (lambda _message: None)
    cdp_url = browser_session.cdp_url
    if not cdp_url:
        raise LinkedInAuthenticationError(
            "Browser Use did not expose its LinkedIn browser connection"
        )

    state = await _linkedin_session_state(cdp_url)
    if state == "logged_in":
        return False
    if state == "blocked":
        raise LinkedInAuthenticationError(
            "LinkedIn requires a checkpoint, CAPTCHA, or manual verification"
        )

    email, password = _credentials()
    if not email or not password:
        raise LinkedInAuthenticationError(
            "LinkedIn is logged out and LINKEDIN_EMAIL or LINKEDIN_PASSWORD is missing"
        )

    notify("LinkedIn is logged out; attempting one Browser Use login")
    browser_profile = getattr(browser_session, "browser_profile", None)
    original_allowed_domains = (
        browser_profile.allowed_domains if browser_profile is not None else None
    )
    if browser_profile is not None:
        browser_profile.allowed_domains = ["*.linkedin.com"]
    login_agent = Agent(
        task=(
            "Sign in to LinkedIn using <secret>linkedin_email</secret> and "
            "<secret>linkedin_password</secret>. Do not use another account or change "
            "account settings. If LinkedIn shows MFA, a security checkpoint, "
            "identity verification, or any other challenge, do not bypass it; stop. "
            "Finish only after the normal signed-in LinkedIn feed is visible."
        ),
        llm=llm_factory(),
        browser_session=browser_session,
        sensitive_data={
            "linkedin_email": email,
            "linkedin_password": password,
        },
        initial_actions=[
            {"navigate": {"url": "https://www.linkedin.com/login", "new_tab": False}}
        ],
        use_vision=True,
        max_failures=3,
    )
    try:
        await login_agent.run(max_steps=12)
    except Exception as exc:
        raise LinkedInAuthenticationError(
            f"Automatic LinkedIn login failed ({type(exc).__name__})"
        ) from exc
    finally:
        if browser_profile is not None:
            browser_profile.allowed_domains = original_allowed_domains

    state = await _linkedin_session_state(cdp_url)
    if state != "logged_in":
        reason = (
            "LinkedIn requires a checkpoint, CAPTCHA, or manual verification"
            if state == "blocked"
            else "Automatic LinkedIn login did not restore the signed-in session"
        )
        raise LinkedInAuthenticationError(reason)
    notify("LinkedIn login restored and verified")
    return True
