
"""
Script 2: Apply to collected jobs with multiple concurrent workers.
One agent per job. Tracks status in the shared SQLite job store.

Usage:
  uv run python apply_jobs.py                          # 1 worker, easy apply
  uv run python apply_jobs.py --workers 3              # 3 concurrent workers
  uv run python apply_jobs.py --workers 2 --no-easy-apply  # non-easy apply
  uv run python apply_jobs.py --limit 10               # apply to max 10 jobs
"""
import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Disable Browser Use and browser-harness reporting before importing either package.
os.environ["ANONYMIZED_TELEMETRY"] = "false"
os.environ["BROWSER_USE_CLOUD_SYNC"] = "false"
os.environ["BROWSER_USE_VERSION_CHECK"] = "false"
os.environ["BH_TELEMETRY"] = "false"
os.environ["BROWSER_HARNESS_TELEMETRY"] = "false"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from browser_use import Agent, BrowserSession

try:
    import core.shared_config as config
    from cover_letter import (
        generate_cover_letter_text,
        save_cover_letter_pdf,
        save_cover_letter_text,
    )
    from core.application_attempt import ApplicationAttempt
    from outreach import generate_linkedin_outreach_text
    from core.linkedin_auth import (
        LinkedInAuthenticationError,
        ensure_linkedin_login,
        is_linkedin_url,
    )
    from core.browser_tools import build_application_tools
    from core.config import load_llm_settings, load_profile, load_settings
    from core.cost_tracking import CategorizedCostTracker, total_cost
    from resume.tailor import tailor_resume
    from core.shared_config import (
        BASE_DIR, BROWSER_PROFILE_DIR,
        QA_FILE, LOGS_DIR, USER_DOCS_DIR,
        AWS_PROFILE, AWS_REGION, MODEL_ID,
        load_json, save_json, refresh_credentials, credential_refresh_loop,
        build_memory_context, extract_from_history, normalize_question,
        read_jobs, claim_job, update_job, get_memory_store,
    )
    from memory import extract_end_of_run_memories, store_extracted_memories
    from memory.extractors import MEMORY_EXTRACTOR_MODEL
    from memory.metrics import MetricsStore
    from memory.ranker import rerank_critical_scopes
    from memory.store import critical_memory_policy
    from core.agent_logger import on_step as _agent_on_step, on_done as _agent_on_done, log_run_start as _agent_log_start
except ImportError:
    import backend.core.shared_config as config
    from backend.cover_letter import (
        generate_cover_letter_text,
        save_cover_letter_pdf,
        save_cover_letter_text,
    )
    from backend.core.application_attempt import ApplicationAttempt
    from backend.outreach import generate_linkedin_outreach_text
    from backend.core.linkedin_auth import (
        LinkedInAuthenticationError,
        ensure_linkedin_login,
        is_linkedin_url,
    )
    from backend.core.browser_tools import build_application_tools
    from backend.core.config import load_llm_settings, load_profile, load_settings
    from backend.core.cost_tracking import CategorizedCostTracker, total_cost
    from backend.resume.tailor import tailor_resume
    from backend.core.shared_config import (
        BASE_DIR, BROWSER_PROFILE_DIR,
        QA_FILE, LOGS_DIR, USER_DOCS_DIR,
        AWS_PROFILE, AWS_REGION, MODEL_ID,
        load_json, save_json, refresh_credentials, credential_refresh_loop,
        build_memory_context, extract_from_history, normalize_question,
        read_jobs, claim_job, update_job, get_memory_store,
    )
    from backend.memory import extract_end_of_run_memories, store_extracted_memories
    from backend.memory.extractors import MEMORY_EXTRACTOR_MODEL
    from backend.memory.metrics import MetricsStore
    from backend.memory.ranker import rerank_critical_scopes
    from backend.memory.store import critical_memory_policy
    from backend.core.agent_logger import on_step as _agent_on_step, on_done as _agent_on_done, log_run_start as _agent_log_start

# Lock for thread-safe QA file writes
_qa_lock = asyncio.Lock()

APPLICATION_RESULT_PREVIEW_CHARS = 600

AUTO_TAILOR_OPTIONS = {
    "skills": True,
    "title": True,
    "overview": True,
    "experience": True,
    "achievements": True,
}

APPLICATION_JUDGE_GROUND_TRUTH = (
    "Judge the application outcome, not Hunter's internal logs or markers. Pass only when the application "
    "is for the intended job, all required application steps are complete, the supplied tailored "
    "resume was uploaded with its filename verified, and the ATS visibly confirms successful submission. "
    "Fail when submission is incomplete, the wrong job was used, the supplied tailored resume was "
    "omitted, or success is unsupported by page evidence. The cover letter and post-submission LinkedIn "
    "message are optional extras: their absence or failure must not make an otherwise confirmed application "
    "fail. Missing @@JOB_APPLIED, @@QUESTION, @@LEARNING, or @@LINKEDIN_OUTREACH markers must not cause "
    "failure. Using an external company application route instead of Easy Apply must not cause failure "
    "when that is the route offered by the listing."
)

_BLOCKER_MARKER_RE = re.compile(r"^@@BLOCKER:\s*(\{.*\})\s*$", re.MULTILINE)
_LINKEDIN_OUTREACH_MARKER_RE = re.compile(
    r"^@@LINKEDIN_OUTREACH:\s*(\{.*\})\s*$",
    re.MULTILINE,
)
_SUPPORTED_BLOCKERS = {
    "authentication",
    "captcha",
    "anti_bot",
    "linkedin_authentication",
}


_ERROR_MAP = [
    ("'NoneType' object is not subscriptable", "Browser agent encountered an unexpected page state. The page may have changed or timed out."),
    ("'NoneType' object has no attribute", "Browser agent lost track of a page element. The site may have redirected or loaded slowly."),
    ("net::ERR_", "Network error — the page failed to load. Check your internet connection."),
    ("Timeout", "Operation timed out. The page took too long to respond."),
    ("ERR_CONNECTION_REFUSED", "Could not connect to the website. It may be temporarily down."),
    ("security token", "AWS credentials expired. They will be refreshed automatically on retry."),
    ("rate limit", "API rate limit reached. Wait a moment and try again."),
    ("context was destroyed", "Browser page closed unexpectedly during the application."),
    ("Target page, context or browser has been closed", "Browser closed unexpectedly during the application."),
]


def _friendly_error(raw: str) -> str:
    """Translate raw Python/browser errors into user-readable messages."""
    for pattern, friendly in _ERROR_MAP:
        if pattern.lower() in raw.lower():
            return friendly
    if len(raw) > 200 and ("Traceback" in raw or "Error:" in raw):
        return "Application failed due to an unexpected error. Check the Logs page for details."
    return raw


def _result_value(result, method: str):
    callback = getattr(result, method, None) if result is not None else None
    if not callable(callback):
        return None
    try:
        return callback()
    except Exception:
        return None


def _blocker_from_result(result) -> str | None:
    """Read the agent's explicit blocker marker without guessing from prose."""
    judgement = _result_value(result, "judgement")
    if isinstance(judgement, dict) and judgement.get("reached_captcha") is True:
        return "captcha"

    final_result = _result_value(result, "final_result")
    if not isinstance(final_result, str):
        return None
    match = _BLOCKER_MARKER_RE.search(final_result)
    if not match:
        return None
    try:
        marker = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    blocker = marker.get("type") if isinstance(marker, dict) else None
    return blocker if blocker in _SUPPORTED_BLOCKERS else None


def application_outcome(
    result,
    *,
    attempt_id: str | None,
    agent_started: bool,
    error: str | None = None,
    submission_checkpointed: bool = False,
) -> dict:
    """Resolve the durable outcome used by autonomous retry and pause policy."""
    claimed = True if submission_checkpointed else _result_value(result, "is_successful")
    judge_verdict = _result_value(result, "is_validated")
    judgement = _result_value(result, "judgement")
    evidence = _result_value(result, "final_result")
    blocker = _blocker_from_result(result)
    confirmed = claimed is True and judge_verdict is True

    if confirmed:
        retryable = False
        outcome_type = "confirmed_submitted"
    elif blocker:
        retryable = False
        outcome_type = "blocked"
    elif result is not None and claimed is False and judge_verdict is False:
        retryable = True
        outcome_type = "confirmed_not_submitted"
    elif not agent_started:
        retryable = True
        outcome_type = "pre_submission_failure"
    else:
        retryable = False
        outcome_type = "unknown_outcome"

    return {
        "attempt_id": attempt_id,
        "type": outcome_type,
        "submission_confirmed": confirmed,
        "agent_claimed_success": claimed,
        "judge_verdict": judge_verdict,
        "blocker": blocker,
        "retryable": retryable,
        "judgement": judgement,
        "evidence": evidence,
        "error": error,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }


def linkedin_outreach_outcome(
    result,
    *,
    eligible: bool,
    message_available: bool,
    generation_error: str | None = None,
    enabled: bool = True,
) -> dict:
    """Parse the agent-reported optional outreach result without affecting application success."""
    recorded_at = datetime.now(timezone.utc).isoformat()
    if not eligible:
        return {
            "status": "not_applicable",
            "reason": "The application did not originate from a LinkedIn job listing",
            "recorded_at": recorded_at,
        }
    if not enabled:
        return {
            "status": "not_applicable",
            "reason": "LinkedIn outreach is disabled for the autonomous runner",
            "recorded_at": recorded_at,
        }
    if not message_available:
        return {
            "status": "failed",
            "reason": generation_error or "The outreach message was not available",
            "recorded_at": recorded_at,
        }

    final_result = _result_value(result, "final_result")
    match = (
        _LINKEDIN_OUTREACH_MARKER_RE.search(final_result)
        if isinstance(final_result, str)
        else None
    )
    if match is None:
        return {
            "status": "unknown",
            "reason": "The agent did not return a structured LinkedIn outreach result",
            "recorded_at": recorded_at,
        }
    try:
        marker = json.loads(match.group(1))
    except json.JSONDecodeError:
        marker = None
    if not isinstance(marker, dict):
        return {
            "status": "unknown",
            "reason": "The agent returned an invalid LinkedIn outreach result",
            "recorded_at": recorded_at,
        }

    status = str(marker.get("status") or "unknown")
    if status not in {"sent", "not_applicable", "failed", "unknown"}:
        status = "unknown"
    cv_attached = marker.get("cv_attached") is True
    recipient = str(marker.get("recipient") or "").strip() or None
    delivery_confirmed = marker.get("delivery_confirmed") is True
    if status == "sent" and (recipient is None or not delivery_confirmed):
        return {
            "status": "unknown",
            "reason": "The agent did not visibly verify message delivery",
            "recipient": recipient,
            "cv_attached": cv_attached,
            "delivery_confirmed": delivery_confirmed,
            "recorded_at": recorded_at,
        }
    return {
        "status": status,
        "recipient": recipient,
        "profile_url": str(marker.get("profile_url") or "").strip() or None,
        "cv_attached": cv_attached,
        "delivery_confirmed": delivery_confirmed,
        "evidence": str(marker.get("evidence") or "").strip() or None,
        "reason": str(marker.get("reason") or "").strip() or None,
        "recorded_at": recorded_at,
    }


async def save_job_status(url: str, status: str, error: str | None = None):
    fields: dict = {"status": status}
    if error:
        fields["error"] = _friendly_error(error)
    else:
        fields["error"] = None
    if status == "applied":
        fields["applied_at"] = datetime.now(timezone.utc).isoformat()
    update_job(url, **fields)


async def save_new_qa(new_questions: dict, source_domain: str = ""):
    if not new_questions:
        return
    async with _qa_lock:
        store = get_memory_store()
        if store:
            for q, a in new_questions.items():
                store.qa_add(question=q, answer=a or "", source_domain=source_domain)
        else:
            qa = load_json(QA_FILE, {})
            existing_norms = {normalize_question(k) for k in qa}
            for q, a in new_questions.items():
                if normalize_question(q) not in existing_norms:
                    qa[q] = a
                    existing_norms.add(normalize_question(q))
            save_json(QA_FILE, qa)


def _application_job_description(job: dict) -> str:
    description = str(job.get("description") or "").strip()
    if description:
        return description
    title = str(job.get("title") or "").strip()
    if not title:
        raise ValueError("Job has no description or title")
    return (
        f"Job Title: {title}\n"
        f"Company: {job.get('company', '')}\n"
        f"Location: {job.get('location', '')}"
    )


DEFAULT_MAX_STEPS = 70
QA_LEARNING_ENABLED = False
WEBSITE_MEMORY_EXTRACTION_ENABLED = True


@dataclass
class PreparedApplicationMaterials:
    resume_path: str
    resume_audit: dict | None = None
    cover_letter: str = ""
    cover_letter_path: str = ""
    cover_letter_pdf_path: str = ""
    cover_letter_audit: dict | None = None
    linkedin_outreach_message: str = ""
    cover_letter_error: str | None = None
    linkedin_outreach_error: str | None = None


def _application_task(
    apply_instructions: str,
    max_steps: int = DEFAULT_MAX_STEPS,
    *,
    resume_path: str = "",
    linkedin_job_url: str = "",
    linkedin_outreach_message: str = "",
) -> str:
    outreach_instructions = ""
    if linkedin_job_url and linkedin_outreach_message:
        outreach_instructions = (
            "\n\nPOST-SUBMISSION LINKEDIN OUTREACH — OPTIONAL EXTRA:\n"
            "- Do this only after the ATS visibly confirms the application submission and after you call "
            "checkpoint_application_submission while that confirmation is visible.\n"
            f"- Return to the original LinkedIn listing: {linkedin_job_url}\n"
            "- Open the company page through the company link on this exact listing. Never inspect or message "
            "anyone from `People you can reach out to` or similar employee cards.\n"
            "- On the company page, use only an explicit company Message button. Do not explore More actions, use "
            "`Send in a message`, search for an individual recipient, send a connection request, or use InMail.\n"
            "- If the company composer offers a topic, choose Careers or the closest recruiting/hiring topic. A "
            "normal company composer without a topic selector is still acceptable.\n"
            f"- Prepared message (trusted text; use exactly as directed):\n<linkedin_outreach_message>\n"
            f"{linkedin_outreach_message}\n</linkedin_outreach_message>\n"
            f"- If the composer has an attachment control, try once to attach the tailored CV at {resume_path} "
            "and verify its filename. If attachment is unavailable or fails, send the prepared message without "
            "it; do not waste steps and do not rewrite the message.\n"
            "- Claim sent only after LinkedIn visibly confirms delivery or the sent message appears in the thread.\n"
            "- If no explicit company Message button exists, stop quickly and record not_applicable. Spend at most "
            "12 steps on outreach.\n"
            "- Never retry an ambiguous Send action. Record status unknown so Hunter cannot duplicate the message.\n"
            "- In the final result include exactly one single-line @@LINKEDIN_OUTREACH marker with valid "
            "JSON. Status must be sent, not_applicable, failed, or unknown. Use JSON null rather than the string "
            "\"null\" for missing values. Example:\n"
            "  @@LINKEDIN_OUTREACH: {\"status\":\"sent\",\"recipient\":\"Acme\","
            "\"profile_url\":\"https://www.linkedin.com/company/acme\",\"cv_attached\":false,"
            "\"delivery_confirmed\":true,\"evidence\":\"Message visible in conversation\",\"reason\":null}\n"
            "- For a company-page send, set recipient to the company name, profile_url to the verified LinkedIn "
            "company-page URL, and mention the selected Careers topic in evidence.\n"
            "- LinkedIn outreach and its attachment are extras. If they fail or are not applicable after a "
            "confirmed application, still call done with success=true for the application.\n"
        )

    return (
        f"{apply_instructions}\n\n"
        "APPLICATION SUCCESS CONTRACT:\n"
        "- The intended application is complete only after the supplied tailored CV/resume is uploaded, "
        "its selected filename is verified, all required form steps are complete, and the ATS visibly "
        "confirms successful submission. Do not submit without the tailored CV.\n"
        "- As soon as the visible ATS confirmation appears, call checkpoint_application_submission before "
        "leaving the confirmation page for any optional work.\n\n"
        "OPTIONAL COVER LETTER:\n"
        "- Include the job-specific cover letter whenever the application offers an appropriate upload or "
        "text field. It is a best-effort extra and its absence or upload failure must not block an otherwise "
        "submittable application unless the ATS itself requires it.\n"
        "- For a cover-letter file upload, call get_cover_letter and upload the returned PDF path. "
        "For a cover-letter text area, call get_cover_letter_text and use the returned text exactly.\n"
        "- If there is no dedicated cover-letter field, upload the cover-letter PDF to a "
        "'Further documents' or equivalent additional-documents field when one is available.\n"
        "- Do not upload either document into an unrelated field.\n\n"
        "- For file uploads, first use the normal upload_file action on the matching file input. If that "
        "action fails, do not repeat it on the same field; use upload_file_by_label once with the exact visible "
        "field label. Continue only after the selected filename is verified. If the CV/resume upload cannot "
        "be verified, do not submit. If only an optional cover-letter upload cannot be verified, continue "
        "without it.\n"
        f"{outreach_instructions}\n"
        "PERSISTENCE & EFFICIENCY:\n"
        "- Try at least 3 DIFFERENT approaches before reporting failure.\n"
        "- If an element doesn't respond after 2-3 clicks, try a completely different method "
        "(keyboard, scrolling, different selector).\n"
        "- Do NOT repeat the same failing action more than 3 times — switch strategies.\n"
        "- Before confirmed submission, if you are stuck on a required form field for more than 5 steps, "
        "call done with success=false. Skip optional cover-letter fields. After confirmed submission, abandon "
        "a stuck outreach extra and call done with success=true for the completed application.\n"
        "- Automatically try existing sessions, configured credentials, SSO, and email OTP when authentication "
        "is required. If authentication or MFA still cannot be completed, include exactly "
        "@@BLOCKER: {\"type\":\"authentication\"} in the final result and stop. If this happens only during "
        "post-submission outreach, record outreach failed instead and keep application success=true.\n"
        "only during post-submission outreach, record outreach failed and keep application success=true.\n"
        f"- You have a maximum of {max_steps} steps total. Budget your steps wisely."
    )


def _user_document_paths() -> list[str]:
    return sorted(str(path) for path in USER_DOCS_DIR.iterdir() if path.is_file())


def _safe_runtime_settings(easy_apply: bool, max_steps: int) -> dict:
    """Return reproducibility settings without credentials or API keys."""
    llm_settings = load_llm_settings()
    provider = str(llm_settings.get("provider") or "")
    provider_settings = llm_settings.get(provider, {})
    app_settings = load_settings()
    try:
        from importlib.metadata import version

        browser_use_version = version("browser-use")
    except Exception:
        browser_use_version = "unknown"
    return {
        "provider": provider,
        "model": provider_settings.get("model") if isinstance(provider_settings, dict) else None,
        "browser_use_version": browser_use_version,
        "browser_headless": app_settings.get("browser_headless") is True,
        "blocked_domains": list(app_settings.get("blocked_domains") or []),
        "easy_apply": easy_apply,
        "max_steps": max_steps,
        "video_framerate": 10,
    }


def _last_history_error(result) -> str | None:
    if result is None:
        return None
    return next((str(error) for error in reversed(result.errors()) if error), None)


def _progress_value(value) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _result_preview(value: str) -> str:
    if len(value) <= APPLICATION_RESULT_PREVIEW_CHARS:
        return value
    preview = value[:APPLICATION_RESULT_PREVIEW_CHARS].rstrip()
    return f"{preview}… [truncated; {len(value):,} chars total]"


def _application_step_progress(browser_state, agent_output, step_num: int) -> list[str]:
    """Recreate Browser Use's readable per-step flow for Hunter's live output."""
    lines = [f"📍 Step {step_num}:"]
    evaluation = str(getattr(agent_output, "evaluation_previous_goal", "") or "").strip()
    memory = str(getattr(agent_output, "memory", "") or "").strip()
    next_goal = str(getattr(agent_output, "next_goal", "") or "").strip()

    if evaluation:
        lowered = evaluation.casefold()
        emoji = "👍" if "success" in lowered else "⚠️" if "failure" in lowered else "❔"
        lines.append(f"{emoji} Eval: {evaluation}")
    if memory:
        lines.append(f"🧠 Memory: {memory}")
    if next_goal:
        lines.append(f"🎯 Next goal: {next_goal}")

    for action in getattr(agent_output, "action", []) or []:
        try:
            dumped = action.model_dump(exclude_none=True, mode="json")
        except TypeError:
            dumped = action.model_dump(exclude_none=True)
        except Exception:
            dumped = {type(action).__name__: str(action)}
        if not isinstance(dumped, dict):
            dumped = {type(action).__name__: dumped}
        for name, parameters in dumped.items():
            if isinstance(parameters, dict):
                detail = ", ".join(
                    f"{key}={_progress_value(value)}"
                    for key, value in parameters.items()
                )
            else:
                detail = _progress_value(parameters)
            lines.append(f"▶️ {name}" + (f": {detail}" if detail else ""))
    return lines


def _application_result_progress(agent) -> list[str]:
    """Return the meaningful action results added to history by the completed step."""
    history = getattr(getattr(agent, "history", None), "history", None) or []
    if not history:
        return []
    lines = []
    for result in getattr(history[-1], "result", None) or []:
        error = str(getattr(result, "error", "") or "").strip()
        extracted = str(getattr(result, "extracted_content", "") or "").strip()
        remembered = str(getattr(result, "long_term_memory", "") or "").strip()
        attachments = getattr(result, "attachments", None) or []
        if error:
            lines.append(f"❌ Result: {_result_preview(error)}")
        elif extracted:
            lines.append(f"✅ Result: {_result_preview(extracted)}")
        elif remembered:
            lines.append(f"✅ Result: {_result_preview(remembered)}")
        if attachments:
            lines.append(f"📎 Files: {', '.join(str(item) for item in attachments)}")
    return lines


async def prepare_application_materials(
    job: dict,
    worker_id: int,
    cost_tracker: CategorizedCostTracker | None = None,
    enable_linkedin_outreach: bool = True,
) -> PreparedApplicationMaterials:
    """Create the required tailored CV and best-effort application extras."""
    description = _application_job_description(job)
    url = job["url"]

    print(f"  ✍️  [W{worker_id}] Tailoring CV...")
    job_title = str(job.get("title") or "")
    tailored = await tailor_resume(
        url,
        description,
        AUTO_TAILOR_OPTIONS,
        job_title,
        cost_tracker=cost_tracker,
    )
    resume_path = str(tailored["path"])
    update_job(url, tailored_resume_path=resume_path)
    print(f"  📄 [W{worker_id}] Tailored CV ready: {resume_path}")

    materials = PreparedApplicationMaterials(
        resume_path=resume_path,
        resume_audit=tailored.get("audit"),
    )

    print(f"  ✉️  [W{worker_id}] Generating optional cover letter...")
    try:
        materials.cover_letter, materials.cover_letter_audit = await generate_cover_letter_text(
            description,
            job_title,
            str(job.get("company") or ""),
            cost_tracker=cost_tracker,
            include_audit=True,
        )
        materials.cover_letter_path = str(
            save_cover_letter_text(url, materials.cover_letter, job_title)
        )
        materials.cover_letter_pdf_path = str(
            save_cover_letter_pdf(url, materials.cover_letter, job_title)
        )
        update_job(
            url,
            cover_letter=materials.cover_letter,
            cover_letter_path=materials.cover_letter_path,
            cover_letter_pdf_path=materials.cover_letter_pdf_path,
            cover_letter_generation_error=None,
        )
        print(f"  ✅ [W{worker_id}] Cover letter ready")
    except Exception as error:
        materials.cover_letter_error = str(error)
        update_job(
            url,
            cover_letter=None,
            cover_letter_path=None,
            cover_letter_pdf_path=None,
            cover_letter_generation_error=materials.cover_letter_error,
        )
        print(f"  ⚠️  [W{worker_id}] Cover letter unavailable; continuing with CV: {error}")

    if is_linkedin_url(url) and enable_linkedin_outreach:
        print(f"  💬 [W{worker_id}] Generating LinkedIn outreach message...")
        try:
            materials.linkedin_outreach_message = await generate_linkedin_outreach_text(
                description,
                job_title,
                str(job.get("company") or ""),
                url,
                cost_tracker=cost_tracker,
            )
            update_job(
                url,
                linkedin_outreach_message=materials.linkedin_outreach_message,
                linkedin_outreach_generated_at=datetime.now(timezone.utc).isoformat(),
                linkedin_outreach_generation_error=None,
            )
            print(f"  ✅ [W{worker_id}] LinkedIn outreach message ready")
        except Exception as error:
            materials.linkedin_outreach_error = str(error)
            update_job(
                url,
                linkedin_outreach_message=None,
                linkedin_outreach_generated_at=None,
                linkedin_outreach_generation_error=materials.linkedin_outreach_error,
            )
            print(f"  ⚠️  [W{worker_id}] LinkedIn outreach unavailable; application will continue: {error}")

    return materials


async def apply_to_job(
    job: dict,
    profile: dict,
    qa: dict,
    applied_labels: list[str],
    easy_apply: bool,
    worker_id: int,
    max_steps: int = DEFAULT_MAX_STEPS,
    progress: Callable[[str], None] | None = None,
    enable_linkedin_outreach: bool = True,
) -> str:
    """Apply to a single job. Returns final status."""
    url = job["url"]
    title = job.get("title", "Unknown")
    company = job.get("company", "Unknown")
    app_settings = load_settings()
    memory_policy = critical_memory_policy(app_settings)
    sensitive_data = app_settings.get("sensitive_data") or {}
    if not isinstance(sensitive_data, dict):
        sensitive_data = {}
    blocked_domains = [
        str(domain)
        for domain in (app_settings.get("blocked_domains") or [])
        if str(domain).strip()
    ]
    print(f"  🚀 [W{worker_id}] Starting: {title} at {company}")

    if not config.validate_job_url(url):
        await save_job_status(url, "blocked", "Invalid or internal URL")
        update_job(
            url,
            last_application_attempt_id=None,
            last_application_outcome={
                **application_outcome(None, attempt_id=None, agent_started=False),
                "type": "blocked",
                "blocker": "invalid_url",
                "retryable": False,
                "error": "Invalid or internal URL",
            },
        )
        print(f"  🚫 [W{worker_id}] Blocked (invalid URL): {title} at {company}")
        return "blocked"

    if any(domain in url for domain in blocked_domains):
        await save_job_status(url, "blocked", "Blocked domain")
        update_job(
            url,
            last_application_attempt_id=None,
            last_application_outcome={
                **application_outcome(None, attempt_id=None, agent_started=False),
                "type": "blocked",
                "blocker": "blocked_domain",
                "retryable": False,
                "error": "Blocked domain",
            },
        )
        print(f"  🚫 [W{worker_id}] Blocked: {title} at {company}")
        return "blocked"

    if not claim_job(url):
        print(f"  ⏭️  [W{worker_id}] Skipped (already claimed): {title} at {company}")
        return "skipped"

    try:
        metrics_store = MetricsStore()
    except Exception:
        metrics_store = None

    try:
        attempt = ApplicationAttempt(
            job,
            profile,
            worker_id,
            easy_apply,
            sensitive_values=[str(sensitive_data.get("password") or "")],
            metrics_store=metrics_store,
        )
    except Exception as audit_error:
        error = f"Application audit dossier could not be created: {audit_error}"
        await save_job_status(url, "failed", error[:2000])
        print(f"  ❌ [W{worker_id}] {error}")
        return "failed"

    browser = None
    browser_process = None
    agent = None
    result = None
    history_saved = False
    terminal_status = "failed"
    return_status = "failed"
    final_error = None
    success = None
    agent_started = False
    durable_outcome = None
    linkedin_outreach = None
    materials = None
    domain = ""
    ats_platform = None
    memories_injected_count = 0
    memories_extracted_count = 0
    mem_store = None
    cost_tracker = CategorizedCostTracker()
    cost_breakdown = None
    token_breakdown = None
    dirty_memory_scopes: set[str] = set()

    try:
        # Always refresh credentials before each job to avoid mid-run expiry.
        refresh_credentials()
        attempt.log("Preparing tailored application materials")
        materials = await prepare_application_materials(
            job,
            worker_id,
            cost_tracker=cost_tracker,
            enable_linkedin_outreach=enable_linkedin_outreach,
        )
        attempt.save_materials(
            materials.resume_path,
            materials.cover_letter_path,
            materials.cover_letter_pdf_path,
        )
        attempt.save_generation_audit(
            materials.resume_audit,
            materials.cover_letter_audit,
        )
        attempt.save_outreach_message(materials.linkedin_outreach_message)

        llm = config.get_llm()
        enable_cache_warmup = getattr(llm, "enable_vision_cache_warmup", None)
        if callable(enable_cache_warmup):
            enable_cache_warmup()
        judge_llm = config.get_llm()
        cost_tracker.register_llm("application_agent", llm)
        cost_tracker.register_llm("judge", judge_llm)
        mem_store = get_memory_store()
        domain = mem_store.extract_domain(url)
        ats_platform = mem_store.detect_ats_platform(domain)
        memories_before = mem_store.get_critical_memories(
            url,
            max_count=memory_policy["max_count"],
            max_tokens=memory_policy["max_tokens"],
        )
        memories_injected_count = len(memories_before)
        memory = build_memory_context(
            profile,
            qa,
            applied_labels,
            job_url=url,
            critical_memories=memories_before,
        )
        user_document_paths = _user_document_paths()
        if user_document_paths:
            memory += (
                "\n\nUSER SUPPORTING DOCUMENTS:\n"
                + "\n".join(f"- {path}" for path in user_document_paths)
                + "\nUpload one of these files only when the application explicitly requests a matching "
                "supporting document, certificate, diploma, or degree record. Choose it by its filename. "
                "Never substitute a supporting document for the tailored resume or cover letter."
            )
        # Contact details live in profile.md. The configured account email remains
        # available as a private fallback and for ATS login.
        agent_sensitive_data = {
            "email": str(sensitive_data.get("email") or "").strip(),
            "account_email": str(sensitive_data.get("email") or "").strip(),
            "password": str(sensitive_data.get("password") or ""),
            "linkedin_email": os.environ.get("LINKEDIN_EMAIL", "").strip(),
            "linkedin_password": os.environ.get("LINKEDIN_PASSWORD", ""),
        }

        login_instructions = (
            "LOGIN — ONLY IF REQUIRED WHILE NAVIGATING:\n"
            "- Do not perform a separate login check before applying. Navigate directly to the job and proceed normally.\n"
            "- If LinkedIn asks you to log in, use <secret>linkedin_email</secret> and "
            "<secret>linkedin_password</secret>. If login cannot be completed, report "
            "@@BLOCKER: {\"type\":\"linkedin_authentication\"}.\n"
            "- If Gmail asks you to log in while retrieving an email or verification code, use "
            "<secret>account_email</secret> and <secret>password</secret>.\n\n"
        )

        otp_instructions = (
            "\n\nOTP/VERIFICATION CODES: If ANY site asks for a verification code, OTP, or 2FA token:\n"
            "1. Choose the EMAIL option if given a choice\n"
            "2. Open a new tab and go to https://mail.google.com\n"
            "3. Find the most recent email with the verification/OTP code\n"
            "4. Copy the code, switch back to the application tab, and enter it\n"
            "5. This is NOT a blocker — always attempt to retrieve the code from Gmail before giving up"
        )

        if easy_apply:
            apply_instructions = (
                f"{login_instructions}"
                f"THEN: Go to {url} on LinkedIn. Click Easy Apply and complete the application. "
                f"Use the tailored resume at {materials.resume_path}. Auto-fill all fields from candidate profile. "
                f"Use get_cover_letter for a cover-letter upload and get_cover_letter_text for a cover-letter text area."
                f"{otp_instructions}"
            )
        else:
            has_password = bool(agent_sensitive_data.get("password", "").strip())
            password_note = ""
            if not has_password:
                password_note = (
                    "\n\nIMPORTANT: No password is configured in Settings. If the external site requires "
                    "account creation or login:\n"
                    "1. First check if you can apply as a guest (without creating an account)\n"
                    "2. Try 'Sign in with LinkedIn' or 'Sign in with Google' buttons\n"
                    "3. If no SSO option, CREATE A NEW ACCOUNT using <secret>account_email</secret> and a generated password "
                    "(create a strong password like 'JobApp2026!')\n"
                    "4. If account creation also fails, try 'Forgot password' → reset via email\n"
                    "5. If nothing works after 3 attempts, report failure and move to the next job\n"
                )

            apply_instructions = (
                f"{login_instructions}"
                f"THEN: Go to {url} on LinkedIn. Click Apply and follow through to the external application page. "
                f"Use the tailored resume at {materials.resume_path}. Auto-fill all fields from candidate profile. "
                f"Use get_cover_letter for a cover-letter upload and get_cover_letter_text for a cover-letter text area.\n\n"
                f"NAVIGATING EXTERNAL SITES:\n"
                f"- As the first action after arriving on each external company site or ATS platform, call "
                f"load_current_platform_critical_memories before interacting with that platform.\n"
                f"- The LinkedIn 'Apply' button often opens a company careers page, NOT the application form directly.\n"
                f"- You MUST explore the landing page: look for 'Apply Now', 'Submit Application', or similar buttons.\n"
                f"- Scroll down — the apply button is often below the job description.\n"
                f"- If you see a job listing page, click on the specific job title first, then look for the apply button.\n"
                f"- Some sites require you to click through 2-3 pages before reaching the actual form.\n"
                f"- If the page looks blank or is loading, wait 3-5 seconds and try scrolling.\n"
                f"- NEVER give up just because the form isn't immediately visible — always explore the page first.\n\n"
                f"EMAIL USAGE:\n"
                f"- For APPLICATION FORM fields, use the contact email in the candidate Markdown profile.\n"
                f"- If the Markdown profile has no contact email, use <secret>email</secret>.\n"
                f"- For LOGGING IN or CREATING ACCOUNTS on external ATS sites: use <secret>account_email</secret> and <secret>password</secret>\n"
                f"{password_note}"
                f"If it's a video funnel or recruitment pitch, report failure and stop. "
                f"If the external form is broken after 3 attempts, report failure and stop.\n\n"
                f"BLOCKED SITES — if redirected to any of these, immediately call done with success=false: {', '.join(blocked_domains)}"
                f"{otp_instructions}"
            )

        task = _application_task(
            apply_instructions,
            max_steps,
            resume_path=materials.resume_path,
            linkedin_job_url=url if is_linkedin_url(url) else "",
            linkedin_outreach_message=materials.linkedin_outreach_message,
        )
        attempt.save_inputs(
            task=task,
            context=memory,
            qa=qa,
            memories=list(memories_before or []),
            runtime_settings=_safe_runtime_settings(easy_apply, max_steps),
        )

        # Use the shared browser profile in OS data dir (same as login endpoint).
        # Browser Use records the currently focused tab, including Gmail/OTP flows.
        headless = config.browser_headless()
        user_agent = config.browser_user_agent()
        cdp_url, browser_process = config.launch_profile_browser(
            BROWSER_PROFILE_DIR,
            headless=headless,
            user_agent=user_agent,
        )
        browser = BrowserSession(
            **(
                {"cdp_url": cdp_url}
                if cdp_url
                else {
                    "user_data_dir": str(BROWSER_PROFILE_DIR),
                    "headless": headless,
                    "user_agent": user_agent,
                    "chromium_sandbox": (sys.platform != "linux"),
                    "args": ["--disable-blink-features=AutomationControlled"],
                }
            ),
            captcha_solver=False,
            record_video_dir=str(attempt.video_dir),
            record_video_framerate=10,
        )

        if is_linkedin_url(url):
            await browser.start()
            await ensure_linkedin_login(
                browser,
                llm_factory=config.get_llm,
                report=attempt.log,
            )

        _agent_log_start("apply", f"{title} at {company}")

        def _on_step(browser_state, agent_output, step_num):
            _agent_on_step(browser_state, agent_output, step_num)
            try:
                attempt.log_step(browser_state, agent_output, step_num)
                if progress is not None:
                    for line in _application_step_progress(
                        browser_state, agent_output, step_num
                    ):
                        progress(str(attempt.redact(line)))
            except Exception:
                pass

        async def _on_step_end(completed_agent):
            if progress is None:
                return
            try:
                for line in _application_result_progress(completed_agent):
                    progress(str(attempt.redact(line)))
            except Exception:
                pass

        agent = Agent(
            task=task,
            llm=llm,
            judge_llm=judge_llm,
            initial_actions=[{"navigate": {"url": url, "new_tab": False}}],
            use_vision=True,
            ground_truth=APPLICATION_JUDGE_GROUND_TRUTH,
            llm_call_timeout=300,  # 5 minutes per step
            max_failures=10,
            loop_detection_enabled=True,
            loop_detection_window=5,
            browser_session=browser,
            tools=build_application_tools(
                materials.cover_letter_pdf_path,
                materials.cover_letter,
                attempt.save_submission_screenshot,
                memory_store=mem_store,
                critical_memory_max_count=memory_policy["max_count"],
                critical_memory_max_tokens=memory_policy["max_tokens"],
            ),
            sensitive_data=agent_sensitive_data,
            extend_system_message=memory,
            available_file_paths=[
                path
                for path in [
                    materials.resume_path,
                    materials.cover_letter_pdf_path,
                    *user_document_paths,
                ]
                if path
            ],
            save_conversation_path=str(attempt.conversation_dir),
            calculate_cost=True,
            register_new_step_callback=_on_step,
            register_done_callback=_agent_on_done,
        )

        attempt.set_running()
        agent_started = True
        run_kwargs = {"max_steps": max_steps}
        if progress is not None:
            run_kwargs["on_step_end"] = _on_step_end
        result = await agent.run(**run_kwargs)

        try:
            attempt.save_final_screenshot(await browser.take_screenshot(full_page=True))
        except Exception as screenshot_error:
            attempt.log(f"Final full-page screenshot failed: {screenshot_error}", level="WARNING")
        attempt.save_history(result)
        history_saved = True

        durable_outcome = application_outcome(
            result,
            attempt_id=attempt.attempt_id,
            agent_started=agent_started,
            submission_checkpointed=bool(
                attempt.manifest.get("submission_checkpoint_recorded_at")
            ),
        )
        success = durable_outcome["submission_confirmed"]
        linkedin_outreach = linkedin_outreach_outcome(
            result,
            eligible=is_linkedin_url(url),
            message_available=bool(materials.linkedin_outreach_message),
            generation_error=materials.linkedin_outreach_error,
            enabled=enable_linkedin_outreach,
        )
        attempt.manifest["linkedin_outreach"] = linkedin_outreach

        if QA_LEARNING_ENABLED:
            # Q&A extraction remains intentionally separate from website memories.
            _, new_questions = extract_from_history(result)
            await save_new_qa(new_questions, source_domain=domain)

        if success:
            terminal_status = "applied"
            return_status = "applied"
            await save_job_status(url, "applied")
            print(f"  ✅ [W{worker_id}] Applied: {title} at {company}")
        else:
            terminal_status = "failed"
            return_status = "failed"
            blocker = durable_outcome.get("blocker")
            if blocker:
                final_error = f"Application blocked by {blocker.replace('_', ' ')}"
            elif durable_outcome.get("type") == "unknown_outcome":
                final_error = "Application outcome could not be verified"
            else:
                final_error = _last_history_error(result) or "Agent reported failure"
            await save_job_status(url, "failed", final_error[:2000])
            print(f"  ❌ [W{worker_id}] Failed: {title} at {company} — {final_error[:100]}")

    except asyncio.CancelledError:
        terminal_status = "cancelled"
        final_error = "Application was interrupted; submission outcome is unknown"
        update_job(url, status="failed", error=final_error)
        attempt.log(final_error, level="WARNING")
        raise
    except Exception as e:
        final_error = str(e)
        if isinstance(e, LinkedInAuthenticationError):
            terminal_status = "failed"
            return_status = "failed"
            durable_outcome = {
                "attempt_id": attempt.attempt_id,
                "type": "blocked",
                "submission_confirmed": False,
                "agent_claimed_success": None,
                "judge_verdict": None,
                "blocker": "linkedin_authentication",
                "retryable": False,
                "judgement": None,
                "evidence": None,
                "error": final_error,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
            await save_job_status(url, "failed", final_error[:2000])
            print(f"  ❌ [W{worker_id}] LinkedIn login stopped: {title} at {company}")
        elif "security token" in final_error.lower() or "expired" in final_error.lower():
            terminal_status = "retry"
            return_status = "retry"
            refresh_credentials()
            await save_job_status(url, "pending", "credentials_expired_retry")
            print(f"  🔑 [W{worker_id}] Credentials expired on: {title} at {company} — refreshed, will retry")
        else:
            if "Application material preparation failed:" not in final_error:
                if not attempt.manifest.get("artifacts", {}).get("resume"):
                    final_error = f"Application material preparation failed: {final_error}"
            terminal_status = "failed"
            return_status = "failed"
            await save_job_status(url, "failed", final_error[:2000])
            print(f"  ❌ [W{worker_id}] Error: {title} at {company} — {final_error[:100]}")
        attempt.log(final_error, level="ERROR")
    finally:
        audit_result = result
        if audit_result is None and agent is not None:
            audit_result = getattr(agent, "history", None)
        if browser is not None and not attempt.manifest.get("artifacts", {}).get("final_screenshot"):
            try:
                attempt.save_final_screenshot(await browser.take_screenshot(full_page=True))
            except Exception as screenshot_error:
                attempt.log(f"Final full-page screenshot failed: {screenshot_error}", level="WARNING")
        if audit_result is not None and not history_saved:
            try:
                attempt.save_history(audit_result)
                history_saved = True
            except Exception as history_error:
                attempt.log(f"History persistence failed: {history_error}", level="ERROR")
        try:
            if browser is not None:
                await browser.close()
        except Exception as close_err:
            print(f"    ⚠️  [W{worker_id}] Browser cleanup error: {close_err}")
            attempt.log(f"Browser cleanup error: {close_err}", level="WARNING")
        finally:
            config.stop_profile_browser(browser_process)
        try:
            attempt.finalize_recording()
        except Exception as video_error:
            attempt.log(f"Video finalization failed: {video_error}", level="WARNING")
        if (
            WEBSITE_MEMORY_EXTRACTION_ENABLED
            and audit_result is not None
            and terminal_status != "cancelled"
            and mem_store is not None
        ):
            try:
                api_key = str(
                    ((load_llm_settings().get("openai") or {}).get("api_key") or "")
                ).strip()
                export_memories = getattr(mem_store, "export_all", None)
                extraction = await extract_end_of_run_memories(
                    audit_result,
                    existing_memories=(export_memories() if callable(export_memories) else []),
                    api_key=api_key,
                )
                if extraction.usage is not None:
                    cost_tracker.add_response_usage(
                        "memory_extract", MEMORY_EXTRACTOR_MODEL, extraction.usage
                    )
                memories_extracted_count = len(extraction.memories)
                new_count, reinforced_count, changed_scopes = store_extracted_memories(
                    mem_store,
                    extraction.memories,
                    source_attempt_id=attempt.attempt_id,
                )
                dirty_memory_scopes.update(changed_scopes)
                if extraction.memories:
                    message = (
                        f"Extracted {len(extraction.memories)} verified platform memories "
                        f"({new_count} new, {reinforced_count} reinforced)"
                    )
                    attempt.log(message)
                    if progress is not None:
                        progress(f"🧠 {message}")
                else:
                    attempt.log("Memory extraction found no verified reusable workaround")
            except Exception as memory_error:
                attempt.log(
                    f"Memory extraction failed: {str(memory_error)[:500]}",
                    level="WARNING",
                )
        try:
            classification_cost = (job.get("classification") or {}).get("cost_usd")
            cost_breakdown = await cost_tracker.breakdown(classification_cost)
            token_breakdown = await cost_tracker.token_breakdown()
        except Exception as cost_error:
            attempt.log(f"Cost breakdown failed: {cost_error}", level="WARNING")
        if linkedin_outreach is None:
            linkedin_outreach = linkedin_outreach_outcome(
                audit_result,
                eligible=is_linkedin_url(url),
                message_available=bool(
                    materials and materials.linkedin_outreach_message
                ),
                generation_error=(materials.linkedin_outreach_error if materials else None),
                enabled=enable_linkedin_outreach,
            )
        attempt.manifest["linkedin_outreach"] = linkedin_outreach
        try:
            attempt.finish(
                terminal_status,
                error=final_error,
                result=audit_result,
                cost_breakdown=cost_breakdown,
                token_breakdown=token_breakdown,
            )
        except Exception as audit_error:
            print(f"    ⚠️  [W{worker_id}] Audit finalization failed: {audit_error}")

        if durable_outcome is None:
            durable_outcome = application_outcome(
                audit_result,
                attempt_id=attempt.attempt_id,
                agent_started=agent_started,
                error=final_error,
                submission_checkpointed=bool(
                    attempt.manifest.get("submission_checkpoint_recorded_at")
                ),
            )
        elif final_error and not durable_outcome.get("error"):
            durable_outcome["error"] = final_error
        update_job(
            url,
            last_application_attempt_id=attempt.attempt_id,
            last_application_outcome=durable_outcome,
            last_linkedin_outreach=linkedin_outreach,
        )

        if metrics_store is not None:
            try:
                finished_at = datetime.fromisoformat(
                    attempt.manifest.get("finished_at") or datetime.now(timezone.utc).isoformat()
                )
                categorized_total = total_cost(cost_breakdown) if cost_breakdown is not None else None
                usage = getattr(audit_result, "usage", None) if audit_result is not None else None
                metrics_store.record_run(
                    run_id=attempt.attempt_id,
                    job_url=url,
                    job_title=title,
                    company=company,
                    website_domain=domain,
                    ats_platform=ats_platform,
                    success=bool(success),
                    started_at=attempt.started_at,
                    finished_at=finished_at,
                    step_count=len(getattr(audit_result, "history", [])) if audit_result is not None else 0,
                    memories_injected=memories_injected_count,
                    memories_extracted=memories_extracted_count,
                    cost_usd=(
                        categorized_total
                        if categorized_total is not None
                        else getattr(usage, "total_cost", None) if usage is not None else None
                    ),
                    cost_breakdown=cost_breakdown,
                    token_breakdown=token_breakdown,
                    error_message=final_error[:2000] if final_error else None,
                )
            except Exception as metrics_err:
                print(f"    ⚠️  [W{worker_id}] Metrics recording failed (non-fatal): {metrics_err}")

        if (
            dirty_memory_scopes
            and memory_policy["auto_rerank"]
            and terminal_status != "cancelled"
            and mem_store is not None
        ):
            try:
                api_key = str(
                    ((load_llm_settings().get("openai") or {}).get("api_key") or "")
                ).strip()
                if not api_key:
                    raise RuntimeError("Hunter's saved OpenAI API key is missing")
                rerank_result = await rerank_critical_scopes(
                    mem_store,
                    dirty_memory_scopes,
                    api_key=api_key,
                    max_count=memory_policy["max_count"],
                    max_tokens=memory_policy["max_tokens"],
                )
                message = (
                    f"Auto-ranked {rerank_result['selected']} critical memories across "
                    f"{rerank_result['scopes']} changed platform scopes"
                )
                attempt.log(message)
                if progress is not None:
                    progress(f"🧠 {message}")
            except Exception as memory_error:
                attempt.log(
                    f"Automatic critical memory ranking failed: {str(memory_error)[:500]}",
                    level="WARNING",
                )

    return return_status


async def worker(
    name: str,
    worker_id: int,
    queue: asyncio.Queue,
    profile: dict,
    qa: dict,
    applied_labels: list,
    easy_apply: bool,
    stats: dict,
    cancel_flag: dict | None = None,
    max_steps: int = DEFAULT_MAX_STEPS,
    requeue_retries: bool = True,
    progress: Callable[[str], None] | None = None,
    enable_linkedin_outreach: bool = True,
    pre_submission_attempt_limit: int = 1,
    notify: Callable[[str], bool] | None = None,
):
    """Worker that pulls jobs from queue and applies."""
    while True:
        if cancel_flag and cancel_flag.get("cancel_requested"):
            print(f"  🛑 [{name}] Stop requested — halting")
            break
        try:
            job = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        job_easy_apply = job.get("easy_apply", easy_apply) if job.get("easy_apply") is not None else easy_apply
        attempt_number = 1
        while True:
            if cancel_flag and cancel_flag.get("cancel_requested"):
                print(f"  🛑 [{name}] Stop requested — not starting another attempt")
                status = "cancelled"
                break
            status = await apply_to_job(
                job,
                profile,
                qa,
                applied_labels,
                job_easy_apply,
                worker_id,
                max_steps=max_steps,
                progress=progress,
                enable_linkedin_outreach=enable_linkedin_outreach,
            )
            saved_job = read_jobs().get(job["url"], {}) if status == "failed" else {}
            outcome = saved_job.get("last_application_outcome") or {}
            setup_failed = outcome.get("type") == "pre_submission_failure"
            stop_requested = bool(cancel_flag and cancel_flag.get("cancel_requested"))
            if (
                setup_failed
                and not stop_requested
                and attempt_number < pre_submission_attempt_limit
            ):
                attempt_number += 1
                update_job(job["url"], status="pending", error=None)
                message = (
                    f"Application setup failed; retrying attempt {attempt_number}/"
                    f"{pre_submission_attempt_limit}: {job['url']}"
                )
                print(f"  🔄 [{name}] {message}")
                if progress is not None:
                    progress(message)
                continue
            if setup_failed and attempt_number >= pre_submission_attempt_limit and notify:
                title = str(job.get("title") or "Unknown role")
                company = str(job.get("company") or "Unknown company")
                message = (
                    f"Hunter: manual application setup failed {pre_submission_attempt_limit} "
                    f"times for {title} at {company}. The job was moved to Failed. "
                    f"{job['url']}"
                )
                try:
                    sent = await asyncio.to_thread(notify, message)
                    if not sent and progress is not None:
                        progress(f"Telegram failure alert could not be sent: {job['url']}")
                except Exception as error:
                    if progress is not None:
                        progress(
                            "Telegram failure alert raised an error: "
                            f"{job['url']} ({str(error)[:200]})"
                        )
            break
        stats[status] = stats.get(status, 0) + 1

        # On retry, put back in queue
        if status == "retry" and requeue_retries:
            queue.put_nowait(job)

        queue.task_done()


async def main():
    parser = argparse.ArgumentParser(description="Apply to collected jobs")
    parser.add_argument("--workers", type=int, default=1, help="Number of concurrent workers")
    parser.add_argument("--limit", type=int, help="Max jobs to process")
    parser.add_argument("--easy-apply", dest="easy_apply", action="store_true", default=True)
    parser.add_argument("--no-easy-apply", dest="easy_apply", action="store_false")
    args = parser.parse_args()

    jobs = read_jobs()
    profile = load_profile()
    qa = load_json(QA_FILE, {})
    LOGS_DIR.mkdir(exist_ok=True)
    # Filter pending jobs by type
    pending = [
        j for j in jobs.values()
        if j.get("status") == "pending"
        and (j.get("easy_apply") is True) == args.easy_apply
    ]

    if args.limit:
        pending = pending[:args.limit]

    if not pending:
        print("No pending jobs to apply to. Run collect_jobs.py first.")
        return

    applied_labels = [
        f"{j.get('title','')} at {j.get('company','')}"
        for j in jobs.values() if j.get("status") == "applied"
    ]

    mode = "Easy Apply" if args.easy_apply else "Non-Easy Apply"
    print(f"Applying to {len(pending)} {mode} jobs with {args.workers} worker(s)\n")

    queue = asyncio.Queue()
    for job in pending:
        queue.put_nowait(job)

    stats = {}
    num_workers = min(args.workers, len(pending))

    # Background credential refresh every 14 min — auto-cancelled when workers finish
    cred_task = asyncio.create_task(credential_refresh_loop(14))

    workers = []
    for i in range(num_workers):
        if i > 0:
            await asyncio.sleep(5)  # stagger browser launches
        workers.append(
            asyncio.create_task(worker(f"W{i+1}", i+1, queue, profile, qa, applied_labels, args.easy_apply, stats))
        )
    try:
        await asyncio.gather(*workers)
    finally:
        cred_task.cancel()
        await asyncio.gather(cred_task, return_exceptions=True)

    print(f"\n{'='*60}")
    print(f"Results: {stats}")
    total_applied = sum(
        1 for j in read_jobs().values() if j.get("status") == "applied"
    )
    print(f"Total applied across all runs: {total_applied}")

    # Memory stats
    mem_stats = get_memory_store().get_stats()
    print(f"🧠 Agent memory: {mem_stats['total_memories']} memories across {mem_stats['unique_domains']} domains")


if __name__ == "__main__":
    asyncio.run(main())
