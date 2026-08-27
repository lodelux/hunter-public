import base64
import io
import json
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from browser_use.browser.video_recorder import VideoRecorderService
from PIL import Image

from backend.core.application_attempt import (
    ApplicationAttempt,
    application_attempt_storage_report,
    cleanup_application_attempts,
    find_attempt_directory,
)


class FakeMetrics:
    def __init__(self):
        self.rows = []

    def log(self, run_id, message, level="INFO", job_url=None):
        self.rows.append((run_id, message, level, job_url))


class FakeUsage:
    total_cost = 0.125

    def model_dump(self, mode="json"):
        return {"total_cost": self.total_cost, "total_tokens": 1234}


class FakeResult:
    def __init__(self, screenshot_path: Path, uploaded_path: Path, password: str):
        self.history = [
            SimpleNamespace(
                state=SimpleNamespace(screenshot_path=str(screenshot_path)),
            )
        ]
        self.usage = FakeUsage()
        self._dump = {
            "history": [
                {
                    "model_output": {
                        "evaluation_previous_goal": "Form loaded",
                        "next_goal": "Upload supporting document",
                        "action": [
                            {
                                "upload_file": {
                                    "index": 3,
                                    "path": str(uploaded_path),
                                    "password": password,
                                }
                            },
                            {
                                "upload_file_by_label": {
                                    "field_name": "Resume",
                                    "path": str(uploaded_path),
                                }
                            },
                        ],
                    },
                    "result": [{"long_term_memory": "Document uploaded"}],
                    "state": {
                        "url": "https://example.com/apply",
                        "title": "Apply",
                        "tabs": [],
                        "interacted_element": [],
                        "screenshot_path": str(screenshot_path),
                    },
                    "metadata": {
                        "step_start_time": 100.0,
                        "step_end_time": 102.0,
                        "step_number": 1,
                        "step_interval": None,
                    },
                    "state_message": None,
                }
            ]
        }

    def model_dump(self, **_kwargs):
        return json.loads(json.dumps(self._dump))

    def is_successful(self):
        return True

    def is_validated(self):
        return True

    def judgement(self):
        return {"verdict": True, "reasoning": "Confirmation page was visible."}

    def final_result(self):
        return "Application submitted. Confirmation page was visible."

    def errors(self):
        return [None]


def _job(url="https://example.com/jobs/1"):
    return {
        "url": url,
        "title": "Senior C++ / Platform Engineer",
        "company": "A/B: Corp",
        "description": "Build systems",
    }


def test_uploaded_paths_include_normal_and_label_fallback_actions():
    history = {
        "history": [
            {
                "model_output": {
                    "action": [
                        {"upload_file": {"path": "/tmp/resume.pdf", "index": 4}},
                        {
                            "upload_file_by_label": {
                                "path": "/tmp/cover-letter.pdf",
                                "field_name": "Cover letter",
                            }
                        },
                    ]
                }
            }
        ]
    }

    assert ApplicationAttempt._uploaded_paths(history) == [
        "/tmp/resume.pdf",
        "/tmp/cover-letter.pdf",
    ]


def _write_test_video(path: Path) -> None:
    image = Image.new("RGB", (32, 32), color=(25, 80, 160))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    frame = base64.b64encode(buffer.getvalue()).decode()
    recorder = VideoRecorderService(
        output_path=path,
        size={"width": 32, "height": 32},
        framerate=10,
    )
    recorder.start()
    assert recorder._is_active is True
    recorder.add_frame(frame)
    recorder.add_frame(frame)
    recorder.stop_and_save()


def test_creates_secure_human_readable_dossier(tmp_path):
    metrics = FakeMetrics()
    started = datetime(2026, 7, 31, 11, 37, tzinfo=timezone.utc)

    attempt = ApplicationAttempt(
        _job(),
        {"markdown": "# Profile\nEmail: person@example.com", "country": "IT"},
        2,
        False,
        sensitive_values=["top-secret"],
        metrics_store=metrics,
        root=tmp_path,
        now=started,
        attempt_id="abc123",
    )

    assert attempt.directory.parent == tmp_path / "2026-07-31"
    assert attempt.directory.name == "113700Z_A_B_Corp_Senior_C_Platform_Engineer_abc123"
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    assert stat.S_IMODE(attempt.directory.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(attempt.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((attempt.directory / "manifest.json").stat().st_mode) == 0o600
    assert json.loads((attempt.directory / "manifest.json").read_text())["status"] == "preparing"
    assert "person@example.com" in (attempt.inputs_dir / "profile.md").read_text()
    assert metrics.rows[0][0] == "abc123"


def test_submission_checkpoint_is_durable_and_survives_optional_outreach_failure(tmp_path):
    attempt = ApplicationAttempt(
        _job(),
        {"markdown": "# Profile"},
        1,
        True,
        root=tmp_path,
        attempt_id="checkpointed",
    )
    assert attempt.save_submission_screenshot(b"submission-png") is True

    persisted = json.loads((attempt.directory / "manifest.json").read_text())
    assert persisted["submission_checkpoint_recorded_at"]
    assert persisted["artifacts"]["submission_confirmation"] == (
        "screenshots/submission_confirmation.png"
    )

    result = SimpleNamespace(
        history=[],
        usage=None,
        is_successful=lambda: False,
        is_validated=lambda: True,
        judgement=lambda: {"verdict": True},
        final_result=lambda: "Company message was unavailable",
        errors=lambda: [],
    )
    attempt.finish("applied", result=result)

    manifest = json.loads((attempt.directory / "manifest.json").read_text())
    assert manifest["agent_claimed_success"] is True
    assert manifest["judge_verdict"] is True
    assert manifest["submission_confirmed"] is True


def test_persists_history_materials_video_and_redacts_password(tmp_path):
    password = "top-secret"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    resume = source_dir / "resume.pdf"
    letter = source_dir / "letter.txt"
    letter_pdf = source_dir / "letter.pdf"
    supporting = source_dir / "degree.pdf"
    screenshot = source_dir / "step.png"
    resume.write_bytes(b"resume")
    letter.write_text("letter")
    letter_pdf.write_bytes(b"%PDF-letter")
    supporting.write_bytes(b"degree")
    screenshot.write_bytes(b"png-data")

    attempt = ApplicationAttempt(
        _job(),
        {"markdown": "# Profile", "country": "IT"},
        1,
        True,
        sensitive_values=[password],
        root=tmp_path / "attempts",
        attempt_id="attempt1",
    )
    attempt.save_inputs(
        task=f"Use {password}",
        context=f"Password is {password}",
        qa={"Question": f"Never store {password}"},
        memories=[],
        runtime_settings={"provider": "openai", "model": "test"},
    )
    attempt.save_materials(str(resume), str(letter), str(letter_pdf))
    attempt.save_generation_audit(
        {
            "positioning": "Lead with backend delivery.",
            "priorities": [],
            "selections": [],
        },
        {
            "positioning": "Keep the letter concise.",
            "decisions": [],
        },
    )
    attempt.save_outreach_message("Hi Hiring Team, I applied today for the role.")
    assert attempt.save_submission_screenshot(b"submission-png") is True
    attempt.manifest["linkedin_outreach"] = {
        "status": "sent",
        "recipient": "Anna",
        "cv_attached": True,
    }
    (attempt.conversation_dir / "conversation_1.txt").write_text(f"secret={password}")

    result = FakeResult(screenshot, supporting, password)
    attempt.save_history(result)
    _write_test_video(attempt.video_dir / "raw.mp4")
    assert attempt.finalize_recording() is True
    cost_breakdown = {
        "classification": 0.01,
        "cover_letter": 0.02,
        "outreach": 0.02,
        "cv": 0.03,
        "application_agent": 0.04,
        "judge": 0.05,
        "memory_extract": 0.06,
    }
    token_breakdown = {
        "cv": {
            "prompt_tokens": 100,
            "cached_prompt_tokens": 20,
            "cache_write_tokens": 0,
            "completion_tokens": 30,
            "visible_completion_tokens": 20,
            "reasoning_tokens": 10,
            "total_tokens": 130,
            "requests": 1,
            "models": {},
        }
    }
    attempt.finish(
        "applied",
        result=result,
        cost_breakdown=cost_breakdown,
        token_breakdown=token_breakdown,
    )

    all_text = "\n".join(
        path.read_text(errors="ignore")
        for path in attempt.directory.rglob("*")
        if path.is_file() and path.suffix in {".json", ".md", ".txt", ".log"}
    )
    assert password not in all_text
    assert "<redacted:password>" in all_text

    history = json.loads((attempt.directory / "history.json").read_text())
    assert history["history"][0]["state"]["screenshot_path"] == "screenshots/step_001.png"
    assert history["usage"]["total_cost"] == 0.125
    assert (attempt.screenshots_dir / "step_001.png").read_bytes() == b"png-data"
    assert (attempt.screenshots_dir / "final.png").read_bytes() == b"png-data"
    assert (attempt.screenshots_dir / "submission_confirmation.png").read_bytes() == b"submission-png"
    assert (attempt.inputs_dir / "linkedin_outreach.txt").read_text().startswith(
        "Hi Hiring Team,"
    )
    assert (attempt.materials_dir / "uploaded_degree.pdf").read_bytes() == b"degree"
    assert (attempt.materials_dir / "cover_letter.pdf").read_bytes() == b"%PDF-letter"
    assert (attempt.materials_dir / "cover_letter.txt").read_text() == "letter"
    assert (attempt.directory / "recording.mp4").stat().st_size > 0

    manifest = json.loads((attempt.directory / "manifest.json").read_text())
    assert manifest["status"] == "applied"
    assert manifest["agent_claimed_success"] is True
    assert manifest["judge_verdict"] is True
    assert manifest["submission_confirmed"] is True
    assert manifest["cost_usd"] == pytest.approx(0.23)
    assert manifest["cost_breakdown"] == cost_breakdown
    assert manifest["token_breakdown"] == token_breakdown
    assert manifest["confirmation_evidence"].startswith("Application submitted")
    assert manifest["provider"] == "openai"
    assert manifest["model"] == "test"
    assert manifest["artifacts"]["recording"]["sha256"]
    assert manifest["artifacts"]["cover_letter"]["path"] == "materials/cover_letter.pdf"
    assert manifest["artifacts"]["cover_letter_text"]["path"] == "materials/cover_letter.txt"
    assert manifest["artifacts"]["generation_audit"] == "materials/generation-audit.json"
    assert manifest["artifacts"]["submission_confirmation"] == "screenshots/submission_confirmation.png"
    assert manifest["artifacts"]["linkedin_outreach_message"] == "inputs/linkedin_outreach.txt"
    assert manifest["linkedin_outreach"]["status"] == "sent"
    assert manifest["files"]["recording.mp4"]["sha256"]
    assert manifest["files"]["summary.md"]["sha256"]
    assert "### Cost breakdown" in (attempt.directory / "summary.md").read_text()
    assert "### Token usage" in (attempt.directory / "summary.md").read_text()
    timeline = (attempt.directory / "timeline.md").read_text()
    assert "Step 1" in timeline
    assert "![Step 1 screenshot](screenshots/step_001.png)" in timeline
    assert "[step_001.png](screenshots/step_001.png)" not in timeline
    summary = (attempt.directory / "summary.md").read_text()
    assert "Submission confirmed: **True**" in summary
    assert "LinkedIn outreach: **sent**" in summary
    assert "### LinkedIn outreach" in summary
    assert "- Recipient: Anna" in summary
    assert "- CV attached: True" in summary
    assert "## Application overview" in summary
    assert "- Form loaded" in summary
    assert "- Document uploaded" in summary
    assert "### Issues encountered\n\n- No issues were recorded." in summary


def test_retries_get_distinct_attempts_with_same_job_key(tmp_path):
    first = ApplicationAttempt(
        _job(),
        {"markdown": ""},
        1,
        True,
        root=tmp_path,
        attempt_id="first",
    )
    second = ApplicationAttempt(
        _job(),
        {"markdown": ""},
        1,
        True,
        root=tmp_path,
        attempt_id="second",
    )

    assert first.directory != second.directory
    assert first.job_key == second.job_key


def test_summary_limits_highlights_to_two_sentences(tmp_path):
    attempt = ApplicationAttempt(
        _job(),
        {"markdown": ""},
        1,
        True,
        root=tmp_path,
        attempt_id="concise-summary",
    )
    (attempt.directory / "history.json").write_text(
        json.dumps(
            {
                "history": [
                    {
                        "model_output": {
                            "evaluation_previous_goal": "First highlight. Extra detail to omit."
                        },
                        "result": [
                            {"long_term_memory": "Second highlight. More detail to omit."},
                        ],
                    },
                    {
                        "model_output": {
                            "evaluation_previous_goal": "Third highlight should be omitted."
                        },
                        "result": [],
                    },
                ]
            }
        )
    )

    attempt._write_summary()

    summary = (attempt.directory / "summary.md").read_text()
    highlights = summary.split("### Highlights\n\n", 1)[1].split("\n\n### Issues encountered", 1)[0]
    assert highlights.splitlines() == ["- First highlight.", "- Second highlight."]


def test_finds_attempt_directory_by_exact_manifest_id(tmp_path):
    attempt = ApplicationAttempt(
        _job(),
        {"markdown": ""},
        1,
        True,
        root=tmp_path,
        attempt_id="attempt-123",
    )

    assert find_attempt_directory("attempt-123", root=tmp_path) == attempt.directory
    assert find_attempt_directory("../attempt-123", root=tmp_path) is None
    assert find_attempt_directory("missing", root=tmp_path) is None


def test_concurrent_attempts_never_share_a_directory(tmp_path):
    def create(_index):
        return ApplicationAttempt(
            _job(),
            {"markdown": ""},
            1,
            True,
            root=tmp_path,
        ).directory

    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(create, range(20)))

    assert len(paths) == len(set(paths))


def test_missing_video_is_recorded_without_failing_attempt(tmp_path):
    attempt = ApplicationAttempt(
        _job(),
        {"markdown": ""},
        1,
        True,
        root=tmp_path,
        attempt_id="no-video",
    )

    assert attempt.finalize_recording() is False
    attempt.finish("failed", error="browser failed")

    manifest = json.loads((attempt.directory / "manifest.json").read_text())
    assert manifest["artifacts"]["recording"] is None
    assert manifest["recording_error"] == "No finalized MP4 was produced"
    assert manifest["status"] == "failed"
    summary = (attempt.directory / "summary.md").read_text()
    assert "Submission confirmation: **not confirmed**" in summary
    assert "- browser failed" in summary
    assert "- No finalized MP4 was produced" in summary


def test_missing_step_screenshot_does_not_leave_a_temporary_path(tmp_path):
    missing = tmp_path / "already-deleted.png"
    attempt = ApplicationAttempt(
        _job(),
        {"markdown": ""},
        1,
        True,
        root=tmp_path / "attempts",
        attempt_id="missing-screenshot",
    )

    attempt.save_history(FakeResult(missing, tmp_path / "missing.pdf", "password"))

    history = json.loads((attempt.directory / "history.json").read_text())
    assert history["history"][0]["state"]["screenshot_path"] is None
    assert attempt.manifest["artifacts"]["screenshots"] == []
    assert "screenshot was missing" in (attempt.directory / "application.log").read_text()


def test_corrupt_video_is_preserved_and_reported(tmp_path):
    attempt = ApplicationAttempt(
        _job(),
        {"markdown": ""},
        1,
        True,
        root=tmp_path,
        attempt_id="corrupt-video",
    )
    (attempt.video_dir / "raw.mp4").write_bytes(b"not-an-mp4")

    assert attempt.finalize_recording() is False
    attempt.finish("failed", error="browser failed")

    manifest = json.loads((attempt.directory / "manifest.json").read_text())
    assert (attempt.directory / "recording.mp4").read_bytes() == b"not-an-mp4"
    assert manifest["recording_error"].startswith("MP4 could not be decoded:")
    assert manifest["files"]["recording.mp4"]["sha256"]


def _retention_dossier(
    root: Path,
    *,
    attempt_id: str,
    started_at: datetime,
    status: str = "applied",
    submission_confirmed: bool = True,
    agent_claimed_success: bool | None = True,
    judge_verdict: bool | None = True,
    error: str | None = None,
) -> Path:
    directory = root / started_at.strftime("%Y-%m-%d") / f"120000Z_Test_Role_{attempt_id}"
    (directory / "screenshots").mkdir(parents=True)
    (directory / ".video").mkdir()
    (directory / "recording.mp4").write_bytes(b"video" * 10)
    (directory / "screenshots" / "step.png").write_bytes(b"png" * 10)
    (directory / ".video" / "raw.webm").write_bytes(b"raw" * 10)
    (directory / "summary.md").write_text("permanent summary")
    manifest = {
        "attempt_id": attempt_id,
        "job_key": attempt_id,
        "job_title": "Test role",
        "company": "Test company",
        "status": status,
        "started_at": started_at.isoformat(),
        "finished_at": started_at.isoformat(),
        "agent_claimed_success": agent_claimed_success,
        "judge_verdict": judge_verdict,
        "submission_confirmed": submission_confirmed,
        "error": error,
        "artifacts": {
            "recording": "recording.mp4",
            "screenshots": ["screenshots/step.png"],
            "final_screenshot": "screenshots/step.png",
        },
        "files": {
            "recording.mp4": {"size_bytes": 50},
            "screenshots/step.png": {"size_bytes": 30},
            "summary.md": {"size_bytes": 17},
        },
    }
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return directory


def test_retention_preview_separates_video_and_full_dossier_cleanup(tmp_path):
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    video_old = _retention_dossier(
        tmp_path,
        attempt_id="video-old",
        started_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    _retention_dossier(
        tmp_path,
        attempt_id="safe-recent",
        started_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )
    _retention_dossier(
        tmp_path,
        attempt_id="dossier-old",
        started_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    active_old = _retention_dossier(
        tmp_path,
        attempt_id="active-old",
        started_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        status="running",
        submission_confirmed=False,
        agent_claimed_success=None,
        judge_verdict=None,
    )

    report = application_attempt_storage_report(
        root=tmp_path,
        video_retention_days=14,
        dossier_retention_days=30,
        now=now,
    )

    assert report["total_dossiers"] == 4
    assert report["video_cleanup_dossiers"] == 1
    assert report["video_cleanup_bytes"] > 0
    assert report["dossier_cleanup_dossiers"] == 1
    assert report["dossier_cleanup_bytes"] > report["video_cleanup_bytes"]
    assert report["reclaimable_dossiers"] == 2
    assert report["protected_incomplete_dossiers"] == 1
    assert {candidate["action"] for candidate in report["candidates"]} == {
        "remove_video",
        "delete_dossier",
    }
    assert video_old.is_dir()
    assert active_old.is_dir()


def test_retention_removes_only_video_then_deletes_completed_dossier(tmp_path):
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    video_old = _retention_dossier(
        tmp_path,
        attempt_id="video-old",
        started_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    dossier_old = _retention_dossier(
        tmp_path,
        attempt_id="dossier-old",
        started_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    ambiguous_old = _retention_dossier(
        tmp_path,
        attempt_id="unknown-old",
        started_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        status="failed",
        submission_confirmed=False,
        agent_claimed_success=None,
        judge_verdict=None,
        error="Application was interrupted; submission outcome is unknown",
    )
    active_old = _retention_dossier(
        tmp_path,
        attempt_id="active-old",
        started_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        status="running",
        submission_confirmed=False,
        agent_claimed_success=None,
        judge_verdict=None,
    )

    result = cleanup_application_attempts(
        root=tmp_path,
        video_retention_days=14,
        dossier_retention_days=30,
        now=now,
    )

    assert result["compacted_dossiers"] == 1
    assert result["deleted_dossiers"] == 2
    assert result["removed_bytes"] > 0
    assert (video_old / "summary.md").read_text() == "permanent summary"
    assert not (video_old / "recording.mp4").exists()
    assert not (video_old / ".video").exists()
    assert (video_old / "screenshots" / "step.png").is_file()
    video_manifest = json.loads((video_old / "manifest.json").read_text())
    assert video_manifest["artifacts"]["recording"] is None
    assert video_manifest["artifacts"]["screenshots"] == ["screenshots/step.png"]
    assert video_manifest["retention"]["removed_bytes"] > 0
    assert not dossier_old.exists()
    assert not ambiguous_old.exists()
    assert (active_old / "recording.mp4").is_file()
    assert (active_old / "screenshots" / "step.png").is_file()
