from datetime import datetime, timezone

import pytest

from outcome_retention import auto_reject_stale_applications


NOW = datetime(2026, 3, 31, 12, tzinfo=timezone.utc)


def run_retention(jobs, months=1):
    def update(fields_by_url):
        for url, fields in fields_by_url.items():
            jobs[url].update(fields)
        return len(fields_by_url)

    return auto_reject_stale_applications(
        months,
        now=NOW,
        jobs_reader=lambda: jobs,
        jobs_updater=update,
    )


def test_rejects_applied_job_after_calendar_month_boundary():
    jobs = {
        "old": {
            "status": "applied",
            "application_status": "applied",
            "applied_at": "2026-02-28T12:00:00Z",
        },
        "new": {
            "status": "applied",
            "application_status": "applied",
            "applied_at": "2026-02-28T12:00:01Z",
        },
    }

    assert run_retention(jobs) == 1
    assert jobs["old"]["application_status"] == "rejected"
    assert jobs["old"]["application_status_source"] == "timeout"
    assert jobs["old"]["application_status_evidence"]["months_without_outcome"] == 1
    assert jobs["new"]["application_status"] == "applied"


def test_preserves_later_outcomes_and_non_applications():
    jobs = {
        "interview": {
            "status": "applied",
            "application_status": "interview",
            "applied_at": "2025-01-01T00:00:00Z",
        },
        "qualified": {
            "status": "pending",
            "application_status": None,
            "applied_at": "2025-01-01T00:00:00Z",
        },
        "invalid-date": {
            "status": "applied",
            "application_status": "applied",
            "applied_at": "not-a-date",
        },
    }

    assert run_retention(jobs) == 0
    assert jobs["interview"]["application_status"] == "interview"


def test_rejects_invalid_month_count():
    with pytest.raises(ValueError, match="at least 1"):
        run_retention({}, months=0)
