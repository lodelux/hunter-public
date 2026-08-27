"""Deterministic European job collection through JobSpy."""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from fractions import Fraction
import logging
import math
import re
import threading
import time
from typing import Callable
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

import pandas as pd
from bs4 import BeautifulSoup
from jobspy import scrape_jobs
from jobspy.linkedin import LinkedIn
from jobspy.model import (
    Country,
    DescriptionFormat,
    Location as JobSpyLocation,
    SalarySource,
    ScraperInput,
    Site,
)
from jobspy.util import get_enum_from_value, set_logger_level

try:
    from core.country_config import COUNTRY_CONFIGS
    from sources.company_filter import blacklisted_company_match
except ImportError:
    from backend.core.country_config import COUNTRY_CONFIGS
    from backend.sources.company_filter import blacklisted_company_match


SUPPORTED_SOURCES = frozenset({"linkedin", "indeed"})
SUPPORTED_JOB_TYPES = frozenset(
    {
        "contract",
        "fulltime",
        "internship",
        "nights",
        "other",
        "parttime",
        "perdiem",
        "summer",
        "temporary",
        "volunteer",
    }
)
INDEED_JOB_TYPES = frozenset({"contract", "fulltime", "internship", "parttime"})
DEFAULT_HOURS_OLD = 7 * 24
HEARTBEAT_SECONDS = 10
JOBSPY_VERBOSE = 2
_JOBSPY_LOGGERS = {
    "linkedin": "JobSpy:LinkedIn",
    "indeed": "JobSpy:Indeed",
}
_OBVIOUSLY_SENIOR_LINKEDIN_LEVELS = frozenset({"director", "executive"})
_OBVIOUSLY_SENIOR_TITLE = re.compile(
    r"\b(?:senior|sr\.?|staff|principal|lead|director|head|chief|vice president|vp)\b",
    re.IGNORECASE,
)
_LINKEDIN_REPOSTED_RE = re.compile(
    r"\b(?:reposted|erneut veröffentlicht|wieder veröffentlicht|neu veröffentlicht)\b",
    re.IGNORECASE,
)
_LINKEDIN_APPLICANT_COUNT_RE = re.compile(
    r"\b(?P<prefix>over|more than|mehr als)?\s*"
    r"(?P<count>\d[\d.,\s]*)\s*(?P<plus>\+)?\s+"
    r"(?:applicants?|bewerber(?::?innen)?)\b",
    re.IGNORECASE,
)
_TRACKING_QUERY_KEYS = frozenset({"refid", "trackingid", "trk"})


class _JobSpyProgressHandler(logging.Handler):
    def __init__(self, report: Callable[[str], None]):
        super().__init__(level=logging.INFO)
        self.report = report

    def emit(self, record: logging.LogRecord):
        try:
            self.report(f"JobSpy: {record.getMessage()}")
        except Exception:
            self.handleError(record)


@dataclass
class CollectionResult:
    jobs: list[dict]
    errors: list[str]
    completed_queries: int
    stopped: bool


def country_name(country_code: str) -> str:
    """Translate a Hunter profile country code to the name JobSpy expects."""
    config = COUNTRY_CONFIGS.get((country_code or "").upper())
    if not config:
        raise ValueError(f"Unsupported profile country: {country_code or '(empty)'}")
    return config["name"]


def hours_old_from_filter(value: object) -> int:
    """Translate existing plugin date values to JobSpy's hour count."""
    if value is None or str(value).strip() == "":
        return DEFAULT_HOURS_OLD

    raw = str(value).strip().lower()
    try:
        if raw.startswith("r"):
            return max(1, int(raw[1:]) // 3600)
        return max(1, int(raw) * 24)
    except ValueError as exc:
        raise ValueError(f"Invalid posted-within filter: {value}") from exc


def normalize_hours_old(source: str, value: object) -> int | float | None:
    """Validate freshness while allowing fractional hours for LinkedIn."""
    if value is None or str(value).strip() == "":
        return None

    try:
        hours = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid hours_old value: {value}") from exc

    if not math.isfinite(hours):
        raise ValueError(f"Invalid hours_old value: {value}")
    if source == "linkedin":
        if hours < 0.25:
            raise ValueError("LinkedIn hours_old must be at least 0.25 (15 minutes)")
    elif hours < 1 or not hours.is_integer():
        raise ValueError("Indeed hours_old must be a whole number of at least 1")

    return int(hours) if hours.is_integer() else hours


def _linkedin_response_frame(job_response) -> pd.DataFrame:
    """Convert JobSpy's LinkedIn response to the public scrape_jobs frame shape."""
    rows: list[dict] = []
    for job in job_response.jobs:
        row = job.model_dump()
        row["site"] = "linkedin"
        row["company"] = row.pop("company_name")

        job_types = row.get("job_type")
        row["job_type"] = (
            ", ".join(job_type.value[0] for job_type in job_types)
            if job_types
            else None
        )
        row["emails"] = ", ".join(row["emails"]) if row.get("emails") else None
        if row.get("location"):
            row["location"] = JobSpyLocation(**row["location"]).display_location()

        compensation = row.pop("compensation", None)
        if compensation:
            interval = compensation.get("interval")
            row["interval"] = interval.value if interval else None
            row["min_amount"] = compensation.get("min_amount")
            row["max_amount"] = compensation.get("max_amount")
            row["currency"] = compensation.get("currency", "USD")
            row["salary_source"] = (
                SalarySource.DIRECT_DATA.value if row["min_amount"] else None
            )
        else:
            row.update(
                interval=None,
                min_amount=None,
                max_amount=None,
                currency=None,
                salary_source=None,
            )
        rows.append(row)

    return pd.DataFrame(rows)


def _scrape_linkedin_with_seconds(
    *,
    search_term: str,
    location: str,
    results_wanted: int,
    offset: int,
    freshness_seconds: int,
    job_type: str | None,
    is_remote: bool,
    country_indeed: str,
    description_format: str,
    linkedin_fetch_description: bool,
    verbose: int,
) -> pd.DataFrame:
    """Use JobSpy's parser while sending LinkedIn an exact-second f_TPR value."""
    set_logger_level(verbose)
    scraper_input = ScraperInput(
        site_type=[Site.LINKEDIN],
        country=Country.from_string(country_indeed),
        search_term=search_term,
        location=location,
        distance=50,
        is_remote=is_remote,
        job_type=get_enum_from_value(job_type) if job_type else None,
        description_format=DescriptionFormat(description_format),
        linkedin_fetch_description=linkedin_fetch_description,
        results_wanted=results_wanted,
        offset=offset,
        hours_old=1,
    )
    # JobSpy's public model only accepts whole hours. Its LinkedIn scraper multiplies
    # this value by 3600, so an exact Fraction produces f_TPR=r900 for 15 minutes.
    scraper_input.hours_old = Fraction(freshness_seconds, 3600)
    return _linkedin_response_frame(LinkedIn().scrape(scraper_input))


def _optional_value(value):
    if value is None:
        return None
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    return value.item() if hasattr(value, "item") else value


def _iso_date(value) -> str | None:
    value = _optional_value(value)
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _is_linkedin_host(host: str) -> bool:
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def _job_url_identity(url: str) -> str:
    """Return a stable identity for exact jobs across harmless URL variants."""
    parsed = urlsplit(str(url).strip())
    host = parsed.netloc.casefold()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/") or "/"
    query_items = parse_qsl(parsed.query, keep_blank_values=True)

    if _is_linkedin_host(host):
        match = re.search(r"/jobs/view/(?:.*-)?(\d+)$", path)
        if match:
            return f"linkedin:{match.group(1)}"
        query = dict(query_items)
        if query.get("currentJobId"):
            return f"linkedin:{query['currentJobId']}"

    if host.endswith("indeed.com"):
        query = dict(query_items)
        indeed_id = query.get("jk") or query.get("vjk")
        if indeed_id:
            return f"indeed:{indeed_id}"

    meaningful_query = sorted(
        (key, value)
        for key, value in query_items
        if not key.casefold().startswith("utm_")
        and key.casefold() not in _TRACKING_QUERY_KEYS
    )
    suffix = f"?{urlencode(meaningful_query)}" if meaningful_query else ""
    return f"{host}{path}{suffix}"


def linkedin_job_id_from_url(url: str) -> str:
    """Extract a numeric LinkedIn job ID from a listing or search-result URL."""
    parsed = urlsplit(str(url).strip())
    host = (parsed.hostname or "").casefold()
    if parsed.scheme not in {"http", "https"} or not _is_linkedin_host(host):
        raise ValueError("Enter a LinkedIn job URL")

    path_match = re.search(r"/jobs/view/(?:.*-)?(\d+)/?$", parsed.path)
    query_job_id = dict(parse_qsl(parsed.query)).get("currentJobId")
    job_id = path_match.group(1) if path_match else query_job_id
    if not job_id or not str(job_id).isdigit():
        raise ValueError("The LinkedIn URL does not contain a job ID")
    return str(job_id)


def _linkedin_job_criteria(soup: BeautifulSoup) -> dict[str, str]:
    criteria: dict[str, str] = {}
    for item in soup.select("li.description__job-criteria-item"):
        label = item.select_one("h3")
        value = item.select_one("span")
        if label and value:
            criteria[label.get_text(" ", strip=True).casefold()] = value.get_text(
                " ", strip=True
            )
    return criteria


def _linkedin_direct_url(soup: BeautifulSoup) -> str | None:
    apply_url = soup.select_one("code#applyUrl")
    if not apply_url:
        return None
    match = re.search(r"[?&]url=([^\"&]+)", apply_url.decode_contents().strip())
    return unquote(match.group(1)) if match else None


def _linkedin_listing_metadata(soup: BeautifulSoup) -> dict:
    """Extract conservative repost and applicant metadata from a public detail page."""
    posted_node = soup.select_one(".posted-time-ago__text")
    applicant_node = soup.select_one(".num-applicants__caption")
    posted_text = posted_node.get_text(" ", strip=True) if posted_node else None
    applicant_text = (
        applicant_node.get_text(" ", strip=True) if applicant_node else None
    )

    applicant_count = None
    applicant_count_is_minimum = None
    if applicant_text and "among the first" not in applicant_text.casefold():
        match = _LINKEDIN_APPLICANT_COUNT_RE.search(applicant_text)
        if match:
            applicant_count = int(re.sub(r"\D", "", match.group("count")))
            applicant_count_is_minimum = bool(
                match.group("prefix") or match.group("plus")
            )

    return {
        "date_posted_text": posted_text,
        "applicant_count": applicant_count,
        "applicant_count_text": applicant_text,
        "applicant_count_is_minimum": applicant_count_is_minimum,
    }


def _fetch_linkedin_listing_metadata(session, url: str) -> dict:
    """Fetch only metadata that JobSpy's normalized JobPost currently discards."""
    response = session.get(url, timeout=5)
    response.raise_for_status()
    if "linkedin.com/signup" in response.url:
        return {}
    return _linkedin_listing_metadata(BeautifulSoup(response.text, "html.parser"))


def import_linkedin_job(url: str) -> dict:
    """Fetch and normalize one public LinkedIn listing from its URL."""
    job_id = linkedin_job_id_from_url(url)
    canonical_url = f"https://www.linkedin.com/jobs/view/{job_id}"
    scraper = LinkedIn()

    try:
        response = scraper.session.get(canonical_url, timeout=10)
        response.raise_for_status()
    except Exception as exc:
        raise RuntimeError(
            "Could not fetch the LinkedIn job; it may be unavailable or temporarily blocked"
        ) from exc
    if "linkedin.com/signup" in response.url:
        raise RuntimeError("LinkedIn requires sign-in to view this job")

    soup = BeautifulSoup(response.text, "html.parser")
    title_node = soup.select_one("h1.top-card-layout__title")
    company_node = soup.select_one("a.topcard__org-name-link")
    location_node = soup.select_one(".topcard__flavor--bullet")
    description_node = soup.select_one("div.show-more-less-html__markup")

    title = title_node.get_text(" ", strip=True) if title_node else ""
    company = company_node.get_text(" ", strip=True) if company_node else ""
    description = (
        description_node.get_text("\n", strip=True) if description_node else ""
    )
    if not title or not company or not description:
        raise RuntimeError(
            "LinkedIn did not return a complete job listing; it may have expired"
        )

    criteria = _linkedin_job_criteria(soup)
    job = _normalize_job(
        {
            "id": f"li-{job_id}",
            "job_url": canonical_url,
            "job_url_direct": _linkedin_direct_url(soup),
            "title": title,
            "company": company,
            "location": (
                location_node.get_text(" ", strip=True) if location_node else ""
            ),
            "date_posted": None,
            "job_type": criteria.get("employment type"),
            "is_remote": None,
            "job_level": criteria.get("seniority level"),
            "job_function": criteria.get("job function"),
            "company_industry": criteria.get("industries"),
            "description": description,
        },
        "linkedin",
        "",
        "",
    )
    if job is None:
        raise RuntimeError("LinkedIn returned an invalid job URL")
    return job


def _normalize_job(row: dict, source: str, title: str, location: str) -> dict | None:
    url = str(_optional_value(row.get("job_url")) or "").strip()
    if not url.startswith(("http://", "https://")):
        return None

    description = _optional_value(row.get("description"))
    direct_url = _optional_value(row.get("job_url_direct"))
    remote = _optional_value(row.get("is_remote"))
    job_level = _optional_value(row.get("job_level"))
    job_function = _optional_value(row.get("job_function"))
    company_industry = _optional_value(row.get("company_industry"))
    date_posted_text = _optional_value(row.get("date_posted_text"))
    salary_source = _optional_value(row.get("salary_source"))
    interval = _optional_value(row.get("interval"))
    currency = _optional_value(row.get("currency"))
    easy_apply = _optional_value(row.get("easy_apply"))
    applicant_count = _optional_value(row.get("applicant_count"))
    applicant_count_text = _optional_value(row.get("applicant_count_text"))
    applicant_count_is_minimum = _optional_value(
        row.get("applicant_count_is_minimum")
    )

    return {
        "url": url,
        "source": source,
        "source_id": _optional_value(row.get("id")),
        "direct_url": str(direct_url).strip() if direct_url else None,
        "title": str(_optional_value(row.get("title")) or "").strip(),
        "company": str(_optional_value(row.get("company")) or "").strip(),
        "location": str(_optional_value(row.get("location")) or "").strip(),
        "date_posted": _iso_date(row.get("date_posted")),
        "date_posted_text": str(date_posted_text).strip() if date_posted_text else None,
        "job_type": _optional_value(row.get("job_type")),
        "is_remote": bool(remote) if remote is not None else None,
        "job_level": str(job_level).strip() if job_level else None,
        "job_function": str(job_function).strip() if job_function else None,
        "company_industry": str(company_industry).strip() if company_industry else None,
        "salary_source": str(salary_source).strip() if salary_source else None,
        "interval": str(interval).strip() if interval else None,
        "min_amount": _optional_value(row.get("min_amount")),
        "max_amount": _optional_value(row.get("max_amount")),
        "currency": str(currency).strip() if currency else None,
        "description": str(description).strip() if description else None,
        "easy_apply": bool(easy_apply) if easy_apply is not None else None,
        "applicant_count": (
            int(applicant_count) if applicant_count is not None else None
        ),
        "applicant_count_text": (
            str(applicant_count_text).strip() if applicant_count_text else None
        ),
        "applicant_count_is_minimum": (
            bool(applicant_count_is_minimum)
            if applicant_count_is_minimum is not None
            else None
        ),
        "search_title": title,
        "search_location": location,
        "status": "pending",
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "applied_at": None,
        "error": None,
    }


def _normalize_linkedin_job_level(job_level: object) -> str:
    normalized_level = " ".join(
        str(job_level or "").strip().casefold().replace("-", " ").split()
    )
    return normalized_level


def linkedin_preclassification_rejection_reason(job: dict) -> str | None:
    """Reject only LinkedIn jobs that are unambiguously senior before classification."""
    if "internship" in str(job.get("job_type") or "").casefold():
        return None

    job_level = _normalize_linkedin_job_level(job.get("job_level"))
    if job_level in _OBVIOUSLY_SENIOR_LINKEDIN_LEVELS:
        return f"LinkedIn level is {job_level}"

    title_match = _OBVIOUSLY_SENIOR_TITLE.search(str(job.get("title") or ""))
    if title_match:
        return f'title contains "{title_match.group(0)}"'

    return None


def linkedin_listing_rejection_reason(job: dict) -> str | None:
    """Reject explicit reposts and listings explicitly above 100 applicants."""
    posted_text = str(job.get("date_posted_text") or "")
    if _LINKEDIN_REPOSTED_RE.search(posted_text):
        return posted_text

    applicant_count = job.get("applicant_count")
    minimum = job.get("applicant_count_is_minimum") is True
    if isinstance(applicant_count, int) and (
        applicant_count > 100 or (applicant_count == 100 and minimum)
    ):
        return str(job.get("applicant_count_text") or f"{applicant_count} applicants")
    return None


def collect_jobs(
    *,
    source: str,
    titles: list[str],
    locations: list[str],
    country_code: str,
    max_jobs: int,
    hours_old: int | float | None = DEFAULT_HOURS_OLD,
    job_type: str | None = None,
    is_remote: bool = False,
    blacklisted_companies: list[str] | None = None,
    existing_urls: set[str] | None = None,
    should_stop: Callable[[], bool] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> CollectionResult:
    """Collect, normalize, deduplicate, and globally limit JobSpy results."""
    if source not in SUPPORTED_SOURCES:
        raise ValueError(f"Unsupported JobSpy source: {source}")
    if not 1 <= max_jobs <= 500:
        raise ValueError("max_jobs must be between 1 and 500")
    hours_old = normalize_hours_old(source, hours_old)
    if job_type is not None and job_type not in SUPPORTED_JOB_TYPES:
        raise ValueError(f"Unsupported JobSpy job type: {job_type}")
    if source == "indeed" and job_type is not None and job_type not in INDEED_JOB_TYPES:
        raise ValueError(f"Indeed does not support the {job_type} job type filter")
    if source == "indeed" and hours_old is not None and (job_type or is_remote):
        raise ValueError("Indeed cannot combine hours_old with job_type or is_remote")

    clean_titles = [title.strip() for title in titles if title and title.strip()]
    if not clean_titles:
        raise ValueError("Add at least one target job title to the profile")

    clean_locations = [location.strip() for location in locations if location and location.strip()]
    if not clean_locations:
        clean_locations = [""]

    country = country_name(country_code)
    known_url_identities = {
        _job_url_identity(url) for url in (existing_urls or set())
    }
    stop_requested = should_stop or (lambda: False)
    report = on_progress or print
    candidates: list[dict] = []
    eligible_urls: set[str] = set()
    skipped_blacklisted = 0
    skipped_seniority = 0
    skipped_listing_metadata = 0
    errors: list[str] = []
    completed_queries = 0
    total_queries = len(clean_titles) * len(clean_locations)
    query_number = 0
    page_size = (
        max(10, ((max_jobs + 9) // 10) * 10)
        if source == "linkedin"
        else max_jobs
    )
    linkedin_metadata_session = None
    linkedin_metadata_cache: dict[str, dict] = {}

    report(
        f"Prepared {total_queries} {source} "
        f"{'search' if total_queries == 1 else 'searches'}"
    )

    for title in clean_titles:
        for location in clean_locations:
            if stop_requested():
                break

            query_number += 1
            effective_location = location or country
            query_label = (
                f"{source}: {title} in {effective_location} "
                f"({query_number}/{total_queries})"
            )
            report(f"🔎 Searching {query_label}")
            heartbeat_stop = threading.Event()
            started_at = time.monotonic()

            def heartbeat():
                while not heartbeat_stop.wait(HEARTBEAT_SECONDS):
                    elapsed = round(time.monotonic() - started_at)
                    report(f"⏳ Still waiting for {query_label} — {elapsed}s elapsed")

            heartbeat_thread = threading.Thread(
                target=heartbeat,
                daemon=True,
                name="jobspy-progress",
            )
            heartbeat_thread.start()
            jobspy_logger = logging.getLogger(_JOBSPY_LOGGERS[source])
            progress_handler = (
                _JobSpyProgressHandler(report) if on_progress is not None else None
            )
            if progress_handler is not None:
                jobspy_logger.addHandler(progress_handler)
            try:
                offset = 0
                raw_urls: set[str] = set()
                query_completed = False

                while not stop_requested():
                    scrape_kwargs = {
                        "search_term": title,
                        "location": effective_location,
                        "results_wanted": page_size,
                        "offset": offset,
                        "job_type": job_type,
                        "is_remote": is_remote,
                        "country_indeed": country,
                        "description_format": "markdown",
                        "linkedin_fetch_description": source == "linkedin",
                        "verbose": JOBSPY_VERBOSE,
                    }
                    if (
                        source == "linkedin"
                        and hours_old is not None
                        and not float(hours_old).is_integer()
                    ):
                        frame = _scrape_linkedin_with_seconds(
                            freshness_seconds=round(hours_old * 3600),
                            **scrape_kwargs,
                        )
                    else:
                        frame = scrape_jobs(
                            site_name=[source],
                            hours_old=hours_old,
                            **scrape_kwargs,
                        )
                    if not query_completed:
                        completed_queries += 1
                        query_completed = True

                    report(
                        f"Found {len(frame)} raw listings for {query_label} "
                        f"at offset {offset}"
                    )
                    new_raw_urls = 0
                    for row in frame.to_dict(orient="records"):
                        job = _normalize_job(row, source, title, effective_location)
                        if not job:
                            continue

                        url = job["url"]
                        if url not in raw_urls:
                            raw_urls.add(url)
                            new_raw_urls += 1

                        blocked_company = blacklisted_company_match(
                            job.get("company"),
                            blacklisted_companies,
                        )
                        if blocked_company:
                            skipped_blacklisted += 1
                            report(
                                "Skipped blacklisted company before classification: "
                                f"{job.get('company') or blocked_company}"
                            )
                            continue

                        rejection_reason = (
                            linkedin_preclassification_rejection_reason(job)
                            if source == "linkedin"
                            else None
                        )
                        if rejection_reason:
                            skipped_seniority += 1
                            report(
                                "Skipped obviously senior listing before "
                                f"classification: {job.get('title') or url} "
                                f"({rejection_reason})"
                            )
                            continue

                        identity = _job_url_identity(url)
                        if identity in known_url_identities or identity in eligible_urls:
                            continue

                        source_id = str(job.get("source_id") or "")
                        parsed_url = urlsplit(url)
                        if (
                            source == "linkedin"
                            and source_id.startswith("li-")
                            and _is_linkedin_host(parsed_url.netloc.casefold())
                        ):
                            if identity not in linkedin_metadata_cache:
                                try:
                                    if linkedin_metadata_session is None:
                                        linkedin_metadata_session = LinkedIn().session
                                    linkedin_metadata_cache[identity] = (
                                        _fetch_linkedin_listing_metadata(
                                            linkedin_metadata_session,
                                            url,
                                        )
                                    )
                                except Exception as exc:
                                    linkedin_metadata_cache[identity] = {}
                                    report(
                                        "Could not inspect LinkedIn applicant/repost "
                                        f"metadata for {url}: {exc}"
                                    )
                            raw_metadata = linkedin_metadata_cache[identity]
                            metadata = {
                                key: value
                                for key, value in raw_metadata.items()
                                if value is not None
                            }
                            job.update(metadata)

                        listing_rejection = (
                            linkedin_listing_rejection_reason(job)
                            if source == "linkedin"
                            else None
                        )
                        if listing_rejection:
                            skipped_listing_metadata += 1
                            report(
                                "Skipped LinkedIn listing before classification: "
                                f"{job.get('title') or url} ({listing_rejection})"
                            )
                            continue

                        candidates.append(job)
                        eligible_urls.add(identity)

                    report(f"Collected {len(candidates)} normalized candidates so far")
                    if len(eligible_urls) >= max_jobs:
                        break
                    if len(frame) < page_size or new_raw_urls == 0:
                        break

                    offset += page_size
                    report(
                        f"Fetching more {query_label} results to reach "
                        f"{max_jobs} valid jobs"
                    )
            except Exception as exc:
                message = f"{source}: {title} in {effective_location}: {exc}"
                errors.append(message)
                report(f"⚠️ {message}")
            finally:
                if progress_handler is not None:
                    jobspy_logger.removeHandler(progress_handler)
                heartbeat_stop.set()
                heartbeat_thread.join(timeout=1)

        if stop_requested():
            break

    stopped = stop_requested()
    if completed_queries == 0 and errors and not stopped:
        raise RuntimeError("All JobSpy queries failed: " + "; ".join(errors))

    if skipped_seniority:
        report(
            f"Skipped {skipped_seniority} "
            f"obviously senior LinkedIn "
            f"{'listing' if skipped_seniority == 1 else 'listings'} before "
            "classification"
        )
    if skipped_blacklisted:
        report(
            f"Skipped {skipped_blacklisted} blacklisted "
            f"{'company listing' if skipped_blacklisted == 1 else 'company listings'}"
        )
    if skipped_listing_metadata:
        report(
            f"Skipped {skipped_listing_metadata} reposted or high-applicant "
            f"LinkedIn {'listing' if skipped_listing_metadata == 1 else 'listings'}"
        )
    report(f"Deduplicating and ranking {len(candidates)} collected candidates")
    candidates.sort(key=lambda job: job.get("date_posted") or "", reverse=True)
    selected: list[dict] = []
    seen_url_identities = set(known_url_identities)
    for job in candidates:
        identity = _job_url_identity(job["url"])
        if identity in seen_url_identities:
            continue
        seen_url_identities.add(identity)
        selected.append(job)
        if len(selected) >= max_jobs:
            break

    report(f"Selected {len(selected)} new jobs after deduplication")

    return CollectionResult(
        jobs=selected,
        errors=errors,
        completed_queries=completed_queries,
        stopped=stopped,
    )
