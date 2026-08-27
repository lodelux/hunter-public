import sys

from cli import collect_jobs
from sources.jobspy_collector import CollectionResult


def test_cli_reuses_jobspy_collector(monkeypatch, capsys):
    captured = {}
    saved = []
    classified = []

    monkeypatch.setattr(
        collect_jobs,
        "load_profile",
        lambda: {
            "country": "IT",
            "years_of_experience": 2,
            "target_job_titles": ["Engineer"],
            "target_locations": ["Milan"],
            "blacklisted_companies": ["Outlier"],
        },
    )
    monkeypatch.setattr(collect_jobs, "read_jobs", lambda: {"known": {}})

    def fake_collect_jobs(**kwargs):
        captured.update(kwargs)
        return CollectionResult(
            jobs=[{"url": "https://jobs.example/one"}],
            errors=[],
            completed_queries=1,
            stopped=False,
        )

    monkeypatch.setattr(collect_jobs, "collect_jobs", fake_collect_jobs)
    monkeypatch.setattr(
        collect_jobs,
        "add_jobs_if_new",
        lambda jobs: saved.extend(jobs) or [job["url"] for job in jobs],
    )
    async def fake_classify(url):
        classified.append(url)
        return "complete"

    monkeypatch.setattr(collect_jobs, "classify_and_store_job", fake_classify)
    monkeypatch.setattr(
        sys,
        "argv",
        ["collect_jobs.py", "--source", "indeed", "--max-jobs", "10"],
    )

    assert collect_jobs.main() == 0
    assert captured["source"] == "indeed"
    assert captured["titles"] == ["Engineer"]
    assert captured["locations"] == ["Milan"]
    assert captured["country_code"] == "IT"
    assert captured["max_jobs"] == 10
    assert captured["hours_old"] == 168
    assert captured["job_type"] is None
    assert captured["is_remote"] is False
    assert captured["blacklisted_companies"] == ["Outlier"]
    assert saved == [{"url": "https://jobs.example/one"}]
    assert classified == ["https://jobs.example/one"]
    output = capsys.readouterr().out
    assert "Classifying 1/1: https://jobs.example/one" in output
    assert "Classified 1/1: https://jobs.example/one" in output


def test_cli_passes_indeed_remote_and_job_type_without_default_freshness(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        collect_jobs,
        "load_profile",
        lambda: {
            "country": "IT",
            "target_job_titles": ["Engineer"],
            "target_locations": ["Milan"],
        },
    )
    monkeypatch.setattr(collect_jobs, "read_jobs", lambda: {})
    monkeypatch.setattr(
        collect_jobs,
        "collect_jobs",
        lambda **kwargs: captured.update(kwargs)
        or CollectionResult([], [], 1, False),
    )
    monkeypatch.setattr(collect_jobs, "add_jobs_if_new", lambda jobs: [])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "collect_jobs.py",
            "--source",
            "indeed",
            "--job-type",
            "fulltime",
            "--is-remote",
        ],
    )

    assert collect_jobs.main() == 0
    assert captured["hours_old"] is None
    assert captured["job_type"] == "fulltime"
    assert captured["is_remote"] is True


def test_cli_returns_failure_for_invalid_profile(monkeypatch):
    monkeypatch.setattr(
        collect_jobs,
        "load_profile",
        lambda: {"country": "IT", "target_job_titles": [], "target_locations": []},
    )
    monkeypatch.setattr(collect_jobs, "read_jobs", lambda: {})
    monkeypatch.setattr(
        collect_jobs,
        "collect_jobs",
        lambda **kwargs: (_ for _ in ()).throw(ValueError("missing title")),
    )
    monkeypatch.setattr(sys, "argv", ["collect_jobs.py"])

    assert collect_jobs.main() == 1
