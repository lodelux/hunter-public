import json
from datetime import datetime, timedelta, timezone

import pytest

import autonomy
from autonomy import (
    ALLOWED_INTERVALS,
    AutomationManager,
    DEFAULT_CONFIG,
    _needs_classification,
    validate_config,
)


def _manager(
    tmp_path,
    jobs,
    *,
    collect=None,
    apply=None,
    qualified=None,
    recovered=None,
    completed=None,
    outreach_values=None,
    notify=None,
):
    async def collect_source(source, config):
        return await collect(source, config) if collect else []

    async def classify_urls(urls):
        for url in urls:
            classification = dict(jobs[url].get("classification") or {})
            classification["status"] = "complete"
            classification.setdefault("score", 80)
            jobs[url]["classification"] = classification
            screening = dict(jobs[url].get("screening") or {})
            screening["policy_version"] = autonomy.SCREENING_POLICY_VERSION
            jobs[url]["screening"] = screening

    async def apply_job(url, max_steps, linkedin_outreach_enabled):
        if outreach_values is not None:
            outreach_values.append(linkedin_outreach_enabled)
        outcome = (
            await apply(url, max_steps)
            if apply
            else {"submission_confirmed": True, "retryable": False, "blocker": None}
        )
        jobs[url]["status"] = (
            "applied" if outcome.get("submission_confirmed") is True else "failed"
        )
        return outcome

    def recover_interrupted(url):
        jobs.setdefault(url, {}).update(
            status="failed",
            last_application_outcome={
                "type": "unknown_outcome",
                "retryable": False,
            },
        )
        (recovered if recovered is not None else []).append(url)

    return AutomationManager(
        tmp_path,
        collect_source=collect_source,
        classify_urls=classify_urls,
        apply_job=apply_job,
        job_snapshot=lambda url: jobs.get(url),
        qualified_urls=lambda: list(
            qualified() if callable(qualified) else qualified or []
        ),
        is_busy=lambda: False,
        recover_interrupted=recover_interrupted,
        mark_blocked=lambda url: jobs[url].update(
            status="blocked",
            error="Reposted listing skipped by autonomous mode",
        ),
        mark_completed=lambda url: (completed if completed is not None else []).append(url),
        notify=notify,
    )


def test_config_defaults_and_validation():
    config = validate_config({})
    assert config["sources"] == ["linkedin", "indeed"]
    assert config["interval_minutes"] == 15
    assert config["daily_job_limit"] == 10
    assert config["daily_company_limit"] == 5
    assert config["max_jobs_per_source"] == 5
    assert config["max_applications_per_cycle"] == 2
    assert config["automation_min_score"] == 70
    assert config["linkedin_outreach_enabled"] is True
    assert config["hours_old"] == 1

    with pytest.raises(ValueError, match="Select at least one"):
        validate_config({"sources": []})
    with pytest.raises(ValueError, match="Interval"):
        validate_config({"interval_minutes": 7})
    with pytest.raises(ValueError, match="requires the LinkedIn source"):
        validate_config({"sources": ["indeed"], "linkedin_search_url": "https://linkedin.com/jobs/search/"})


@pytest.mark.asyncio
async def test_autonomous_apply_receives_linkedin_outreach_policy(tmp_path):
    outreach_values = []
    manager = _manager(
        tmp_path,
        {"job": {"status": "pending"}},
        outreach_values=outreach_values,
    )
    manager.save_config({**DEFAULT_CONFIG, "linkedin_outreach_enabled": False})

    assert await manager._attempt_job("job") is True
    assert outreach_values == [False]


def test_stale_screening_policy_is_reprocessed_without_reclassifying_current_jobs():
    assert _needs_classification(
        {
            "classification": {"status": "complete", "score": 80},
            "screening": {"policy_version": autonomy.SCREENING_POLICY_VERSION - 1},
        }
    )
    assert not _needs_classification(
        {
            "classification": {"status": "complete", "score": 80},
            "screening": {"policy_version": autonomy.SCREENING_POLICY_VERSION},
        }
    )


def test_startup_prunes_manually_unqualified_jobs_from_persisted_queue(tmp_path):
    jobs = {
        "manual": {
            "status": "pending",
            "classification": {"status": "complete", "score": 80},
            "screening": {"status": "qualified", "override": "rejected"},
        },
        "qualified": {
            "status": "pending",
            "classification": {"status": "complete", "score": 80},
            "screening": {"status": "qualified", "override": None},
        },
    }
    completed = []

    manager = _manager(
        tmp_path,
        jobs,
        qualified=["manual", "qualified"],
        completed=completed,
    )

    assert manager.snapshot()["status"]["queued_count"] == 1
    assert completed == []


def test_all_pending_qualified_jobs_above_minimum_are_derived_into_queue(tmp_path):
    jobs = {
        "eligible": {
            "status": "pending",
            "autonomous_completed_at": "2026-08-20T10:00:00+00:00",
            "classification": {"status": "complete", "score": 80},
            "screening": {
                "status": "qualified",
                "override": None,
                "policy_version": autonomy.SCREENING_POLICY_VERSION,
            },
        },
        "below-minimum": {
            "status": "pending",
            "classification": {"status": "complete", "score": 69},
            "screening": {
                "status": "qualified",
                "override": None,
                "policy_version": autonomy.SCREENING_POLICY_VERSION,
            },
        },
        "failed": {
            "status": "failed",
            "classification": {"status": "complete", "score": 90},
            "screening": {
                "status": "qualified",
                "override": None,
                "policy_version": autonomy.SCREENING_POLICY_VERSION,
            },
        },
        "unresolved": {
            "status": "pending",
            "last_application_outcome": {
                "type": "unknown_outcome",
                "retryable": False,
            },
            "classification": {"status": "complete", "score": 90},
            "screening": {
                "status": "qualified",
                "override": None,
                "policy_version": autonomy.SCREENING_POLICY_VERSION,
            },
        },
        "unclassified": {
            "status": "pending",
            "screening": {"status": "qualified", "override": None},
        },
    }

    manager = _manager(tmp_path, jobs, qualified=list(jobs))

    assert manager.snapshot()["status"]["queued_count"] == 1
    assert manager._state["queued_urls"] == ["eligible"]


def test_resolved_non_submission_reenters_derived_queue(tmp_path):
    jobs = {
        "job": {
            "status": "failed",
            "classification": {"status": "complete", "score": 85},
            "screening": {
                "status": "qualified",
                "override": None,
                "policy_version": autonomy.SCREENING_POLICY_VERSION,
            },
            "last_application_outcome": {
                "type": "unknown_outcome",
                "retryable": False,
            },
        }
    }
    manager = _manager(
        tmp_path,
        jobs,
        qualified=lambda: [
            url
            for url, job in jobs.items()
            if job.get("status") == "pending"
            and (
                (job.get("screening") or {}).get("override")
                or (job.get("screening") or {}).get("status")
            )
            == "qualified"
        ],
    )
    assert manager.snapshot()["status"]["queued_count"] == 0

    jobs["job"].update(status="pending", last_application_outcome=None)
    jobs["job"]["screening"]["override"] = "qualified"

    assert manager.snapshot()["status"]["queued_count"] == 1


def test_minimum_score_updates_derived_queue_membership(tmp_path):
    jobs = {
        url: {
            "status": "pending",
            "classification": {"status": "complete", "score": score},
            "screening": {
                "status": "qualified",
                "override": None,
                "policy_version": autonomy.SCREENING_POLICY_VERSION,
            },
        }
        for url, score in (("high", 80), ("medium", 65))
    }
    manager = _manager(tmp_path, jobs, qualified=list(jobs))

    assert manager.snapshot()["status"]["queued_count"] == 1

    manager.save_config({**DEFAULT_CONFIG, "automation_min_score": 60})

    assert manager.snapshot()["status"]["queued_count"] == 2


def test_discard_url_removes_manual_override_immediately(tmp_path):
    completed = []
    manager = _manager(
        tmp_path,
        {"job": {"screening": {"override": "rejected"}}},
        completed=completed,
    )
    manager._state["queued_urls"] = ["job"]

    manager.discard_url("job")

    assert manager.snapshot()["status"]["queued_count"] == 0
    assert completed == ["job"]


@pytest.mark.parametrize("interval", ALLOWED_INTERVALS)
@pytest.mark.asyncio
async def test_selectable_cadence_sets_the_next_cycle(tmp_path, monkeypatch, interval):
    now = datetime(2026, 8, 15, 9, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(autonomy, "_now", lambda: now)
    monkeypatch.setattr(autonomy.random, "uniform", lambda low, high: (low + high) / 2)
    manager = _manager(tmp_path, {})
    manager.save_config({**DEFAULT_CONFIG, "sources": ["indeed"], "interval_minutes": interval})

    await manager._run_cycle()

    next_run = datetime.fromisoformat(manager.snapshot()["status"]["next_run_at"])
    assert next_run == now + timedelta(minutes=interval)


def test_scan_interval_is_jittered_within_twenty_percent(monkeypatch):
    monkeypatch.setattr(autonomy.random, "uniform", lambda low, high: high)

    assert autonomy._jittered_interval_minutes(15) == 18


@pytest.mark.asyncio
async def test_linkedin_scan_uses_last_scan_freshness_with_overlap(tmp_path, monkeypatch):
    now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(autonomy, "_now", lambda: now)
    configs = []

    async def collect_source(_source, config):
        configs.append(config)
        return []

    manager = _manager(tmp_path, {}, collect=collect_source)
    manager.save_config(
        {
            **DEFAULT_CONFIG,
            "sources": ["linkedin"],
            "linkedin_search_url": "https://www.linkedin.com/jobs/search/?keywords=data",
        }
    )

    await manager._run_cycle()
    now += timedelta(minutes=15)
    await manager._run_cycle()

    assert configs[0]["_max_results_to_inspect"] == 25
    assert datetime.fromisoformat(configs[0]["_posted_after"]) == datetime(
        2026, 8, 16, 11, 0, tzinfo=timezone.utc
    )
    assert datetime.fromisoformat(configs[1]["_posted_after"]) == datetime(
        2026, 8, 16, 11, 45, tzinfo=timezone.utc
    )


@pytest.mark.asyncio
async def test_automation_ranks_by_score_and_limits_each_cycle(tmp_path):
    jobs = {
        "medium": {
            "company": "Medium",
            "status": "pending",
            "date_posted": "2026-08-16T12:00:00+00:00",
            "classification": {"status": "complete", "score": 80},
            "screening": {"status": "qualified"},
        },
        "best": {
            "company": "Best",
            "status": "pending",
            "date_posted": "2026-08-16T10:00:00+00:00",
            "classification": {"status": "complete", "score": 95},
            "screening": {"status": "qualified"},
        },
        "good": {
            "company": "Good",
            "status": "pending",
            "date_posted": "2026-08-16T11:00:00+00:00",
            "classification": {"status": "complete", "score": 85},
            "screening": {"status": "qualified"},
        },
        "low": {
            "company": "Low",
            "status": "pending",
            "classification": {"status": "complete", "score": 69},
            "screening": {"status": "qualified"},
        },
    }
    calls = []
    completed = []

    async def apply_job(url, _max_steps):
        calls.append(url)
        return {"submission_confirmed": True, "retryable": False, "blocker": None}

    manager = _manager(
        tmp_path,
        jobs,
        apply=apply_job,
        qualified=list(jobs),
        completed=completed,
    )

    await manager._run_cycle()

    assert calls == ["best", "good"]
    assert "low" in completed
    assert manager.snapshot()["status"]["queued_count"] == 1


@pytest.mark.asyncio
async def test_company_limit_defers_the_sixth_application_until_next_day(tmp_path, monkeypatch):
    now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(autonomy, "_now", lambda: now)
    jobs = {
        f"job-{index}": {
            "company": "Example GmbH" if index % 2 else " example  GMBH ",
            "status": "pending",
            "classification": {"status": "complete", "score": 100 - index},
            "screening": {"status": "qualified"},
        }
        for index in range(6)
    }
    calls = []

    async def apply_job(url, _max_steps):
        calls.append(url)
        return {"submission_confirmed": True, "retryable": False, "blocker": None}

    manager = _manager(tmp_path, jobs, apply=apply_job, qualified=list(jobs))
    manager.save_config({**DEFAULT_CONFIG, "max_applications_per_cycle": 20})

    await manager._run_cycle()

    assert calls == [f"job-{index}" for index in range(5)]
    assert manager.snapshot()["status"]["queued_count"] == 1

    now += timedelta(days=1)
    await manager._run_cycle()

    assert calls == [*[f"job-{index}" for index in range(5)], "job-5"]


@pytest.mark.asyncio
async def test_reposted_job_is_skipped_before_classification_or_application(tmp_path):
    jobs = {
        "reposted": {
            "company": "Example",
            "status": "pending",
            "date_posted_text": "Reposted 20 minutes ago",
            "screening": {"status": "qualified"},
        }
    }
    calls = []
    completed = []

    async def apply_job(url, _max_steps):
        calls.append(url)
        return {"submission_confirmed": True, "retryable": False, "blocker": None}

    manager = _manager(
        tmp_path,
        jobs,
        apply=apply_job,
        qualified=[],
        completed=completed,
    )
    manager._state["queued_urls"] = ["reposted"]

    await manager._run_cycle()

    assert calls == []
    assert completed == ["reposted"]
    assert jobs["reposted"]["status"] == "blocked"
    assert jobs["reposted"]["error"] == "Reposted listing skipped by autonomous mode"
    assert "classification" not in jobs["reposted"]
    assert any(
        "Moved reposted autonomous job to Dismissed" in line
        for line in manager.snapshot()["status"]["log"]
    )


def test_daily_cap_and_captcha_counter_reset_at_local_midnight(tmp_path, monkeypatch):
    now = datetime(2026, 8, 15, 23, 59, tzinfo=timezone(timedelta(hours=2)))
    monkeypatch.setattr(autonomy, "_now", lambda: now)
    manager = _manager(tmp_path, {})
    manager._state.update(
        {
            "daily_date": "2026-08-15",
            "today_started_urls": ["one"],
            "captcha_count": 2,
        }
    )

    now = now + timedelta(minutes=2)
    status = manager.snapshot()["status"]

    assert status["today_started"] == 0
    assert status["captcha_count"] == 0
    assert manager._state["daily_date"] == "2026-08-16"


@pytest.mark.asyncio
async def test_cycle_applies_new_qualified_jobs_with_single_budget_and_cap(tmp_path, monkeypatch):
    now = datetime(2026, 8, 16, 18, 30, tzinfo=timezone(timedelta(hours=2)))
    monkeypatch.setattr(autonomy, "_now", lambda: now)
    jobs = {
        "new-one": {"status": "pending", "screening": {"status": "qualified", "override": None}},
        "new-two": {"status": "pending", "screening": {"status": "qualified", "override": None}},
        "review": {"status": "pending", "screening": {"status": "review", "override": None}},
    }
    collected = False
    limits = []
    completed = []

    async def collect_source(_source, _config):
        nonlocal collected
        if collected:
            return []
        collected = True
        return ["new-one", "new-two", "review"]

    async def apply_job(url, max_steps):
        limits.append((url, max_steps))
        jobs[url]["status"] = "applied"
        return {"submission_confirmed": True, "retryable": False, "blocker": None}

    manager = _manager(
        tmp_path,
        jobs,
        collect=collect_source,
        apply=apply_job,
        completed=completed,
    )
    manager.save_config({**DEFAULT_CONFIG, "sources": ["linkedin"], "daily_job_limit": 1})
    await manager._run_cycle()

    assert limits == [("new-one", 180)]
    assert "review" in completed
    status = manager.snapshot()["status"]
    assert status["today_started"] == 1
    assert status["queued_count"] == 1
    assert datetime.fromisoformat(status["next_run_at"]) == datetime(
        2026, 8, 17, 0, 0, tzinfo=timezone(timedelta(hours=2))
    )


@pytest.mark.asyncio
async def test_cycle_finishes_existing_queue_before_collecting(tmp_path):
    jobs = {
        "existing": {
            "status": "pending",
            "classification": {"status": "complete", "score": 80},
            "screening": {"status": "qualified", "override": None},
        }
    }
    events = []

    async def collect_source(source, _config):
        events.append(("collect", source))
        return []

    async def apply_job(url, _max_steps):
        events.append(("apply", url))
        return {"submission_confirmed": True, "retryable": False, "blocker": None}

    manager = _manager(
        tmp_path,
        jobs,
        collect=collect_source,
        apply=apply_job,
        qualified=["existing"],
    )

    await manager._run_cycle()

    assert events == [("apply", "existing"), ("collect", "linkedin"), ("collect", "indeed")]


@pytest.mark.asyncio
async def test_fatal_collection_error_pauses_autonomy(tmp_path):
    async def collect_source(_source, _config):
        raise autonomy.FatalAutomationError("LinkedIn login could not be restored")

    manager = _manager(tmp_path, {}, collect=collect_source)
    await manager._run_cycle()

    status = manager.snapshot()["status"]
    assert status["state"] == "paused"
    assert status["next_run_at"] is None
    assert "LinkedIn login" in status["pause_reason"]


@pytest.mark.asyncio
async def test_cycle_does_not_collect_while_existing_queue_is_capped(tmp_path, monkeypatch):
    now = datetime(2026, 8, 16, 20, 15, tzinfo=timezone(timedelta(hours=2)))
    monkeypatch.setattr(autonomy, "_now", lambda: now)
    jobs = {
        "existing": {
            "status": "pending",
            "classification": {"status": "complete", "score": 80},
            "screening": {"status": "qualified", "override": None},
        }
    }
    collected = []

    async def collect_source(source, _config):
        collected.append(source)
        return []

    manager = _manager(tmp_path, jobs, collect=collect_source, qualified=["existing"])
    manager.save_config({**DEFAULT_CONFIG, "daily_job_limit": 1})
    manager._state.update(
        {
            "daily_date": autonomy._now().date().isoformat(),
            "today_started_urls": ["already-started"],
        }
    )

    await manager._run_cycle()

    assert collected == []
    status = manager.snapshot()["status"]
    assert status["queued_count"] == 1
    assert datetime.fromisoformat(status["next_run_at"]) == datetime(
        2026, 8, 17, 0, 0, tzinfo=timezone(timedelta(hours=2))
    )


@pytest.mark.asyncio
async def test_cycle_does_not_collect_when_daily_cap_is_reached_with_empty_queue(
    tmp_path, monkeypatch
):
    now = datetime(2026, 8, 16, 21, 45, tzinfo=timezone(timedelta(hours=2)))
    monkeypatch.setattr(autonomy, "_now", lambda: now)
    collected = []

    async def collect_source(source, _config):
        collected.append(source)
        return []

    manager = _manager(tmp_path, {}, collect=collect_source)
    manager.save_config({**DEFAULT_CONFIG, "daily_job_limit": 1})
    manager._state.update(
        {
            "daily_date": autonomy._now().date().isoformat(),
            "today_started_urls": ["already-started"],
        }
    )

    await manager._run_cycle()

    assert collected == []
    status = manager.snapshot()["status"]
    assert any(
        "skipping discovery until it resets" in line
        for line in status["log"]
    )
    assert datetime.fromisoformat(status["next_run_at"]) == datetime(
        2026, 8, 17, 0, 0, tzinfo=timezone(timedelta(hours=2))
    )


@pytest.mark.asyncio
async def test_captchas_are_counted_without_retries_or_pausing(tmp_path):
    urls = ["one", "two", "three", "four"]
    jobs = {
        url: {
            "status": "pending",
            "classification": {"status": "complete", "score": 80},
            "screening": {"status": "qualified", "override": None},
        }
        for url in urls
    }
    calls = []

    async def apply_job(url, max_steps):
        calls.append((url, max_steps))
        return {"submission_confirmed": False, "retryable": False, "blocker": "captcha"}

    manager = _manager(tmp_path, jobs, apply=apply_job, qualified=urls)
    manager.save_config({**DEFAULT_CONFIG, "max_applications_per_cycle": 4})
    await manager._run_cycle()

    assert calls == [("one", 180), ("two", 180), ("three", 180), ("four", 180)]
    status = manager.snapshot()["status"]
    assert status["state"] != "paused"
    assert status["captcha_count"] == 4
    assert status["pause_reason"] is None


@pytest.mark.asyncio
async def test_linkedin_authentication_blocker_pauses_before_next_job(tmp_path):
    urls = ["first", "second"]
    jobs = {
        url: {
            "status": "pending",
            "classification": {"status": "complete", "score": 80},
            "screening": {"status": "qualified", "override": None},
        }
        for url in urls
    }
    calls = []

    async def apply_job(url, _max_steps):
        calls.append(url)
        return {
            "submission_confirmed": False,
            "retryable": False,
            "blocker": "linkedin_authentication",
        }

    manager = _manager(tmp_path, jobs, apply=apply_job, qualified=urls)
    manager.save_config({**DEFAULT_CONFIG, "max_applications_per_cycle": 2})
    await manager._run_cycle()

    assert calls == ["first"]
    status = manager.snapshot()["status"]
    assert status["state"] == "paused"
    assert "LinkedIn login" in status["pause_reason"]


@pytest.mark.parametrize("blocker", ["authentication", "anti_bot"])
@pytest.mark.asyncio
async def test_job_specific_blocker_is_skipped_and_next_job_runs(tmp_path, blocker):
    jobs = {
        url: {
            "status": "pending",
            "classification": {"status": "complete", "score": 80},
            "screening": {"status": "qualified", "override": None},
        }
        for url in ("blocked", "next")
    }
    calls = []
    completed = []

    async def apply_job(url, max_steps):
        calls.append((url, max_steps))
        if url == "blocked":
            return {"submission_confirmed": False, "retryable": False, "blocker": blocker}
        return {"submission_confirmed": True, "retryable": False, "blocker": None}

    manager = _manager(
        tmp_path,
        jobs,
        apply=apply_job,
        qualified=["blocked", "next"],
        completed=completed,
    )
    await manager._run_cycle()

    status = manager.snapshot()["status"]
    assert calls == [("blocked", 180), ("next", 180)]
    assert completed == ["blocked", "next"]
    assert status["state"] == "running"
    assert status["pause_reason"] is None


@pytest.mark.asyncio
async def test_confirmed_non_submission_is_skipped_without_retry(tmp_path):
    jobs = {
        url: {
            "status": "pending",
            "classification": {"status": "complete", "score": 80},
            "screening": {"status": "qualified", "override": None},
        }
        for url in ("failed", "next")
    }
    calls = []
    completed = []

    async def apply_job(url, max_steps):
        calls.append((url, max_steps))
        if url == "failed":
            return {
                "type": "confirmed_not_submitted",
                "submission_confirmed": False,
                "retryable": True,
                "blocker": None,
            }
        return {"submission_confirmed": True, "retryable": False, "blocker": None}

    manager = _manager(
        tmp_path,
        jobs,
        apply=apply_job,
        qualified=["failed", "next"],
        completed=completed,
    )

    await manager._run_cycle()

    assert calls == [("failed", 180), ("next", 180)]
    assert completed == ["failed", "next"]
    assert manager.snapshot()["status"]["pause_reason"] is None


@pytest.mark.asyncio
async def test_pre_agent_failure_retries_setup_twice_then_notifies(tmp_path):
    jobs = {
        url: {
            "status": "pending",
            "classification": {"status": "complete", "score": 80},
            "screening": {"status": "qualified", "override": None},
        }
        for url in ("failed", "next")
    }
    calls = []
    completed = []
    notifications = []

    async def apply_job(url, max_steps):
        calls.append((url, max_steps))
        if url == "failed":
            return {
                "type": "pre_submission_failure",
                "submission_confirmed": False,
                "retryable": True,
                "blocker": None,
            }
        return {"submission_confirmed": True, "retryable": False, "blocker": None}

    manager = _manager(
        tmp_path,
        jobs,
        apply=apply_job,
        qualified=["failed", "next"],
        completed=completed,
        notify=lambda message: notifications.append(message) or False,
    )

    await manager._run_cycle()

    assert calls == [
        ("failed", 180),
        ("failed", 180),
        ("failed", 180),
        ("next", 180),
    ]
    assert completed == ["failed", "next"]
    assert len(notifications) == 1
    assert "failed 3 times" in notifications[0]
    assert "moved to Failed" in notifications[0]
    assert manager.snapshot()["status"]["pause_reason"] is None


@pytest.mark.asyncio
async def test_ambiguous_post_submission_outcome_is_skipped_without_pausing(tmp_path):
    jobs = {
        url: {
            "status": "pending",
            "classification": {"status": "complete", "score": 80},
            "screening": {"status": "qualified", "override": None},
        }
        for url in ("ambiguous", "next")
    }
    calls = []

    async def apply_job(url, max_steps):
        calls.append((url, max_steps))
        if url == "ambiguous":
            return {
                "submission_confirmed": False,
                "retryable": False,
                "blocker": None,
            }
        return {"submission_confirmed": True, "retryable": False, "blocker": None}

    manager = _manager(
        tmp_path,
        jobs,
        apply=apply_job,
        qualified=["ambiguous", "next"],
    )

    await manager._run_cycle()

    assert calls == [("ambiguous", 180), ("next", 180)]
    assert manager.snapshot()["status"]["pause_reason"] is None


@pytest.mark.asyncio
async def test_restart_queue_is_classified_before_eligibility_check(tmp_path):
    jobs = {
        "job": {
            "status": "pending",
            "screening": {"status": "qualified", "override": None},
        }
    }
    completed = []
    manager = _manager(
        tmp_path,
        jobs,
        qualified=[],
        completed=completed,
    )
    manager._state["queued_urls"] = ["job"]

    await manager._run_cycle()

    assert jobs["job"]["classification"]["status"] == "complete"
    assert completed == ["job"]
    assert manager.snapshot()["status"]["queued_count"] == 0


def test_interrupted_application_is_removed_and_paused_on_restart(tmp_path):
    (tmp_path / "automation_config.json").write_text(
        json.dumps({**DEFAULT_CONFIG, "enabled": True}),
        encoding="utf-8",
    )
    (tmp_path / "automation_state.json").write_text(
        json.dumps(
            {
                "state": "running",
                "phase": "applying",
                "queued_urls": ["job"],
                "current_job_url": "job",
            }
        ),
        encoding="utf-8",
    )
    recovered = []
    completed = []

    manager = _manager(
        tmp_path,
        {},
        qualified=["job"],
        recovered=recovered,
        completed=completed,
    )

    status = manager.snapshot()["status"]
    assert recovered == ["job"]
    assert completed == ["job"]
    assert status["state"] == "paused"
    assert status["queued_count"] == 0
    assert "unknown" in status["pause_reason"]


def test_queue_refresh_keeps_the_active_application(tmp_path):
    jobs = {
        "job": {
            "status": "pending",
            "classification": {"status": "complete", "score": 80},
            "screening": {
                "status": "qualified",
                "policy_version": autonomy.SCREENING_POLICY_VERSION,
            },
        }
    }
    manager = _manager(tmp_path, jobs, qualified=["job"])
    manager._state.update({"phase": "applying", "current_job_url": "job"})
    jobs["job"]["status"] = "in_progress"

    assert manager.snapshot()["status"]["queued_count"] == 1


def test_unexpected_application_worker_error_is_treated_as_unknown(tmp_path):
    recovered = []
    completed = []
    manager = _manager(
        tmp_path,
        {"job": {}},
        qualified=["job"],
        recovered=recovered,
        completed=completed,
    )
    manager._state.update({"phase": "applying", "current_job_url": "job"})

    async def fail():
        raise RuntimeError("browser vanished")

    manager._run_loop = fail
    manager._thread_main()

    status = manager.snapshot()["status"]
    assert recovered == ["job"]
    assert completed == ["job"]
    assert status["state"] == "paused"
    assert status["queued_count"] == 0
    assert "unknown" in status["pause_reason"]


@pytest.mark.asyncio
async def test_forced_shutdown_during_application_recovers_as_unknown(tmp_path):
    jobs = {
        "job": {
            "status": "pending",
            "classification": {"status": "complete", "score": 80},
            "screening": {"status": "qualified", "override": None},
        }
    }
    recovered = []
    completed = []
    manager = None

    async def apply_job(_url, _max_steps):
        manager.shutdown()
        return {"submission_confirmed": False, "retryable": False, "blocker": None}

    manager = _manager(
        tmp_path,
        jobs,
        apply=apply_job,
        qualified=["job"],
        recovered=recovered,
        completed=completed,
    )
    await manager._run_cycle()
    assert manager._state["phase"] == "applying"
    assert manager._state["current_job_url"] == "job"

    restarted = _manager(
        tmp_path,
        jobs,
        qualified=["job"],
        recovered=recovered,
        completed=completed,
    )

    status = restarted.snapshot()["status"]
    assert recovered == ["job"]
    assert completed == ["job"]
    assert status["state"] == "paused"
    assert status["queued_count"] == 0
