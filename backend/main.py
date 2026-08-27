"""FastAPI backend for the Hunter web application."""
import asyncio
from email.header import decode_header
import json
import signal
import subprocess
import sys
import os
import threading
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import unquote, urlsplit

# Browser Use and browser-harness enable reporting by default. Hunter never opts in.
os.environ["ANONYMIZED_TELEMETRY"] = "false"
os.environ["BROWSER_USE_CLOUD_SYNC"] = "false"
os.environ["BROWSER_USE_VERSION_CHECK"] = "false"
os.environ["BH_TELEMETRY"] = "false"
os.environ["BROWSER_HARNESS_TELEMETRY"] = "false"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
import uvicorn

try:
    from models import (
        ApplyRequest,
        ApplicationOutcomeResolutionRequest,
        AutomationConfigRequest,
        ClassifyRequest,
        CleanupRequest,
        CollectRequest,
        CriticalMemorySettingsRequest,
        DecayRequest,
        GmailConnectRequest,
        GmailReviewRequest,
        JobCategoriesRequest,
        JobCategoryRequest,
        ScreenRequest,
        ScreeningOverrideRequest,
    )
except ImportError:
    from backend.models import (
        ApplyRequest,
        ApplicationOutcomeResolutionRequest,
        AutomationConfigRequest,
        ClassifyRequest,
        CleanupRequest,
        CollectRequest,
        CriticalMemorySettingsRequest,
        DecayRequest,
        GmailConnectRequest,
        GmailReviewRequest,
        JobCategoriesRequest,
        JobCategoryRequest,
        ScreenRequest,
        ScreeningOverrideRequest,
    )

from core.config import (
    PROFILE_MARKDOWN_TEMPLATE,
    get_data_dir,
    load_profile,
    load_profile_markdown,
    load_settings,
    save_profile,
    save_profile_markdown,
    save_settings,
)
from core.config import load_llm_settings, save_llm_settings
from core.system_status import collect_system_status
from core.application_attempt import (
    DOSSIER_RETENTION_DEFAULT_DAYS,
    VIDEO_RETENTION_DEFAULT_DAYS,
    application_attempt_storage_report,
    cleanup_application_attempts,
    find_attempt_directory,
)
from outcome_retention import auto_reject_stale_applications

DEFAULT_PORT = 8743
MAX_LOG_LINES = 500
CREDENTIAL_REFRESH_MINUTES = 14
OUTCOME_RETENTION_POLL_SECONDS = 60 * 60
MAX_RESUME_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_PLUGIN_UPLOAD_BYTES = 1024 * 1024
_automation_manager = None
_gmail_outcome_manager = None


def _assert_port_available(port: int) -> None:
    """Fail closed when another process already owns the backend port."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        # The dev launcher probes and closes this port immediately before us.
        # Reuse that closed socket state without permitting a live listener.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))


def _upload_exceeds_limit(request: Request, limit: int) -> bool:
    value = request.headers.get("content-length")
    if not value:
        return False
    try:
        return int(value) > limit
    except ValueError:
        return True


# ── Lifespan ──────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic."""
    _log.info("Backend starting (build 20260425-v3)...")
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    _log.info(f"Data directory: {data_dir}")
    # Clean up orphaned browser processes from previous crash
    _kill_browser_processes()
    # Reset any jobs stuck in "in_progress" from a previous crash
    _recover_stale_jobs()
    # Clean up old run logs (>30 days)
    _metrics = _get_metrics_store()
    if _metrics:
        cleaned = _metrics.cleanup_old_logs(days=30)
        if cleaned:
            _log.info(f"Cleaned up {cleaned} old log entries")
    try:
        retention_settings = load_settings()
        if retention_settings.get("dossier_retention_enabled", True):
            retention_result = await asyncio.to_thread(
                cleanup_application_attempts,
                video_retention_days=int(
                    retention_settings.get(
                        "dossier_retention_days", VIDEO_RETENTION_DEFAULT_DAYS
                    )
                ),
                dossier_retention_days=int(
                    retention_settings.get(
                        "dossier_delete_after_days",
                        DOSSIER_RETENTION_DEFAULT_DAYS,
                    )
                ),
            )
            if retention_result["removed_bytes"]:
                _log.info(
                    "Cleaned %s dossier video(s), deleted %s dossier(s), reclaiming %s bytes",
                    retention_result["compacted_dossiers"],
                    retention_result["deleted_dossiers"],
                    retention_result["removed_bytes"],
                )
    except Exception as exc:
        # Retention is best-effort and must never prevent Hunter from starting.
        _log.warning("Automatic dossier retention could not run: %s", exc)
    # Auto-install Chromium if needed (runs in background so /health is available immediately)
    _ensure_chromium_async()
    # Stop with the launcher so browser workers are not orphaned.
    _start_parent_watchdog()
    _get_automation_manager().start_if_enabled()
    gmail_manager = _get_gmail_outcome_manager()
    gmail_task = asyncio.create_task(gmail_manager.run(), name="gmail-outcomes")
    outcome_retention_task = asyncio.create_task(
        _run_outcome_retention(), name="outcome-retention"
    )
    yield
    # Graceful shutdown: stop running operations
    import traceback
    _log.info(f"Backend shutting down... (trigger: lifespan exit)")
    _log.info(f"Shutdown stack:\n{''.join(traceback.format_stack())}")
    if _automation_manager is not None:
        _automation_manager.shutdown()
    gmail_manager.shutdown()
    gmail_task.cancel()
    outcome_retention_task.cancel()
    await asyncio.gather(gmail_task, outcome_retention_task, return_exceptions=True)
    for status_dict in (_collection_status, _apply_status):
        if status_dict.get("running"):
            _force_stop(status_dict)
    _stop_manual_browser_session()
    if _classification_status.get("running"):
        with _classification_lock:
            _classification_status["cancel_requested"] = True
    # Give workers a moment to clean up, then force-kill browsers
    import time
    time.sleep(2)
    _kill_browser_processes()


async def _run_outcome_retention() -> None:
    """Apply the configured no-response timeout on startup and once per hour."""
    while True:
        try:
            settings = load_settings()
            updated = await asyncio.to_thread(
                auto_reject_stale_applications,
                int(settings.get("auto_reject_after_months", 1)),
            )
            if updated:
                _log.info("Automatically moved %s stale application(s) to Rejected", updated)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            _log.warning("Automatic application outcome aging could not run: %s", exc)
        await asyncio.sleep(OUTCOME_RETENTION_POLL_SECONDS)

app = FastAPI(title="Hunter Backend", version="1.0.0", lifespan=lifespan)

# ── Auth token (used by the dev proxy, CLI, and operational scripts) ──
import secrets as _secrets
import logging
from logging.handlers import RotatingFileHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        RotatingFileHandler(
            get_data_dir() / "backend.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
        ),
        logging.StreamHandler(),
    ],
)
# Silence noisy low-level loggers but keep browser_use agent logs visible
for _noisy in (
    "browser_use.dom", "browser_use.browser.chrome",
    "browser_use.browser.cdp", "browser_use.browser.session",
    "httpx", "httpcore", "urllib3", "filelock",
    "websockets", "charset_normalizer",
):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
_log = logging.getLogger("backend")

_API_TOKEN = os.environ.get("JOB_APPLICANT_TOKEN") or _secrets.token_hex(32)

# Persist the token for trusted local launchers and operational scripts.
import stat as _stat
_token_path = get_data_dir() / ".api_token"
_token_path.parent.mkdir(parents=True, exist_ok=True)
_token_path.write_text(_API_TOKEN)
try:
    os.chmod(_token_path, _stat.S_IRUSR | _stat.S_IWUSR)
except OSError as e:
    _log.warning(f"Could not set restrictive permissions on token file: {e}")
_log.info("API token written with owner-only permissions")

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
import time as _time

class _RateLimiter:
    """Simple per-path rate limiter using a sliding window."""
    def __init__(self, max_calls: int = 30, window_seconds: int = 60):
        self._max = max_calls
        self._window = window_seconds
        self._calls: dict[str, list[float]] = {}

    def is_allowed(self, path: str) -> bool:
        now = _time.monotonic()
        calls = self._calls.setdefault(path, [])
        calls[:] = [t for t in calls if now - t < self._window]
        if len(calls) >= self._max:
            return False
        calls.append(now)
        return True

_rate_limiter = _RateLimiter(max_calls=60, window_seconds=60)
_rate_limited_prefixes = (
    "/jobs/collect",
    "/jobs/import",
    "/jobs/classify",
    "/jobs/screen",
    "/apply/start",
    "/apply/stop",
    "/browser/manual",
    "/automation",
    "/system/status",
    "/llm/test",
    "/auth/login",
    "/gmail/outcomes",
)

_ALLOWED_ORIGINS = {"http://localhost:1420", "http://127.0.0.1:1420"}


def _decode_identity_header(value: str) -> str:
    """Decode the RFC2047 form Tailscale may use for identity headers."""
    try:
        return "".join(
            part.decode(charset or "utf-8") if isinstance(part, bytes) else part
            for part, charset in decode_header(value)
        )
    except (LookupError, UnicodeDecodeError):
        return ""


def _tailscale_web_request(request: Request) -> bool:
    """Accept the configured owner forwarded by Tailscale Serve."""
    expected_user = os.environ.get("HUNTER_TAILSCALE_USER", "").strip().casefold()
    expected_origin = os.environ.get("HUNTER_WEB_ORIGIN", "").strip().rstrip("/")
    if not expected_user or not expected_origin:
        return False

    parsed_origin = urlsplit(expected_origin)
    if (
        parsed_origin.scheme != "https"
        or not parsed_origin.hostname
        or not parsed_origin.hostname.endswith(".ts.net")
        or parsed_origin.path not in ("", "/")
        or parsed_origin.query
        or parsed_origin.fragment
    ):
        return False

    if request.headers.get("host", "").casefold() != parsed_origin.netloc.casefold():
        return False

    actual_user = _decode_identity_header(
        request.headers.get("Tailscale-User-Login", "")
    ).strip().casefold()
    return actual_user == expected_user


def _tailscale_origin_allowed(request: Request) -> bool:
    """Prevent another website from issuing authenticated commands to Hunter."""
    origin = request.headers.get("Origin", "").rstrip("/")
    expected_origin = os.environ.get("HUNTER_WEB_ORIGIN", "").strip().rstrip("/")
    if origin:
        return origin == expected_origin
    return request.method in {"GET", "HEAD", "OPTIONS"}


def _gmail_oauth_redirect_uri() -> str:
    """Choose the OAuth callback from trusted server configuration."""
    web_origin = os.environ.get("HUNTER_WEB_ORIGIN", "").strip().rstrip("/")
    if web_origin:
        parsed = urlsplit(web_origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.endswith(".ts.net")
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("HUNTER_WEB_ORIGIN is not a valid private Hunter origin")
        return f"{web_origin}/gmail/oauth/callback"
    return "http://127.0.0.1:8743/gmail/oauth/callback"

class _AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        _log.debug(
            "REQ %s %s origin=%r auth=%s",
            request.method,
            request.url.path,
            request.headers.get("Origin", ""),
            "yes" if request.headers.get("Authorization") else "no",
        )
        if request.url.path in ("/health", "/chromium/status", "/gmail/oauth/callback"):
            return await call_next(request)
        origin = request.headers.get("Origin", "")
        if _tailscale_web_request(request):
            if not _tailscale_origin_allowed(request):
                _log.warning("403 blocked Tailscale web origin: %r", origin)
                return JSONResponse({"error": "forbidden"}, status_code=403)
        else:
            if origin and origin not in _ALLOWED_ORIGINS:
                _log.warning(f"403 blocked origin: '{origin}' not in {_ALLOWED_ORIGINS}")
                return JSONResponse({"error": "forbidden"}, status_code=403)
            auth_header = request.headers.get("Authorization", "")
            token = auth_header.removeprefix("Bearer ").strip()
            if token != _API_TOKEN:
                _log.warning(
                    "401 %s %s | invalid or missing bearer token",
                    request.method,
                    request.url.path,
                )
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        if any(request.url.path.startswith(p) for p in _rate_limited_prefixes):
            if not _rate_limiter.is_allowed(request.url.path):
                return JSONResponse({"error": "rate limited"}, status_code=429)
        return await call_next(request)

app.add_middleware(_AuthMiddleware)


# ── Helpers ───────────────────────────────────────────────────────────────

def _start_parent_watchdog():
    """Exit if the launcher dies so automation workers are not orphaned."""
    parent_pid = os.getppid()
    if parent_pid <= 1:
        _log.info(f"Parent watchdog skipped (ppid={parent_pid}, already orphaned or init)")
        return

    def _watch():
        import time as _time
        try:
            import psutil
            parent = psutil.Process(parent_pid)
        except Exception:
            parent = None

        # Grace period: wait 10s before starting checks.
        _time.sleep(10)

        while True:
            _time.sleep(5)
            if parent:
                if not parent.is_running():
                    break
            else:
                # Fallback to os.getppid() if psutil fails
                if os.getppid() != parent_pid:
                    break

        _log.info("Parent process died — shutting down backend")
        os.kill(os.getpid(), signal.SIGTERM)

    t = threading.Thread(target=_watch, daemon=True, name="parent-watchdog")
    t.start()
    _log.info(f"Parent watchdog started (monitoring PID {parent_pid})")


def _recover_stale_jobs():
    """Preserve interrupted applications as unknown outcomes for manual review."""
    try:
        from core.shared_config import mutate_jobs
    except ImportError:
        from backend.core.shared_config import mutate_jobs

    now = datetime.now(timezone.utc).isoformat()

    def recover(jobs: dict) -> int:
        recovered = 0
        for job in jobs.values():
            if job.get("status") != "in_progress":
                continue
            previous = job.get("last_application_outcome") or {}
            attempt_id = previous.get("attempt_id") if isinstance(previous, dict) else None
            job.update(
                status="failed",
                error="Application was interrupted; submission outcome is unknown",
                last_application_outcome={
                    "attempt_id": attempt_id,
                    "type": "unknown_outcome",
                    "submission_confirmed": False,
                    "agent_claimed_success": None,
                    "judge_verdict": None,
                    "blocker": None,
                    "retryable": False,
                    "judgement": None,
                    "evidence": None,
                    "error": "Hunter stopped during the application",
                    "recorded_at": now,
                },
            )
            recovered += 1
        return recovered

    recovered = mutate_jobs(recover)
    if recovered:
        _log.info(
            "Recovered %s interrupted application(s) as unknown outcomes", recovered
        )


def _api_error(code: str, message: str, status_code: int = 400) -> JSONResponse:
    """Return a structured error response."""
    return JSONResponse(
        {"ok": False, "error": {"code": code, "message": message}},
        status_code=status_code,
    )


def _load_jobs() -> dict:
    from core.shared_config import read_jobs

    return read_jobs()


def _get_memory_store():
    """Get the memory store, falling back gracefully."""
    try:
        from memory.store import MemoryStore
        return MemoryStore()
    except (ImportError, FileNotFoundError, OSError) as e:
        _log.warning(f"Memory store unavailable: {e}")
        return None


def _get_metrics_store():
    """Get the metrics store, falling back gracefully."""
    try:
        from memory.metrics import MetricsStore
        return MetricsStore()
    except (ImportError, FileNotFoundError, OSError) as e:
        _log.warning(f"Metrics store unavailable: {e}")
        return None


# ── Playwright Chromium Discovery ─────────────────────────────────────────
def _find_playwright_chromium() -> str | None:
    """Find the Playwright-installed Chromium/Chrome binary.

    Handles both old and new Playwright naming conventions:
      Old: chromium-*/chrome-mac/Chromium.app/.../Chromium
      New: chromium-*/chrome-mac-arm64/Google Chrome for Testing.app/.../Google Chrome for Testing
    Searches in the correct OS-specific Playwright cache directory.
    """
    import glob as _glob

    if sys.platform == "linux":
        try:
            from core.shared_config import browser_executable_path
        except ImportError:
            from backend.core.shared_config import browser_executable_path

        if chrome_path := browser_executable_path():
            return chrome_path

    home = Path.home()

    # Playwright browser cache directories (OS-specific)
    cache_dirs = []
    if sys.platform == "darwin":
        cache_dirs.append(home / "Library" / "Caches" / "ms-playwright")
    elif sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local"))
        cache_dirs.append(Path(local_app) / "ms-playwright")
    cache_dirs.append(home / ".cache" / "ms-playwright")

    # Also check inside the venv's playwright package
    try:
        import importlib
        pw_module = importlib.import_module("playwright")
        venv_browsers = Path(pw_module.__file__).parent / "driver" / "package" / ".local-browsers"
        if venv_browsers.exists():
            cache_dirs.insert(0, venv_browsers)
    except (ImportError, AttributeError):
        pass

    # macOS executable patterns (both old and new Playwright naming)
    mac_patterns = [
        # New Playwright (v1.40+): Google Chrome for Testing
        "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        "chrome-mac/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        # Old Playwright: Chromium
        "chrome-mac/Chromium.app/Contents/MacOS/Chromium",
        "chrome-mac-arm64/Chromium.app/Contents/MacOS/Chromium",
    ]

    for cache_dir in cache_dirs:
        if not cache_dir.exists():
            continue
        # Sort chromium dirs by version number descending (newest first)
        chromium_dirs = sorted(cache_dir.glob("chromium-*"), reverse=True)
        for chromium_dir in chromium_dirs:
            if sys.platform == "darwin":
                for pattern in mac_patterns:
                    exe = chromium_dir / pattern
                    if exe.exists():
                        return str(exe)
            elif sys.platform == "win32":
                for pattern in ["chrome-win/chrome.exe", "chrome-win64/chrome.exe"]:
                    exe = chromium_dir / pattern
                    if exe.exists():
                        return str(exe)
            else:  # Linux
                for pattern in ["chrome-linux/chrome", "chrome-linux64/chrome"]:
                    exe = chromium_dir / pattern
                    if exe.exists():
                        return str(exe)

    return None


# ── Chromium Auto-Install ─────────────────────────────────────────────────
_chromium_status: dict = {"state": "checking", "message": "Checking for Chromium..."}


def _get_playwright_install_cmd() -> list[str] | None:
    """Build the command to run ``playwright install chromium``."""
    # Prefer the Playwright package's own driver (node + cli.js).
    try:
        from playwright._impl._driver import compute_driver_executable
        result = compute_driver_executable()
        if isinstance(result, tuple):
            node_bin, cli_js = result
            if os.path.exists(node_bin) and os.path.exists(cli_js):
                return [str(node_bin), str(cli_js), "install", "chromium"]
        elif os.path.exists(str(result)):
            return [str(result), "install", "chromium"]
    except (ImportError, Exception) as e:
        _log.warning(f"Bundled Playwright driver not usable: {e}")

    # Fallback: try system playwright
    import shutil
    if shutil.which("playwright"):
        return ["playwright", "install", "chromium"]
    for python in ["python3", "python"]:
        if shutil.which(python):
            try:
                r = subprocess.run([python, "-m", "playwright", "--version"],
                                   capture_output=True, text=True, timeout=10)
                if r.returncode == 0:
                    return [python, "-m", "playwright", "install", "chromium"]
            except Exception:
                pass
    return None


def _ensure_chromium_async():
    """Check if Playwright Chromium is installed, install in background if not.
    Updates _chromium_status so the frontend can poll progress."""
    global _chromium_status

    if _find_playwright_chromium() is not None:
        _chromium_status = {"state": "ready", "message": "Chromium found"}
        _log.info("Chromium found")
        return

    _chromium_status = {"state": "installing", "message": "Installing Chromium (one-time download, ~400MB)..."}
    _log.info("Chromium not found — installing in background")

    def _install():
        global _chromium_status
        try:
            cmd = _get_playwright_install_cmd()
            if not cmd:
                _chromium_status = {"state": "failed", "message": "Could not find Playwright installer. Please reinstall the application."}
                _log.error("No working Playwright install command found")
                return
            _log.info(f"Chromium install command: {cmd}")
            _chromium_status = {"state": "installing", "message": "Downloading Chromium browser (~162 MB). This may take a few minutes, please don't close the app..."}

            # Monitor the download directory for progress instead of parsing stdout
            # (Playwright suppresses progress bars when stdout is not a TTY)
            import re as _re
            cache_dir = Path.home() / "Library" / "Caches" / "ms-playwright"
            if sys.platform == "win32":
                local_app = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
                cache_dir = Path(local_app) / "ms-playwright"
            elif sys.platform != "darwin":
                cache_dir = Path.home() / ".cache" / "ms-playwright"

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )

            # Poll the download directory size while process runs
            while proc.poll() is None:
                import time as _t
                _t.sleep(3)
                # Check for partial download files
                try:
                    total_bytes = 0
                    for f in cache_dir.rglob("*"):
                        if f.is_file():
                            total_bytes += f.stat().st_size
                    mb = total_bytes / (1024 * 1024)
                    if mb > 1:
                        _chromium_status = {"state": "installing", "message": f"Downloading Chromium... {mb:.0f} MB downloaded. Please don't close the app."}
                except Exception:
                    pass

            # Read any remaining output
            out = proc.stdout.read()
            if out:
                text = out.decode("utf-8", errors="replace").strip()
                clean = _re.sub(r'\x1b\[[0-9;]*m', '', text)
                for line in clean.splitlines():
                    if line.strip():
                        _log.info(f"Chromium: {line.strip()}")
            if proc.returncode == 0:
                _chromium_status = {"state": "ready", "message": "Chromium installed successfully"}
                _log.info("Chromium installed successfully")
            else:
                cur_msg = _chromium_status.get("message", "Unknown error")
                _chromium_status = {"state": "failed", "message": f"Install failed: {cur_msg}"}
                _log.error(f"Chromium install failed (code {proc.returncode}): {cur_msg}")
        except Exception as e:
            msg = str(e)
            _chromium_status = {"state": "failed", "message": f"Install failed: {msg[:200]}"}
            _log.error(f"Chromium install error: {e}")

    threading.Thread(target=_install, daemon=True, name="chromium-install").start()


@app.get("/chromium/status")
async def chromium_status():
    return _chromium_status


# ── Health ────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    db_ok = False
    try:
        store = _get_memory_store()
        if store:
            store.get_stats()
            db_ok = True
    except Exception:
        pass

    chromium_ok = _chromium_status.get("state") == "ready"
    chromium_installing = _chromium_status.get("state") == "installing"
    if not chromium_ok and not chromium_installing:
        try:
            chromium_ok = _find_playwright_chromium() is not None
        except Exception:
            pass

    llm_configured = bool(load_llm_settings().get("provider"))
    worker_running = (
        _collection_status.get("running")
        or _classification_status.get("running")
        or _apply_status.get("running")
    )

    all_ok = db_ok and chromium_ok and llm_configured
    return {
        "status": "ok" if all_ok else "degraded",
        "version": "1.0.0",
        "checks": {
            "database": db_ok,
            "chromium": chromium_ok,
            "chromium_installing": chromium_installing,
            "llm_configured": llm_configured,
            "worker_running": bool(worker_running),
        },
    }


@app.get("/system/status")
def system_status():
    """Return current resource pressure for the machine running Hunter."""
    return collect_system_status()


# ── Profile ───────────────────────────────────────────────────────────────
@app.get("/profile")
async def get_profile():
    return load_profile()

@app.put("/profile")
async def update_profile(profile: dict):
    if "markdown" in profile:
        save_profile_markdown(profile["markdown"])
    save_profile(profile)
    return {"success": True}


@app.post("/profile/resume")
async def upload_resume(request: Request):
    """Store a browser-uploaded base resume on the Hunter host."""
    if _upload_exceeds_limit(request, MAX_RESUME_UPLOAD_BYTES):
        return _api_error("resume_too_large", "Resume PDF must be 15 MB or smaller", 413)
    data = await request.body()
    if not data or len(data) > MAX_RESUME_UPLOAD_BYTES:
        return _api_error("resume_too_large", "Resume PDF must be 15 MB or smaller", 413)
    if b"%PDF-" not in data[:1024]:
        return _api_error("invalid_resume", "Resume must be a PDF file", 400)
    try:
        import fitz

        with fitz.open(stream=data, filetype="pdf") as document:
            if document.needs_pass or document.page_count < 1:
                raise ValueError("PDF has no readable pages")
    except Exception:
        return _api_error("invalid_resume", "Resume must be a readable PDF file", 400)

    resume_dir = get_data_dir() / "resumes"
    resume_dir.mkdir(parents=True, exist_ok=True)
    resume_path = resume_dir / "resume.pdf"
    with tempfile.NamedTemporaryFile(
        dir=resume_dir,
        prefix=".resume-",
        suffix=".tmp",
        delete=False,
    ) as temporary_file:
        temporary_file.write(data)
        temporary_path = Path(temporary_file.name)
    try:
        temporary_path.chmod(0o600)
        temporary_path.replace(resume_path)
    finally:
        temporary_path.unlink(missing_ok=True)

    settings = load_settings()
    settings["resume_path"] = str(resume_path)
    save_settings(settings)
    return {"success": True, "resume_path": str(resume_path)}


# ── LLM Settings ──────────────────────────────────────────────────────────
@app.get("/settings/llm")
async def get_llm():
    return load_llm_settings()

@app.put("/settings/llm")
async def update_llm(settings: dict):
    save_llm_settings(settings)
    return {"success": True}

@app.post("/llm/test")
async def test_llm(settings: dict):
    """Test LLM connection with given settings."""
    try:
        from core.llm_factory import create_llm, test_connection
        llm = create_llm(settings)
        result = await test_connection(llm)
        return {"success": True, "message": result}
    except Exception as e:
        msg = str(e)
        if "api_key" in msg.lower() or "unauthorized" in msg.lower() or "401" in msg:
            return {"success": False, "message": "Invalid API key or unauthorized. Check your credentials."}
        if "timeout" in msg.lower() or "connect" in msg.lower():
            return {"success": False, "message": "Connection failed. Check your network and provider settings (is your local LLM server running?)"}

        from browser_use.llm.exceptions import ModelProviderError
        if isinstance(e, ModelProviderError):
            return {"success": False, "message": f"LLM Provider Error: {msg}"}

        _log.error(f"LLM test failed: {e}", exc_info=True)
        return {"success": False, "message": f"LLM test failed: {msg}"}


@app.post("/llm/ollama-models")
async def list_ollama_models(body: dict):
    """Fetch available models from an Ollama server."""
    import httpx
    base_url = body.get("base_url", "http://localhost:11434").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{base_url}/api/tags")
            resp.raise_for_status()
            data = resp.json()
            models = [m["name"] for m in data.get("models", [])]
            return {"success": True, "models": models}
    except Exception as e:
        return {"success": False, "models": [], "message": str(e)}


# ── App Settings ──────────────────────────────────────────────────────────
@app.get("/settings")
async def get_settings():
    return load_settings()

@app.put("/settings")
async def update_settings(settings: dict):
    save_settings(settings)
    return {"success": True}


# ── Gmail hiring outcomes ────────────────────────────────────────────────
def _get_gmail_outcome_manager():
    global _gmail_outcome_manager
    if _gmail_outcome_manager is None:
        try:
            from gmail_outcomes import GmailOutcomeManager
        except ImportError:
            from backend.gmail_outcomes import GmailOutcomeManager
        _gmail_outcome_manager = GmailOutcomeManager(get_data_dir())
    return _gmail_outcome_manager


@app.get("/gmail/outcomes/status")
async def get_gmail_outcome_status():
    return _get_gmail_outcome_manager().status()


@app.post("/gmail/outcomes/connect")
async def connect_gmail_outcomes(body: GmailConnectRequest):
    try:
        authorization_url = _get_gmail_outcome_manager().start_authorization(
            client_id=body.client_id,
            client_secret=body.client_secret,
            redirect_uri=_gmail_oauth_redirect_uri(),
        )
    except ValueError as exc:
        return _api_error("gmail_oauth_invalid", str(exc), 400)
    except Exception as exc:
        _log.warning("Could not start Gmail OAuth: %s", type(exc).__name__)
        return _api_error("gmail_oauth_failed", "Could not start Gmail authorization", 500)
    return {"success": True, "authorization_url": authorization_url}


@app.get("/gmail/oauth/callback", response_class=HTMLResponse)
async def gmail_oauth_callback(request: Request, state: str = ""):
    try:
        await asyncio.to_thread(
            _get_gmail_outcome_manager().complete_authorization,
            state=state,
            query_string=request.url.query,
        )
        asyncio.create_task(_get_gmail_outcome_manager().sync_safely())
    except Exception as exc:
        _log.warning("Gmail OAuth callback failed: %s", type(exc).__name__)
        return HTMLResponse(
            "<main><h1>Gmail could not be connected</h1>"
            "<p>Return to Hunter and start the connection again.</p></main>",
            status_code=400,
        )
    return HTMLResponse(
        "<main><h1>Gmail connected</h1>"
        "<p>Hunter will now synchronize explicit hiring outcomes. Returning to Settings.</p>"
        "<script>window.setTimeout(() => "
        "window.location.replace('/#/settings?section=gmail'), 800)</script></main>"
    )


@app.post("/gmail/outcomes/sync")
async def sync_gmail_outcomes():
    manager = _get_gmail_outcome_manager()
    try:
        result = await manager.sync_once()
    except ValueError as exc:
        return _api_error("gmail_not_connected", str(exc), 400)
    except Exception as exc:
        manager._record_error(exc)
        return _api_error("gmail_sync_failed", "Gmail outcome synchronization failed", 502)
    return {"success": True, **result, "status": manager.status()}


@app.post("/gmail/outcomes/disconnect")
async def disconnect_gmail_outcomes():
    _get_gmail_outcome_manager().disconnect()
    return {"success": True}


@app.post("/gmail/outcomes/reviewed")
async def dismiss_gmail_outcome_review(body: GmailReviewRequest):
    if not body.message_id.strip():
        return _api_error("missing_field", "Gmail message ID is required", 400)
    if not _get_gmail_outcome_manager().dismiss_review(body.message_id.strip()):
        return _api_error("not_found", "Gmail review item not found", 404)
    return {"success": True}


# ── In-process task runner ────────────────────────────────────────────────
import io


class _LogCapture(io.StringIO):
    """Capture one worker thread's output and batch its persisted run logs."""
    _ansi_re = __import__("re").compile(r"\x1b\[[0-9;]*m")

    def __init__(self, log_list: list, status_dict: dict = None,
                 max_lines: int = MAX_LOG_LINES, metrics_store=None, run_id: str = ""):
        super().__init__()
        self._log = log_list
        self._status = status_dict
        self._max = max_lines
        self._metrics = metrics_store
        self._run_id = run_id
        self._metric_rows: list[tuple[str, str | None, str, str]] = []

    def append_line(self, line: str, *, level: str = "INFO") -> None:
        line = self._ansi_re.sub("", line).strip()
        if not line:
            return
        with _status_lock:
            self._log.append(line)
            if len(self._log) > self._max:
                del self._log[:len(self._log) - self._max // 2]
            if self._status is not None and "💾" in line:
                match = __import__("re").search(r"total this title:\s*(\d+)", line)
                if match:
                    self._status["collected"] = int(match.group(1))
        if self._metrics and self._run_id:
            self._metric_rows.append((self._run_id, None, level, line))
            if len(self._metric_rows) >= 50:
                self.flush_metrics()

    def write(self, s):
        for line in s.splitlines():
            level = "ERROR" if "❌" in line else "WARNING" if "⚠️" in line else "INFO"
            self.append_line(line, level=level)
        return len(s)

    def flush(self):
        pass

    def flush_metrics(self) -> None:
        if not self._metric_rows or not self._metrics:
            return
        rows, self._metric_rows = self._metric_rows, []
        try:
            self._metrics.log_batch(rows)
        except Exception:
            pass


class _ThreadStreamRouter(io.TextIOBase):
    """Route worker-thread prints without redirecting other process output."""

    def __init__(self, fallback):
        self._fallback = fallback
        self._captures: dict[int, _LogCapture] = {}
        self._lock = threading.Lock()

    def register(self, thread_id: int, capture: _LogCapture) -> None:
        with self._lock:
            self._captures[thread_id] = capture

    def unregister(self, thread_id: int) -> None:
        with self._lock:
            self._captures.pop(thread_id, None)

    def write(self, value):
        with self._lock:
            capture = self._captures.get(threading.get_ident())
        return capture.write(value) if capture is not None else self._fallback.write(value)

    def flush(self):
        with self._lock:
            capture = self._captures.get(threading.get_ident())
        if capture is not None:
            return capture.flush()
        return self._fallback.flush()

    @property
    def encoding(self):
        return getattr(self._fallback, "encoding", None)

    def isatty(self):
        return bool(getattr(self._fallback, "isatty", lambda: False)())

    def fileno(self):
        return self._fallback.fileno()


_stream_router_lock = threading.Lock()
_stdout_router: _ThreadStreamRouter | None = None
_stderr_router: _ThreadStreamRouter | None = None


def _register_thread_capture(capture: _LogCapture) -> tuple[_ThreadStreamRouter, _ThreadStreamRouter]:
    global _stdout_router, _stderr_router
    with _stream_router_lock:
        if sys.stdout is not _stdout_router:
            _stdout_router = _ThreadStreamRouter(sys.stdout)
            sys.stdout = _stdout_router
        if sys.stderr is not _stderr_router:
            _stderr_router = _ThreadStreamRouter(sys.stderr)
            sys.stderr = _stderr_router
        thread_id = threading.get_ident()
        _stdout_router.register(thread_id, capture)
        _stderr_router.register(thread_id, capture)
        return _stdout_router, _stderr_router


def _run_async_in_thread(coro_factory, status_dict):
    """Run an async function in a new thread with its own event loop, capturing stdout and logs."""
    run_id = str(uuid.uuid4())[:8]
    status_dict["run_id"] = run_id
    status_dict["running"] = True
    status_dict["cancel_requested"] = False
    status_dict["error"] = None
    status_dict["fatal_error"] = False
    status_dict["finished_at"] = None
    metrics = _get_metrics_store()

    def _worker():
        worker_thread_id = threading.get_ident()
        capture = _LogCapture(status_dict["log"], status_dict=status_dict,
                              metrics_store=metrics, run_id=run_id)
        stdout_router, stderr_router = _register_thread_capture(capture)

        class _ListHandler(logging.Handler):
            _noise = {"httpx", "httpcore", "urllib3", "filelock", "websockets",
                       "charset_normalizer", "botocore", "boto3", "s3transfer"}
            _collected_re = __import__("re").compile(r"(\d+)\s+jobs?\s+collected", __import__("re").IGNORECASE)
            _ansi_re = __import__("re").compile(r"\x1b\[[0-9;]*m")
            def emit(self, record):
                if record.thread != worker_thread_id or record.name.split(".")[0] in self._noise:
                    return
                msg = self._ansi_re.sub("", record.getMessage()).strip()
                if msg and not msg.startswith("🌎"):
                    capture.append_line(msg, level=record.levelname)
                    if "collected" in status_dict:
                        match = self._collected_re.search(msg)
                        if match:
                            with _status_lock:
                                status_dict["collected"] = max(
                                    status_dict.get("collected", 0), int(match.group(1))
                                )

        log_handler = _ListHandler()
        log_handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(log_handler)

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            status_dict["_loop"] = loop
            loop.run_until_complete(coro_factory())
            with _status_lock:
                if status_dict.get("cancel_requested"):
                    status_dict["log"].append("🛑 Stopped by user")
                    status_dict["error"] = "cancelled"
                elif (
                    status_dict is _collection_status
                    and _classification_status.get("running")
                ):
                    status_dict["log"].append(
                        "🔎 Collection finished; classification is still running"
                    )
                    status_dict["error"] = None
                else:
                    status_dict["log"].append("✅ Finished successfully")
                    status_dict["error"] = None
        except asyncio.CancelledError:
            with _status_lock:
                status_dict["log"].append("🛑 Stopped by user")
                status_dict["error"] = "cancelled"
        except Exception as e:
            _log.error(f"Worker thread error: {e}", exc_info=True)
            with _status_lock:
                status_dict["log"].append(f"❌ Error: {e}")
                status_dict["error"] = str(e)
                status_dict["fatal_error"] = bool(
                    getattr(e, "fatal_automation", False)
                )
        finally:
            logging.getLogger().removeHandler(log_handler)
            stdout_router.unregister(worker_thread_id)
            stderr_router.unregister(worker_thread_id)
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(loop.shutdown_default_executor())
            capture.flush_metrics()
            if metrics is not None:
                metrics.close()
            loop.close()
            asyncio.set_event_loop(None)
            with _status_lock:
                status_dict["running"] = False
                status_dict["cancel_requested"] = False
                status_dict["finished_at"] = datetime.now().isoformat()
            status_dict.pop("_loop", None)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return t


def _kill_browser_processes():
    """Kill any Chromium processes using the shared browser profile.
    Uses psutil for cross-platform support (macOS, Linux, Windows)."""
    try:
        import psutil
        target = "langhire/browser_profile"
        procs_to_kill = []
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmdline = proc.info.get("cmdline") or []
                if any(target in arg for arg in cmdline):
                    procs_to_kill.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        for proc in procs_to_kill:
            try:
                proc.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        _, alive = psutil.wait_procs(procs_to_kill, timeout=3)
        for proc in alive:
            try:
                proc.kill()
                _log.warning(f"Force-killed browser process {proc.pid}")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except ImportError:
        import subprocess
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/FI", "IMAGENAME eq chrome.exe"],
                    timeout=5, capture_output=True,
                )
                subprocess.run(
                    ["taskkill", "/F", "/FI", "IMAGENAME eq chromium.exe"],
                    timeout=5, capture_output=True,
                )
            else:
                subprocess.run(
                    ["pkill", "-f", "user-data-dir=.*langhire/browser_profile"],
                    timeout=3, capture_output=True,
                )
        except Exception:
            pass


def _force_stop(status_dict):
    """Force-stop a running operation: cancel async tasks and kill browser processes."""
    with _status_lock:
        status_dict["cancel_requested"] = True

    # Cancel all tasks in the worker's event loop
    loop = status_dict.get("_loop")
    if loop and loop.is_running():
        def cancel_worker_tasks():
            for task in asyncio.all_tasks(loop):
                task.cancel()

        loop.call_soon_threadsafe(cancel_worker_tasks)

    _kill_browser_processes()
    with _status_lock:
        status_dict["log"].append("🛑 Force stopped")


# ── Thread-safe status access ─────────────────────────────────────────────
_status_lock = threading.Lock()


# ── Job Classification ──────────────────────────────────────────────────
CLASSIFICATION_HEARTBEAT_SECONDS = 10
_classification_lock = threading.Lock()
_classification_queue: list[tuple[str, bool, bool]] = []
_classification_status: dict = {
    "running": False,
    "total": 0,
    "completed": 0,
    "failed": 0,
    "current_job": None,
    "log": [],
    "error": None,
}
_classification_thread: threading.Thread | None = None


def _classification_log(message: str):
    _log.info(message)
    with _classification_lock:
        _classification_status["log"].append(message)
        if len(_classification_status["log"]) > MAX_LOG_LINES:
            del _classification_status["log"][: MAX_LOG_LINES // 2]


def _report_classification(message: str, mirror_to_collection: bool):
    _classification_log(message)
    if mirror_to_collection:
        _collection_progress(f"🤖 {message}")


async def _classification_worker():
    try:
        from job_classifier import (
            CLASSIFIER_MODEL,
            classify_and_store_job,
            safe_error_message,
        )
    except ImportError:
        from backend.job_classifier import (
            CLASSIFIER_MODEL,
            classify_and_store_job,
            safe_error_message,
        )
    from core.shared_config import get_job

    while True:
        with _classification_lock:
            if _classification_status.get("cancel_requested"):
                skipped = len(_classification_queue)
                _classification_queue.clear()
                _classification_status["log"].append(
                    f"Stopped after the active classification; skipped {skipped} queued jobs"
                )
                _classification_status["running"] = False
                _classification_status["current_job"] = None
                _classification_status["error"] = "cancelled"
                _classification_status["finished_at"] = datetime.now().isoformat()
                return
            if not _classification_queue:
                _classification_status["running"] = False
                _classification_status["current_job"] = None
                _classification_status["finished_at"] = datetime.now().isoformat()
                return
            url, force, mirror_to_collection = _classification_queue.pop(0)
            _classification_status["current_job"] = url
            position = _classification_status["total"] - len(_classification_queue)

        job = get_job(url) or {}
        title = str(job.get("title") or "").strip()
        company = str(job.get("company") or "").strip()
        label = " at ".join(part for part in (title, company) if part) or url
        _report_classification(
            f"Classifying {position}/{_classification_status['total']}: "
            f"{label} with {CLASSIFIER_MODEL}",
            mirror_to_collection,
        )
        try:
            task = asyncio.create_task(classify_and_store_job(url, force=force))
            elapsed = 0
            while True:
                try:
                    result = await asyncio.wait_for(
                        asyncio.shield(task),
                        timeout=CLASSIFICATION_HEARTBEAT_SECONDS,
                    )
                    break
                except asyncio.TimeoutError:
                    elapsed += CLASSIFICATION_HEARTBEAT_SECONDS
                    _report_classification(
                        f"Still classifying {position}/{_classification_status['total']}: "
                        f"{label} — {elapsed}s elapsed",
                        mirror_to_collection,
                    )
        except Exception as exc:
            safe_error = safe_error_message(exc)
            _log.error("Unexpected classification error for %s: %s", url, safe_error)
            result = "failed"
            _report_classification(
                f"Unexpected classification error for {label}: {safe_error}",
                mirror_to_collection,
            )

        saved_job = get_job(url) or {}
        with _classification_lock:
            if result == "failed":
                _classification_status["failed"] += 1
                message = (
                    f"Classification failed for {position}/{_classification_status['total']}: "
                    f"{label}"
                )
            elif result == "complete":
                _classification_status["completed"] += 1
                classification = saved_job.get("classification") or {}
                screening = saved_job.get("screening") or {}
                score = classification.get("score")
                effective_status = screening.get("override") or screening.get("status")
                details = []
                if score is not None:
                    details.append(f"score {score}")
                if effective_status:
                    details.append(str(effective_status))
                suffix = f" — {', '.join(details)}" if details else ""
                message = (
                    f"Classified {position}/{_classification_status['total']}: "
                    f"{label}{suffix}"
                )
            elif result == "skipped":
                message = f"Classification already current: {label}"
            else:
                _classification_status["failed"] += 1
                message = f"Job no longer exists: {url}"
        _report_classification(message, mirror_to_collection)


def _classification_thread_main():
    try:
        asyncio.run(_classification_worker())
    except Exception as exc:
        _log.error("Classification worker failed", exc_info=True)
        with _classification_lock:
            _classification_status["running"] = False
            _classification_status["current_job"] = None
            _classification_status["error"] = str(exc)
            _classification_status["log"].append(f"Classification worker error: {exc}")
            _classification_status["finished_at"] = datetime.now().isoformat()


def _enqueue_classification(
    urls: list[str],
    *,
    force: bool = False,
    mirror_to_collection: bool = False,
) -> int:
    """Add unique URLs to the one sequential classification worker."""
    global _classification_thread

    should_start = False
    with _classification_lock:
        queued = {url for url, _, _ in _classification_queue}
        current = _classification_status.get("current_job")
        new_urls = [url for url in urls if url and url not in queued and url != current]
        if not new_urls:
            return 0

        if not _classification_status.get("running"):
            _classification_status.update(
                {
                    "running": True,
                    "total": 0,
                    "completed": 0,
                    "failed": 0,
                    "current_job": None,
                    "log": [],
                    "error": None,
                    "cancel_requested": False,
                    "finished_at": None,
                }
            )
            should_start = True

        _classification_queue.extend(
            (url, force, mirror_to_collection) for url in new_urls
        )
        _classification_status["total"] += len(new_urls)

    if should_start:
        _classification_thread = threading.Thread(
            target=_classification_thread_main,
            daemon=True,
            name="job-classifier",
        )
        _classification_thread.start()
    return len(new_urls)


@app.post("/jobs/classify")
async def start_classification(body: ClassifyRequest):
    """Queue supplied jobs, or all missing/failed/stale jobs."""
    try:
        from job_classifier import classification_needs_processing
    except ImportError:
        from backend.job_classifier import classification_needs_processing
    from core.shared_config import read_jobs

    jobs = read_jobs()
    profile = load_profile()
    if body.job_urls is not None:
        unknown = [url for url in body.job_urls if url not in jobs]
        if unknown:
            return _api_error("not_found", f"Job not found: {unknown[0]}", 404)
        candidates = body.job_urls
    else:
        candidates = list(jobs)

    candidates = [
        url
        for url in candidates
        if classification_needs_processing(jobs[url], profile, force=body.force)
    ]
    queued = _enqueue_classification(candidates, force=body.force)
    return {"success": True, "queued": queued}


@app.get("/jobs/classify/status")
async def classification_status():
    with _classification_lock:
        return {
            "running": _classification_status.get("running", False),
            "total": _classification_status.get("total", 0),
            "completed": _classification_status.get("completed", 0),
            "failed": _classification_status.get("failed", 0),
            "current_job": _classification_status.get("current_job"),
            "log": list(_classification_status.get("log", [])[-100:]),
            "error": _classification_status.get("error"),
            "finished_at": _classification_status.get("finished_at"),
        }


@app.post("/jobs/classify/stop")
async def stop_classification():
    with _classification_lock:
        _classification_status["cancel_requested"] = True
        _classification_status["log"].append(
            "Stop requested; waiting for the active OpenAI call"
        )
    return {"success": True}


@app.post("/jobs/screen")
async def screen_jobs(body: ScreenRequest):
    """Rerun deterministic rules without an OpenAI call."""
    try:
        from job_screening import evaluate_screening
    except ImportError:
        from backend.job_screening import evaluate_screening
    from core.shared_config import read_jobs, update_jobs

    jobs = read_jobs()
    urls = body.job_urls if body.job_urls is not None else list(jobs)
    unknown = [url for url in urls if url not in jobs]
    if unknown:
        return _api_error("not_found", f"Job not found: {unknown[0]}", 404)

    profile = load_profile()
    update_jobs(
        {
            url: {"screening": evaluate_screening(jobs[url], profile)}
            for url in urls
        }
    )
    return {"success": True, "evaluated": len(urls)}


@app.put("/jobs/screening-override")
async def update_screening_override(body: ScreeningOverrideRequest):
    try:
        from job_screening import evaluate_screening
    except ImportError:
        from backend.job_screening import evaluate_screening
    from core.shared_config import read_jobs, update_job

    jobs = read_jobs()
    job = jobs.get(body.url)
    if job is None:
        return _api_error("not_found", "Job not found", 404)

    screening = evaluate_screening(job, load_profile())
    screening["override"] = body.override
    update_job(body.url, screening=screening)
    return {
        "success": True,
        "url": body.url,
        "status": body.override or screening["status"],
        "override": body.override,
    }


# ── Job Collection ────────────────────────────────────────────────────────
_collection_status: dict = {"running": False, "title": None, "log": []}
_collection_thread: threading.Thread | None = None


def _collection_progress(message: str):
    with _status_lock:
        _collection_status["log"].append(message)
        if len(_collection_status["log"]) > MAX_LOG_LINES:
            del _collection_status["log"][: MAX_LOG_LINES // 2]


@app.post("/jobs/collect")
async def start_collection(body: CollectRequest):
    """Start JobSpy collection or an authenticated LinkedIn URL scrape."""
    global _collection_thread, _collection_status

    if _collection_status["running"]:
        return {"success": False, "message": "Collection already running"}

    title = body.title
    max_jobs = body.max_jobs
    source = body.source
    search_url = (body.search_url or "").strip()
    filters = body.filters or {}
    profile = load_profile()
    titles = [title] if title and title.strip() else profile.get("target_job_titles", [])

    if search_url:
        if source != "linkedin":
            return _api_error(
                "invalid_collection_config",
                "LinkedIn search URLs require the LinkedIn source",
                400,
            )
        if _apply_status.get("running") or _login_running or _manual_browser_active():
            return _api_error(
                "browser_profile_busy",
                "Close the login or application browser before scraping LinkedIn",
                409,
            )
        try:
            from sources.linkedin_browser_collector import (
                linkedin_search_label,
                validate_linkedin_search_url,
            )

            search_url = validate_linkedin_search_url(search_url)
            title = linkedin_search_label(search_url)
        except ValueError as exc:
            return _api_error("invalid_linkedin_search_url", str(exc), 400)
    elif not any(isinstance(item, str) and item.strip() for item in titles):
        return _api_error(
            "missing_job_titles",
            "Add at least one target job title to the profile or enter one for this run",
            400,
        )

    from sources.jobspy_collector import (
        INDEED_JOB_TYPES,
        SUPPORTED_JOB_TYPES,
        country_name,
        hours_old_from_filter,
        normalize_hours_old,
    )
    location = ""
    hours_old = None
    job_type = None
    is_remote = False
    try:
        if not search_url:
            country_name(profile.get("country", ""))
            raw_hours_old = filters.get("hours_old")
            if raw_hours_old is None or str(raw_hours_old).strip() == "":
                # this is leftover code from previous UI, we leave it as maybe we will reimplement that more user-friendly dropdown menu
                hours_old = hours_old_from_filter(filters.get("date_posted"))
            else:
                hours_old = normalize_hours_old(source, raw_hours_old)

            location = str(filters.get("location", "")).strip()
            job_type = str(filters.get("job_type", "")).strip().lower() or None
            if job_type is not None and job_type not in SUPPORTED_JOB_TYPES:
                raise ValueError(f"Unsupported JobSpy job type: {job_type}")
            if source == "indeed" and job_type not in INDEED_JOB_TYPES | {None}:
                raise ValueError(f"Indeed does not support the {job_type} job type filter")

            raw_is_remote = filters.get("is_remote", False)
            if isinstance(raw_is_remote, bool):
                is_remote = raw_is_remote
            elif str(raw_is_remote).strip().lower() in {"true", "1", "yes"}:
                is_remote = True
            elif str(raw_is_remote).strip().lower() in {"false", "0", "no", ""}:
                is_remote = False
            else:
                raise ValueError("is_remote must be true or false")

            if source == "indeed" and (job_type or is_remote):
                hours_old = None
    except (TypeError, ValueError) as exc:
        return _api_error("invalid_collection_config", str(exc), 400)

    _collection_status = {
        "running": True,
        "title": title or "all titles",
        "log": [
            "Starting personalized LinkedIn URL collection..."
            if search_url
            else f"Starting {source} collection..."
        ],
        "collected": 0,
        "max_jobs": max_jobs,
        "added_urls": [],
    }

    async def _do_collect():
        from core.shared_config import add_jobs_if_new, read_jobs
        from sources.jobspy_collector import CollectionResult, _normalize_job, collect_jobs

        locations = [location] if location else profile.get("target_locations", [])
        existing_urls = set(read_jobs())
        if search_url:
            from core.shared_config import browser_user_agent
            from sources.linkedin_browser_collector import (
                linkedin_job_id,
                scrape_linkedin_search_url,
            )

            existing_job_ids = {
                job_id
                for existing_url in existing_urls
                if (job_id := linkedin_job_id(existing_url)) is not None
            }
            posted_after = None
            raw_posted_after = filters.get("posted_after")
            if raw_posted_after:
                try:
                    posted_after = datetime.fromisoformat(
                        str(raw_posted_after).replace("Z", "+00:00")
                    )
                except ValueError as exc:
                    raise ValueError("posted_after must be an ISO date-time") from exc
            max_results_to_inspect = filters.get("max_results_to_inspect")
            if max_results_to_inspect is not None:
                max_results_to_inspect = int(max_results_to_inspect)
            browser_result = await scrape_linkedin_search_url(
                search_url=search_url,
                max_jobs=max_jobs,
                max_results_to_inspect=max_results_to_inspect,
                profile_dir=_get_browser_profile_dir(),
                user_agent=browser_user_agent(headless=True),
                existing_job_ids=existing_job_ids,
                posted_after=posted_after,
                blacklisted_companies=profile.get("blacklisted_companies", []),
                should_stop=lambda: _collection_status.get("cancel_requested", False),
                on_progress=_collection_progress,
            )
            normalized_jobs = [
                job
                for raw_job in browser_result.jobs
                if (
                    job := _normalize_job(
                        raw_job,
                        "linkedin",
                        title or "",
                        "",
                    )
                )
            ]
            result = CollectionResult(
                jobs=normalized_jobs,
                errors=browser_result.errors,
                completed_queries=1,
                stopped=browser_result.stopped,
            )
        else:
            result = collect_jobs(
                source=source,
                titles=titles,
                locations=locations,
                country_code=profile.get("country", ""),
                max_jobs=max_jobs,
                hours_old=hours_old,
                job_type=job_type,
                is_remote=is_remote,
                blacklisted_companies=profile.get("blacklisted_companies", []),
                existing_urls=existing_urls,
                should_stop=lambda: _collection_status.get("cancel_requested", False),
                on_progress=_collection_progress,
            )
        if body.automation:
            discovered_at = datetime.now(timezone.utc).isoformat()
            for job in result.jobs:
                job["autonomous_discovered_at"] = discovered_at
        added_urls = add_jobs_if_new(result.jobs)
        added = len(added_urls)
        with _status_lock:
            _collection_status["collected"] = added
            _collection_status["added_urls"] = list(added_urls)

        skipped = len(result.jobs) - added
        print(f"💾 Saved {added} new jobs" + (f" ({skipped} became duplicates)" if skipped else ""))
        if added_urls and not body.automation:
            queued = _enqueue_classification(
                added_urls,
                mirror_to_collection=True,
            )
            print(f"🤖 Queued {queued} new jobs for sequential classification")
        elif added_urls:
            print("🤖 Autonomous mode will classify after all sources finish")
        if result.errors:
            print(f"⚠️ {len(result.errors)} queries failed; successful results were kept")
        if result.stopped:
            print("🛑 Stop requested — saved results from completed queries")
        else:
            print(f"Collection complete: {added} new jobs from {source}")

    def make_coro():
        return _do_collect()

    _collection_thread = _run_async_in_thread(make_coro, _collection_status)
    return {"success": True, "message": f"Collection started for {title or 'all titles'}"}


@app.post("/jobs/collect/stop")
async def stop_collection():
    with _status_lock:
        _collection_status["cancel_requested"] = True
        _collection_status["log"].append("🛑 Stop requested; waiting for the active search")
    return {"success": True}


@app.get("/jobs/collect/status")
async def collection_status():
    with _status_lock:
        return {
            "running": _collection_status.get("running", False),
            "title": _collection_status.get("title"),
            "log": list(_collection_status.get("log", [])[-100:]),
            "collected": _collection_status.get("collected", 0),
            "max_jobs": _collection_status.get("max_jobs", 0),
            "classification_running": _classification_status.get("running", False),
            "classification_total": _classification_status.get("total", 0),
            "classification_completed": _classification_status.get("completed", 0),
            "classification_failed": _classification_status.get("failed", 0),
            "error": _collection_status.get("error"),
            "finished_at": _collection_status.get("finished_at"),
            "run_id": _collection_status.get("run_id"),
            "added_urls": list(_collection_status.get("added_urls", [])),
        }


# ── Application Control ──────────────────────────────────────────────────
_apply_status: dict = {"running": False, "mode": None, "workers": 1, "log": []}
_apply_thread: threading.Thread | None = None


@app.post("/apply/start")
async def start_applying(body: ApplyRequest):
    """Start job application in-process."""
    global _apply_thread, _apply_status

    if _apply_status["running"]:
        return {"success": False, "message": "Application already running"}
    if _manual_browser_active():
        return _api_error(
            "browser_profile_busy",
            "Close the manual remote browser before starting an application",
            409,
        )

    from core.shared_config import read_jobs

    requested_urls = [body.job_url] if body.job_url else list(body.job_urls or [])
    if requested_urls:
        jobs = read_jobs()
        unknown_urls = [
            url
            for url in requested_urls
            if url
            and isinstance((jobs.get(url) or {}).get("last_application_outcome"), dict)
            and (jobs[url]["last_application_outcome"]).get("type") == "unknown_outcome"
            and (jobs[url]["last_application_outcome"]).get("retryable") is False
        ]
        if unknown_urls:
            return {
                "success": False,
                "message": (
                    "Application outcome is unknown; review the saved dossier before "
                    "making a manual submission decision"
                ),
            }

    # Kill any leftover browser processes from previous runs
    _kill_browser_processes()

    workers = body.workers
    mode = body.mode
    limit = body.limit
    max_steps = body.max_steps
    target_job_url = body.job_url
    target_job_urls = body.job_urls

    if target_job_url:
        start_message = "Applying to single job..."
        if max_steps is not None:
            start_message = f"Retrying single job with a {max_steps}-step limit..."
        _apply_status = {"running": True, "mode": mode, "workers": 1, "log": [start_message]}
    elif target_job_urls:
        _apply_status = {"running": True, "mode": mode, "workers": workers, "log": [f"Applying to {len(target_job_urls)} selected jobs..."]}
    else:
        _apply_status = {"running": True, "mode": mode, "workers": workers, "log": [f"Starting {mode} apply with {workers} worker(s)..."]}

    # mode="all" applies to all pending jobs regardless of easy_apply status
    easy_apply_filter = None if mode == "all" else (mode != "external")

    async def _do_apply():
        """Run apply logic directly, bypassing argparse."""
        from core.shared_config import (
            QA_FILE,
            LOGS_DIR,
            credential_refresh_loop,
            get_memory_store,
            load_json,
            read_jobs,
        )
        from cli import apply_jobs
        try:
            from autonomy import PRE_AGENT_ATTEMPT_LIMIT
            from gmail_outcomes import send_telegram_notification
        except ImportError:
            from backend.autonomy import PRE_AGENT_ATTEMPT_LIMIT
            from backend.gmail_outcomes import send_telegram_notification
        import core.shared_config as _config
        # Override config.get_llm with the user's UI-configured LLM
        llm_settings = load_llm_settings()
        if llm_settings.get("provider"):
            from core.llm_factory import create_llm
            _config.get_llm = lambda: create_llm(llm_settings)
            print(f"🤖 Using {llm_settings['provider']} LLM from settings")

        jobs = read_jobs()
        profile = load_profile()
        qa = load_json(QA_FILE, {})
        LOGS_DIR.mkdir(exist_ok=True)

        if target_job_url:
            target = jobs.get(target_job_url)
            if not target:
                print("Job not found.")
                return
            if target.get("status") not in ("pending", "failed"):
                print(f"Job is already {target.get('status')}.")
                return
            # Reset to pending so the worker can claim it
            from core.shared_config import update_job as _update_job
            _update_job(target_job_url, status="pending", error=None)
            target["status"] = "pending"
            pending = [target]
        elif target_job_urls:
            # Batch apply: only apply to the specific selected jobs
            from core.shared_config import update_job as _update_job
            pending = []
            for url in target_job_urls:
                j = jobs.get(url)
                if (
                    j
                    and j.get("status") in ("pending", "failed")
                ):
                    _update_job(url, status="pending", error=None)
                    j["status"] = "pending"
                    pending.append(j)
        elif easy_apply_filter is None:
            pending = [
                j
                for j in jobs.values()
                if j.get("status") == "pending"
            ]
        else:
            pending = [
                j for j in jobs.values()
                if j.get("status") == "pending"
                and (j.get("easy_apply") is True) == easy_apply_filter
            ]
        if limit and not target_job_url and not target_job_urls:
            pending = pending[:limit]

        if not pending:
            print("No pending jobs to apply to.")
            return

        applied_labels = [
            f"{j.get('title','')} at {j.get('company','')}"
            for j in jobs.values() if j.get("status") == "applied"
        ]

        label = "All" if easy_apply_filter is None else ("Easy Apply" if easy_apply_filter else "Non-Easy Apply")
        print(f"Applying to {len(pending)} {label} jobs with {workers} worker(s)\n")

        queue = asyncio.Queue()
        for job in pending:
            queue.put_nowait(job)

        stats = {}
        num_workers = min(workers, len(pending))
        cred_task = asyncio.create_task(credential_refresh_loop(CREDENTIAL_REFRESH_MINUTES))

        # For mode="all", pass True for easy_apply (agent handles both types via the job URL)
        worker_easy_apply = True if easy_apply_filter is None else easy_apply_filter
        tasks = []

        def report_application_progress(message: str) -> None:
            with _status_lock:
                _apply_status["log"].append(message)
                if len(_apply_status["log"]) > MAX_LOG_LINES:
                    del _apply_status["log"][: MAX_LOG_LINES // 2]

        for i in range(num_workers):
            if i > 0:
                await asyncio.sleep(5)
            worker_kwargs = {
                "cancel_flag": _apply_status,
                "max_steps": max_steps or apply_jobs.DEFAULT_MAX_STEPS,
                "progress": report_application_progress,
                "enable_linkedin_outreach": body.linkedin_outreach_enabled,
            }
            if body.automation:
                worker_kwargs["requeue_retries"] = False
            else:
                worker_kwargs["pre_submission_attempt_limit"] = PRE_AGENT_ATTEMPT_LIMIT
                worker_kwargs["notify"] = send_telegram_notification
            tasks.append(asyncio.create_task(
                apply_jobs.worker(
                    f"W{i+1}",
                    i+1,
                    queue,
                    profile,
                    qa,
                    applied_labels,
                    worker_easy_apply,
                    stats,
                    **worker_kwargs,
                )
            ))
        try:
            await asyncio.gather(*tasks)
        finally:
            cred_task.cancel()
            await asyncio.gather(cred_task, return_exceptions=True)

        print(f"\nResults: {stats}")
        total_applied = sum(
            1 for j in read_jobs().values() if j.get("status") == "applied"
        )
        print(f"Total applied: {total_applied}")

    def make_coro():
        return _do_apply()

    _apply_thread = _run_async_in_thread(make_coro, _apply_status)
    return {"success": True, "message": f"Started {mode} apply with {workers} worker(s)"}


@app.post("/apply/stop")
async def stop_applying():
    _force_stop(_apply_status)
    _recover_stale_jobs()
    return {"success": True}


@app.get("/apply/status")
async def apply_status():
    with _status_lock:
        return {
            "running": _apply_status.get("running", False),
            "mode": _apply_status.get("mode"),
            "workers": _apply_status.get("workers", 1),
            "log": list(_apply_status.get("log", [])[-100:]),
            "error": _apply_status.get("error"),
            "finished_at": _apply_status.get("finished_at"),
            "run_id": _apply_status.get("run_id"),
        }


# ── Autonomous Mode ─────────────────────────────────────────────────────
def _application_llm_is_configured(settings: dict) -> bool:
    provider = str(settings.get("provider") or "")
    provider_settings = settings.get(provider) or {}
    if provider in {"openai", "anthropic", "gemini", "openrouter"}:
        return bool(str(provider_settings.get("api_key") or "").strip())
    if provider == "bedrock":
        if provider_settings.get("auth_mode") == "keys":
            return bool(
                str(provider_settings.get("access_key") or "").strip()
                and str(provider_settings.get("secret_key") or "").strip()
            )
        return True
    if provider in {"ollama", "openai_compatible"}:
        return bool(str(provider_settings.get("base_url") or "").strip())
    return False


def _automation_preflight(config: dict) -> list[str]:
    errors: list[str] = []
    profile = load_profile()
    llm = load_llm_settings()

    needs_profile_search = (
        "indeed" in config["sources"]
        or ("linkedin" in config["sources"] and not config.get("linkedin_search_url"))
    )
    titles = [config.get("title")] if config.get("title") else profile.get("target_job_titles", [])
    if needs_profile_search and not any(str(title or "").strip() for title in titles):
        errors.append("Add at least one target job title or configure an automation title")

    classifier_key = str((llm.get("openai") or {}).get("api_key") or "").strip()
    if not classifier_key:
        errors.append("Configure the OpenAI API key required by job classification")
    if not _application_llm_is_configured(llm):
        errors.append("Configure the selected application LLM provider")

    try:
        from sources.jobspy_collector import (
            INDEED_JOB_TYPES,
            SUPPORTED_JOB_TYPES,
            country_name,
        )

        country_name(profile.get("country", ""))
        job_type = str(config.get("job_type") or "").strip().lower()
        if job_type and job_type not in SUPPORTED_JOB_TYPES:
            errors.append(f"Unsupported JobSpy job type: {job_type}")
        if "indeed" in config["sources"] and job_type and job_type not in INDEED_JOB_TYPES:
            errors.append(f"Indeed does not support the {job_type} job type filter")
    except ValueError as exc:
        errors.append(str(exc))

    search_url = str(config.get("linkedin_search_url") or "").strip()
    if search_url:
        try:
            from sources.linkedin_browser_collector import validate_linkedin_search_url

            validate_linkedin_search_url(search_url)
        except ValueError as exc:
            errors.append(str(exc))
    return list(dict.fromkeys(errors))


def _automation_qualified_urls() -> list[str]:
    try:
        from core.shared_config import read_jobs
    except ImportError:
        from backend.core.shared_config import read_jobs
    return [
        url
        for url, job in read_jobs().items()
        if job.get("status", "pending") == "pending"
        and (
            (job.get("screening") or {}).get("override")
            or (job.get("screening") or {}).get("status")
        )
        == "qualified"
    ]


def _automation_mark_completed(url: str) -> None:
    try:
        from core.shared_config import update_job
    except ImportError:
        from backend.core.shared_config import update_job
    update_job(
        url,
        autonomous_completed_at=datetime.now(timezone.utc).isoformat(),
        autonomous_attempt_in_progress=False,
    )


def _automation_mark_blocked(url: str) -> None:
    try:
        from core.shared_config import update_job
    except ImportError:
        from backend.core.shared_config import update_job
    update_job(
        url,
        status="blocked",
        error="Reposted listing skipped by autonomous mode",
        autonomous_attempt_in_progress=False,
    )


def _automation_recover_interrupted(url: str) -> None:
    try:
        from core.shared_config import update_job
    except ImportError:
        from backend.core.shared_config import update_job
    now = datetime.now(timezone.utc).isoformat()
    update_job(
        url,
        status="failed",
        error="Autonomous application was interrupted; submission outcome is unknown",
        autonomous_attempt_in_progress=False,
        last_application_outcome={
            "attempt_id": None,
            "type": "unknown_outcome",
            "submission_confirmed": False,
            "agent_claimed_success": None,
            "judge_verdict": None,
            "blocker": None,
            "retryable": False,
            "judgement": None,
            "evidence": None,
            "error": "Hunter stopped during the application",
            "recorded_at": now,
        },
    )


def _automation_job_snapshot(url: str) -> dict | None:
    try:
        from core.shared_config import get_job
    except ImportError:
        from backend.core.shared_config import get_job
    return get_job(url)


def _automation_is_busy() -> bool:
    return bool(
        _collection_status.get("running")
        or _classification_status.get("running")
        or _apply_status.get("running")
        or _login_running
        or _manual_browser_active()
    )


async def _automation_collect_source(source: str, config: dict) -> list[str]:
    filters: dict = {"hours_old": config["hours_old"]}
    if config.get("location"):
        filters["location"] = config["location"]
    if config.get("job_type"):
        filters["job_type"] = config["job_type"]
    if config.get("is_remote"):
        filters["is_remote"] = True
    if source == "indeed" and (config.get("job_type") or config.get("is_remote")):
        filters.pop("hours_old", None)
    if source == "linkedin" and config.get("linkedin_search_url"):
        filters["posted_after"] = config.get("_posted_after")
        filters["max_results_to_inspect"] = config.get("_max_results_to_inspect")

    response = await start_collection(
        CollectRequest(
            title=config.get("title") or None,
            max_jobs=config["max_jobs_per_source"],
            source=source,
            search_url=(
                config.get("linkedin_search_url") or None
                if source == "linkedin"
                else None
            ),
            filters=filters,
            automation=True,
        )
    )
    if not isinstance(response, dict) or not response.get("success"):
        message = response.get("message", "Collection could not start") if isinstance(response, dict) else "Collection could not start"
        raise RuntimeError(message)
    thread = _collection_thread
    if thread is not None:
        await asyncio.to_thread(thread.join)
    error = _collection_status.get("error")
    if error and error != "cancelled":
        if _collection_status.get("fatal_error"):
            try:
                from autonomy import FatalAutomationError
            except ImportError:
                from backend.autonomy import FatalAutomationError
            raise FatalAutomationError(str(error))
        raise RuntimeError(str(error))
    return list(_collection_status.get("added_urls") or [])


async def _automation_classify_urls(urls: list[str]) -> None:
    _enqueue_classification(urls)
    while True:
        with _classification_lock:
            running = _classification_status.get("running", False)
        if not running:
            return
        await asyncio.sleep(0.2)


async def _automation_apply_job(
    url: str,
    max_steps: int,
    linkedin_outreach_enabled: bool,
) -> dict:
    try:
        from core.shared_config import get_job, update_job
    except ImportError:
        from backend.core.shared_config import get_job, update_job
    update_job(
        url,
        autonomous_attempt_in_progress=True,
        autonomous_attempt_started_at=datetime.now(timezone.utc).isoformat(),
        last_application_outcome=None,
    )
    response = await start_applying(
        ApplyRequest(
            mode="all",
            workers=1,
            max_steps=max_steps,
            job_url=url,
            automation=True,
            linkedin_outreach_enabled=linkedin_outreach_enabled,
        )
    )
    if not response.get("success"):
        update_job(url, autonomous_attempt_in_progress=False)
        raise RuntimeError(response.get("message") or "Application could not start")
    thread = _apply_thread
    if thread is not None:
        await asyncio.to_thread(thread.join)
    job = get_job(url) or {}
    update_job(url, autonomous_attempt_in_progress=False)
    outcome = job.get("last_application_outcome")
    if isinstance(outcome, dict):
        return outcome
    return {
        "type": "unknown_outcome",
        "submission_confirmed": False,
        "blocker": None,
        "retryable": False,
    }


def _get_automation_manager():
    global _automation_manager
    if _automation_manager is None:
        try:
            from autonomy import AutomationManager
            from gmail_outcomes import send_telegram_notification
        except ImportError:
            from backend.autonomy import AutomationManager
            from backend.gmail_outcomes import send_telegram_notification
        _automation_manager = AutomationManager(
            get_data_dir(),
            collect_source=_automation_collect_source,
            classify_urls=_automation_classify_urls,
            apply_job=_automation_apply_job,
            job_snapshot=_automation_job_snapshot,
            qualified_urls=_automation_qualified_urls,
            is_busy=_automation_is_busy,
            recover_interrupted=_automation_recover_interrupted,
            mark_blocked=_automation_mark_blocked,
            mark_completed=_automation_mark_completed,
            notify=send_telegram_notification,
        )
    return _automation_manager


@app.get("/automation")
async def get_automation():
    return _get_automation_manager().snapshot()


@app.put("/automation/config")
async def update_automation_config(body: AutomationConfigRequest):
    raw = body.model_dump()
    search_url = str(raw.get("linkedin_search_url") or "").strip()
    if search_url:
        try:
            from sources.linkedin_browser_collector import validate_linkedin_search_url

            raw["linkedin_search_url"] = validate_linkedin_search_url(search_url)
        except ValueError as exc:
            return _api_error("invalid_automation_config", str(exc), 400)
    job_type = str(raw.get("job_type") or "").strip().lower()
    if job_type:
        from sources.jobspy_collector import INDEED_JOB_TYPES, SUPPORTED_JOB_TYPES

        if job_type not in SUPPORTED_JOB_TYPES:
            return _api_error(
                "invalid_automation_config",
                f"Unsupported JobSpy job type: {job_type}",
                400,
            )
        if "indeed" in raw["sources"] and job_type not in INDEED_JOB_TYPES:
            return _api_error(
                "invalid_automation_config",
                f"Indeed does not support the {job_type} job type filter",
                400,
            )
        raw["job_type"] = job_type
    try:
        config = _get_automation_manager().save_config(raw)
    except ValueError as exc:
        return _api_error("invalid_automation_config", str(exc), 400)
    return {"success": True, "config": config}


@app.post("/automation/start")
async def start_automation():
    manager = _get_automation_manager()
    snapshot = manager.snapshot()
    errors = _automation_preflight(snapshot["config"])
    if errors:
        return _api_error("automation_not_ready", "; ".join(errors), 400)
    manager.start()
    return {"success": True, **manager.snapshot()}


@app.post("/automation/stop")
async def stop_automation():
    manager = _get_automation_manager()
    manager.stop()
    return {"success": True, **manager.snapshot()}


# ── Jobs ──────────────────────────────────────────────────────────────────
JOB_CATEGORIES = (
    "review",
    "qualified",
    "unqualified",
    "applied",
    "online_assessment",
    "rejected",
    "interview",
    "offer",
    "accepted",
    "refused",
    "failed",
    "blocked",
)
APPLICATION_CATEGORIES = (
    "applied",
    "online_assessment",
    "rejected",
    "interview",
    "offer",
    "accepted",
    "refused",
)


def _job_category(job: dict) -> str:
    """Resolve the single Review & Apply folder for a stored job."""
    from core.shared_config import job_status_stage

    return job_status_stage(job)


@app.get("/jobs")
async def get_jobs(
    status: Optional[str] = Query(None),
    screening_status: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    limit: int = Query(500),
):
    """Get all jobs, optionally filtered by automation, screening, or category."""
    if category and category not in JOB_CATEGORIES:
        return _api_error(
            "invalid_category",
            f"Category must be one of: {', '.join(JOB_CATEGORIES)}",
            400,
        )
    from core.shared_config import job_status_history

    jobs = _load_jobs()
    result = []
    for url, job in jobs.items():
        job["url"] = url
        if status and job.get("status") != status:
            continue
        screening = job.get("screening") or {}
        effective_screening_status = screening.get("override") or screening.get("status") or "review"
        if screening_status and effective_screening_status != screening_status:
            continue
        job["category"] = _job_category(job)
        job["status_history"] = job_status_history(job)
        if category and job["category"] != category:
            continue
        if search:
            q = search.lower()
            if q not in (job.get("title", "") or "").lower() and \
               q not in (job.get("company", "") or "").lower():
                continue
        result.append(job)
    # Sort by collected_at desc
    result.sort(key=lambda j: j.get("collected_at", ""), reverse=True)
    return result[:limit]


@app.get("/jobs/stats")
async def get_job_stats():
    jobs = _load_jobs()
    stats = {
        "total": len(jobs),
        "pending": 0,
        "applied": 0,
        "failed": 0,
        "blocked": 0,
        "in_progress": 0,
        "qualified": 0,
        "review": 0,
        "rejected": 0,
        "application_applied": 0,
        "application_online_assessment": 0,
        "application_rejected": 0,
        "application_interview": 0,
        "application_offer": 0,
        "application_accepted": 0,
        "application_refused": 0,
        "category_counts": {category: 0 for category in JOB_CATEGORIES},
    }
    for j in jobs.values():
        s = j.get("status", "pending")
        if s in stats:
            stats[s] += 1
        stats["category_counts"][_job_category(j)] += 1
        if s == "pending":
            screening = j.get("screening") or {}
            screening_status = screening.get("override") or screening.get("status") or "review"
            if screening_status in ("qualified", "review", "rejected"):
                stats[screening_status] += 1
        elif s == "applied":
            application_status = j.get("application_status") or "applied"
            application_key = f"application_{application_status}"
            if application_key in stats:
                stats[application_key] += 1
    return stats


@app.get("/jobs/export")
async def export_jobs_csv():
    """Export all jobs as a downloadable CSV file."""
    import csv
    import io
    from fastapi.responses import StreamingResponse

    jobs = _load_jobs()
    rows = list(jobs.values())
    # Newest first by applied_at, then collected_at — matches the History view ordering.
    rows.sort(key=lambda j: (j.get("applied_at") or "", j.get("collected_at") or ""), reverse=True)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Date Applied", "Date Collected", "Job Title", "Company", "Location", "Status", "Source", "Easy Apply", "URL"])
    for j in rows:
        writer.writerow([
            j.get("applied_at", "") or "",
            j.get("collected_at", "") or "",
            j.get("title", "") or "",
            j.get("company", "") or "",
            j.get("location", "") or "",
            j.get("status", "") or "",
            j.get("source", "") or "",
            "" if j.get("easy_apply") is None else ("yes" if j.get("easy_apply") else "no"),
            j.get("url", "") or "",
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="langhire-jobs.csv"'},
    )


# ── Job Management ────────────────────────────────────────────────────────

@app.put("/jobs/status")
async def update_job_status(body: dict):
    """Manually change a job's status."""
    url = body.get("url", "").strip()
    new_status = body.get("status", "").strip()
    if not url:
        return _api_error("missing_field", "Job URL is required", 400)
    valid_statuses = ("pending", "applied", "failed", "blocked")
    if new_status not in valid_statuses:
        return _api_error("invalid_status", f"Status must be one of: {', '.join(valid_statuses)}", 400)

    jobs = _load_jobs()
    if url not in jobs:
        return _api_error("not_found", "Job not found", 404)

    from core.shared_config import update_job
    update_job(url, transition_source="manual", status=new_status, error=None)
    return {"success": True, "url": url, "status": new_status}


def _category_update_fields(
    job: dict,
    category: str,
    *,
    now: str,
    profile: dict,
) -> dict:
    """Build the persisted fields for one category transition."""
    fields = {"error": None}
    if category in ("review", "qualified", "unqualified"):
        try:
            from job_screening import evaluate_screening
        except ImportError:
            from backend.job_screening import evaluate_screening

        screening = evaluate_screening(job, profile)
        screening["override"] = {
            "review": "review",
            "qualified": "qualified",
            "unqualified": "rejected",
        }[category]
        fields.update(
            status="pending",
            screening=screening,
            application_status=None,
            application_status_updated_at=None,
            application_status_source=None,
            application_status_evidence=None,
        )
    elif category in APPLICATION_CATEGORIES:
        fields.update(
            status="applied",
            application_status=category,
            application_status_updated_at=now,
            application_status_source="manual",
            application_status_evidence=None,
        )
        if not job.get("applied_at"):
            fields["applied_at"] = now
    else:
        fields.update(
            status=category,
            application_status=None,
            application_status_updated_at=None,
            application_status_source=None,
            application_status_evidence=None,
        )
    return fields


@app.put("/jobs/category")
async def update_job_category(body: JobCategoryRequest):
    """Move a job between unified Review & Apply categories."""
    from core.shared_config import update_job

    jobs = _load_jobs()
    job = jobs.get(body.url)
    if job is None:
        return _api_error("not_found", "Job not found", 404)
    if job.get("status") == "in_progress":
        return _api_error(
            "application_in_progress",
            "Stop the active application before changing its category.",
            409,
        )

    fields = _category_update_fields(
        job,
        body.category,
        now=datetime.now(timezone.utc).isoformat(),
        profile=(
            load_profile()
            if body.category in ("review", "qualified", "unqualified")
            else {}
        ),
    )

    update_job(body.url, transition_source="manual", **fields)
    if body.category in ("unqualified", "blocked") and _automation_manager is not None:
        _automation_manager.discard_url(body.url)
    updated = {**job, **fields, "url": body.url}
    updated["category"] = _job_category(updated)
    return {"success": True, "job": updated}


@app.put("/jobs/categories")
async def update_job_categories(body: JobCategoriesRequest):
    """Move multiple jobs in one storage transaction."""
    try:
        from core.shared_config import update_jobs
    except ImportError:
        from backend.core.shared_config import update_jobs

    urls = list(dict.fromkeys(url.strip() for url in body.urls if url.strip()))
    if not urls:
        return _api_error("missing_field", "At least one job URL is required", 400)
    if len(urls) > 500:
        return _api_error("too_many_jobs", "Move at most 500 jobs at once", 400)

    jobs = _load_jobs()
    active_urls = [
        url for url in urls
        if jobs.get(url, {}).get("status") == "in_progress"
    ]
    if active_urls:
        return _api_error(
            "application_in_progress",
            "Stop active applications before moving the selected jobs.",
            409,
        )

    profile = (
        load_profile()
        if body.category in ("review", "qualified", "unqualified")
        else {}
    )
    now = datetime.now(timezone.utc).isoformat()
    fields_by_url = {
        url: _category_update_fields(
            jobs[url],
            body.category,
            now=now,
            profile=profile,
        )
        for url in urls
        if url in jobs
    }
    updated = update_jobs(fields_by_url, transition_source="manual")
    if body.category in ("unqualified", "blocked") and _automation_manager is not None:
        for url in fields_by_url:
            _automation_manager.discard_url(url)
    return {
        "success": True,
        "updated": updated,
        "missing": [url for url in urls if url not in jobs],
    }


@app.put("/jobs/application-status")
async def update_application_status(body: dict):
    """Update the real-world hiring outcome without changing automation state."""
    url = body.get("url", "").strip()
    new_status = body.get("status", "").strip()
    if not url:
        return _api_error("missing_field", "Job URL is required", 400)
    valid_statuses = (
        "applied",
        "online_assessment",
        "rejected",
        "interview",
        "offer",
        "accepted",
        "refused",
    )
    if new_status not in valid_statuses:
        return _api_error(
            "invalid_status",
            f"Application status must be one of: {', '.join(valid_statuses)}",
            400,
        )

    jobs = _load_jobs()
    if url not in jobs:
        return _api_error("not_found", "Job not found", 404)

    from core.shared_config import update_job
    updated_at = datetime.now(timezone.utc).isoformat()
    update_job(
        url,
        transition_source="manual",
        application_status=new_status,
        application_status_updated_at=updated_at,
        application_status_source="manual",
        application_status_evidence=None,
    )
    return {
        "success": True,
        "url": url,
        "status": new_status,
        "updated_at": updated_at,
    }


@app.post("/jobs/application-outcome/resolve")
async def resolve_application_outcome(body: ApplicationOutcomeResolutionRequest):
    """Record the user's decision for a previously ambiguous application."""
    from core.shared_config import get_job, update_job

    job = get_job(body.url)
    if job is None:
        return _api_error("not_found", "Job not found", 404)
    if job.get("status") == "in_progress":
        return _api_error(
            "application_in_progress",
            "Stop the active application before resolving its outcome.",
            409,
        )

    outcome = job.get("last_application_outcome")
    if not (
        isinstance(outcome, dict)
        and outcome.get("type") == "unknown_outcome"
        and outcome.get("retryable") is False
    ):
        return _api_error(
            "outcome_already_resolved",
            "This application no longer has an unresolved outcome.",
            409,
        )

    outcome_attempt_id = str(outcome.get("attempt_id") or "")
    if body.attempt_id and outcome_attempt_id and body.attempt_id != outcome_attempt_id:
        return _api_error(
            "stale_application_attempt",
            "A newer application attempt exists; review its dossier instead.",
            409,
        )

    resolved_at = datetime.now(timezone.utc).isoformat()
    review = {
        "decision": body.decision,
        "resolved_at": resolved_at,
        "attempt_id": body.attempt_id or outcome_attempt_id or None,
        "previous_outcome": outcome,
    }
    if body.decision == "not_submitted":
        screening = dict(job.get("screening") or {})
        screening["override"] = "qualified"
        fields = {
            "status": "pending",
            "screening": screening,
            "error": None,
            "application_status": None,
            "application_status_updated_at": None,
            "last_application_outcome": None,
            "last_application_outcome_review": review,
        }
    else:
        fields = {
            "status": "applied",
            "error": None,
            "application_status": "applied",
            "application_status_updated_at": resolved_at,
            "last_application_outcome": {
                "type": "manual_confirmation",
                "submission_confirmed": True,
                "retryable": False,
                "attempt_id": body.attempt_id or outcome_attempt_id or None,
                "confirmed_at": resolved_at,
            },
            "last_application_outcome_review": review,
        }
        if not job.get("applied_at"):
            fields["applied_at"] = resolved_at

    update_job(body.url, transition_source="manual", **fields)
    updated = {**job, **fields, "url": body.url}
    updated["category"] = _job_category(updated)
    return {
        "success": True,
        "decision": body.decision,
        "job": updated,
    }


@app.post("/jobs/add")
async def add_job_manually(body: dict):
    """Manually add a job URL to the collection queue."""
    from datetime import datetime, timezone
    from core.shared_config import add_jobs_if_new, validate_job_url

    url = body.get("url", "").strip()
    if not url:
        return _api_error("missing_field", "Job URL is required", 400)
    if not validate_job_url(url):
        return _api_error("invalid_url", "Invalid or internal URL", 400)

    job = {
        "url": url,
        "title": body.get("title", "").strip() or "Manually Added",
        "company": body.get("company", "").strip() or "",
        "location": body.get("location", "").strip() or "",
        "easy_apply": None,
        "status": "pending",
        "source": body.get("source", "manual"),
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "applied_at": None,
        "error": None,
    }
    if not add_jobs_if_new([job]):
        return _api_error("duplicate", "Job already exists in the queue", 409)
    return {"success": True, "url": url}


@app.post("/jobs/import/linkedin")
async def import_linkedin_job_url(body: dict):
    """Import one public LinkedIn listing and queue its classification."""
    from core.shared_config import add_jobs_if_new, read_jobs
    from sources.jobspy_collector import (
        _job_url_identity,
        import_linkedin_job,
        linkedin_job_id_from_url,
    )
    from sources.company_filter import blacklisted_company_match

    url = body.get("url", "").strip()
    if not url:
        return _api_error("missing_field", "LinkedIn job URL is required", 400)

    try:
        job_id = linkedin_job_id_from_url(url)
    except ValueError as exc:
        return _api_error("invalid_linkedin_url", str(exc), 400)

    identity = f"linkedin:{job_id}"
    if any(
        _job_url_identity(existing_url) == identity for existing_url in read_jobs()
    ):
        return _api_error("duplicate", "Job already exists in Hunter", 409)

    try:
        job = import_linkedin_job(url)
    except RuntimeError as exc:
        return _api_error("linkedin_import_failed", str(exc), 502)

    blocked_company = blacklisted_company_match(
        job.get("company"),
        load_profile().get("blacklisted_companies", []),
    )
    if blocked_company:
        return _api_error(
            "blacklisted_company",
            f"{job.get('company') or blocked_company} is excluded from collection",
            400,
        )

    added_urls = add_jobs_if_new([job])
    if not added_urls:
        return _api_error("duplicate", "Job already exists in Hunter", 409)

    queued = _enqueue_classification(added_urls)
    return {
        "success": True,
        "url": added_urls[0],
        "job": job,
        "classification_queued": queued == 1,
    }


@app.delete("/jobs")
async def delete_jobs(body: dict):
    """Delete one or more jobs by URL."""
    from core.shared_config import delete_jobs as delete_persisted_jobs

    urls = body.get("urls", [])
    if not urls or not isinstance(urls, list):
        return _api_error("missing_field", "urls array is required", 400)

    deleted = delete_persisted_jobs(urls)
    return {"success": True, "deleted": deleted}


# ── Auth / Login Sessions ─────────────────────────────────────────────────
_login_browser_process = None  # track the login browser subprocess

_MANUAL_BROWSER_DISPLAY = ":99"
_MANUAL_BROWSER_VNC_PORT = 5901
_manual_browser_lock = threading.Lock()
_manual_browser_stop = threading.Event()
_manual_browser_thread: threading.Thread | None = None
_manual_browser_processes: dict[str, subprocess.Popen] = {}
_manual_browser_status: dict = {
    "state": "stopped",
    "message": None,
    "started_at": None,
}


def _manual_browser_dependencies() -> list[str]:
    if sys.platform != "linux":
        return []
    import shutil

    return [name for name in ("Xvfb", "x11vnc") if shutil.which(name) is None]


def _manual_browser_active() -> bool:
    with _manual_browser_lock:
        return _manual_browser_status["state"] in {"starting", "running", "stopping"}


def _manual_browser_status_payload() -> dict:
    missing = _manual_browser_dependencies()
    with _manual_browser_lock:
        status = dict(_manual_browser_status)
    return {
        **status,
        "supported": sys.platform == "linux",
        "dependencies_ready": sys.platform == "linux" and not missing,
        "missing_dependencies": missing,
        "display": _MANUAL_BROWSER_DISPLAY,
        "vnc_port": _MANUAL_BROWSER_VNC_PORT,
    }


def _terminate_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _wait_for_manual_browser(
    ready,
    process: subprocess.Popen,
    description: str,
    timeout: float = 10,
) -> None:
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if _manual_browser_stop.is_set():
            return
        if process.poll() is not None:
            raise RuntimeError(f"{description} exited before it became ready")
        if ready():
            return
        _time.sleep(0.1)
    raise RuntimeError(f"Timed out waiting for {description}")


def _run_manual_browser_session() -> None:
    global _manual_browser_thread
    from core.shared_config import launch_profile_browser, stop_profile_browser

    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = data_dir / "manual_browser.log"
    password_path = data_dir / "manual_browser.password"
    display_number = _MANUAL_BROWSER_DISPLAY.removeprefix(":").split(".", 1)[0]
    display_socket = Path(f"/tmp/.X11-unix/X{display_number}")
    failure: str | None = None

    with log_path.open("ab", buffering=0) as log_file:
        try:
            xvfb = subprocess.Popen(
                [
                    "Xvfb",
                    _MANUAL_BROWSER_DISPLAY,
                    "-screen",
                    "0",
                    "1440x900x24",
                    "-nolisten",
                    "tcp",
                    "-noreset",
                ],
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
            with _manual_browser_lock:
                _manual_browser_processes["xvfb"] = xvfb
            _wait_for_manual_browser(display_socket.exists, xvfb, "virtual display")
            if _manual_browser_stop.is_set():
                return

            password_path.write_text(f"{uuid.uuid4().hex[:8]}\n")
            os.chmod(password_path, 0o600)
            vnc = subprocess.Popen(
                [
                    "x11vnc",
                    "-display",
                    _MANUAL_BROWSER_DISPLAY,
                    "-localhost",
                    "-rfbport",
                    str(_MANUAL_BROWSER_VNC_PORT),
                    "-passwdfile",
                    str(password_path),
                    "-forever",
                    "-shared",
                    "-noxdamage",
                ],
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
            with _manual_browser_lock:
                _manual_browser_processes["vnc"] = vnc

            def vnc_ready() -> bool:
                import socket

                try:
                    with socket.create_connection(
                        ("127.0.0.1", _MANUAL_BROWSER_VNC_PORT), timeout=0.2
                    ):
                        return True
                except OSError:
                    return False

            _wait_for_manual_browser(vnc_ready, vnc, "private VNC server")
            if _manual_browser_stop.is_set():
                return

            _, chrome = launch_profile_browser(
                _get_browser_profile_dir(),
                headless=False,
                display=_MANUAL_BROWSER_DISPLAY,
                start_url="https://www.linkedin.com/jobs/",
            )
            if chrome is None:
                raise RuntimeError("Chrome could not be launched on the virtual display")
            with _manual_browser_lock:
                _manual_browser_processes["chrome"] = chrome
                _manual_browser_status.update(
                    {
                        "state": "running",
                        "message": (
                            "Remote browser ready. Run scripts/connect-remote-browser.sh "
                            "on your Mac; it will copy the VNC password."
                        ),
                    }
                )

            while not _manual_browser_stop.wait(0.5):
                if chrome.poll() is not None:
                    break
                if vnc.poll() is not None or xvfb.poll() is not None:
                    raise RuntimeError("The remote display service stopped unexpectedly")
        except Exception as exc:
            failure = str(exc)
            _log.error("Manual remote browser failed: %s", exc, exc_info=True)
        finally:
            with _manual_browser_lock:
                processes = dict(_manual_browser_processes)
            stop_profile_browser(processes.get("chrome"))
            _terminate_process(processes.get("vnc"))
            _terminate_process(processes.get("xvfb"))
            password_path.unlink(missing_ok=True)
            with _manual_browser_lock:
                _manual_browser_processes.clear()
                _manual_browser_thread = None
                _manual_browser_status.update(
                    {
                        "state": "error" if failure else "stopped",
                        "message": failure,
                        "started_at": None,
                    }
                )
            _manual_browser_stop.clear()


def _stop_manual_browser_session() -> None:
    _manual_browser_stop.set()
    with _manual_browser_lock:
        if _manual_browser_status["state"] in {"starting", "running"}:
            _manual_browser_status.update(
                {"state": "stopping", "message": "Stopping remote browser..."}
            )
        processes = dict(_manual_browser_processes)
    _terminate_process(processes.get("chrome"))
    _terminate_process(processes.get("vnc"))
    _terminate_process(processes.get("xvfb"))


@app.get("/browser/manual/status")
async def manual_browser_status():
    return _manual_browser_status_payload()


@app.post("/browser/manual/start")
async def start_manual_browser():
    global _manual_browser_thread

    if sys.platform != "linux":
        return _api_error(
            "manual_browser_unsupported",
            "The remote browser session is supported on the Linux Hunter host",
            400,
        )
    missing = _manual_browser_dependencies()
    if missing:
        return _api_error(
            "manual_browser_dependencies_missing",
            (
                f"Install {', '.join(missing)} on the remote host with "
                "scripts/setup-remote-browser.sh"
            ),
            503,
        )
    if _apply_status.get("running") or _collection_status.get("running") or _login_running:
        return _api_error(
            "browser_profile_busy",
            "Wait for collection, application, or login browser work to finish",
            409,
        )
    with _manual_browser_lock:
        if _manual_browser_status["state"] in {"starting", "running", "stopping"}:
            return _api_error(
                "manual_browser_running",
                "The manual remote browser is already active",
                409,
            )
        _manual_browser_status.update(
            {
                "state": "starting",
                "message": "Starting private virtual display and Chrome...",
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        _manual_browser_stop.clear()
        _manual_browser_thread = threading.Thread(
            target=_run_manual_browser_session,
            daemon=True,
            name="manual-remote-browser",
        )
        _manual_browser_thread.start()
    return {"success": True, **_manual_browser_status_payload()}


@app.post("/browser/manual/stop")
async def stop_manual_browser():
    _stop_manual_browser_session()
    return {"success": True, **_manual_browser_status_payload()}

def _get_browser_profile_dir() -> str:
    """Get the shared browser profile directory. Migrates from old per-worker profiles if needed."""
    data_dir = get_data_dir()
    profile = data_dir / "browser_profile"
    profile.mkdir(parents=True, exist_ok=True)

    # Auto-migrate: if shared profile has no cookies but an old one does, copy it
    if not (profile / "Default" / "Cookies").exists():
        import shutil
        for old_name in ["browser_profile_w1", "browser_profile_collect"]:
            old = data_dir / old_name
            if (old / "Default" / "Cookies").exists():
                for item in old.iterdir():
                    dest = profile / item.name
                    if not dest.exists():
                        if item.is_dir():
                            shutil.copytree(item, dest)
                        else:
                            shutil.copy2(item, dest)
                _log.info(f"Migrated browser profile from {old_name}")
                break

    return str(profile)


@app.get("/auth/status")
async def auth_status():
    """Check if LinkedIn and Gmail sessions are valid by inspecting cookies."""
    import sqlite3
    profile_dir = _get_browser_profile_dir()
    cookies_db = Path(profile_dir) / "Default" / "Cookies"

    result = {"linkedin": {"logged_in": False}, "gmail": {"logged_in": False}}

    if not cookies_db.exists():
        return result

    try:
        import shutil, tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir) / "Cookies"
            shutil.copy2(cookies_db, tmp)

            conn = sqlite3.connect(str(tmp))
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT COUNT(*) FROM cookies WHERE host_key LIKE '%linkedin.com' AND name = 'li_at'"
                )
                li_count = cursor.fetchone()[0]
                result["linkedin"]["logged_in"] = li_count > 0

                cursor.execute(
                    "SELECT COUNT(*) FROM cookies WHERE host_key LIKE '%google.com' AND name IN ('SID', 'SSID', 'HSID')"
                )
                gmail_count = cursor.fetchone()[0]
                result["gmail"]["logged_in"] = gmail_count >= 2
            finally:
                conn.close()
    except Exception as e:
        _log.warning(f"Cookie check failed: {e}")

    return result


_login_running = False

@app.post("/auth/login/{service}")
async def auth_login(service: str):
    """Launch the shared profile through BrowserSession for manual login."""
    global _login_running

    if service not in ("linkedin", "gmail"):
        return {"success": False, "message": f"Unknown service: {service}"}

    if _login_running:
        return {"success": False, "message": "A login browser is already open. Please use it or close it first."}
    if _manual_browser_active():
        return {
            "success": False,
            "message": "Close the manual remote browser before opening a login browser.",
        }

    urls = {
        "linkedin": "https://www.linkedin.com/login",
        "gmail": "https://accounts.google.com/signin/v2/identifier?service=mail",
    }

    profile_dir = _get_browser_profile_dir()

    def _run_login_browser():
        """Run a headed BrowserSession in a thread until the login tab closes."""
        global _login_running
        _login_running = True

        async def _login():
            from browser_use import BrowserSession
            from playwright.async_api import Error as PlaywrightError, async_playwright
            from core.shared_config import (
                browser_user_agent,
                launch_profile_browser,
                stop_profile_browser,
            )

            browser = None
            browser_process = None
            try:
                user_agent = browser_user_agent(headless=False)
                cdp_url, browser_process = launch_profile_browser(
                    profile_dir,
                    headless=False,
                    user_agent=user_agent,
                )
                browser = BrowserSession(
                    **(
                        {"cdp_url": cdp_url}
                        if cdp_url
                        else {
                            "user_data_dir": profile_dir,
                            "headless": False,
                            "user_agent": user_agent,
                            "chromium_sandbox": (sys.platform != "linux"),
                            "args": ["--disable-blink-features=AutomationControlled"],
                        }
                    ),
                    captcha_solver=False,
                )
                await browser.start()
                _log.info("BrowserSession login browser started for %s", service)

                async with async_playwright() as playwright:
                    connected = await playwright.chromium.connect_over_cdp(
                        browser.cdp_url
                    )
                    if not connected.contexts:
                        raise RuntimeError("Login browser did not provide a context")
                    context = connected.contexts[0]
                    page = context.pages[0] if context.pages else await context.new_page()
                    await page.goto(urls[service], wait_until="domcontentloaded")
                    _log.info("Navigated to %s", urls[service])
                    try:
                        for _ in range(600):
                            if page.is_closed():
                                break
                            await page.wait_for_timeout(500)
                    except PlaywrightError:
                        # Closing the login window tears down the attached page.
                        pass
            finally:
                if browser is not None:
                    try:
                        await browser.kill()
                    except Exception:
                        pass
                stop_profile_browser(browser_process)

        try:
            _log.info(
                "Launching BrowserSession login browser for %s with profile: %s",
                service,
                profile_dir,
            )
            asyncio.run(_login())
            _log.info("Login browser closed by user")
        except Exception as e:
            _log.error(f"Login browser error: {e}", exc_info=True)
        finally:
            _login_running = False

    try:
        import threading
        t = threading.Thread(target=_run_login_browser, daemon=True)
        t.start()
        return {"success": True, "message": f"Browser opened for {service} login. Please log in and close the browser when done."}
    except Exception as e:
        _login_running = False
        _log.error(f"Failed to launch login browser: {e}")
        return {"success": False, "message": f"Failed to launch browser: {e}"}


# ── Memory ────────────────────────────────────────────────────────────────
@app.get("/memory/stats")
async def get_memory_stats():
    store = _get_memory_store()
    if store:
        from memory.store import critical_memory_policy

        policy = critical_memory_policy(load_settings())
        return store.get_stats(
            max_count=policy["max_count"],
            max_tokens=policy["max_tokens"],
        )
    return {"total_memories": 0, "unique_domains": 0, "by_category": {}}


@app.get("/memory/domains")
async def get_memory_domains():
    store = _get_memory_store()
    if store:
        return store.get_all_domains()
    return []


@app.get("/memory/domain/{domain}")
async def get_memories_for_domain(domain: str, limit: int = Query(50)):
    store = _get_memory_store()
    if store:
        return store.search(website_domain=domain, success_only=False, limit=limit)
    return []


@app.get("/memory/search")
async def search_memories(q: str = Query(""), limit: int = Query(50)):
    store = _get_memory_store()
    if not store or not q:
        return []
    # Simple text search across all memories
    all_mems = store.export_all()
    q_lower = q.lower()
    results = [m for m in all_mems if q_lower in m.get("content", "").lower() or q_lower in m.get("website_domain", "").lower()]
    return results[:limit]


@app.post("/memory/decay")
async def decay_memories(body: DecayRequest):
    store = _get_memory_store()
    if store:
        affected = store.decay_confidence(days_old=body.days, decay_factor=body.factor)
        return {"success": True, "affected": affected}
    return {"success": False, "message": "Memory store not available"}


@app.post("/memory/cleanup")
async def cleanup_memories(body: CleanupRequest):
    store = _get_memory_store()
    if store:
        deleted = store.delete_low_confidence(threshold=body.threshold)
        return {"success": True, "deleted": deleted}
    return {"success": False, "message": "Memory store not available"}


@app.get("/memory/export")
async def export_memories():
    store = _get_memory_store()
    if store:
        return store.export_all()
    return []


@app.get("/memory/critical-settings")
async def get_critical_memory_settings():
    from memory.store import critical_memory_policy

    return critical_memory_policy(load_settings())


@app.put("/memory/critical-settings")
async def update_critical_memory_settings(body: CriticalMemorySettingsRequest):
    settings = {
        "memory_auto_rerank": body.auto_rerank,
        "critical_memory_max_count": body.max_count,
        "critical_memory_max_tokens": body.max_tokens,
    }
    save_settings(settings)
    store = _get_memory_store()
    trimmed = (
        store.enforce_critical_limits(
            max_count=body.max_count,
            max_tokens=body.max_tokens,
        )
        if store
        else {"selected": 0, "scopes": 0, "estimated_tokens": 0}
    )
    return {
        "success": True,
        "auto_rerank": body.auto_rerank,
        "max_count": body.max_count,
        "max_tokens": body.max_tokens,
        "selected": trimmed["selected"],
    }


@app.post("/memory/rerank-critical")
async def rerank_critical_memories():
    """Use Hunter's saved classifier key to replace the bounded critical sets."""
    store = _get_memory_store()
    if not store:
        return _api_error("memory_unavailable", "Memory store is not available", 503)
    memories = [memory for memory in store.export_all() if memory.get("success")]
    if not memories:
        return {
            "success": True,
            "selected": 0,
            "scopes": 0,
            "estimated_tokens": 0,
            "model": "gpt-5.4-mini",
        }
    api_key = str(
        ((load_llm_settings().get("openai") or {}).get("api_key") or "")
    ).strip()
    if not api_key:
        return _api_error(
            "openai_key_missing",
            "Save an OpenAI API key in Settings before re-ranking memories",
        )
    try:
        from memory.ranker import MEMORY_RANKER_MODEL, rerank_critical_scopes
        from memory.store import critical_memory_policy

        policy = critical_memory_policy(load_settings())
        result = await rerank_critical_scopes(
            store,
            {store.memory_scope(memory) for memory in memories},
            api_key=api_key,
            max_count=policy["max_count"],
            max_tokens=policy["max_tokens"],
        )
        return {"success": True, "model": MEMORY_RANKER_MODEL, **result}
    except Exception as error:
        from job_classifier import safe_error_message

        _log.warning("Critical memory re-ranking failed: %s", safe_error_message(error))
        return _api_error(
            "memory_rerank_failed",
            safe_error_message(error),
            502,
        )


# ── Profile: Parse Resume ─────────────────────────────────────────────────
@app.post("/profile/parse-resume")
async def parse_resume_to_profile():
    """Read the user's resume PDF and turn it into the editable Markdown profile."""

    settings = load_settings()
    resume_path = settings.get("resume_path", "").strip()
    if not resume_path:
        return {"success": False, "message": "No resume path configured. Set it in Settings first."}

    resume_file = Path(resume_path).resolve()
    if not resume_file.exists():
        return {"success": False, "message": f"Resume file not found: {resume_path}"}
    if resume_file.suffix.lower() != ".pdf":
        return {"success": False, "message": "Resume must be a PDF file."}
    home = Path.home()
    if not (str(resume_file).startswith(str(home)) or str(resume_file).startswith("/tmp")):
        return {"success": False, "message": "Resume path must be within your home directory."}

    try:
        import fitz  # pymupdf
        doc = fitz.open(str(resume_file))
        resume_text = ""
        for page in doc:
            resume_text += page.get_text()
        doc.close()
        resume_text = resume_text.strip()
        if not resume_text:
            return {"success": False, "message": "Could not extract any text from the resume PDF."}
    except Exception as e:
        return {"success": False, "message": f"Failed to read resume PDF: {e}"}

    # 2. Ask the LLM to format the resume as the editable Markdown profile.
    llm_settings = load_llm_settings()
    if not llm_settings.get("provider"):
        return {"success": False, "message": "No LLM provider configured. Set it in LLM Settings first."}

    try:
        from core.llm_factory import create_llm
        llm = create_llm(llm_settings)
    except Exception as e:
        return {"success": False, "message": f"Failed to create LLM: {e}"}

    prompt = f"""Convert this resume into a complete candidate profile in Markdown.

Use these premade top-level sections in this order:
# Personal Details
# Professional Profile
# Experience
# Education
# Skills
# Work Preferences
# Application Materials
# Additional Notes

Rules:
- Preserve all factual names, contact details, employers, titles, dates, education, skills, and achievements.
- Include the full work history, not only the latest role.
- Do not invent missing facts.
- Use clear labels and bullet lists where useful.
- Return only Markdown, with no code fence and no introductory text.

RESUME TEXT:
{resume_text[:6000]}"""

    try:
        from browser_use.llm.messages import UserMessage
        response = await llm.ainvoke([UserMessage(content=prompt)])
        response_text = response.completion if hasattr(response, 'completion') else (response.content if hasattr(response, 'content') else str(response))
        markdown = response_text.strip()
        if markdown.startswith("```"):
            markdown = markdown.removeprefix("```markdown").removeprefix("```md").removeprefix("```")
            markdown = markdown.removesuffix("```").strip()
        if not markdown:
            return {"success": False, "message": "LLM returned an empty profile. Try again."}

        save_profile_markdown(markdown)
        profile = {**load_profile(), "markdown": markdown}

        return {
            "success": True,
            "message": "Imported your resume into profile.md.",
            "profile": profile,
            "fields_filled": 1,
        }

    except Exception as e:
        return {"success": False, "message": f"LLM parsing failed: {e}"}


# ── Setup Status ──────────────────────────────────────────────────────────
@app.get("/setup/status")
async def get_setup_status():
    """Check which onboarding steps are complete."""
    profile = load_profile()
    llm = load_llm_settings()
    settings = load_settings()

    profile_done = (
        load_profile_markdown().strip() != PROFILE_MARKDOWN_TEMPLATE.strip()
    )
    llm_done = False
    provider = llm.get("provider", "")
    if provider == "openai":
        llm_done = bool((llm.get("openai") or {}).get("api_key", "").strip())
    elif provider == "anthropic":
        llm_done = bool((llm.get("anthropic") or {}).get("api_key", "").strip())
    elif provider == "bedrock":
        bedrock = llm.get("bedrock") or {}
        if bedrock.get("auth_mode") == "keys":
            llm_done = bool(bedrock.get("access_key", "").strip() and bedrock.get("secret_key", "").strip())
        else:
            # Profile mode — considered configured even with default profile
            llm_done = True
    elif provider == "openrouter":
        llm_done = bool((llm.get("openrouter") or {}).get("api_key", "").strip())
    elif provider == "ollama":
        ollama = llm.get("ollama") or {}
        llm_done = bool(ollama.get("base_url", "").strip())
    elif provider == "gemini":
        llm_done = bool((llm.get("gemini") or {}).get("api_key", "").strip())
    elif provider == "openai_compatible":
        compat = llm.get("openai_compatible") or {}
        llm_done = bool(compat.get("base_url", "").strip())

    resume_done = bool(settings.get("resume_path", "").strip())
    onboarding_completed = settings.get("onboarding_completed", False)

    # Check Chromium
    chromium_done = _find_playwright_chromium() is not None

    # Check login status
    auth = await auth_status()
    linkedin_done = auth.get("linkedin", {}).get("logged_in", False)
    gmail_done = auth.get("gmail", {}).get("logged_in", False)

    return {
        "profile": profile_done,
        "llm": llm_done,
        "resume": resume_done,
        "chromium": chromium_done,
        "linkedin": linkedin_done,
        "gmail": gmail_done,
        "onboarding_completed": onboarding_completed,
        "all_required_done": profile_done and llm_done and linkedin_done,
    }


@app.post("/setup/complete-onboarding")
async def complete_onboarding():
    """Mark the onboarding wizard as completed."""
    settings = load_settings()
    settings["onboarding_completed"] = True
    save_settings(settings)
    return {"success": True}


# ── Metrics / Dashboard ───────────────────────────────────────────────────
@app.get("/dashboard")
async def get_dashboard():
    stats = await get_job_stats()
    mem_stats = await get_memory_stats()

    # Try to get metrics data
    metrics_data = {}
    metrics = _get_metrics_store()
    if metrics:
        try:
            metrics_data = {
                "overall": metrics.get_overall_stats(),
                "memory_impact": metrics.get_memory_impact(),
                "domain_stats": metrics.get_domain_stats(),
                "trend": metrics.get_trend(window_size=5),
                "recent_runs": metrics.get_all_runs(limit=10),
            }
        except (OSError, ValueError) as e:
            _log.warning(f"Failed to load metrics: {e}")

    return {
        "jobs": stats,
        "memory": mem_stats,
        "metrics": metrics_data,
        "gmail_outcomes": _get_gmail_outcome_manager().status(),
    }


@app.get("/metrics/runs")
async def get_metric_runs(limit: int = Query(50)):
    metrics = _get_metrics_store()
    if metrics:
        return metrics.get_all_runs(limit=limit)
    return []


@app.get("/metrics/domains")
async def get_metric_domains():
    metrics = _get_metrics_store()
    if metrics:
        return metrics.get_domain_stats()
    return []


@app.get("/application-attempts/storage")
async def get_application_attempt_storage(
    video_retention_days: int = Query(
        VIDEO_RETENTION_DEFAULT_DAYS, ge=1, le=3650
    ),
    dossier_retention_days: int = Query(
        DOSSIER_RETENTION_DEFAULT_DAYS, ge=1, le=3650
    ),
):
    if dossier_retention_days < video_retention_days:
        return _api_error(
            "invalid_retention",
            "Full dossiers cannot be deleted before their video retention ends",
        )
    return await asyncio.to_thread(
        application_attempt_storage_report,
        video_retention_days=video_retention_days,
        dossier_retention_days=dossier_retention_days,
    )


@app.post("/application-attempts/retention/cleanup")
async def cleanup_application_attempt_storage(body: dict):
    try:
        video_retention_days = int(
            body.get("video_retention_days", VIDEO_RETENTION_DEFAULT_DAYS)
        )
        dossier_retention_days = int(
            body.get("dossier_retention_days", DOSSIER_RETENTION_DEFAULT_DAYS)
        )
    except (TypeError, ValueError):
        return _api_error("invalid_retention", "Retention values must be whole numbers")
    if (
        not 1 <= video_retention_days <= 3650
        or not video_retention_days <= dossier_retention_days <= 3650
    ):
        return _api_error(
            "invalid_retention",
            "Video retention must be 1–3650 days and full-dossier retention "
            "must be at least as long",
        )
    return await asyncio.to_thread(
        cleanup_application_attempts,
        video_retention_days=video_retention_days,
        dossier_retention_days=dossier_retention_days,
    )


def _attempt_artifact_path(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("path"), str):
        return value["path"]
    return None


def _attempt_file_entry(manifest: dict, label: str, path: str):
    indexed = (manifest.get("files") or {}).get(path) or {}
    return {
        "label": label,
        "path": path,
        "size_bytes": indexed.get("size_bytes"),
    }


@app.get("/application-attempts/{attempt_id}")
async def get_application_attempt(attempt_id: str):
    directory = find_attempt_directory(attempt_id)
    if directory is None:
        return _api_error("not_found", "Application audit dossier not found", 404)

    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        history_path = directory / "history.json"
        history = (
            json.loads(history_path.read_text(encoding="utf-8"))
            if history_path.is_file()
            else {}
        )
    except (OSError, json.JSONDecodeError) as exc:
        return _api_error("invalid_dossier", f"Could not read application dossier: {exc}", 500)

    history_items = history.get("history") or []
    first_start = next(
        (
            item.get("metadata", {}).get("step_start_time")
            for item in history_items
            if isinstance(item.get("metadata", {}).get("step_start_time"), (int, float))
        ),
        None,
    )
    steps = []
    for index, item in enumerate(history_items, start=1):
        state = item.get("state") or {}
        output = item.get("model_output") or {}
        start = (item.get("metadata") or {}).get("step_start_time")
        results = []
        for result in item.get("result") or []:
            if result.get("error"):
                results.append({"kind": "error", "text": str(result["error"])})
            elif result.get("extracted_content"):
                results.append({"kind": "result", "text": str(result["extracted_content"])})
            elif result.get("long_term_memory"):
                results.append({"kind": "result", "text": str(result["long_term_memory"])})
        steps.append(
            {
                "number": index,
                "elapsed_seconds": (
                    round(max(0.0, start - first_start), 1)
                    if isinstance(start, (int, float)) and isinstance(first_start, (int, float))
                    else None
                ),
                "url": state.get("url"),
                "evaluation": output.get("evaluation_previous_goal"),
                "next_goal": output.get("next_goal"),
                "actions": output.get("action") or [],
                "results": results,
                "screenshot": state.get("screenshot_path"),
            }
        )

    artifacts = manifest.get("artifacts") or {}
    screenshots = []
    for path in [
        _attempt_artifact_path(artifacts.get("final_screenshot")),
        *(artifacts.get("screenshots") or []),
    ]:
        if isinstance(path, str) and path not in screenshots and (directory / path).is_file():
            screenshots.append(path)

    files = []
    preferred_files = [
        ("Tailored CV", _attempt_artifact_path(artifacts.get("resume"))),
        ("Cover letter", _attempt_artifact_path(artifacts.get("cover_letter"))),
        ("Document generation audit", _attempt_artifact_path(artifacts.get("generation_audit"))),
        ("LinkedIn outreach", _attempt_artifact_path(artifacts.get("linkedin_outreach_message"))),
        ("Summary", "summary.md"),
        ("Timeline", _attempt_artifact_path(artifacts.get("timeline"))),
        ("Application log", _attempt_artifact_path(artifacts.get("log"))),
        ("Structured history", _attempt_artifact_path(artifacts.get("history"))),
        ("Review notes", "review.md"),
    ]
    for label, path in preferred_files:
        if isinstance(path, str) and (directory / path).is_file():
            files.append(_attempt_file_entry(manifest, label, path))

    recording = _attempt_artifact_path(artifacts.get("recording"))
    if not recording or not (directory / recording).is_file():
        recording = None

    generation_audit = None
    generation_audit_path = _attempt_artifact_path(artifacts.get("generation_audit"))
    if generation_audit_path and (directory / generation_audit_path).is_file():
        try:
            generation_audit = json.loads(
                (directory / generation_audit_path).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            generation_audit = None

    original_listing_text = None
    job_path = directory / "job.json"
    if job_path.is_file():
        try:
            archived_job = json.loads(job_path.read_text(encoding="utf-8"))
            description = (
                archived_job.get("description")
                if isinstance(archived_job, dict)
                else None
            )
            if isinstance(description, str) and description.strip():
                original_listing_text = description.strip()
        except (OSError, json.JSONDecodeError):
            pass

    return {
        "manifest": manifest,
        "steps": steps,
        "screenshots": screenshots,
        "recording": recording,
        "files": files,
        "generation_audit": generation_audit,
        "original_listing_text": original_listing_text,
    }


@app.get("/application-attempts/{attempt_id}/files/{file_path:path}")
async def get_application_attempt_file(attempt_id: str, file_path: str):
    from fastapi.responses import FileResponse

    directory = find_attempt_directory(attempt_id)
    if directory is None:
        return _api_error("not_found", "Application audit dossier not found", 404)

    root = directory.resolve()
    requested = (directory / file_path).resolve()
    if requested == root or root not in requested.parents or not requested.is_file():
        return _api_error("not_found", "Application audit file not found", 404)
    return FileResponse(requested, filename=requested.name, content_disposition_type="inline")


# ── Logs ──────────────────────────────────────────────────────────────────
@app.get("/logs/runs/{run_id}")
async def get_run_logs(run_id: str, limit: int = Query(500)):
    metrics = _get_metrics_store()
    if metrics:
        return metrics.get_run_logs(run_id, limit=limit)
    return []


@app.get("/logs/recent")
async def get_recent_logs(limit: int = Query(100)):
    metrics = _get_metrics_store()
    if metrics:
        return metrics.get_recent_logs(limit=limit)
    return []


@app.get("/logs/runs")
async def get_runs_with_logs(limit: int = Query(50)):
    metrics = _get_metrics_store()
    if metrics:
        return metrics.get_runs_with_logs(limit=limit)
    return []


# ── Q&A Repository ────────────────────────────────────────────────────────

@app.get("/qa")
async def get_qa(
    search: str = Query(""),
    folder: str = Query("all"),
    unanswered: bool = Query(False),
):
    store = _get_memory_store()
    if not store:
        return []
    if folder not in ("all", "reviewed", "to_review", "unanswered"):
        return _api_error("invalid_folder", "Invalid Q&A folder", 400)
    return store.qa_list(search=search, folder="unanswered" if unanswered else folder)

@app.get("/qa/stats")
async def get_qa_stats():
    store = _get_memory_store()
    if not store:
        return {"total": 0, "answered": 0, "unanswered": 0}
    return store.qa_stats()

@app.put("/qa/{qa_id}")
async def update_qa(qa_id: int, body: dict):
    store = _get_memory_store()
    if not store:
        return _api_error("no_store", "Memory store not available")
    if "answer" in body:
        store.qa_update(qa_id, body.get("answer") or "")
    if "reviewed" in body:
        store.qa_set_reviewed(qa_id, bool(body["reviewed"]))
    return {"success": True}

@app.delete("/qa/{qa_id}")
async def delete_qa(qa_id: int):
    store = _get_memory_store()
    if not store:
        return _api_error("no_store", "Memory store not available")
    store.qa_delete(qa_id)
    return {"success": True}

@app.post("/qa/{source_id}/merge/{target_id}")
async def merge_qa(source_id: int, target_id: int):
    store = _get_memory_store()
    if not store:
        return _api_error("no_store", "Memory store not available")
    store.qa_merge(source_id, target_id)
    return {"success": True}

@app.post("/qa/auto-squash")
async def auto_squash_qa():
    store = _get_memory_store()
    if not store:
        return _api_error("no_store", "Memory store not available")
    merged = store.qa_auto_squash()
    return {"success": True, "merged": merged}

@app.post("/qa/smart-squash")
async def smart_squash_qa():
    """LLM-based semantic deduplication of Q&A questions."""
    store = _get_memory_store()
    if not store:
        return _api_error("no_store", "Memory store not available")
    questions = store.qa_list()
    if len(questions) < 2:
        return {"success": True, "merged": 0}

    from core.llm_factory import create_llm
    llm_settings = load_llm_settings()
    if not llm_settings.get("provider"):
        return _api_error("no_llm", "No LLM configured. Set up an LLM provider in Settings first.")

    llm = create_llm(llm_settings)
    q_list = "\n".join(f"[{q['id']}] {q['question']}" for q in questions)
    prompt = (
        "Below is a numbered list of screening questions from job applications. "
        "Find groups of questions that ask the same thing in different words. "
        "Return ONLY a JSON array of merge instructions: [{\"keep\": <id_to_keep>, \"merge\": [<ids_to_merge>]}, ...]. "
        "Only group questions that are truly semantically identical. If no duplicates exist, return [].\n\n"
        f"{q_list}"
    )
    try:
        from browser_use.llm.messages import UserMessage
        response = await llm.ainvoke([UserMessage(content=prompt)])
        import json as _json
        text = response.completion if hasattr(response, "completion") else (response.content if hasattr(response, "content") else str(response))
        # Extract JSON from response
        import re as _re_mod
        match = _re_mod.search(r"\[.*\]", text, _re_mod.DOTALL)
        if not match:
            return {"success": True, "merged": 0}
        groups = _json.loads(match.group(0))
        merged_count = 0
        for group in groups:
            keep_id = group.get("keep")
            merge_ids = group.get("merge", [])
            for mid in merge_ids:
                store.qa_merge(mid, keep_id)
                merged_count += 1
        return {"success": True, "merged": merged_count}
    except Exception as e:
        return _api_error("llm_error", f"Smart squash failed: {str(e)}", 500)


# ── Resume Tailoring ──────────────────────────────────────────────────────

@app.post("/resume/tailor")
async def tailor_resumes(body: dict):
    """Generate tailored resumes for one or more jobs."""
    from resume.tailor import tailor_resume
    from core.shared_config import read_jobs, update_job

    job_urls = body.get("job_urls", [])
    options = {
        "skills": True,
        "overview": True,
        "experience": True,
        "title": True,
        "achievements": True,
    }

    if not job_urls:
        return _api_error("missing_field", "job_urls is required", 400)

    jobs = read_jobs()
    results = []

    for url in job_urls:
        job = jobs.get(url)
        if not job:
            results.append({"url": url, "status": "error", "message": "Job not found"})
            continue
        description = job.get("description", "")
        if not description:
            # Fallback: use title + company + location as context for tailoring
            title = job.get("title", "")
            company = job.get("company", "")
            location = job.get("location", "")
            if not title:
                results.append({"url": url, "status": "error", "message": "No job description or title available"})
                continue
            description = f"Job Title: {title}\nCompany: {company}\nLocation: {location}\n\nTailor the resume for this role based on the job title and company."
        try:
            result = await tailor_resume(
                url,
                description,
                options,
                str(job.get("title") or ""),
            )
            update_job(url, tailored_resume_path=result["path"])
            results.append({"url": url, "status": "done", "path": result["path"], "content": result["content"]})
        except Exception as e:
            results.append({"url": url, "status": "error", "message": str(e)})

    return {"success": True, "results": results}


@app.post("/resume/tailor/refine")
async def refine_tailored_resume(body: dict):
    """Refine an existing tailored resume with additional instructions."""
    from resume.tailor import tailor_resume
    from core.shared_config import read_jobs, update_job

    job_url = body.get("job_url", "")
    instruction = body.get("instruction", "")

    if not job_url:
        return _api_error("missing_field", "job_url is required", 400)

    jobs = read_jobs()
    job = jobs.get(job_url)
    if not job:
        return _api_error("not_found", "Job not found", 404)

    description = job.get("description", "")
    options = body.get("options", {
        "skills": True,
        "overview": False,
        "experience": True,
        "title": True,
        "achievements": True,
    })

    try:
        result = await tailor_resume(
            job_url,
            description + f"\n\nADDITIONAL INSTRUCTION: {instruction}",
            options,
            str(job.get("title") or ""),
        )
        update_job(job_url, tailored_resume_path=result["path"])
        return {"success": True, "content": result["content"], "path": result["path"]}
    except Exception as e:
        return _api_error("tailor_error", str(e), 500)


@app.post("/resume/tailor/get-by-url")
async def get_tailored_by_url(body: dict):
    """Get tailored resume content by job URL."""
    from resume.tailor import get_tailored_content
    job_url = body.get("job_url", "")
    if not job_url:
        return _api_error("missing_field", "job_url is required", 400)
    result = get_tailored_content(job_url)
    if not result:
        return _api_error("not_found", "No tailored resume for this job", 404)
    return {"success": True, "content": result["content"], "path": result["path"]}


@app.get("/resume/tailor/pdf-by-url")
async def get_tailored_pdf_by_url(job_url: str = Query(...)):
    """Serve a generated resume to the browser development UI."""
    from fastapi.responses import FileResponse
    from resume.tailor import get_tailored_resume_path

    pdf_path = get_tailored_resume_path(job_url)
    if not pdf_path:
        return _api_error("not_found", "No tailored resume for this job", 404)
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=Path(pdf_path).name,
        content_disposition_type="inline",
    )


@app.get("/resume/tailor/{url_hash}")
async def get_tailored_resume(url_hash: str):
    """Get tailored resume content by URL hash."""
    from resume.tailor import _get_tailored_dir
    md_path = _get_tailored_dir() / f"{url_hash}.md"
    artifact_dir = _get_tailored_dir() / url_hash
    human_paths = sorted(artifact_dir.glob("Candidate_*.pdf"))
    pdf_path = (
        human_paths[0]
        if human_paths
        else _get_tailored_dir() / f"{url_hash}.pdf"
    )
    if not md_path.exists():
        return _api_error("not_found", "No tailored resume found", 404)
    return {
        "success": True,
        "content": md_path.read_text(encoding="utf-8"),
        "path": str(pdf_path) if pdf_path.exists() else None,
    }


@app.delete("/resume/tailor/{url_hash}")
async def delete_tailored(url_hash: str):
    """Delete a tailored resume."""
    from resume.tailor import _get_tailored_dir
    md_path = _get_tailored_dir() / f"{url_hash}.md"
    pdf_path = _get_tailored_dir() / f"{url_hash}.pdf"
    cover_letter_path = _get_tailored_dir() / f"{url_hash}.cover-letter.txt"
    artifact_dir = _get_tailored_dir() / url_hash
    if md_path.exists():
        md_path.unlink()
    if pdf_path.exists():
        pdf_path.unlink()
    if cover_letter_path.exists():
        cover_letter_path.unlink()
    if artifact_dir.exists():
        for artifact in artifact_dir.iterdir():
            if artifact.is_file():
                artifact.unlink()
        artifact_dir.rmdir()
    return {"success": True}


# ── Cover Letter Generation ───────────────────────────────────────────────

@app.post("/cover-letter/generate")
async def generate_cover_letter(body: dict):
    """Generate a tailored cover letter using LLM based on job description + profile."""
    job_description = body.get("job_description", "").strip()
    job_title = body.get("job_title", "")
    company = body.get("company", "")
    job_url = body.get("job_url", "").strip()

    if not job_description:
        return _api_error("missing_field", "Job description is required", 400)

    try:
        from cover_letter import (
            generate_cover_letter_text,
            save_cover_letter_pdf,
            save_cover_letter_text,
        )

        text = await generate_cover_letter_text(
            job_description,
            job_title,
            company,
        )
        cover_letter_path = ""
        cover_letter_pdf_path = ""
        if job_url:
            from core.shared_config import update_job

            cover_letter_path = str(
                save_cover_letter_text(job_url, text, job_title)
            )
            cover_letter_pdf_path = str(
                save_cover_letter_pdf(job_url, text, job_title)
            )
            update_job(
                job_url,
                cover_letter=text,
                cover_letter_path=cover_letter_path,
                cover_letter_pdf_path=cover_letter_pdf_path,
            )
        return {
            "success": True,
            "cover_letter": text,
            "cover_letter_path": cover_letter_path,
            "cover_letter_pdf_path": cover_letter_pdf_path,
        }
    except ValueError as e:
        if str(e) == "No LLM configured":
            return _api_error(
                "no_llm",
                "No LLM configured. Set up an LLM provider in Settings first.",
            )
        return _api_error("llm_error", f"Cover letter generation failed: {e}", 500)
    except Exception as e:
        return _api_error("llm_error", f"Cover letter generation failed: {str(e)}", 500)


def _ensure_cover_letter_pdf(job_url: str) -> Path | None:
    """Find the PDF or lazily render one for an older text-only letter."""
    from cover_letter import (
        get_cover_letter_path,
        get_cover_letter_pdf_path,
        save_cover_letter_pdf,
    )
    from core.shared_config import read_jobs, update_job

    pdf_path = get_cover_letter_pdf_path(job_url)
    if pdf_path:
        return pdf_path
    text_path = get_cover_letter_path(job_url)
    if not text_path:
        return None
    job = read_jobs().get(job_url, {})
    pdf_path = save_cover_letter_pdf(
        job_url,
        text_path.read_text(encoding="utf-8"),
        str(job.get("title") or ""),
    )
    update_job(job_url, cover_letter_pdf_path=str(pdf_path))
    return pdf_path


@app.get("/cover-letter/file-by-url")
async def get_cover_letter_file_by_url(job_url: str = Query(...)):
    """Serve the actual saved cover-letter PDF to the browser UI."""
    from fastapi.responses import FileResponse

    cover_letter_path = _ensure_cover_letter_pdf(job_url)
    if not cover_letter_path:
        return _api_error("not_found", "No saved cover letter for this job", 404)
    return FileResponse(
        cover_letter_path,
        media_type="application/pdf",
        filename=cover_letter_path.name,
        content_disposition_type="inline",
    )


# ── Countries & Plugins ───────────────────────────────────────────────────

from core.country_config import COUNTRY_CONFIGS, get_country_config, NOTICE_PERIOD_OPTIONS

try:
    from models import PluginToggleRequest
except ImportError:
    from backend.models import PluginToggleRequest

from sources.registry import PluginRegistry

_plugin_registry: PluginRegistry | None = None


def _get_plugin_registry() -> PluginRegistry:
    global _plugin_registry
    if _plugin_registry is None:
        _plugin_registry = PluginRegistry()
    return _plugin_registry


@app.get("/countries")
async def get_countries():
    """Return all supported country configurations."""
    return {"success": True, "countries": COUNTRY_CONFIGS, "notice_period_options": NOTICE_PERIOD_OPTIONS}


@app.get("/countries/{code}")
async def get_country(code: str):
    """Get config for a specific country."""
    if code not in COUNTRY_CONFIGS:
        return _api_error("not_found", f"Country code '{code}' not supported", 404)
    return {"success": True, "config": COUNTRY_CONFIGS[code]}


@app.get("/plugins")
async def get_plugins(country: Optional[str] = Query(None)):
    """List all plugins, optionally filtered by country."""
    registry = _get_plugin_registry()
    if country:
        plugins = registry.get_for_country(country)
    else:
        plugins = registry.get_all()
    return {
        "success": True,
        "plugins": [
            {
                "name": p.name,
                "display_name": p.display_name,
                "version": p.version,
                "author": p.author,
                "description": p.description,
                "countries": p.countries,
                "website": p.website,
                "requires_login": p.requires_login,
                "login_url": p.login_url,
                "is_builtin": p.is_builtin,
                "enabled": p.enabled,
                "filters": [
                    {
                        "key": f.key,
                        "label": f.label,
                        "type": f.type,
                        "options": f.options,
                        "url_param": f.url_param,
                        "default": f.default,
                    }
                    for f in p.filters
                ],
            }
            for p in plugins
        ],
    }


@app.post("/plugins/import")
async def import_plugin(request: Request):
    """Import a community plugin uploaded by the browser."""
    encoded_name = request.headers.get("x-hunter-filename", "")
    filename = Path(unquote(encoded_name)).name
    if not filename or Path(filename).suffix.lower() not in {".yaml", ".yml"}:
        return _api_error("import_failed", "Plugin must be a .yaml or .yml file", 400)
    if _upload_exceeds_limit(request, MAX_PLUGIN_UPLOAD_BYTES):
        return _api_error("import_failed", "Plugin file must be 1 MB or smaller", 413)
    data = await request.body()
    if not data or len(data) > MAX_PLUGIN_UPLOAD_BYTES:
        return _api_error("import_failed", "Plugin file must be 1 MB or smaller", 413)

    registry = _get_plugin_registry()
    try:
        data_dir = get_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="hunter-plugin-", dir=data_dir) as directory:
            upload_path = Path(directory) / filename
            upload_path.write_bytes(data)
            plugin = registry.import_plugin(str(upload_path))
        return {"success": True, "plugin": {"name": plugin.name, "display_name": plugin.display_name}}
    except (FileNotFoundError, ValueError) as e:
        return _api_error("import_failed", str(e), 400)


@app.put("/plugins/{name}/toggle")
async def toggle_plugin(name: str, body: PluginToggleRequest):
    """Enable or disable a plugin."""
    registry = _get_plugin_registry()
    if not registry.set_enabled(name, body.enabled):
        return _api_error("not_found", f"Plugin '{name}' not found", 404)
    return {"success": True, "name": name, "enabled": body.enabled}


@app.delete("/plugins/{name}")
async def remove_plugin(name: str):
    """Remove a community plugin."""
    registry = _get_plugin_registry()
    try:
        registry.remove_plugin(name)
        return {"success": True}
    except ValueError as e:
        return _api_error("remove_failed", str(e), 400)


@app.post("/plugins/reload")
async def reload_plugins():
    """Reload all plugins from disk."""
    registry = _get_plugin_registry()
    registry.reload()
    return {"success": True, "count": len(registry.get_all())}


# ── Hosted web frontend ─────────────────────────────────────────────────
def _hosted_web_enabled() -> bool:
    return os.environ.get("HUNTER_WEB_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _hosted_web_dist_dir() -> Path:
    configured = os.environ.get("HUNTER_WEB_DIST_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    deployed = get_data_dir() / "web" / "current"
    if (deployed / "index.html").is_file():
        return deployed.resolve()
    return (PROJECT_ROOT / "dist").resolve()


def _hosted_web_file(path: Path, *, immutable: bool = False):
    if not path.is_file():
        return _api_error("web_frontend_unavailable", "Hunter web frontend is not built", 503)
    cache_control = "public, max-age=31536000, immutable" if immutable else "no-cache"
    return FileResponse(path, headers={"Cache-Control": cache_control})


@app.get("/", include_in_schema=False)
async def hosted_web_root():
    if not _hosted_web_enabled():
        return _api_error("not_found", "Not found", 404)
    return _hosted_web_file(_hosted_web_dist_dir() / "index.html")


@app.get("/{web_path:path}", include_in_schema=False)
async def hosted_web_path(web_path: str):
    """Serve hashed assets or the SPA shell after all API routes are considered."""
    if not _hosted_web_enabled():
        return _api_error("not_found", "Not found", 404)

    dist_dir = _hosted_web_dist_dir()
    requested = (dist_dir / web_path).resolve()
    if requested.is_relative_to(dist_dir) and requested.is_file():
        return _hosted_web_file(
            requested,
            immutable=web_path.startswith("assets/"),
        )
    if "." in Path(web_path).name:
        return _api_error("not_found", "Frontend asset not found", 404)
    return _hosted_web_file(dist_dir / "index.html")


# ── Main ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    _log.info(f"Starting uvicorn on port {port} (pid={os.getpid()}, ppid={os.getppid()})")

    # Never kill an unknown listener. A second Hunter instance may interrupt an
    # application whose submission outcome still needs to be finalized.
    try:
        _assert_port_available(port)
    except OSError as exc:
        _log.error(
            "Port %s is already in use; refusing to replace the active listener: %s",
            port,
            exc,
        )
        raise SystemExit(1) from exc
    _log.info(f"Port {port} is available")

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
