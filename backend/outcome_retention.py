"""Automatic aging of applications that never receive a later outcome."""

from __future__ import annotations

import calendar
from datetime import datetime, timezone
from typing import Callable

try:
    from core.shared_config import read_jobs, update_jobs
except ImportError:
    from backend.core.shared_config import read_jobs, update_jobs


def _months_before(value: datetime, months: int) -> datetime:
    month_index = value.year * 12 + value.month - 1 - months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def auto_reject_stale_applications(
    months: int,
    *,
    now: datetime | None = None,
    jobs_reader: Callable[[], dict[str, dict]] = read_jobs,
    jobs_updater: Callable[[dict[str, dict]], int] = update_jobs,
) -> int:
    """Mark still-pending applications rejected after ``months`` calendar months."""
    if months < 1:
        raise ValueError("months must be at least 1")

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    cutoff = _months_before(current, months)
    updates: dict[str, dict] = {}

    for url, job in jobs_reader().items():
        if not isinstance(job, dict) or job.get("status") != "applied":
            continue
        if (job.get("application_status") or "applied") != "applied":
            continue
        applied_at = str(job.get("applied_at") or "")
        try:
            applied = datetime.fromisoformat(applied_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if applied.tzinfo is None:
            applied = applied.replace(tzinfo=timezone.utc)
        if applied.astimezone(timezone.utc) > cutoff:
            continue

        updated_at = current.isoformat()
        updates[url] = {
            "application_status": "rejected",
            "application_status_updated_at": updated_at,
            "application_status_source": "timeout",
            "application_status_evidence": {
                "source": "timeout",
                "applied_at": applied_at,
                "rejected_at": updated_at,
                "months_without_outcome": months,
                "reason": f"No later hiring outcome was recorded within {months} month{'s' if months != 1 else ''}",
            },
        }

    return jobs_updater(updates) if updates else 0
