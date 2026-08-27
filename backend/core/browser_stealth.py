"""Small, shared Playwright stealth configuration for Hunter browser contexts."""

from playwright_stealth import Stealth


# Keep Chrome's real platform, user agent, and client hints. Hunter already sets a
# matching headed Chrome user agent; overriding those independently creates a
# less credible fingerprint than leaving them alone.
_STEALTH = Stealth(
    navigator_platform=False,
    navigator_platform_override=None,
    navigator_user_agent=False,
    navigator_user_agent_data=False,
    sec_ch_ua=False,
)


def apply_stealth_sync(page_or_context) -> None:
    _STEALTH.apply_stealth_sync(page_or_context)


async def apply_stealth_async(page_or_context) -> None:
    await _STEALTH.apply_stealth_async(page_or_context)
