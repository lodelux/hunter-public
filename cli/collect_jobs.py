"""Collect European job listings deterministically with JobSpy.

Usage:
  uv run python cli/collect_jobs.py
  uv run python cli/collect_jobs.py --source indeed --title "Data Analyst"
  uv run python cli/collect_jobs.py --location "Milan, Italy" --hours-old 24
  uv run python cli/collect_jobs.py --job-type fulltime --is-remote
"""

import argparse
import asyncio
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = PROJECT_ROOT / "backend"
for path in (str(PROJECT_ROOT), str(BACKEND_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from core.config import load_profile
from core.shared_config import add_jobs_if_new, get_job, read_jobs
from job_classifier import classify_and_store_job
from sources.jobspy_collector import (
    DEFAULT_HOURS_OLD,
    INDEED_JOB_TYPES,
    SUPPORTED_JOB_TYPES,
    SUPPORTED_SOURCES,
    collect_jobs,
)

CLASSIFICATION_HEARTBEAT_SECONDS = 10


def _max_jobs(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 500:
        raise argparse.ArgumentTypeError("max jobs must be between 1 and 500")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=sorted(SUPPORTED_SOURCES), default="linkedin")
    parser.add_argument("--title", help="Use one title instead of all profile titles")
    parser.add_argument("--location", help="Use one location instead of all profile locations")
    parser.add_argument("--max-jobs", type=_max_jobs, default=20)
    parser.add_argument(
        "--hours-old",
        type=float,
        help="Freshness in hours; LinkedIn accepts decimals such as 0.25 for 15 minutes",
    )
    parser.add_argument("--job-type", choices=sorted(SUPPORTED_JOB_TYPES))
    parser.add_argument("--is-remote", action="store_true")
    args = parser.parse_args()

    if args.source == "indeed" and args.job_type not in INDEED_JOB_TYPES | {None}:
        parser.error(f"Indeed does not support the {args.job_type} job type filter")
    if args.source == "indeed" and args.hours_old is not None and (
        args.job_type or args.is_remote
    ):
        parser.error("Indeed cannot combine --hours-old with --job-type or --is-remote")

    hours_old = args.hours_old if args.hours_old is not None else DEFAULT_HOURS_OLD
    if args.source == "indeed" and (args.job_type or args.is_remote):
        hours_old = None

    profile = load_profile()
    titles = [args.title] if args.title else profile.get("target_job_titles", [])
    locations = [args.location] if args.location else profile.get("target_locations", [])
    existing_urls = set(read_jobs())

    try:
        result = collect_jobs(
            source=args.source,
            titles=titles,
            locations=locations,
            country_code=profile.get("country", ""),
            max_jobs=args.max_jobs,
            hours_old=hours_old,
            job_type=args.job_type,
            is_remote=args.is_remote,
            blacklisted_companies=profile.get("blacklisted_companies", []),
            existing_urls=existing_urls,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"Collection failed: {exc}", file=sys.stderr)
        return 1

    added_urls = add_jobs_if_new(result.jobs)
    print(f"\nCollection complete: {len(added_urls)} new jobs saved")
    if result.errors:
        print(f"{len(result.errors)} queries failed; successful results were kept")

    async def _classify_new_jobs():
        total = len(added_urls)
        for index, url in enumerate(added_urls, start=1):
            job = get_job(url) or {}
            title = str(job.get("title") or "").strip()
            company = str(job.get("company") or "").strip()
            label = " at ".join(part for part in (title, company) if part) or url
            print(f"🤖 Classifying {index}/{total}: {label}")

            task = asyncio.create_task(classify_and_store_job(url))
            elapsed = 0
            while True:
                try:
                    outcome = await asyncio.wait_for(
                        asyncio.shield(task),
                        timeout=CLASSIFICATION_HEARTBEAT_SECONDS,
                    )
                    break
                except asyncio.TimeoutError:
                    elapsed += CLASSIFICATION_HEARTBEAT_SECONDS
                    print(
                        f"⏳ Still classifying {index}/{total}: "
                        f"{label} — {elapsed}s elapsed"
                    )

            if outcome == "failed":
                print(f"Classification failed for {url}; the job remains saved")
            elif outcome == "complete":
                saved = get_job(url) or {}
                score = (saved.get("classification") or {}).get("score")
                screening = saved.get("screening") or {}
                status = screening.get("override") or screening.get("status")
                details = []
                if score is not None:
                    details.append(f"score {score}")
                if status:
                    details.append(str(status))
                suffix = f" — {', '.join(details)}" if details else ""
                print(f"✅ Classified {index}/{total}: {label}{suffix}")

    if added_urls:
        asyncio.run(_classify_new_jobs())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
