from datetime import date
import time

import pandas as pd
import pytest

from sources import jobspy_collector


def _frame(*rows):
    return pd.DataFrame(rows)


def test_country_and_freshness_mapping():
    assert jobspy_collector.country_name("IT") == "Italy"
    assert jobspy_collector.hours_old_from_filter(None) == 168
    assert jobspy_collector.hours_old_from_filter("7") == 168
    assert jobspy_collector.hours_old_from_filter("r86400") == 24


def test_invalid_country_and_freshness_are_rejected():
    with pytest.raises(ValueError, match="Unsupported profile country"):
        jobspy_collector.country_name("ZZ")
    with pytest.raises(ValueError, match="Invalid posted-within"):
        jobspy_collector.hours_old_from_filter("last-week")


def test_sub_hour_freshness_is_linkedin_only():
    assert jobspy_collector.normalize_hours_old("linkedin", "0.25") == 0.25
    assert jobspy_collector.normalize_hours_old("linkedin", "1") == 1

    with pytest.raises(ValueError, match="15 minutes"):
        jobspy_collector.normalize_hours_old("linkedin", "0.1")
    with pytest.raises(ValueError, match="whole number"):
        jobspy_collector.normalize_hours_old("indeed", "0.25")


def test_linkedin_sub_hour_freshness_sends_exact_seconds(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = ""

    class FakeSession:
        def get(self, url, params, timeout):
            captured.update(url=url, params=params, timeout=timeout)
            return FakeResponse()

    linkedin = jobspy_collector.LinkedIn()
    linkedin.session = FakeSession()
    monkeypatch.setattr(jobspy_collector, "LinkedIn", lambda: linkedin)

    frame = jobspy_collector._scrape_linkedin_with_seconds(
        search_term="Software Engineer",
        location="Berlin",
        results_wanted=10,
        offset=0,
        freshness_seconds=900,
        job_type=None,
        is_remote=False,
        country_indeed="Germany",
        description_format="markdown",
        linkedin_fetch_description=True,
        verbose=2,
    )

    assert frame.empty
    assert captured["params"]["f_TPR"] == "r900"
    assert captured["timeout"] == 10


def test_linkedin_sub_hour_response_keeps_jobspy_fields():
    from jobspy.model import (
        Compensation,
        CompensationInterval,
        JobPost,
        JobResponse,
        JobType,
        Location,
    )

    frame = jobspy_collector._linkedin_response_frame(
        JobResponse(
            jobs=[
                JobPost(
                    id="li-123",
                    title="Software Engineer",
                    company_name="Example",
                    job_url="https://www.linkedin.com/jobs/view/123",
                    location=Location(city="Berlin", country="Germany"),
                    job_type=[JobType.FULL_TIME],
                    compensation=Compensation(
                        interval=CompensationInterval.YEARLY,
                        min_amount=50_000,
                        max_amount=60_000,
                        currency="EUR",
                    ),
                    description="Build software.",
                )
            ]
        )
    )

    row = frame.iloc[0]
    assert row["company"] == "Example"
    assert row["location"] == "Berlin, Germany"
    assert row["job_type"] == "fulltime"
    assert row["interval"] == "yearly"
    assert row["min_amount"] == 50_000
    assert row["salary_source"] == "direct_data"


def test_linkedin_listing_metadata_preserves_approximate_applicant_count():
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(
        """
        <span class="posted-time-ago__text">Reposted 20 minutes ago</span>
        <figcaption class="num-applicants__caption">Over 100 applicants</figcaption>
        """,
        "html.parser",
    )

    metadata = jobspy_collector._linkedin_listing_metadata(soup)

    assert metadata == {
        "date_posted_text": "Reposted 20 minutes ago",
        "applicant_count": 100,
        "applicant_count_text": "Over 100 applicants",
        "applicant_count_is_minimum": True,
    }


def test_linkedin_listing_rejection_is_conservative():
    assert jobspy_collector.linkedin_listing_rejection_reason(
        {"date_posted_text": "Reposted 20 minutes ago"}
    ) == "Reposted 20 minutes ago"
    assert jobspy_collector.linkedin_listing_rejection_reason(
        {
            "applicant_count": 100,
            "applicant_count_text": "Over 100 applicants",
            "applicant_count_is_minimum": True,
        }
    ) == "Over 100 applicants"
    assert (
        jobspy_collector.linkedin_listing_rejection_reason(
            {
                "applicant_count": 100,
                "applicant_count_text": "100 applicants",
                "applicant_count_is_minimum": False,
            }
        )
        is None
    )


def test_linkedin_job_id_accepts_listing_and_search_urls():
    assert (
        jobspy_collector.linkedin_job_id_from_url(
            "https://www.linkedin.com/jobs/view/ai-researcher-4428487670/"
        )
        == "4428487670"
    )
    assert (
        jobspy_collector.linkedin_job_id_from_url(
            "https://www.linkedin.com/jobs/search-results/?currentJobId=4428487670"
        )
        == "4428487670"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/jobs/view/4428487670",
        "https://www.linkedin.com/jobs/search-results/",
        "https://notlinkedin.com/jobs/view/4428487670",
        "ftp://www.linkedin.com/jobs/view/4428487670",
    ],
)
def test_linkedin_job_id_rejects_non_listing_urls(url):
    with pytest.raises(ValueError):
        jobspy_collector.linkedin_job_id_from_url(url)


def test_import_linkedin_job_fetches_and_normalizes_listing(monkeypatch):
    html = """
    <h1 class="top-card-layout__title">AI Security Researcher</h1>
    <a class="topcard__org-name-link">Opera</a>
    <span class="topcard__flavor--bullet">Warsaw, Poland</span>
    <div class="show-more-less-html__markup">
      <p>Research agentic AI systems.</p>
      <ul><li>Build reproducible security tooling.</li></ul>
    </div>
    <ul>
      <li class="description__job-criteria-item"><h3>Seniority level</h3><span>Mid-Senior level</span></li>
      <li class="description__job-criteria-item"><h3>Employment type</h3><span>Full-time</span></li>
      <li class="description__job-criteria-item"><h3>Job function</h3><span>Research</span></li>
      <li class="description__job-criteria-item"><h3>Industries</h3><span>Software Development</span></li>
    </ul>
    <code id="applyUrl">https://www.linkedin.com/redirect?url=https%3A%2F%2Fjobs.example%2Fapply&amp;x=1</code>
    """

    class FakeResponse:
        url = "https://www.linkedin.com/jobs/view/4428487670"
        text = html

        def raise_for_status(self):
            return None

    class FakeSession:
        def get(self, url, timeout):
            assert url == "https://www.linkedin.com/jobs/view/4428487670"
            assert timeout == 10
            return FakeResponse()

    class FakeLinkedIn:
        def __init__(self):
            self.session = FakeSession()

    monkeypatch.setattr(jobspy_collector, "LinkedIn", FakeLinkedIn)

    job = jobspy_collector.import_linkedin_job(
        "https://www.linkedin.com/jobs/search-results/?currentJobId=4428487670"
    )

    assert job["url"] == "https://www.linkedin.com/jobs/view/4428487670"
    assert job["source_id"] == "li-4428487670"
    assert job["title"] == "AI Security Researcher"
    assert job["company"] == "Opera"
    assert job["location"] == "Warsaw, Poland"
    assert job["direct_url"] == "https://jobs.example/apply"
    assert job["job_level"] == "Mid-Senior level"
    assert job["job_type"] == "Full-time"
    assert job["job_function"] == "Research"
    assert job["company_industry"] == "Software Development"
    assert "Research agentic AI systems." in job["description"]
    assert job["status"] == "pending"


def test_collects_all_queries_then_deduplicates_sorts_and_globally_limits(monkeypatch):
    calls = []

    def fake_scrape_jobs(**kwargs):
        calls.append(kwargs)
        title = kwargs["search_term"]
        location = kwargs["location"]
        suffix = f"{title}-{location}".replace(" ", "-")
        return _frame(
            {
                "id": f"id-{suffix}",
                "job_url": f"https://jobs.example/{suffix}",
                "job_url_direct": f"https://ats.example/{suffix}",
                "title": title,
                "company": "Example",
                "location": location,
                "date_posted": date(2026, 7, 20 + len(calls)),
                "job_type": "fulltime",
                "is_remote": False,
                "job_level": "Entry level",
                "job_function": "Information Technology",
                "company_industry": "Financial Services",
                "salary_source": "direct_data",
                "interval": "yearly",
                "min_amount": 32000,
                "max_amount": 40000,
                "currency": "EUR",
                "description": f"Description for {title}",
            },
            {
                "id": "duplicate",
                "job_url": "https://jobs.example/duplicate",
                "title": "Duplicate",
                "company": "Example",
                "location": location,
                "date_posted": date(2026, 7, 1),
                "description": pd.NA,
            },
        )

    monkeypatch.setattr(jobspy_collector, "scrape_jobs", fake_scrape_jobs)

    result = jobspy_collector.collect_jobs(
        source="linkedin",
        titles=["Engineer", "Analyst"],
        locations=["Milan", "Berlin"],
        country_code="IT",
        max_jobs=3,
        job_type="fulltime",
        is_remote=True,
        existing_urls={"https://jobs.example/duplicate"},
    )

    assert len(calls) == 4
    assert all(call["country_indeed"] == "Italy" for call in calls)
    assert all(call["hours_old"] == 168 for call in calls)
    assert all(call["job_type"] == "fulltime" for call in calls)
    assert all(call["is_remote"] is True for call in calls)
    assert all(call["linkedin_fetch_description"] is True for call in calls)
    assert all(call["results_wanted"] == 10 for call in calls)
    assert all(call["offset"] == 0 for call in calls)
    assert all(call["verbose"] == 2 for call in calls)
    assert len(result.jobs) == 3
    assert [job["date_posted"] for job in result.jobs] == [
        "2026-07-24",
        "2026-07-23",
        "2026-07-22",
    ]
    assert result.jobs[0]["direct_url"].startswith("https://ats.example/")
    assert result.jobs[0]["description"].startswith("Description for")
    assert result.jobs[0]["easy_apply"] is None
    assert result.jobs[0]["job_level"] == "Entry level"
    assert result.jobs[0]["job_function"] == "Information Technology"
    assert result.jobs[0]["company_industry"] == "Financial Services"
    assert result.jobs[0]["salary_source"] == "direct_data"
    assert result.jobs[0]["interval"] == "yearly"
    assert result.jobs[0]["min_amount"] == 32000
    assert result.jobs[0]["max_amount"] == 40000
    assert result.jobs[0]["currency"] == "EUR"
    assert result.completed_queries == 4
    assert result.errors == []


def test_partial_failures_keep_successful_results(monkeypatch):
    def fake_scrape_jobs(**kwargs):
        if kwargs["location"] == "Paris":
            raise RuntimeError("blocked")
        return _frame(
            {
                "id": "one",
                "job_url": "https://jobs.example/one",
                "title": "Engineer",
                "company": "Example",
                "location": "Milan",
                "date_posted": pd.NaT,
                "is_remote": pd.NA,
                "job_level": "Entry level",
            }
        )

    monkeypatch.setattr(jobspy_collector, "scrape_jobs", fake_scrape_jobs)

    result = jobspy_collector.collect_jobs(
        source="linkedin",
        titles=["Engineer"],
        locations=["Paris", "Milan"],
        country_code="FR",
        max_jobs=20,
    )

    assert len(result.jobs) == 1
    assert result.jobs[0]["date_posted"] is None
    assert result.jobs[0]["is_remote"] is None
    assert result.jobs[0]["job_level"] == "Entry level"
    assert result.jobs[0]["min_amount"] is None
    assert result.completed_queries == 1
    assert len(result.errors) == 1


def test_blacklisted_companies_are_skipped_before_selection(monkeypatch):
    messages = []
    monkeypatch.setattr(
        jobspy_collector,
        "scrape_jobs",
        lambda **_kwargs: _frame(
            {
                "job_url": "https://jobs.example/outlier",
                "title": "AI Trainer",
                "company": "OUTLIER!",
            },
            {
                "job_url": "https://jobs.example/alignerr",
                "title": "AI Trainer",
                "company": "Alignerr",
            },
            {
                "job_url": "https://jobs.example/example",
                "title": "Software Engineer",
                "company": "Example",
            },
        ),
    )

    result = jobspy_collector.collect_jobs(
        source="indeed",
        titles=["Engineer"],
        locations=["Berlin"],
        country_code="DE",
        max_jobs=20,
        blacklisted_companies=["Outlier", "alignerr"],
        on_progress=messages.append,
    )

    assert [job["company"] for job in result.jobs] == ["Example"]
    assert any("Skipped 2 blacklisted company listings" in line for line in messages)


def test_linkedin_rejects_only_obviously_senior_jobs_before_classification(monkeypatch):
    messages = []

    monkeypatch.setattr(
        jobspy_collector,
        "scrape_jobs",
        lambda **kwargs: _frame(
            {
                "job_url": "https://jobs.example/internship",
                "title": "Software Engineering Intern",
                "job_level": "Internship",
            },
            {
                "job_url": "https://jobs.example/junior",
                "title": "Junior Software Engineer",
                "job_level": "Entry level",
            },
            {
                "job_url": "https://jobs.example/director",
                "title": "Engineering Director",
                "job_level": "Director",
            },
            {
                "job_url": "https://jobs.example/executive",
                "title": "VP Engineering",
                "job_level": "executive",
            },
            {
                "job_url": "https://jobs.example/senior",
                "title": "Software Engineer",
                "job_level": "Mid-Senior level",
            },
            {
                "job_url": "https://jobs.example/unknown",
                "title": "Security Engineer",
                "job_level": "Not Applicable",
            },
            {
                "job_url": "https://jobs.example/internship-override",
                "title": "Senior Software Engineering Intern",
                "job_level": "Director",
                "job_type": "internship",
            },
        ),
    )

    result = jobspy_collector.collect_jobs(
        source="linkedin",
        titles=["Engineer"],
        locations=["Milan"],
        country_code="IT",
        max_jobs=7,
        on_progress=messages.append,
    )

    assert [job["url"] for job in result.jobs] == [
        "https://jobs.example/internship",
        "https://jobs.example/junior",
        "https://jobs.example/senior",
        "https://jobs.example/unknown",
        "https://jobs.example/internship-override",
    ]
    assert any(
        "Skipped 2 obviously senior LinkedIn listings" in message
        for message in messages
    )


def test_linkedin_rejects_explicit_seniority_in_title():
    assert jobspy_collector.linkedin_preclassification_rejection_reason(
        {"title": "Senior Software Engineer", "job_level": "Mid-Senior level"}
    )
    assert jobspy_collector.linkedin_preclassification_rejection_reason(
        {"title": "Principal Engineer", "job_level": "Not Applicable"}
    )
    assert jobspy_collector.linkedin_preclassification_rejection_reason(
        {"title": "Software Engineer", "job_level": "Mid-Senior level"}
    ) is None
    assert jobspy_collector.linkedin_preclassification_rejection_reason(
        {"title": "Working Student", "job_level": None}
    ) is None


def test_seniority_skips_are_replaced_from_later_jobspy_pages(monkeypatch):
    calls = []

    def fake_scrape_jobs(**kwargs):
        calls.append(kwargs)
        if kwargs["offset"] == 0:
            return _frame(
                *[
                    {
                        "job_url": f"https://jobs.example/senior-{index}",
                        "title": f"Senior Software Engineer {index}",
                        "job_level": "Mid-Senior level",
                    }
                    for index in range(9)
                ],
                {
                    "job_url": "https://jobs.example/junior-1",
                    "job_level": "Entry level",
                },
            )
        return _frame(
            {
                "job_url": "https://jobs.example/junior-2",
                "job_level": "Junior",
            },
            {
                "job_url": "https://jobs.example/junior-3",
                "job_level": "Entry level",
            },
            {
                "job_url": "https://jobs.example/internship",
                "job_level": "Internship",
            },
            {
                "job_url": "https://jobs.example/junior-4",
                "job_level": "Entry level",
            },
        )

    monkeypatch.setattr(jobspy_collector, "scrape_jobs", fake_scrape_jobs)

    result = jobspy_collector.collect_jobs(
        source="linkedin",
        titles=["Engineer"],
        locations=["Milan"],
        country_code="IT",
        max_jobs=5,
    )

    assert [call["results_wanted"] for call in calls] == [10, 10]
    assert [call["offset"] for call in calls] == [0, 10]
    assert len(result.jobs) == 5
    assert [job["url"] for job in result.jobs] == [
        "https://jobs.example/junior-1",
        "https://jobs.example/junior-2",
        "https://jobs.example/junior-3",
        "https://jobs.example/internship",
        "https://jobs.example/junior-4",
    ]


def test_reposts_and_jobs_over_100_applicants_are_replaced_from_later_pages(
    monkeypatch,
):
    calls = []

    def fake_scrape_jobs(**kwargs):
        calls.append(kwargs)
        if kwargs["offset"] == 0:
            return _frame(
                *[
                    {
                        "id": f"li-{index}",
                        "job_url": f"https://www.linkedin.com/jobs/view/{index}",
                        "title": f"Engineer {index}",
                    }
                    for index in range(10)
                ]
            )
        return _frame(
            {
                "id": "li-10",
                "job_url": "https://www.linkedin.com/jobs/view/10",
                "title": "Engineer 10",
            },
            {
                "id": "li-11",
                "job_url": "https://www.linkedin.com/jobs/view/11",
                "title": "Engineer 11",
            },
        )

    def fake_metadata(_session, url):
        job_id = int(url.rsplit("/", 1)[-1])
        if job_id < 5:
            return {"date_posted_text": "Reposted 20 minutes ago"}
        if job_id < 10:
            return {
                "applicant_count": 101,
                "applicant_count_text": "101 applicants",
                "applicant_count_is_minimum": False,
            }
        return {
            "applicant_count": 100,
            "applicant_count_text": "100 applicants",
            "applicant_count_is_minimum": False,
        }

    monkeypatch.setattr(jobspy_collector, "scrape_jobs", fake_scrape_jobs)
    monkeypatch.setattr(
        jobspy_collector,
        "_fetch_linkedin_listing_metadata",
        fake_metadata,
    )

    result = jobspy_collector.collect_jobs(
        source="linkedin",
        titles=["Engineer"],
        locations=["Berlin"],
        country_code="DE",
        max_jobs=2,
    )

    assert [call["offset"] for call in calls] == [0, 10]
    assert [job["source_id"] for job in result.jobs] == ["li-10", "li-11"]


def test_existing_jobs_json_urls_do_not_consume_the_collection_limit(monkeypatch):
    calls = []
    existing_urls = {
        f"https://www.linkedin.com/jobs/view/{job_id}"
        for job_id in range(100, 110)
    }

    def fake_scrape_jobs(**kwargs):
        calls.append(kwargs)
        if kwargs["offset"] == 0:
            return _frame(
                *[
                    {
                        "job_url": (
                            f"https://www.linkedin.com/jobs/view/existing-role-{job_id}/"
                            "?utm_source=tracker"
                        ),
                        "job_level": "Entry level",
                    }
                    for job_id in range(100, 110)
                ]
            )
        return _frame(
            {
                "job_url": "https://www.linkedin.com/jobs/view/new-role-200/",
                "job_level": "Entry level",
            },
            {
                "job_url": "https://www.linkedin.com/jobs/view/new-role-201/",
                "job_level": "Junior",
            },
        )

    monkeypatch.setattr(jobspy_collector, "scrape_jobs", fake_scrape_jobs)

    result = jobspy_collector.collect_jobs(
        source="linkedin",
        titles=["Engineer"],
        locations=["Berlin"],
        country_code="DE",
        max_jobs=2,
        existing_urls=existing_urls,
    )

    assert [call["offset"] for call in calls] == [0, 10]
    assert [jobspy_collector._job_url_identity(job["url"]) for job in result.jobs] == [
        "linkedin:200",
        "linkedin:201",
    ]


def test_empty_locations_use_profile_country_as_the_search_location(monkeypatch):
    calls = []

    def fake_scrape_jobs(**kwargs):
        calls.append(kwargs)
        return _frame()

    monkeypatch.setattr(jobspy_collector, "scrape_jobs", fake_scrape_jobs)

    result = jobspy_collector.collect_jobs(
        source="linkedin",
        titles=["Engineer"],
        locations=[],
        country_code="DE",
        max_jobs=20,
    )

    assert calls[0]["location"] == "Germany"
    assert result.completed_queries == 1


def test_all_query_failures_raise(monkeypatch):
    monkeypatch.setattr(
        jobspy_collector,
        "scrape_jobs",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("blocked")),
    )

    with pytest.raises(RuntimeError, match="All JobSpy queries failed"):
        jobspy_collector.collect_jobs(
            source="indeed",
            titles=["Engineer"],
            locations=["Rome"],
            country_code="IT",
            max_jobs=20,
        )


def test_stop_is_checked_between_queries(monkeypatch):
    stopped = False

    def fake_scrape_jobs(**kwargs):
        nonlocal stopped
        stopped = True
        return _frame(
            {
                "id": "one",
                "job_url": "https://jobs.example/one",
                "title": "Engineer",
                "company": "Example",
                "location": kwargs["location"],
                "job_level": "junior",
            }
        )

    monkeypatch.setattr(jobspy_collector, "scrape_jobs", fake_scrape_jobs)

    result = jobspy_collector.collect_jobs(
        source="linkedin",
        titles=["Engineer"],
        locations=["Milan", "Rome"],
        country_code="IT",
        max_jobs=20,
        should_stop=lambda: stopped,
    )

    assert result.stopped is True
    assert result.completed_queries == 1
    assert len(result.jobs) == 1


def test_progress_reports_query_boundaries_and_heartbeat(monkeypatch):
    messages = []
    monkeypatch.setattr(jobspy_collector, "HEARTBEAT_SECONDS", 0.01)

    def slow_scrape(**kwargs):
        time.sleep(0.03)
        return _frame()

    monkeypatch.setattr(jobspy_collector, "scrape_jobs", slow_scrape)

    jobspy_collector.collect_jobs(
        source="linkedin",
        titles=["Engineer"],
        locations=["Milan"],
        country_code="IT",
        max_jobs=20,
        on_progress=messages.append,
    )

    assert messages[0] == "Prepared 1 linkedin search"
    assert any("Searching linkedin: Engineer in Milan (1/1)" in line for line in messages)
    assert any("Still waiting for linkedin: Engineer in Milan" in line for line in messages)
    assert any("Found 0 raw listings" in line for line in messages)
    assert messages[-1] == "Selected 0 new jobs after deduplication"


def test_progress_forwards_jobspy_logger_records(monkeypatch):
    messages = []

    def logged_scrape(**kwargs):
        import logging

        logging.getLogger("JobSpy:LinkedIn").info("search page: 1 / 1")
        return _frame()

    monkeypatch.setattr(jobspy_collector, "scrape_jobs", logged_scrape)

    jobspy_collector.collect_jobs(
        source="linkedin",
        titles=["Engineer"],
        locations=["Milan"],
        country_code="IT",
        max_jobs=20,
        on_progress=messages.append,
    )

    assert "JobSpy: search page: 1 / 1" in messages


def test_rejects_unsupported_source():
    with pytest.raises(ValueError, match="Unsupported JobSpy source"):
        jobspy_collector.collect_jobs(
            source="glassdoor",
            titles=["Engineer"],
            locations=["Milan"],
            country_code="IT",
            max_jobs=20,
        )


def test_rejects_indeed_filter_combinations_and_unsupported_job_types():
    with pytest.raises(ValueError, match="cannot combine"):
        jobspy_collector.collect_jobs(
            source="indeed",
            titles=["Engineer"],
            locations=["Milan"],
            country_code="IT",
            max_jobs=20,
            hours_old=24,
            is_remote=True,
        )

    with pytest.raises(ValueError, match="Indeed does not support"):
        jobspy_collector.collect_jobs(
            source="indeed",
            titles=["Engineer"],
            locations=["Milan"],
            country_code="IT",
            max_jobs=20,
            hours_old=None,
            job_type="temporary",
        )
