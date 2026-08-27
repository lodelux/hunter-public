"""Deterministic company exclusions shared by every collection source."""

import re
import unicodedata


DEFAULT_BLACKLISTED_COMPANIES = ["Alignerr", "Outlier"]


def _normalized_company_name(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


def blacklisted_company_match(
    company: object,
    blacklisted_companies: list[str] | None,
) -> str | None:
    """Return the configured company name when an exact normalized match exists."""
    normalized_company = _normalized_company_name(company)
    if not normalized_company:
        return None

    for blocked in blacklisted_companies or []:
        if _normalized_company_name(blocked) == normalized_company:
            return str(blocked).strip()
    return None
