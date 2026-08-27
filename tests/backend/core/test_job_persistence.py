import json
import threading

from core import shared_config


def test_add_jobs_if_new_never_overwrites_existing_job(tmp_path, monkeypatch):
    jobs_file = tmp_path / "jobs.json"
    jobs_file.write_text(
        json.dumps(
            {
                "https://jobs.example/existing": {
                    "url": "https://jobs.example/existing",
                    "status": "applied",
                }
            }
        )
    )
    monkeypatch.setattr(shared_config, "JOBS_FILE", jobs_file)
    monkeypatch.setattr(shared_config, "JOBS_LOCK", tmp_path / "jobs.db.lock")

    added = shared_config.add_jobs_if_new(
        [
            {"url": "https://jobs.example/existing", "status": "pending"},
            {"url": "https://jobs.example/new", "status": "pending"},
        ]
    )

    saved = shared_config.read_jobs()
    assert added == ["https://jobs.example/new"]
    assert saved["https://jobs.example/existing"]["status"] == "applied"
    assert saved["https://jobs.example/new"]["status"] == "pending"
    assert shared_config.jobs_db_path().exists()


def test_legacy_json_is_migrated_once(tmp_path, monkeypatch):
    jobs_file = tmp_path / "jobs.json"
    jobs_file.write_text(json.dumps({"u1": {"status": "pending"}}))
    monkeypatch.setattr(shared_config, "JOBS_FILE", jobs_file)
    monkeypatch.setattr(shared_config, "JOBS_LOCK", tmp_path / "jobs.db.lock")

    assert shared_config.read_jobs() == {"u1": {"status": "pending"}}

    # The JSON file is a backup/migration source, not a live mirror.
    jobs_file.write_text(json.dumps({"u1": {"status": "applied"}}))
    assert shared_config.read_jobs() == {"u1": {"status": "pending"}}


def test_concurrent_updates_to_one_job_preserve_both_changes(tmp_path, monkeypatch):
    jobs_file = tmp_path / "jobs.json"
    monkeypatch.setattr(shared_config, "JOBS_FILE", jobs_file)
    monkeypatch.setattr(shared_config, "JOBS_LOCK", tmp_path / "jobs.db.lock")
    shared_config.write_jobs({"u1": {"status": "pending"}})
    ready = threading.Barrier(3)

    def update(**fields):
        ready.wait()
        assert shared_config.update_job("u1", **fields)

    first = threading.Thread(target=update, kwargs={"title": "Engineer"})
    second = threading.Thread(target=update, kwargs={"company": "Acme"})
    first.start()
    second.start()
    ready.wait()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert shared_config.get_job("u1") == {
        "status": "pending",
        "title": "Engineer",
        "company": "Acme",
    }


def test_status_history_records_the_full_job_funnel(tmp_path, monkeypatch):
    jobs_file = tmp_path / "jobs.json"
    monkeypatch.setattr(shared_config, "JOBS_FILE", jobs_file)
    monkeypatch.setattr(shared_config, "JOBS_LOCK", tmp_path / "jobs.db.lock")
    url = "https://jobs.example/funnel"

    shared_config.add_jobs_if_new(
        [{"url": url, "status": "pending", "collected_at": "2026-01-01T09:00:00Z"}]
    )
    shared_config.update_job(
        url,
        screening={
            "status": "qualified",
            "override": None,
            "evaluated_at": "2026-01-01T09:05:00Z",
        },
    )
    shared_config.update_job(
        url,
        status="applied",
        applied_at="2026-01-02T10:00:00Z",
    )
    shared_config.update_job(
        url,
        application_status="online_assessment",
        application_status_updated_at="2026-01-04T11:00:00Z",
        application_status_source="gmail",
    )
    shared_config.update_job(
        url,
        application_status="rejected",
        application_status_updated_at="2026-01-07T12:00:00Z",
        application_status_source="gmail",
    )

    assert shared_config.get_job(url)["status_history"] == [
        {
            "status": "review",
            "changed_at": "2026-01-01T09:00:00Z",
            "source": "collection",
        },
        {
            "status": "qualified",
            "changed_at": "2026-01-01T09:05:00Z",
            "source": "classification",
        },
        {
            "status": "applied",
            "changed_at": "2026-01-02T10:00:00Z",
            "source": "automation",
        },
        {
            "status": "online_assessment",
            "changed_at": "2026-01-04T11:00:00Z",
            "source": "gmail",
        },
        {
            "status": "rejected",
            "changed_at": "2026-01-07T12:00:00Z",
            "source": "gmail",
        },
    ]


def test_legacy_job_history_is_reconstructed_from_existing_timestamps():
    history = shared_config.job_status_history(
        {
            "status": "applied",
            "collected_at": "2026-01-01T09:00:00Z",
            "screening": {
                "status": "qualified",
                "evaluated_at": "2026-01-01T09:05:00Z",
            },
            "applied_at": "2026-01-02T10:00:00Z",
            "application_status": "interview",
            "application_status_updated_at": "2026-01-05T12:00:00Z",
            "application_status_source": "gmail",
        }
    )

    assert [entry["status"] for entry in history] == [
        "review",
        "qualified",
        "applied",
        "interview",
    ]
    assert history[-1]["changed_at"] == "2026-01-05T12:00:00Z"
