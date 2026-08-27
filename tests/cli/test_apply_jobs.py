import asyncio

import pytest
from types import SimpleNamespace

from cli import apply_jobs


class FakeCostTracker:
    def __init__(self):
        self.registered_categories = set()

    def register_llm(self, _category, llm):
        self.registered_categories.add(_category)
        return llm

    def add_response_usage(self, category, _model, usage):
        if usage is not None:
            self.registered_categories.add(category)

    async def breakdown(self, classification_cost=None):
        return {
            "classification": float(classification_cost or 0),
            "cover_letter": 0.03,
            "outreach": 0.01 if "outreach" in self.registered_categories else 0.0,
            "cv": 0.04,
            "application_agent": 0.25,
            "judge": 0.06,
            "memory_extract": 0.02 if "memory_extract" in self.registered_categories else 0.0,
        }

    async def token_breakdown(self):
        return {}


def test_browser_reporting_is_disabled_before_browser_use_import():
    assert apply_jobs.os.environ["ANONYMIZED_TELEMETRY"] == "false"
    assert apply_jobs.os.environ["BROWSER_USE_CLOUD_SYNC"] == "false"
    assert apply_jobs.os.environ["BROWSER_USE_VERSION_CHECK"] == "false"
    assert apply_jobs.os.environ["BH_TELEMETRY"] == "false"
    assert apply_jobs.os.environ["BROWSER_HARNESS_TELEMETRY"] == "false"


def test_application_result_progress_truncates_large_browser_payloads():
    extracted = "Found elements: " + ("x" * 2_000)
    agent = SimpleNamespace(
        history=SimpleNamespace(
            history=[
                SimpleNamespace(
                    result=[
                        SimpleNamespace(
                            error=None,
                            extracted_content=extracted,
                            long_term_memory=None,
                            attachments=[],
                        )
                    ]
                )
            ]
        )
    )

    assert apply_jobs._application_result_progress(agent) == [
        "✅ Result: "
        + extracted[: apply_jobs.APPLICATION_RESULT_PREVIEW_CHARS]
        + "… [truncated; 2,016 chars total]"
    ]


@pytest.mark.asyncio
async def test_prepares_tailored_cv_and_cover_letter(monkeypatch):
    calls = {}

    async def fake_tailor(url, description, options, title, **_kwargs):
        calls["tailor"] = (url, description, options, title)
        return {"path": "/tmp/tailored.pdf", "audit": {"positioning": "Backend fit"}}

    async def fake_cover(description, title, company, **_kwargs):
        calls["cover"] = (description, title, company)
        return "Dear team", {"positioning": "Concise letter"}

    updates = []
    monkeypatch.setattr(apply_jobs, "tailor_resume", fake_tailor)
    monkeypatch.setattr(apply_jobs, "generate_cover_letter_text", fake_cover)
    monkeypatch.setattr(
        apply_jobs,
        "save_cover_letter_text",
        lambda url, text, title: "/tmp/tailored.cover-letter.txt",
    )
    monkeypatch.setattr(
        apply_jobs,
        "save_cover_letter_pdf",
        lambda url, text, title: "/tmp/tailored.cover-letter.pdf",
    )
    monkeypatch.setattr(
        apply_jobs,
        "update_job",
        lambda url, **fields: updates.append((url, fields)),
    )

    materials = await apply_jobs.prepare_application_materials(
        {
            "url": "https://example.com/job",
            "title": "Backend Developer",
            "company": "Acme",
            "description": "Build APIs",
        },
        worker_id=1,
    )

    assert materials.resume_path == "/tmp/tailored.pdf"
    assert materials.cover_letter == "Dear team"
    assert materials.cover_letter_path == "/tmp/tailored.cover-letter.txt"
    assert materials.cover_letter_pdf_path == "/tmp/tailored.cover-letter.pdf"
    assert materials.resume_audit == {"positioning": "Backend fit"}
    assert materials.cover_letter_audit == {"positioning": "Concise letter"}
    assert materials.linkedin_outreach_message == ""
    assert calls["tailor"] == (
        "https://example.com/job",
        "Build APIs",
        apply_jobs.AUTO_TAILOR_OPTIONS,
        "Backend Developer",
    )
    assert calls["cover"] == ("Build APIs", "Backend Developer", "Acme")
    assert updates == [
        (
            "https://example.com/job",
            {"tailored_resume_path": "/tmp/tailored.pdf"},
        ),
        (
            "https://example.com/job",
            {
                "cover_letter": "Dear team",
                "cover_letter_path": "/tmp/tailored.cover-letter.txt",
                "cover_letter_pdf_path": "/tmp/tailored.cover-letter.pdf",
                "cover_letter_generation_error": None,
            },
        ),
    ]


@pytest.mark.asyncio
async def test_prepares_linkedin_outreach_before_browser_agent(monkeypatch):
    async def fake_tailor(*_args, **_kwargs):
        return {"path": "/tmp/tailored.pdf"}

    async def fake_cover(*_args, **_kwargs):
        return "Dear team", {"positioning": "Concise letter"}

    async def fake_outreach(description, title, company, job_url, **_kwargs):
        assert (description, title, company) == ("Build APIs", "Backend Developer", "Acme")
        assert job_url == "https://www.linkedin.com/jobs/view/123"
        return "Hi Hiring Team, I applied today for Backend Developer at Acme."

    updates = []
    monkeypatch.setattr(apply_jobs, "tailor_resume", fake_tailor)
    monkeypatch.setattr(apply_jobs, "generate_cover_letter_text", fake_cover)
    monkeypatch.setattr(apply_jobs, "generate_linkedin_outreach_text", fake_outreach)
    monkeypatch.setattr(apply_jobs, "save_cover_letter_text", lambda *_args: "/tmp/letter.txt")
    monkeypatch.setattr(apply_jobs, "save_cover_letter_pdf", lambda *_args: "/tmp/letter.pdf")
    monkeypatch.setattr(apply_jobs, "update_job", lambda url, **fields: updates.append((url, fields)))

    materials = await apply_jobs.prepare_application_materials(
        {
            "url": "https://www.linkedin.com/jobs/view/123",
            "title": "Backend Developer",
            "company": "Acme",
            "description": "Build APIs",
        },
        worker_id=1,
    )

    assert materials.linkedin_outreach_message.startswith("Hi Hiring Team,")
    assert updates[-1][1]["linkedin_outreach_message"] == materials.linkedin_outreach_message
    assert updates[-1][1]["linkedin_outreach_generation_error"] is None


@pytest.mark.asyncio
async def test_skips_linkedin_outreach_when_disabled(monkeypatch):
    async def fake_tailor(*_args, **_kwargs):
        return {"path": "/tmp/tailored.pdf"}

    async def fake_cover(*_args, **_kwargs):
        return "Dear team", {"positioning": "Concise letter"}

    async def unexpected_outreach(*_args, **_kwargs):
        raise AssertionError("Outreach generation should be skipped")

    monkeypatch.setattr(apply_jobs, "tailor_resume", fake_tailor)
    monkeypatch.setattr(apply_jobs, "generate_cover_letter_text", fake_cover)
    monkeypatch.setattr(apply_jobs, "generate_linkedin_outreach_text", unexpected_outreach)
    monkeypatch.setattr(apply_jobs, "save_cover_letter_text", lambda *_args: "/tmp/letter.txt")
    monkeypatch.setattr(apply_jobs, "save_cover_letter_pdf", lambda *_args: "/tmp/letter.pdf")
    monkeypatch.setattr(apply_jobs, "update_job", lambda *_args, **_kwargs: None)

    materials = await apply_jobs.prepare_application_materials(
        {
            "url": "https://www.linkedin.com/jobs/view/123",
            "title": "Backend Developer",
            "company": "Acme",
            "description": "Build APIs",
        },
        worker_id=1,
        enable_linkedin_outreach=False,
    )

    assert materials.linkedin_outreach_message == ""
    assert materials.linkedin_outreach_error is None


@pytest.mark.asyncio
async def test_optional_material_generation_failures_do_not_block_cv(monkeypatch):
    async def fake_tailor(*_args, **_kwargs):
        return {"path": "/tmp/tailored.pdf"}

    async def fail_cover(*_args, **_kwargs):
        raise RuntimeError("cover unavailable")

    async def fail_outreach(*_args, **_kwargs):
        raise RuntimeError("outreach unavailable")

    updates = []
    monkeypatch.setattr(apply_jobs, "tailor_resume", fake_tailor)
    monkeypatch.setattr(apply_jobs, "generate_cover_letter_text", fail_cover)
    monkeypatch.setattr(apply_jobs, "generate_linkedin_outreach_text", fail_outreach)
    monkeypatch.setattr(apply_jobs, "update_job", lambda url, **fields: updates.append((url, fields)))

    materials = await apply_jobs.prepare_application_materials(
        {
            "url": "https://www.linkedin.com/jobs/view/123",
            "title": "Backend Developer",
            "company": "Acme",
            "description": "Build APIs",
        },
        worker_id=1,
    )

    assert materials.resume_path == "/tmp/tailored.pdf"
    assert materials.cover_letter == ""
    assert materials.cover_letter_error == "cover unavailable"
    assert materials.linkedin_outreach_message == ""
    assert materials.linkedin_outreach_error == "outreach unavailable"
    assert updates[-2][1]["cover_letter"] is None
    assert updates[-1][1]["linkedin_outreach_message"] is None


def test_description_falls_back_to_job_metadata():
    description = apply_jobs._application_job_description(
        {
            "title": "Security Engineer",
            "company": "Acme",
            "location": "Berlin",
        }
    )

    assert "Job Title: Security Engineer" in description
    assert "Company: Acme" in description
    assert "Location: Berlin" in description


def test_judged_application_task_excludes_internal_tracking():
    task = apply_jobs._application_task("Apply to the job and verify submission.")

    assert "verify submission" in task
    assert "Do not submit without the tailored CV" in task
    assert "OPTIONAL COVER LETTER" in task
    assert "best-effort extra" in task
    assert "upload the cover-letter PDF" in task
    assert "call get_cover_letter and upload the returned PDF path" in task
    assert "call get_cover_letter_text" in task
    assert "'Further documents' or equivalent additional-documents field" in task
    assert "Do not upload either document into an unrelated field" in task
    assert "first use the normal upload_file action" in task
    assert "use upload_file_by_label once with the exact visible field label" in task
    assert "If only an optional cover-letter upload cannot be verified, continue" in task
    assert "@@JOB_APPLIED" not in task
    assert "@@QUESTION" not in task
    assert "internal logs or markers" in apply_jobs.APPLICATION_JUDGE_GROUND_TRUTH
    assert "must not cause failure" in apply_jobs.APPLICATION_JUDGE_GROUND_TRUTH


def test_application_task_includes_prepared_linkedin_outreach_after_submission():
    task = apply_jobs._application_task(
        "Apply first.",
        resume_path="/tmp/tailored.pdf",
        linkedin_job_url="https://www.linkedin.com/jobs/view/123",
        linkedin_outreach_message="Hi Hiring Team, I applied today for the role.",
    )

    assert task.index("APPLICATION SUCCESS CONTRACT") < task.index("POST-SUBMISSION LINKEDIN OUTREACH")
    assert "checkpoint_application_submission" in task
    assert "Never inspect or message anyone from `People you can reach out to`" in task
    assert "Open the company page through the company link on this exact listing" in task
    assert "use only an explicit company Message button" in task
    assert "choose Careers" in task
    assert "Do not explore More actions" in task
    assert "use `Send in a message`" in task
    assert "search for an individual recipient" in task
    assert "Hi Hiring Team, I applied today for the role." in task
    assert "try once to attach the tailored CV at /tmp/tailored.pdf" in task
    assert "send the prepared message without it" in task
    assert "Spend at most 12 steps" in task
    assert "mention the selected Careers topic in evidence" in task
    assert "still call done with success=true for the application" in task


def test_application_task_uses_custom_step_limit():
    task = apply_jobs._application_task("Retry the application.", max_steps=125)

    assert "maximum of 125 steps total" in task
    assert "maximum of 70 steps total" not in task


def test_application_outcome_requires_agent_and_judge_confirmation():
    result = SimpleNamespace(
        is_successful=lambda: True,
        is_validated=lambda: None,
        judgement=lambda: None,
        final_result=lambda: "Application may have submitted",
    )

    outcome = apply_jobs.application_outcome(
        result,
        attempt_id="attempt-1",
        agent_started=True,
    )

    assert outcome["submission_confirmed"] is False
    assert outcome["type"] == "unknown_outcome"
    assert outcome["retryable"] is False


def test_submission_checkpoint_preserves_success_after_optional_outreach_failure():
    result = SimpleNamespace(
        is_successful=lambda: False,
        is_validated=lambda: True,
        judgement=lambda: {"verdict": True},
        final_result=lambda: "Company messaging was unavailable",
    )

    outcome = apply_jobs.application_outcome(
        result,
        attempt_id="attempt-1",
        agent_started=True,
        submission_checkpointed=True,
    )

    assert outcome["submission_confirmed"] is True
    assert outcome["agent_claimed_success"] is True


def test_linkedin_outreach_sent_allows_missing_cv_when_delivery_is_confirmed():
    result = SimpleNamespace(
        final_result=lambda: (
            'Application submitted\n@@LINKEDIN_OUTREACH: {"status":"sent","recipient":"Anna",'
            '"profile_url":"https://www.linkedin.com/company/acme","cv_attached":false,'
            '"delivery_confirmed":true,'
            '"evidence":"Message visible with PDF","reason":null}'
        )
    )

    outcome = apply_jobs.linkedin_outreach_outcome(
        result,
        eligible=True,
        message_available=True,
    )

    assert outcome["status"] == "sent"
    assert outcome["recipient"] == "Anna"
    assert outcome["cv_attached"] is False
    assert outcome["delivery_confirmed"] is True


def test_linkedin_outreach_downgrades_unconfirmed_sent_claim():
    result = SimpleNamespace(
        final_result=lambda: (
            '@@LINKEDIN_OUTREACH: {"status":"sent","recipient":"Anna",'
            '"cv_attached":true,"delivery_confirmed":false}'
        )
    )

    outcome = apply_jobs.linkedin_outreach_outcome(
        result,
        eligible=True,
        message_available=True,
    )

    assert outcome["status"] == "unknown"
    assert "verify message delivery" in outcome["reason"]


def test_disabled_linkedin_outreach_is_not_applicable():
    outcome = apply_jobs.linkedin_outreach_outcome(
        None,
        eligible=True,
        message_available=False,
        enabled=False,
    )

    assert outcome["status"] == "not_applicable"
    assert "disabled" in outcome["reason"]


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ('@@BLOCKER: {"type":"authentication"}', "authentication"),
        ('@@BLOCKER: {"type":"anti_bot"}', "anti_bot"),
        ('@@BLOCKER: {"type":"linkedin_authentication"}', "linkedin_authentication"),
    ],
)
def test_application_outcome_reads_structured_blockers(marker, expected):
    result = SimpleNamespace(
        is_successful=lambda: False,
        is_validated=lambda: False,
        judgement=lambda: {"reached_captcha": False},
        final_result=lambda: marker,
    )

    outcome = apply_jobs.application_outcome(
        result,
        attempt_id="attempt-2",
        agent_started=True,
    )

    assert outcome["blocker"] == expected
    assert outcome["retryable"] is False


@pytest.mark.asyncio
async def test_autonomous_worker_does_not_requeue_credential_retry(monkeypatch):
    calls = []

    async def fake_apply(*_args, **_kwargs):
        calls.append("attempt")
        return "retry"

    monkeypatch.setattr(apply_jobs, "apply_to_job", fake_apply)
    queue = asyncio.Queue()
    queue.put_nowait({"url": "https://example.com/job", "status": "pending"})
    stats = {}

    await apply_jobs.worker(
        "W1",
        1,
        queue,
        profile={},
        qa={},
        applied_labels=[],
        easy_apply=True,
        stats=stats,
        requeue_retries=False,
    )

    assert calls == ["attempt"]
    assert stats == {"retry": 1}
    assert queue.empty()


@pytest.mark.asyncio
async def test_manual_worker_retries_pre_submission_failure_twice_then_notifies(monkeypatch):
    calls = []
    updates = []
    notifications = []
    job = {
        "url": "https://example.com/job",
        "title": "Developer",
        "company": "Acme",
        "status": "pending",
    }

    async def fake_apply(*_args, **_kwargs):
        calls.append("attempt")
        return "failed"

    monkeypatch.setattr(apply_jobs, "apply_to_job", fake_apply)
    monkeypatch.setattr(
        apply_jobs,
        "read_jobs",
        lambda: {
            job["url"]: {
                **job,
                "status": "failed",
                "last_application_outcome": {"type": "pre_submission_failure"},
            }
        },
    )
    monkeypatch.setattr(
        apply_jobs,
        "update_job",
        lambda url, **fields: updates.append((url, fields)),
    )
    queue = asyncio.Queue()
    queue.put_nowait(job)
    stats = {}

    await apply_jobs.worker(
        "W1",
        1,
        queue,
        profile={},
        qa={},
        applied_labels=[],
        easy_apply=True,
        stats=stats,
        pre_submission_attempt_limit=3,
        notify=lambda message: notifications.append(message) or False,
    )

    assert calls == ["attempt", "attempt", "attempt"]
    assert updates == [
        (job["url"], {"status": "pending", "error": None}),
        (job["url"], {"status": "pending", "error": None}),
    ]
    assert stats == {"failed": 1}
    assert len(notifications) == 1
    assert "manual application setup failed 3 times" in notifications[0]
    assert "Developer at Acme" in notifications[0]
    assert queue.empty()


@pytest.mark.asyncio
async def test_manual_worker_does_not_retry_when_stop_arrives_at_retry_boundary(
    monkeypatch,
):
    calls = []
    cancel_flag = {"cancel_requested": False}
    job = {
        "url": "https://example.com/job",
        "title": "Developer",
        "company": "Acme",
        "status": "pending",
    }

    async def fake_apply(*_args, **_kwargs):
        calls.append("attempt")
        return "failed"

    monkeypatch.setattr(apply_jobs, "apply_to_job", fake_apply)
    monkeypatch.setattr(
        apply_jobs,
        "read_jobs",
        lambda: {
            job["url"]: {
                **job,
                "status": "failed",
                "last_application_outcome": {"type": "pre_submission_failure"},
            }
        },
    )
    monkeypatch.setattr(apply_jobs, "update_job", lambda *_args, **_kwargs: None)
    queue = asyncio.Queue()
    queue.put_nowait(job)
    stats = {}

    def stop_when_retry_is_announced(message):
        if "retrying attempt" in message:
            cancel_flag["cancel_requested"] = True

    await apply_jobs.worker(
        "W1",
        1,
        queue,
        profile={},
        qa={},
        applied_labels=[],
        easy_apply=True,
        stats=stats,
        cancel_flag=cancel_flag,
        pre_submission_attempt_limit=3,
        progress=stop_when_retry_is_announced,
    )

    assert calls == ["attempt"]
    assert stats == {"cancelled": 1}
    assert queue.empty()


def test_user_documents_are_sorted_files_only(monkeypatch, tmp_path):
    (tmp_path / "Bachelor.pdf").write_bytes(b"pdf")
    (tmp_path / "Transcript.pdf").write_bytes(b"pdf")
    (tmp_path / "folder").mkdir()
    monkeypatch.setattr(apply_jobs, "USER_DOCS_DIR", tmp_path)

    assert apply_jobs._user_document_paths() == [
        str(tmp_path / "Bachelor.pdf"),
        str(tmp_path / "Transcript.pdf"),
    ]


@pytest.mark.asyncio
async def test_application_stops_when_material_preparation_fails(monkeypatch):
    async def fail_prepare(job, worker_id, **_kwargs):
        raise RuntimeError("tailoring failed")

    saved = []

    async def fake_save(url, status, error=None):
        saved.append((url, status, error))

    attempts = []

    class FakeAttempt:
        def __init__(self, *_args, **_kwargs):
            self.attempt_id = "attempt-test"
            self.started_at = __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            )
            self.manifest = {"artifacts": {}, "finished_at": None}
            self.finished = None
            attempts.append(self)

        def log(self, *_args, **_kwargs):
            pass

        def finalize_recording(self):
            pass

        def finish(self, status, error=None, result=None, **_kwargs):
            self.finished = (status, error, result)
            self.manifest["finished_at"] = self.started_at.isoformat()

    class FakeMetrics:
        def log(self, *_args, **_kwargs):
            pass

        def record_run(self, **_kwargs):
            pass

    monkeypatch.setattr(apply_jobs.config, "validate_job_url", lambda url: True)
    monkeypatch.setattr(apply_jobs, "claim_job", lambda url: True)
    monkeypatch.setattr(apply_jobs, "refresh_credentials", lambda: True)
    monkeypatch.setattr(apply_jobs, "prepare_application_materials", fail_prepare)
    monkeypatch.setattr(apply_jobs, "save_job_status", fake_save)
    monkeypatch.setattr(apply_jobs, "ApplicationAttempt", FakeAttempt)
    monkeypatch.setattr(apply_jobs, "MetricsStore", FakeMetrics)
    monkeypatch.setattr(apply_jobs, "CategorizedCostTracker", FakeCostTracker)

    status = await apply_jobs.apply_to_job(
        {
            "url": "https://example.com/job",
            "title": "Developer",
            "company": "Acme",
        },
        profile={},
        qa={},
        applied_labels=[],
        easy_apply=True,
        worker_id=1,
    )

    assert status == "failed"
    assert saved[0][1] == "failed"
    assert "Application material preparation failed" in saved[0][2]
    assert attempts[0].finished[0] == "failed"


@pytest.mark.asyncio
async def test_successful_application_connects_dossier_video_and_metrics(monkeypatch, tmp_path):
    events = []
    saved_statuses = []
    metrics_rows = []
    browser_kwargs = {}
    agent_kwargs = {}
    application_tools = object()
    cover_letters_for_tools = []
    job_updates = []
    job_url = "https://www.linkedin.com/jobs/view/123"
    live_progress = []

    class FakeAttempt:
        def __init__(self, *_args, **_kwargs):
            self.attempt_id = "attempt-success"
            self.started_at = __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            )
            self.directory = tmp_path / self.attempt_id
            self.conversation_dir = self.directory / "conversation"
            self.video_dir = self.directory / ".video"
            self.conversation_dir.mkdir(parents=True)
            self.video_dir.mkdir()
            self.manifest = {"artifacts": {}, "finished_at": None}
            self.finished = None

        def log(self, message, level="INFO"):
            events.append(("log", message, level))

        def log_step(self, *_args):
            events.append(("step",))

        def redact(self, value):
            return value

        def save_materials(self, resume, letter, letter_pdf):
            events.append(("materials", resume, letter, letter_pdf))
            self.manifest["artifacts"]["resume"] = {"path": "materials/resume.pdf"}

        def save_generation_audit(self, resume_audit, cover_letter_audit):
            events.append(("generation_audit", resume_audit, cover_letter_audit))

        def save_outreach_message(self, message):
            events.append(("outreach_message", message))

        def save_submission_screenshot(self, screenshot):
            events.append(("submission_checkpoint", screenshot))
            return True

        def save_inputs(self, **kwargs):
            events.append(("inputs", kwargs))

        def set_running(self):
            events.append(("running",))

        def save_final_screenshot(self, screenshot):
            events.append(("screenshot", screenshot))

        def save_history(self, result):
            events.append(("history", result))

        def finalize_recording(self):
            events.append(("video",))

        def finish(self, status, error=None, result=None, cost_breakdown=None):
            self.finished = (status, error, result)
            self.cost_breakdown = cost_breakdown
            self.manifest["finished_at"] = self.started_at.isoformat()

    class FakeMetrics:
        def log(self, *_args, **_kwargs):
            pass

        def record_run(self, **kwargs):
            metrics_rows.append(kwargs)

    class FakeMemory:
        def extract_domain(self, _url):
            return "example.com"

        def detect_ats_platform(self, _domain):
            return "test-ats"

        def get_critical_memories(self, _url, **_kwargs):
            return [{
                "id": 41,
                "category": "navigation",
                "content": "Use the external form's lower Apply button.",
            }]

    class FakeBrowser:
        def __init__(self, **kwargs):
            browser_kwargs.update(kwargs)

        async def take_screenshot(self, full_page=False):
            assert full_page is True
            return "cG5n"

        async def start(self):
            events.append(("browser_started",))

        async def close(self):
            events.append(("closed",))

    result = SimpleNamespace(
        history=[object(), object()],
        usage=SimpleNamespace(total_cost=0.42),
        is_successful=lambda: True,
        is_validated=lambda: True,
        judgement=lambda: {"verdict": True},
        final_result=lambda: (
            'Application submitted\n@@LINKEDIN_OUTREACH: {"status":"sent","recipient":"Anna",'
            '"profile_url":"https://www.linkedin.com/in/anna","cv_attached":true,'
            '"delivery_confirmed":true,'
            '"evidence":"Message and CV visible","reason":null}'
        ),
        errors=lambda: [None, None],
    )

    class FakeAction:
        def model_dump(self, **_kwargs):
            return {"click": {"index": 42, "label": "Submit"}}

    step_output = SimpleNamespace(
        evaluation_previous_goal="Success: the form is complete.",
        memory="The tailored resume is attached.",
        next_goal="Submit the application and verify confirmation.",
        action=[FakeAction()],
    )

    class FakeAgent:
        def __init__(self, **kwargs):
            agent_kwargs.update(kwargs)
            self.history = SimpleNamespace(
                history=[
                    SimpleNamespace(
                        result=[
                            SimpleNamespace(
                                error=None,
                                extracted_content="Clicked Submit",
                                long_term_memory=None,
                                attachments=["receipt.pdf"],
                            )
                        ]
                    )
                ]
            )

        async def run(self, max_steps, on_step_end=None):
            agent_kwargs["run_max_steps"] = max_steps
            agent_kwargs["register_new_step_callback"](None, step_output, 1)
            if on_step_end is not None:
                await on_step_end(self)
            return result

    class FakeLLM:
        def __init__(self):
            self.vision_cache_warmup_enabled = False

        def enable_vision_cache_warmup(self):
            self.vision_cache_warmup_enabled = True

    llms = [FakeLLM(), FakeLLM()]

    async def fake_save_status(url, status, error=None):
        saved_statuses.append((url, status, error))

    async def fake_prepare(_job, _worker_id, **_kwargs):
        return apply_jobs.PreparedApplicationMaterials(
            resume_path="/tmp/resume.pdf",
            cover_letter="Dear team",
            cover_letter_path="/tmp/letter.txt",
            cover_letter_pdf_path="/tmp/letter.pdf",
            linkedin_outreach_message="Hi Hiring Team, I applied today for the role.",
        )

    async def fake_linkedin_login(*_args, **_kwargs):
        events.append(("linkedin_login",))

    async def fake_rerank(store, scopes, **kwargs):
        events.append(("auto_rerank", store, scopes, kwargs))
        return {"selected": 2, "scopes": 1, "estimated_tokens": 80}

    extracted_memory = {
        "platform_url": "https://example.com/apply",
        "website_domain": "example.com",
        "ats_platform": "test-ats",
        "scope": "test-ats",
        "category": "element_interaction",
        "content": "Use the verified platform-specific control after the visible option fails.",
        "evidence_steps": [1, 2],
    }

    async def fake_extract(*_args, **kwargs):
        assert kwargs["api_key"] == "test-key"
        events.append(("memory_extract",))
        return SimpleNamespace(
            memories=[extracted_memory],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
        )

    def fake_store_extracted(store, memories, *, source_attempt_id):
        events.append(("memory_store", store, memories, source_attempt_id))
        return 1, 0, {"test-ats"}

    monkeypatch.setattr(apply_jobs.config, "validate_job_url", lambda _url: True)
    monkeypatch.setattr(apply_jobs.config, "get_llm", lambda: llms.pop(0))
    monkeypatch.setattr(apply_jobs.config, "browser_headless", lambda: True)
    monkeypatch.setattr(apply_jobs.config, "browser_user_agent", lambda: "test-user-agent")
    monkeypatch.setattr(
        apply_jobs.config,
        "launch_profile_browser",
        lambda *_args, **_kwargs: (None, None),
    )
    monkeypatch.setattr(apply_jobs.config, "stop_profile_browser", lambda _process: None)
    monkeypatch.setattr(apply_jobs, "claim_job", lambda _url: True)
    monkeypatch.setattr(apply_jobs, "refresh_credentials", lambda: True)
    monkeypatch.setattr(
        apply_jobs,
        "load_settings",
        lambda: {
            "sensitive_data": {},
            "blocked_domains": [],
            "memory_auto_rerank": True,
            "critical_memory_max_count": 7,
            "critical_memory_max_tokens": 900,
        },
    )
    monkeypatch.setattr(
        apply_jobs,
        "load_llm_settings",
        lambda: {"openai": {"api_key": "test-key"}},
    )
    monkeypatch.setattr(apply_jobs, "rerank_critical_scopes", fake_rerank)
    monkeypatch.setattr(apply_jobs, "extract_end_of_run_memories", fake_extract)
    monkeypatch.setattr(apply_jobs, "store_extracted_memories", fake_store_extracted)
    monkeypatch.setattr(apply_jobs, "prepare_application_materials", fake_prepare)
    monkeypatch.setattr(apply_jobs, "ensure_linkedin_login", fake_linkedin_login)
    monkeypatch.setattr(apply_jobs, "ApplicationAttempt", FakeAttempt)
    monkeypatch.setattr(apply_jobs, "MetricsStore", FakeMetrics)
    monkeypatch.setattr(apply_jobs, "CategorizedCostTracker", FakeCostTracker)
    monkeypatch.setattr(apply_jobs, "get_memory_store", lambda: FakeMemory())
    monkeypatch.setattr(apply_jobs, "build_memory_context", lambda *_args, **_kwargs: "context")
    monkeypatch.setattr(apply_jobs, "_user_document_paths", lambda: [])
    monkeypatch.setattr(apply_jobs, "_safe_runtime_settings", lambda *_args: {"provider": "test"})
    monkeypatch.setattr(apply_jobs, "BrowserSession", FakeBrowser)
    def fake_build_application_tools(cover_letter_pdf_path, cover_letter_text, checkpoint, **kwargs):
        cover_letters_for_tools.append((cover_letter_pdf_path, cover_letter_text))
        assert callable(checkpoint)
        assert kwargs["memory_store"].detect_ats_platform("example.com") == "test-ats"
        assert kwargs["critical_memory_max_count"] == 7
        assert kwargs["critical_memory_max_tokens"] == 900
        assert "attempt_id" not in kwargs
        assert "on_memory_changed" not in kwargs
        return application_tools

    monkeypatch.setattr(apply_jobs, "build_application_tools", fake_build_application_tools)
    monkeypatch.setattr(apply_jobs, "Agent", FakeAgent)
    monkeypatch.setattr(apply_jobs, "_agent_log_start", lambda *_args: None)
    monkeypatch.setattr(apply_jobs, "_agent_on_done", lambda *_args: None)
    monkeypatch.setattr(apply_jobs, "_agent_on_step", lambda *_args: None)
    def unexpected_qa_call(*_args, **_kwargs):
        raise AssertionError("Q&A learning must remain disabled")

    monkeypatch.setattr(apply_jobs, "extract_from_history", unexpected_qa_call)
    monkeypatch.setattr(apply_jobs, "save_job_status", fake_save_status)
    monkeypatch.setattr(apply_jobs, "update_job", lambda url, **fields: job_updates.append((url, fields)))

    status = await apply_jobs.apply_to_job(
        {
            "url": job_url,
            "title": "Developer",
            "company": "Acme",
            "classification": {"cost_usd": 0.02},
        },
        profile={"markdown": "# Profile"},
        qa={},
        applied_labels=[],
        easy_apply=True,
        worker_id=1,
        max_steps=125,
        progress=live_progress.append,
    )

    assert status == "applied"
    assert browser_kwargs["record_video_framerate"] == 10
    assert browser_kwargs["record_video_dir"].endswith(".video")
    assert browser_kwargs["user_agent"] == "test-user-agent"
    assert browser_kwargs["captcha_solver"] is False
    assert agent_kwargs["save_conversation_path"].endswith("conversation")
    assert agent_kwargs["tools"] is application_tools
    assert agent_kwargs["initial_actions"] == [
        {"navigate": {"url": job_url, "new_tab": False}}
    ]
    assert agent_kwargs["available_file_paths"] == [
        "/tmp/resume.pdf",
        "/tmp/letter.pdf",
    ]
    assert cover_letters_for_tools == [("/tmp/letter.pdf", "Dear team")]
    assert agent_kwargs["judge_llm"] is not agent_kwargs["llm"]
    assert agent_kwargs["llm"].vision_cache_warmup_enabled is True
    assert agent_kwargs["judge_llm"].vision_cache_warmup_enabled is False
    assert agent_kwargs["run_max_steps"] == 125
    assert live_progress == [
        "📍 Step 1:",
        "👍 Eval: Success: the form is complete.",
        "🧠 Memory: The tailored resume is attached.",
        "🎯 Next goal: Submit the application and verify confirmation.",
        "▶️ click: index=42, label=Submit",
        "✅ Result: Clicked Submit",
        "📎 Files: receipt.pdf",
        "🧠 Extracted 1 verified platform memories (1 new, 0 reinforced)",
        "🧠 Auto-ranked 2 critical memories across 1 changed platform scopes",
    ]
    inputs = next(event[1] for event in events if event[0] == "inputs")
    assert inputs["memories"][0]["id"] == 41
    assert "maximum of 125 steps total" in inputs["task"]
    assert "POST-SUBMISSION LINKEDIN OUTREACH" in inputs["task"]
    assert "Hi Hiring Team, I applied today for the role." in inputs["task"]
    assert ("outreach_message", "Hi Hiring Team, I applied today for the role.") in events
    assert ("history", result) in events
    assert ("video",) in events
    assert saved_statuses[-1][1] == "applied"
    assert metrics_rows[0]["run_id"] == "attempt-success"
    assert metrics_rows[0]["job_url"] == job_url
    assert metrics_rows[0]["cost_usd"] == 0.42
    assert metrics_rows[0]["cost_breakdown"]["judge"] == 0.06
    assert metrics_rows[0]["cost_breakdown"]["memory_extract"] == 0.02
    assert metrics_rows[0]["memories_injected"] == 1
    assert metrics_rows[0]["memories_extracted"] == 1
    assert ("memory_extract",) in events
    memory_store = next(event for event in events if event[0] == "memory_store")
    assert memory_store[3] == "attempt-success"
    auto_rerank = next(event for event in events if event[0] == "auto_rerank")
    assert auto_rerank[2] == {"test-ats"}
    assert auto_rerank[3]["max_count"] == 7
    assert auto_rerank[3]["max_tokens"] == 900
    assert job_updates[-1][1]["last_linkedin_outreach"]["status"] == "sent"
    assert job_updates[-1][1]["last_linkedin_outreach"]["cv_attached"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_error", "expected_attempt_status", "expected_job_status", "expected_return"),
    [
        (RuntimeError("security token expired"), "retry", "pending", "retry"),
        (RuntimeError("browser crashed"), "failed", "failed", "failed"),
        (asyncio.CancelledError("Stopped by user"), "cancelled", None, None),
    ],
)
async def test_failed_agent_records_terminal_attempt(
    monkeypatch,
    tmp_path,
    agent_error,
    expected_attempt_status,
    expected_job_status,
    expected_return,
):
    finished = []
    saved_statuses = []
    saved_histories = []

    class FakeAttempt:
        def __init__(self, *_args, **_kwargs):
            self.attempt_id = "attempt-retry"
            self.started_at = __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            )
            self.conversation_dir = tmp_path / "conversation"
            self.video_dir = tmp_path / "video"
            self.conversation_dir.mkdir()
            self.video_dir.mkdir()
            self.manifest = {"artifacts": {}, "finished_at": None}

        def log(self, *_args, **_kwargs):
            pass

        def save_materials(self, *_args):
            self.manifest["artifacts"]["resume"] = {"path": "resume.pdf"}

        def save_generation_audit(self, *_args):
            pass

        def save_outreach_message(self, _message):
            pass

        def save_submission_screenshot(self, _screenshot):
            return True

        def save_inputs(self, **_kwargs):
            pass

        def set_running(self):
            pass

        def save_final_screenshot(self, _screenshot):
            self.manifest["artifacts"]["final_screenshot"] = "screenshots/final.png"

        def save_history(self, result):
            saved_histories.append(result)

        def finalize_recording(self):
            pass

        def finish(self, status, error=None, result=None, **_kwargs):
            finished.append((status, error, result))
            self.manifest["finished_at"] = self.started_at.isoformat()

    class FakeMetrics:
        def log(self, *_args, **_kwargs):
            pass

        def record_run(self, **_kwargs):
            pass

    class FakeMemory:
        def extract_domain(self, _url):
            return "example.com"

        def detect_ats_platform(self, _domain):
            return None

        def get_critical_memories(self, _url, **_kwargs):
            return []

    class FakeBrowser:
        def __init__(self, **_kwargs):
            pass

        async def close(self):
            pass

        async def take_screenshot(self, full_page=False):
            assert full_page is True
            return b"png"

    partial_history = SimpleNamespace(
        history=[object()],
        usage=None,
        is_successful=lambda: None,
        is_validated=lambda: None,
        judgement=lambda: None,
        errors=lambda: ["browser crashed"],
    )

    class FailingAgent:
        def __init__(self, **_kwargs):
            self.history = partial_history if str(agent_error) == "browser crashed" else None

        async def run(self, max_steps):
            assert max_steps == apply_jobs.DEFAULT_MAX_STEPS
            raise agent_error

    async def fake_save_status(url, status, error=None):
        saved_statuses.append((url, status, error))

    async def fake_prepare(_job, _worker_id, **_kwargs):
        return apply_jobs.PreparedApplicationMaterials(
            resume_path="/tmp/resume.pdf",
            cover_letter="Dear team",
            cover_letter_path="/tmp/letter.txt",
            cover_letter_pdf_path="/tmp/letter.pdf",
        )

    monkeypatch.setattr(apply_jobs.config, "validate_job_url", lambda _url: True)
    monkeypatch.setattr(apply_jobs.config, "get_llm", lambda: object())
    monkeypatch.setattr(apply_jobs.config, "browser_headless", lambda: True)
    monkeypatch.setattr(apply_jobs.config, "browser_user_agent", lambda: "test-user-agent")
    monkeypatch.setattr(
        apply_jobs.config,
        "launch_profile_browser",
        lambda *_args, **_kwargs: (None, None),
    )
    monkeypatch.setattr(apply_jobs.config, "stop_profile_browser", lambda _process: None)
    monkeypatch.setattr(apply_jobs, "claim_job", lambda _url: True)
    monkeypatch.setattr(apply_jobs, "refresh_credentials", lambda: True)
    monkeypatch.setattr(apply_jobs, "prepare_application_materials", fake_prepare)
    monkeypatch.setattr(apply_jobs, "ApplicationAttempt", FakeAttempt)
    monkeypatch.setattr(apply_jobs, "MetricsStore", FakeMetrics)
    monkeypatch.setattr(apply_jobs, "CategorizedCostTracker", FakeCostTracker)
    monkeypatch.setattr(apply_jobs, "get_memory_store", lambda: FakeMemory())
    monkeypatch.setattr(apply_jobs, "build_memory_context", lambda *_args, **_kwargs: "context")
    monkeypatch.setattr(apply_jobs, "_user_document_paths", lambda: [])
    monkeypatch.setattr(apply_jobs, "_safe_runtime_settings", lambda *_args: {})
    monkeypatch.setattr(apply_jobs, "BrowserSession", FakeBrowser)
    monkeypatch.setattr(apply_jobs, "Agent", FailingAgent)
    monkeypatch.setattr(apply_jobs, "_agent_log_start", lambda *_args: None)
    monkeypatch.setattr(apply_jobs, "save_job_status", fake_save_status)

    call = apply_jobs.apply_to_job(
        {"url": "https://example.com/job", "title": "Developer", "company": "Acme"},
        profile={"markdown": "# Profile"},
        qa={},
        applied_labels=[],
        easy_apply=True,
        worker_id=1,
    )
    if isinstance(agent_error, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await call
    else:
        assert await call == expected_return

    assert finished[0][0] == expected_attempt_status
    if isinstance(agent_error, asyncio.CancelledError):
        assert "outcome is unknown" in finished[0][1]
    else:
        assert agent_error.args[0] in finished[0][1]
    if expected_job_status:
        assert saved_statuses[-1][1] == expected_job_status
    else:
        assert saved_statuses == []
    if str(agent_error) == "browser crashed":
        assert saved_histories == [partial_history]
