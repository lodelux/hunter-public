"""Explainable deterministic screening over jobs and classifier facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal


POLICY_VERSION = 3
CLASSIFIER_SALARY_MIN_CONFIDENCE = 0.8
MissingBehavior = Literal["pass", "review", "reject"]

LANGUAGE_ALIASES = {
    "deutsch": "german",
    "englisch": "english",
    "italienisch": "italian",
}


@dataclass(frozen=True)
class HardFilterRule:
    code: str
    field: str
    operator: str
    message: str
    value: Any = None
    profile_field: str | None = None
    min_confidence: float = 0.0
    on_missing: MissingBehavior = "pass"
    when_profile_field: str | None = None
    when_operator: str = "equals"
    when_value: Any = True
    enabled: bool = True


HARD_FILTER_RULES = [
    HardFilterRule(
        code="seniority_outside_target",
        field="classification.facts.seniority",
        operator="in",
        value=("internship", "junior", "mid"),
        min_confidence=0.8,
        message="The listing explicitly targets senior, lead, or executive candidates",
    ),
    HardFilterRule(
        code="required_language_missing",
        field="classification.facts.required_languages",
        operator="subset_of",
        profile_field="languages",
        min_confidence=0.75,
        message="The listing explicitly requires a language absent from the profile",
    ),
    HardFilterRule(
        code="sponsorship_unavailable",
        field="classification.facts.sponsorship",
        operator="equals",
        value="available",
        min_confidence=0.8,
        when_profile_field="visa_sponsorship_needed",
        message="The candidate needs sponsorship but the listing says it is unavailable",
    ),
    HardFilterRule(
        code="salary_below_minimum",
        field="resolved.salary_max",
        operator="gte",
        profile_field="salary_expectation.min",
        min_confidence=0.8,
        when_profile_field="salary_expectation.min",
        when_operator="gt",
        when_value=0,
        message="The known salary maximum is below the profile minimum",
    ),
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_path(value: Any, path: str) -> Any:
    """Resolve a safe dot path through dictionaries without dynamic evaluation."""
    current = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _is_missing(value: Any) -> bool:
    return value is None or value == "" or value == "unknown"


def _normalize_interval(interval: Any) -> str | None:
    value = str(interval or "").strip().lower()
    if value in {"annual", "annually", "year", "yearly"}:
        return "annual"
    if value in {"month", "monthly"}:
        return "monthly"
    return None


def _normalize_language(value: Any) -> str:
    language = str(value).strip().casefold()
    return LANGUAGE_ALIASES.get(language, language)


def resolve_salary(job: dict, profile: dict) -> dict | None:
    """Resolve a comparable salary maximum, preferring JobSpy provenance."""
    expectation = profile.get("salary_expectation") or {}
    profile_currency = str(expectation.get("currency") or "").upper()
    target_period = _normalize_interval(expectation.get("period"))
    if not profile_currency or target_period is None:
        return None

    salary = get_path(job, "classification.facts.salary") or {}
    candidates = [
        {
            "maximum": job.get("max_amount"),
            "currency": job.get("currency"),
            "interval": job.get("interval"),
            "confidence": 1.0,
            "source": "jobspy",
        },
        {
            "maximum": salary.get("maximum"),
            "currency": salary.get("currency"),
            "interval": salary.get("interval"),
            "confidence": float(salary.get("confidence") or 0),
            "source": "classifier",
        },
    ]

    for candidate in candidates:
        maximum = candidate["maximum"]
        if not isinstance(maximum, (int, float)):
            continue
        if (
            candidate["source"] == "classifier"
            and candidate["confidence"] < CLASSIFIER_SALARY_MIN_CONFIDENCE
        ):
            continue

        salary_currency = str(candidate["currency"] or "").upper()
        source_period = _normalize_interval(candidate["interval"])
        if salary_currency != profile_currency or source_period is None:
            continue

        comparable = float(maximum)
        if source_period == "monthly" and target_period == "annual":
            comparable *= 12
        elif source_period == "annual" and target_period == "monthly":
            comparable /= 12

        return {
            "value": comparable,
            "confidence": candidate["confidence"],
            "source": candidate["source"],
            "currency": salary_currency,
            "period": target_period,
        }
    return None


def resolve_field(job: dict, profile: dict, path: str) -> Any:
    if path == "resolved.salary_max":
        return resolve_salary(job, profile)
    return get_path(job, path)


def _fact_value(raw: Any, min_confidence: float) -> tuple[Any, float | None]:
    if isinstance(raw, list):
        values = []
        confidences = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            confidence = float(item.get("confidence") or 0)
            value = item.get("value")
            if confidence >= min_confidence and not _is_missing(value):
                values.append(value)
                confidences.append(confidence)
        if not raw:
            return [], 1.0
        return (values, min(confidences)) if values else (None, None)

    if isinstance(raw, dict) and "value" in raw:
        confidence = float(raw.get("confidence") or 0)
        if confidence < min_confidence or _is_missing(raw.get("value")):
            return None, confidence
        return raw.get("value"), confidence

    return raw, None


def _compare(left: Any, operator: str, right: Any) -> bool:
    if operator == "equals":
        return left == right
    if operator == "not_equals":
        return left != right
    if operator == "gte":
        return left >= right
    if operator == "lte":
        return left <= right
    if operator == "gt":
        return left > right
    if operator == "lt":
        return left < right
    if operator == "in":
        return left in right
    if operator == "not_in":
        return left not in right
    if operator == "contains":
        return right in left
    if operator == "subset_of":
        return {_normalize_language(item) for item in left}.issubset(
            {_normalize_language(item) for item in right}
        )
    raise ValueError(f"Unsupported screening operator: {operator}")


def _missing_outcome(rule: HardFilterRule) -> str:
    return rule.on_missing


def evaluate_rule(rule: HardFilterRule, job: dict, profile: dict) -> dict:
    if not rule.enabled:
        return {"outcome": "pass"}

    if rule.when_profile_field:
        condition = get_path(profile, rule.when_profile_field)
        if _is_missing(condition) or not _compare(
            condition, rule.when_operator, rule.when_value
        ):
            return {"outcome": "pass"}

    raw = resolve_field(job, profile, rule.field)
    left, confidence = _fact_value(raw, rule.min_confidence)
    right = (
        get_path(profile, rule.profile_field)
        if rule.profile_field
        else rule.value
    )
    if _is_missing(left) or _is_missing(right):
        return {"outcome": _missing_outcome(rule)}

    try:
        passed = _compare(left, rule.operator, right)
    except (TypeError, ValueError):
        return {"outcome": _missing_outcome(rule)}

    if passed:
        return {"outcome": "pass"}
    return {
        "outcome": "reject",
        "reason": {
            "code": rule.code,
            "message": rule.message,
            "field": rule.field,
            "confidence": confidence,
        },
    }


def evaluate_screening(
    job: dict,
    profile: dict,
    *,
    rules: list[HardFilterRule] | None = None,
) -> dict:
    """Evaluate hard rules while preserving any existing manual override."""
    classification = job.get("classification") or {}
    previous = job.get("screening") or {}
    reasons = []
    uncertain = False

    for rule in rules if rules is not None else HARD_FILTER_RULES:
        result = evaluate_rule(rule, job, profile)
        if result["outcome"] == "reject":
            reasons.append(result["reason"])
        elif result["outcome"] == "review":
            uncertain = True

    if reasons:
        status = "rejected"
    elif classification.get("status") != "complete" or uncertain:
        status = "review"
    else:
        status = "qualified"

    return {
        "status": status,
        "reasons": reasons,
        "policy_version": POLICY_VERSION,
        "evaluated_at": _utc_now(),
        "override": previous.get("override"),
    }


def effective_screening_status(job: dict) -> str | None:
    screening = job.get("screening") or {}
    return screening.get("override") or screening.get("status")


def is_screening_rejected(job: dict) -> bool:
    return effective_screening_status(job) == "rejected"
