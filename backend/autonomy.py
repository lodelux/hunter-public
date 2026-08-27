"""Persisted in-process orchestration for autonomous job applications."""

from __future__ import annotations

import asyncio
import copy
import json
import random
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable

try:
    from job_screening import POLICY_VERSION as SCREENING_POLICY_VERSION
except ImportError:
    from backend.job_screening import POLICY_VERSION as SCREENING_POLICY_VERSION


ALLOWED_INTERVALS = (5, 10, 15, 30, 60)
APPLICATION_STEP_LIMIT = 180
PRE_AGENT_ATTEMPT_LIMIT = 3
MAX_LOG_LINES = 200
LINKEDIN_SCAN_LIMIT = 25
FRESHNESS_OVERLAP_MINUTES = 15
SCAN_INTERVAL_JITTER = 0.20
_REPOSTED_RE = re.compile(
    r"\b(?:reposted|erneut veröffentlicht|wieder veröffentlicht|neu veröffentlicht)\b",
    re.IGNORECASE,
)

DEFAULT_CONFIG = {
    "enabled": False,
    "sources": ["linkedin", "indeed"],
    "interval_minutes": 15,
    "daily_job_limit": 10,
    "daily_company_limit": 5,
    "max_jobs_per_source": 5,
    "max_applications_per_cycle": 2,
    "automation_min_score": 70,
    "linkedin_outreach_enabled": True,
    "hours_old": 1,
    "title": "",
    "location": "",
    "job_type": "",
    "is_remote": False,
    "linkedin_search_url": "",
}

DEFAULT_STATE = {
    "state": "stopped",
    "phase": "idle",
    "queued_urls": [],
    "current_source": None,
    "current_job_url": None,
    "next_run_at": None,
    "last_cycle_started_at": None,
    "last_cycle_finished_at": None,
    "daily_date": None,
    "today_started_urls": [],
    "today_started_company_counts": {},
    "last_source_scans": {},
    "captcha_count": 0,
    "pause_reason": None,
    "log": [],
}


class FatalAutomationError(RuntimeError):
    """A serious condition that requires the autonomous runner to pause."""


def _jittered_interval_minutes(interval: int) -> float:
    return random.uniform(
        interval * (1 - SCAN_INTERVAL_JITTER),
        interval * (1 + SCAN_INTERVAL_JITTER),
    )


def _now() -> datetime:
    return datetime.now().astimezone()


def _needs_classification(job: dict) -> bool:
    return (
        (job.get("classification") or {}).get("status") != "complete"
        or (job.get("screening") or {}).get("policy_version")
        != SCREENING_POLICY_VERSION
    )


def _is_reposted(job: dict) -> bool:
    return bool(_REPOSTED_RE.search(str(job.get("date_posted_text") or "")))


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2), encoding="utf-8")
    tmp.replace(path)


def _load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return copy.deepcopy(default)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return copy.deepcopy(default)
    if not isinstance(value, dict):
        return copy.deepcopy(default)
    return {**copy.deepcopy(default), **value}


def validate_config(raw: dict) -> dict:
    config = {**DEFAULT_CONFIG, **(raw or {})}
    sources = config.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("Select at least one automation source")
    if any(source not in {"linkedin", "indeed"} for source in sources):
        raise ValueError("Automation sources must be LinkedIn or Indeed")
    config["sources"] = list(dict.fromkeys(sources))

    interval = int(config.get("interval_minutes", 15))
    if interval not in ALLOWED_INTERVALS:
        raise ValueError("Interval must be 5, 10, 15, 30, or 60 minutes")
    config["interval_minutes"] = interval

    daily_limit = int(config.get("daily_job_limit", 10))
    if not 1 <= daily_limit <= 100:
        raise ValueError("Daily job limit must be between 1 and 100")
    config["daily_job_limit"] = daily_limit

    company_limit = int(config.get("daily_company_limit", 5))
    if not 1 <= company_limit <= 20:
        raise ValueError("Daily company limit must be between 1 and 20")
    config["daily_company_limit"] = company_limit

    max_jobs = int(config.get("max_jobs_per_source", 5))
    if not 1 <= max_jobs <= 500:
        raise ValueError("Maximum jobs per source must be between 1 and 500")
    config["max_jobs_per_source"] = max_jobs

    max_per_cycle = int(config.get("max_applications_per_cycle", 2))
    if not 1 <= max_per_cycle <= 20:
        raise ValueError("Applications per cycle must be between 1 and 20")
    config["max_applications_per_cycle"] = max_per_cycle

    min_score = int(config.get("automation_min_score", 70))
    if not 0 <= min_score <= 100:
        raise ValueError("Automation minimum score must be between 0 and 100")
    config["automation_min_score"] = min_score
    config["linkedin_outreach_enabled"] = config.get("linkedin_outreach_enabled") is True

    hours_old = int(config.get("hours_old", 1))
    if not 1 <= hours_old <= 720:
        raise ValueError("Freshness must be between 1 and 720 hours")
    config["hours_old"] = hours_old

    for key in ("title", "location", "job_type", "linkedin_search_url"):
        config[key] = str(config.get(key) or "").strip()
    config["is_remote"] = config.get("is_remote") is True
    config["enabled"] = config.get("enabled") is True
    if config["linkedin_search_url"] and "linkedin" not in config["sources"]:
        raise ValueError("A LinkedIn search URL requires the LinkedIn source")
    return config


class AutomationManager:
    """Runs one serialized collection -> classification -> application loop."""

    def __init__(
        self,
        data_dir: Path,
        *,
        collect_source: Callable[[str, dict], Awaitable[list[str]]],
        classify_urls: Callable[[list[str]], Awaitable[None]],
        apply_job: Callable[[str, int, bool], Awaitable[dict]],
        job_snapshot: Callable[[str], dict | None],
        qualified_urls: Callable[[], list[str]],
        is_busy: Callable[[], bool],
        recover_interrupted: Callable[[str], None],
        mark_blocked: Callable[[str], None],
        mark_completed: Callable[[str], None],
        notify: Callable[[str], bool] | None = None,
    ):
        self.config_path = data_dir / "automation_config.json"
        self.state_path = data_dir / "automation_state.json"
        self._lock = threading.RLock()
        self._config = validate_config(_load_json(self.config_path, DEFAULT_CONFIG))
        self._state = _load_json(self.state_path, DEFAULT_STATE)
        self._collect_source = collect_source
        self._classify_urls = classify_urls
        self._apply_job = apply_job
        self._job_snapshot = job_snapshot
        self._qualified_urls = qualified_urls
        self._is_busy = is_busy
        self._recover_interrupted = recover_interrupted
        self._mark_blocked = mark_blocked
        self._mark_completed = mark_completed
        self._notify = notify
        self._thread: threading.Thread | None = None
        self._stop_requested = False
        self._shutdown_requested = False
        self._reconcile_queue()
        self._recover_interrupted_application()

    def _persist(self) -> None:
        _atomic_json(self.config_path, self._config)
        _atomic_json(self.state_path, self._state)

    def _log(self, message: str) -> None:
        entry = f"{_now().isoformat()} {message}"
        with self._lock:
            self._state["log"].append(entry)
            self._state["log"] = self._state["log"][-MAX_LOG_LINES:]
            self._persist()

    def _reconcile_queue(self) -> None:
        existing_queue = list(self._state.get("queued_urls") or [])
        queued = list(existing_queue)
        staged_urls = set(existing_queue)
        queued.extend(self._qualified_urls())
        kept = []
        for url in dict.fromkeys(queued):
            if (
                url == self._state.get("current_job_url")
                and self._state.get("phase") == "applying"
            ):
                kept.append(url)
                continue
            job = self._job_snapshot(url) or {}
            if job.get("status", "pending") != "pending":
                continue
            outcome = job.get("last_application_outcome")
            if isinstance(outcome, dict) and outcome.get("type") == "unknown_outcome":
                continue
            screening = job.get("screening") or {}
            effective = screening.get("override") or screening.get("status")
            if effective in {"review", "rejected"}:
                continue
            if _needs_classification(job):
                classification = job.get("classification") or {}
                if url in staged_urls or classification.get("status") == "complete":
                    kept.append(url)
                continue
            classification = job.get("classification") or {}
            score = classification.get("score")
            if (
                effective != "qualified"
                or not isinstance(score, (int, float))
                or score < self._config["automation_min_score"]
            ):
                continue
            kept.append(url)
        if kept != existing_queue:
            self._state["queued_urls"] = kept
            self._persist()

    def _recover_interrupted_application(self) -> None:
        current = self._state.get("current_job_url")
        if not current or self._state.get("phase") != "applying":
            return
        self._recover_interrupted(current)
        self._mark_completed(current)
        self._state["queued_urls"] = [
            item for item in self._state.get("queued_urls") or [] if item != current
        ]
        self._state.update(
            {
                "state": "paused",
                "phase": "idle",
                "pause_reason": (
                    "Hunter stopped during an autonomous application. The outcome is unknown; "
                    "review the dossier before resuming."
                ),
                "current_job_url": None,
                "next_run_at": None,
            }
        )
        self._config["enabled"] = True
        self._persist()

    def _reset_daily_counters(self) -> None:
        today = _now().date().isoformat()
        if self._state.get("daily_date") == today:
            return
        self._state["daily_date"] = today
        self._state["today_started_urls"] = []
        self._state["today_started_company_counts"] = {}
        self._state["captcha_count"] = 0
        self._persist()

    def snapshot(self) -> dict:
        with self._lock:
            self._reset_daily_counters()
            self._reconcile_queue()
            state = dict(self._state)
            state["log"] = list(state.get("log") or [])
            state["queued_count"] = len(state.get("queued_urls") or [])
            state["today_started"] = len(state.get("today_started_urls") or [])
            state["today_limit"] = self._config["daily_job_limit"]
            state.pop("queued_urls", None)
            state.pop("today_started_urls", None)
            state.pop("today_started_companies", None)
            state.pop("today_started_company_counts", None)
            return {"config": copy.deepcopy(self._config), "status": state}

    def save_config(self, raw: dict) -> dict:
        with self._lock:
            enabled = self._config.get("enabled", False)
            config = validate_config({**raw, "enabled": enabled})
            self._config = config
            self._reconcile_queue()
            self._persist()
            return dict(config)

    def start(self) -> None:
        with self._lock:
            self._config["enabled"] = True
            self._state.update(
                {
                    "state": "running",
                    "phase": "waiting",
                    "pause_reason": None,
                    "next_run_at": _now().isoformat(),
                }
            )
            self._stop_requested = False
            self._shutdown_requested = False
            self._persist()
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._thread_main,
                daemon=True,
                name="hunter-autonomy",
            )
            self._thread.start()

    def start_if_enabled(self) -> None:
        if self._config.get("enabled") and self._state.get("state") != "paused":
            self.start()

    def stop(self) -> None:
        with self._lock:
            self._config["enabled"] = False
            self._stop_requested = True
            self._state["next_run_at"] = None
            if self._state.get("phase") != "applying":
                self._state.update({"state": "stopped", "phase": "idle"})
            self._persist()
        self._log("Stop requested; no further autonomous jobs will start")

    def discard_url(self, url: str) -> None:
        """Permanently remove an excluded job from autonomous work."""
        with self._lock:
            if url not in (self._state.get("queued_urls") or []):
                return
        self._remove_from_queue(url)
        self._log(f"Removed excluded job from autonomous queue: {url}")

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown_requested = True
            self._stop_requested = True

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run_loop())
        except Exception as exc:
            with self._lock:
                current = self._state.get("current_job_url")
                applying = self._state.get("phase") == "applying"
            if applying and current:
                self._recover_interrupted(current)
                self._remove_from_queue(current)
                self._pause(
                    "The autonomous application worker stopped unexpectedly. "
                    "The outcome is unknown; review the dossier before resuming."
                )
                return
            self._pause(f"Autonomous worker error: {exc}")
        finally:
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None
                should_restart = bool(
                    self._config.get("enabled")
                    and self._state.get("state") == "running"
                    and not self._stop_requested
                    and not self._shutdown_requested
                )
            if should_restart:
                self.start()

    async def _run_loop(self) -> None:
        self._log("Autonomous mode started")
        while True:
            with self._lock:
                if self._shutdown_requested:
                    return
                if self._stop_requested or not self._config.get("enabled"):
                    self._state.update(
                        {
                            "state": "stopped",
                            "phase": "idle",
                            "next_run_at": None,
                            "current_job_url": None,
                            "current_source": None,
                        }
                    )
                    self._persist()
                    return
                if self._state.get("state") == "paused":
                    return
                next_run = self._state.get("next_run_at")

            if next_run:
                try:
                    delay = (datetime.fromisoformat(next_run) - _now()).total_seconds()
                except ValueError:
                    delay = 0
                if delay > 0:
                    await asyncio.sleep(min(delay, 1))
                    continue

            await self._wait_until_idle()
            if self._stop_requested or self._shutdown_requested:
                continue
            await self._run_cycle()

    async def _wait_until_idle(self) -> None:
        announced = False
        while self._is_busy():
            if not announced:
                self._set_phase("waiting")
                self._log("Waiting for manual Hunter work to finish")
                announced = True
            if self._stop_requested or self._shutdown_requested:
                return
            await asyncio.sleep(1)

    def _set_phase(self, phase: str, **fields) -> None:
        with self._lock:
            self._state.update({"state": "running", "phase": phase, **fields})
            self._persist()

    def _pause(self, reason: str) -> None:
        with self._lock:
            self._state.update(
                {
                    "state": "paused",
                    "phase": "idle",
                    "pause_reason": reason,
                    "next_run_at": None,
                    "current_job_url": None,
                    "current_source": None,
                }
            )
            self._persist()
        self._log(f"Paused: {reason}")

    async def _run_cycle(self) -> None:
        self._reset_daily_counters()
        self._reconcile_queue()
        self._set_phase(
            "waiting",
            last_cycle_started_at=_now().isoformat(),
            current_source=None,
        )
        self._log("Starting autonomous cycle")

        cycle_started_urls: set[str] = set()
        await self._classify_queued_jobs()
        queue_drained = await self._process_queue(cycle_started_urls)
        if self._shutdown_requested or self._stop_requested:
            return
        if self._state.get("state") == "paused":
            return

        daily_limit_reached = len(self._state.get("today_started_urls") or []) >= self._config[
            "daily_job_limit"
        ]
        if daily_limit_reached:
            self._log("Daily autonomous job limit reached; skipping discovery until it resets")
        elif queue_drained:
            self._log("No immediately eligible queued jobs; starting discovery")
            for source in self._config["sources"]:
                if self._stop_requested or self._shutdown_requested:
                    return
                self._set_phase("collecting", current_source=source)
                scan_started_at = _now()
                source_config = dict(self._config)
                if source == "linkedin" and source_config.get("linkedin_search_url"):
                    freshness_floor = scan_started_at - timedelta(hours=source_config["hours_old"])
                    raw_previous_scan = (self._state.get("last_source_scans") or {}).get(source)
                    try:
                        previous_scan = datetime.fromisoformat(raw_previous_scan)
                    except (TypeError, ValueError):
                        previous_scan = None
                    if previous_scan is not None:
                        freshness_floor = max(
                            freshness_floor,
                            previous_scan - timedelta(minutes=FRESHNESS_OVERLAP_MINUTES),
                        )
                    source_config["_posted_after"] = freshness_floor.isoformat()
                    source_config["_max_results_to_inspect"] = LINKEDIN_SCAN_LIMIT
                try:
                    added = await self._collect_source(source, source_config)
                except FatalAutomationError as exc:
                    self._pause(str(exc))
                    return
                except Exception as exc:
                    self._log(f"{source.title()} collection failed: {exc}")
                    continue
                with self._lock:
                    queued = list(self._state.get("queued_urls") or [])
                    queued.extend(added)
                    self._state["queued_urls"] = list(dict.fromkeys(queued))
                    scans = dict(self._state.get("last_source_scans") or {})
                    scans[source] = scan_started_at.isoformat()
                    self._state["last_source_scans"] = scans
                    self._persist()
                self._log(f"{source.title()} added {len(added)} new jobs")

            await self._classify_queued_jobs()
            await self._process_queue(cycle_started_urls)
        if self._shutdown_requested or self._stop_requested:
            return
        if self._state.get("state") == "paused":
            return
        now = _now()
        daily_limit_reached = len(self._state.get("today_started_urls") or []) >= self._config[
            "daily_job_limit"
        ]
        if daily_limit_reached:
            next_run = (now + timedelta(days=1)).replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
            next_run_message = f"Daily limit reached; next cycle at {next_run.isoformat()}"
        else:
            interval = _jittered_interval_minutes(self._config["interval_minutes"])
            next_run = now + timedelta(minutes=interval)
            next_run_message = f"Cycle finished; next scan in {interval:.1f} minutes"
        self._set_phase(
            "waiting",
            current_source=None,
            current_job_url=None,
            last_cycle_finished_at=now.isoformat(),
            next_run_at=next_run.isoformat(),
        )
        self._log(next_run_message)

    async def _classify_queued_jobs(self) -> None:
        queued_to_classify = []
        for url in list(self._state.get("queued_urls") or []):
            job = self._job_snapshot(url) or {}
            if _is_reposted(job):
                self._block_reposted(url)
                continue
            if _needs_classification(job):
                queued_to_classify.append(url)
        if queued_to_classify:
            self._set_phase("classifying", current_source=None)
            await self._classify_urls(list(dict.fromkeys(queued_to_classify)))

    @staticmethod
    def _rank_key(job: dict) -> tuple[float, float]:
        score = (job.get("classification") or {}).get("score")
        numeric_score = float(score) if isinstance(score, (int, float)) else -1.0
        try:
            raw_posted_at = str(job.get("date_posted") or "").replace("Z", "+00:00")
            posted_at = datetime.fromisoformat(raw_posted_at)
            if posted_at.tzinfo is None:
                posted_at = posted_at.replace(tzinfo=_now().tzinfo)
            posted_timestamp = posted_at.timestamp()
        except (TypeError, ValueError):
            posted_timestamp = 0.0
        return numeric_score, posted_timestamp

    async def _process_queue(self, cycle_started_urls: set[str]) -> bool:
        eligible_urls: list[str] = []
        min_score = self._config["automation_min_score"]
        for url in list(self._state.get("queued_urls") or []):
            job = self._job_snapshot(url)
            if not job:
                self._remove_from_queue(url)
                continue
            if job.get("status", "pending") != "pending":
                self._remove_from_queue(url)
                continue
            classification = job.get("classification") or {}
            screening = job.get("screening") or {}
            effective = screening.get("override") or screening.get("status")
            if classification.get("status") != "complete" or effective != "qualified":
                self._remove_from_queue(url)
                continue
            if job.get("status") == "applied":
                self._remove_from_queue(url)
                continue
            if _is_reposted(job):
                self._block_reposted(url)
                continue
            score = classification.get("score")
            if not isinstance(score, (int, float)) or score < min_score:
                self._remove_from_queue(url)
                continue

            eligible_urls.append(url)

        eligible_urls.sort(
            key=lambda url: self._rank_key(self._job_snapshot(url) or {}),
            reverse=True,
        )

        for url in eligible_urls:
            if self._stop_requested or self._shutdown_requested:
                return False
            self._reset_daily_counters()
            started = self._state.get("today_started_urls") or []
            if (
                url not in cycle_started_urls
                and len(cycle_started_urls) >= self._config["max_applications_per_cycle"]
            ):
                self._log(
                    "Per-cycle autonomous application limit reached; qualified jobs remain queued"
                )
                return False
            if url not in started and len(started) >= self._config["daily_job_limit"]:
                self._log("Daily autonomous job limit reached; qualified jobs remain queued")
                return False
            job = self._job_snapshot(url) or {}
            company = " ".join(str(job.get("company") or "").casefold().split())
            raw_company_counts = self._state.get("today_started_company_counts") or {}
            company_counts = dict(raw_company_counts) if isinstance(raw_company_counts, dict) else {}
            if not company_counts:
                company_counts = {
                    str(value): 1
                    for value in self._state.get("today_started_companies") or []
                }
            if (
                url not in started
                and company
                and int(company_counts.get(company) or 0) >= self._config["daily_company_limit"]
            ):
                self._log(
                    f"Deferred another autonomous application to {job.get('company')} "
                    f"until the daily company limit resets: {url}"
                )
                continue
            if url not in started:
                with self._lock:
                    self._state["today_started_urls"].append(url)
                    if company:
                        counts = dict(self._state.get("today_started_company_counts") or {})
                        counts[company] = int(counts.get(company) or 0) + 1
                        self._state["today_started_company_counts"] = counts
                    self._persist()
            cycle_started_urls.add(url)

            completed = await self._attempt_job(url)
            if completed:
                self._remove_from_queue(url)
            if self._state.get("state") == "paused":
                return False
        return True

    def _block_reposted(self, url: str) -> None:
        self._mark_blocked(url)
        self._remove_from_queue(url)
        self._log(f"Moved reposted autonomous job to Dismissed: {url}")

    async def _attempt_job(self, url: str) -> bool:
        attempt_number = 1
        while True:
            self._set_phase("applying", current_job_url=url, current_source=None)
            self._log(
                f"Applying to queued job ({APPLICATION_STEP_LIMIT} step budget): {url}"
            )
            outcome = await self._apply_job(
                url,
                APPLICATION_STEP_LIMIT,
                self._config["linkedin_outreach_enabled"],
            )
            if (
                self._shutdown_requested
                and outcome.get("submission_confirmed") is not True
            ):
                return False
            blocker = outcome.get("blocker")
            if outcome.get("submission_confirmed") is True:
                self._log(f"Confirmed application submission: {url}")
                return True
            if blocker == "authentication":
                self._log(
                    "Authentication or MFA could not be completed; "
                    f"moving to the next autonomous job: {url}"
                )
                return True
            if blocker == "linkedin_authentication":
                self._pause(
                    "LinkedIn login could not be restored; autonomous work stopped before "
                    "another application was started"
                )
                return False
            if blocker == "anti_bot":
                self._log(
                    "Application site blocked Hunter as automated traffic; "
                    f"moving to the next autonomous job: {url}"
                )
                return True
            if blocker == "captcha":
                with self._lock:
                    self._state["captcha_count"] = int(self._state.get("captcha_count") or 0) + 1
                    captcha_count = self._state["captcha_count"]
                    self._persist()
                self._log(f"Unresolved CAPTCHA recorded ({captcha_count} today)")
                return True
            if blocker in {"invalid_url", "blocked_domain"}:
                self._log(f"Skipped permanently blocked autonomous job: {url}")
                return True
            if not outcome.get("retryable"):
                self._log(
                    "Skipped application with an unverifiable outcome to avoid a duplicate: "
                    f"{url}"
                )
                return True
            if self._stop_requested or self._shutdown_requested:
                return False
            if (
                outcome.get("type") == "pre_submission_failure"
                and attempt_number < PRE_AGENT_ATTEMPT_LIMIT
            ):
                attempt_number += 1
                self._log(
                    "Application failed before the browser agent started; retrying setup: "
                    f"{url}"
                )
                continue
            if outcome.get("type") == "pre_submission_failure":
                self._log(
                    f"Application setup failed {PRE_AGENT_ATTEMPT_LIMIT} times; "
                    f"moving to the next job: {url}"
                )
                if self._notify is not None:
                    job = self._job_snapshot(url) or {}
                    title = str(job.get("title") or "Unknown role")
                    company = str(job.get("company") or "Unknown company")
                    message = (
                        f"Hunter: application setup failed {PRE_AGENT_ATTEMPT_LIMIT} times "
                        f"for {title} at {company}. The job was moved to Failed. {url}"
                    )
                    try:
                        sent = await asyncio.to_thread(self._notify, message)
                        if not sent:
                            self._log(f"Telegram failure alert could not be sent: {url}")
                    except Exception as error:
                        self._log(
                            "Telegram failure alert raised an error: "
                            f"{url} ({str(error)[:200]})"
                        )
                return True
            self._log(f"Confirmed non-submission; moving to the next job: {url}")
            return True

    def _remove_from_queue(self, url: str) -> None:
        self._mark_completed(url)
        with self._lock:
            self._state["queued_urls"] = [
                item for item in self._state.get("queued_urls") or [] if item != url
            ]
            if self._state.get("current_job_url") == url:
                self._state["current_job_url"] = None
            self._persist()
