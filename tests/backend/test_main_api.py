"""
Industry-grade API / integration tests for the LangHire FastAPI backend.

These exercise ``backend/main.py`` in-process with FastAPI's ``TestClient``
(no real HTTP server, no subprocess, no browser, no network). Heavy
dependencies (LLM creation, resume tailoring, Playwright) are monkeypatched so
only the request/response and persistence layers are exercised.

Auth model (see ``_AuthMiddleware`` in ``backend/main.py``):
  * ``/health`` and ``/chromium/status`` are unauthenticated.
  * Every other path requires ``Authorization: Bearer <_API_TOKEN>``.
  * If an ``Origin`` header is present it must be in ``_ALLOWED_ORIGINS``,
    otherwise the request is rejected with 403 *before* the token is checked.

``_API_TOKEN`` is captured from ``$JOB_APPLICANT_TOKEN`` (or a random secret)
*at import time*, so we set that env var before importing ``main`` and reuse
the value the module actually loaded.
"""
import json
import logging
import os
import socket
import threading
from pathlib import Path

# Pin the auth token BEFORE importing the backend so the middleware uses a
# value we know. main.py reads JOB_APPLICANT_TOKEN at import time.
os.environ.setdefault("JOB_APPLICANT_TOKEN", "test-token-deadbeef")

import pytest
from fastapi.testclient import TestClient

import main  # noqa: E402  (import after env var is set)

TOKEN = main._API_TOKEN
AUTH = {"Authorization": f"Bearer {TOKEN}"}


# ── Fixtures ────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _isolate_state(data_dir, monkeypatch):
    """Repoint stateful module-level paths at the per-test tmp data dir.

    Several backend modules resolve their storage paths *once at import time*
    from the real ``$HOME`` (``core.shared_config.JOBS_FILE``,
    ``memory.store.DB_PATH``, ``memory.metrics.DB_PATH``). Because ``main`` is
    imported at module load — before the ``data_dir`` fixture repoints HOME —
    those constants still point at the developer's real data dir. We rebind
    them here so every test reads and writes the same isolated tmp storage and
    never touches (or asserts against) real local data.
    """
    import core.shared_config as shared_config
    import memory.store as mem_store
    import memory.metrics as mem_metrics

    jobs_file = data_dir / "jobs.json"
    monkeypatch.setattr(shared_config, "DATA_DIR", data_dir)
    monkeypatch.setattr(shared_config, "JOBS_FILE", jobs_file)
    monkeypatch.setattr(shared_config, "JOBS_LOCK", data_dir / "jobs.db.lock")
    monkeypatch.setattr(shared_config, "QA_FILE", data_dir / "qa_repository.json")
    monkeypatch.setattr(
        shared_config, "CANDIDATE_PROFILE", data_dir / "candidate_profile.json"
    )

    db_path = data_dir / "memory_store.db"
    monkeypatch.setattr(mem_store, "DB_PATH", db_path)
    monkeypatch.setattr(mem_metrics, "DB_PATH", db_path)
    # The store/metrics default arg captured the old DB_PATH at class-definition
    # time, and main._get_*_store() construct them with no args. Force both
    # helpers to build instances against the isolated db.
    monkeypatch.setattr(main, "_get_memory_store", lambda: mem_store.MemoryStore(db_path))
    monkeypatch.setattr(
        main, "_get_metrics_store", lambda: mem_metrics.MetricsStore(db_path)
    )
    monkeypatch.setattr(main, "_gmail_outcome_manager", None)
    main._manual_browser_stop.clear()
    main._manual_browser_processes.clear()
    main._manual_browser_status.update(
        {"state": "stopped", "message": None, "started_at": None}
    )
    main._manual_browser_thread = None


@pytest.fixture
def client(data_dir):
    """A TestClient bound to the FastAPI app with an isolated data dir.

    ``data_dir`` (from conftest) repoints HOME at a tmp dir so all config /
    profile / settings / memory writes go to throwaway storage. We do NOT enter
    the TestClient context manager because that runs the app lifespan, which
    kills browser processes, spawns a parent watchdog and triggers a background
    Chromium install — none of which the API tests need.
    """
    return TestClient(main.app)


@pytest.fixture
def auth_client(client):
    """A client whose every request carries the valid bearer token."""
    client.headers.update(AUTH)
    return client


class TestWorkerInfrastructure:
    def test_port_check_refuses_an_existing_listener(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]

            with pytest.raises(OSError):
                main._assert_port_available(port)

    def test_worker_drains_tasks_and_does_not_capture_other_threads(self):
        ready = threading.Event()
        release = threading.Event()
        lingering_task_cancelled = threading.Event()
        status = {"log": []}

        async def work():
            import asyncio

            async def linger():
                try:
                    await asyncio.Event().wait()
                finally:
                    lingering_task_cancelled.set()

            asyncio.create_task(linger())
            print("worker-only-output")
            ready.set()
            await asyncio.to_thread(release.wait, 2)

        thread = main._run_async_in_thread(work, status)
        assert ready.wait(timeout=2)
        print("outside-worker-output")
        logging.getLogger("outside-worker").warning("outside-worker-log")
        release.set()
        thread.join(timeout=5)

        assert not thread.is_alive()
        assert lingering_task_cancelled.is_set()
        assert any("worker-only-output" in line for line in status["log"])
        assert not any("outside-worker" in line for line in status["log"])
        assert status["running"] is False


# ── Health & Chromium (unauthenticated) ─────────────────────────────────────
class TestHealthAndChromium:
    """Endpoints that the auth middleware lets through without a token."""

    def test_health_no_auth_required(self, client):
        """/health responds 200 even without an Authorization header."""
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] in ("ok", "degraded")
        assert "checks" in body
        for key in ("database", "chromium", "llm_configured", "worker_running"):
            assert key in body["checks"]

    def test_chromium_status_no_auth_required(self, client):
        """/chromium/status is reachable without a token and reports a known state."""
        resp = client.get("/chromium/status")
        assert resp.status_code == 200
        state = resp.json().get("state")
        assert state in ("ready", "installed", "installing", "not_installed", "checking", "failed")


class TestSystemStatus:
    def test_system_status_requires_auth(self, client):
        resp = client.get("/system/status")
        assert resp.status_code == 401

    def test_system_status_returns_current_machine_snapshot(self, auth_client, monkeypatch):
        snapshot = {
            "collected_at": "2026-08-21T12:00:00+00:00",
            "cpu": {"percent": 18.5},
            "memory": {
                "percent": 52.0,
                "used_bytes": 2_000,
                "total_bytes": 4_000,
                "available_bytes": 1_920,
            },
            "swap": {"percent": 10.0, "used_bytes": 100, "total_bytes": 1_000},
            "disk": {
                "percent": 20.0,
                "used_bytes": 20_000,
                "total_bytes": 100_000,
                "free_bytes": 80_000,
                "hunter_data_bytes": 6_000,
            },
            "temperature": {
                "available": True,
                "celsius": 58.0,
                "sensor": "CPU package",
                "high_celsius": 80.0,
                "critical_celsius": 95.0,
            },
            "headroom": {
                "state": "healthy",
                "summary": "Resources are within a safe range for browser work.",
            },
        }
        monkeypatch.setattr(main, "collect_system_status", lambda: snapshot)

        resp = auth_client.get("/system/status")

        assert resp.status_code == 200
        assert resp.json() == snapshot


# ── Auth / middleware ────────────────────────────────────────────────────────
class TestAuth:
    """Authorization, origin and rate-limit behaviour of the middleware."""

    def test_protected_endpoint_rejects_without_token(self, client):
        """A protected endpoint returns 401 when no token is supplied."""
        resp = client.get("/profile")
        assert resp.status_code == 401
        assert resp.json() == {"error": "unauthorized"}

    def test_protected_endpoint_rejects_bad_token(self, client):
        """A wrong bearer token is rejected with 401."""
        resp = client.get("/profile", headers={"Authorization": "Bearer nope"})
        assert resp.status_code == 401

    def test_protected_endpoint_accepts_valid_token(self, auth_client):
        """The valid bearer token lets the request through."""
        resp = auth_client.get("/profile")
        assert resp.status_code == 200

    def test_disallowed_origin_is_forbidden(self, client):
        """A non-allowlisted Origin is blocked with 403 before the token check."""
        resp = client.get(
            "/profile",
            headers={"Origin": "https://evil.example.com", **AUTH},
        )
        assert resp.status_code == 403
        assert resp.json() == {"error": "forbidden"}

    def test_allowed_origin_passes(self, auth_client):
        """An allowlisted Origin combined with a valid token succeeds."""
        resp = auth_client.get("/profile", headers={"Origin": "http://localhost:1420"})
        assert resp.status_code == 200

    def test_health_bypasses_origin_check(self, client):
        """/health is exempt from both the origin and token checks."""
        resp = client.get("/health", headers={"Origin": "https://evil.example.com"})
        assert resp.status_code == 200

    def test_tailscale_owner_can_use_api_through_loopback_serve(
        self, monkeypatch
    ):
        monkeypatch.setenv("HUNTER_TAILSCALE_USER", "candidate@example.com")
        monkeypatch.setenv(
            "HUNTER_WEB_ORIGIN", "https://hunter.example.ts.net"
        )
        web_client = TestClient(
            main.app,
            base_url="https://hunter.example.ts.net",
            client=("127.0.0.1", 50000),
            headers={"Tailscale-User-Login": "candidate@example.com"},
        )

        assert web_client.get("/profile").status_code == 200
        assert web_client.post(
            "/setup/complete-onboarding",
            headers={"Origin": "https://hunter.example.ts.net"},
        ).status_code == 200

    def test_tailscale_web_rejects_unsafe_request_without_same_origin(
        self, monkeypatch
    ):
        monkeypatch.setenv("HUNTER_TAILSCALE_USER", "candidate@example.com")
        monkeypatch.setenv(
            "HUNTER_WEB_ORIGIN", "https://hunter.example.ts.net"
        )
        web_client = TestClient(
            main.app,
            base_url="https://hunter.example.ts.net",
            client=("127.0.0.1", 50000),
            headers={"Tailscale-User-Login": "candidate@example.com"},
        )

        missing_origin = web_client.post("/setup/complete-onboarding")
        wrong_origin = web_client.post(
            "/setup/complete-onboarding",
            headers={"Origin": "https://evil.example.com"},
        )

        assert missing_origin.status_code == 403
        assert wrong_origin.status_code == 403

    def test_tailscale_web_rejects_wrong_host_or_user(self, monkeypatch):
        monkeypatch.setenv("HUNTER_TAILSCALE_USER", "candidate@example.com")
        monkeypatch.setenv(
            "HUNTER_WEB_ORIGIN", "https://hunter.example.ts.net"
        )
        wrong_host = TestClient(
            main.app,
            base_url="https://other.example.ts.net",
            headers={"Tailscale-User-Login": "candidate@example.com"},
        )
        other_user = TestClient(
            main.app,
            base_url="https://hunter.example.ts.net",
            client=("127.0.0.1", 50000),
            headers={"Tailscale-User-Login": "other@example.com"},
        )

        assert wrong_host.get("/profile").status_code == 401
        assert other_user.get("/profile").status_code == 401


class TestHostedWebFrontend:
    def test_serves_index_assets_and_spa_routes(self, monkeypatch, tmp_path):
        dist = tmp_path / "dist"
        assets = dist / "assets"
        assets.mkdir(parents=True)
        (dist / "index.html").write_text("<main>Hunter web</main>")
        (assets / "app-123.js").write_text("console.log('hunter')")
        monkeypatch.setenv("HUNTER_WEB_ENABLED", "true")
        monkeypatch.setenv("HUNTER_WEB_DIST_DIR", str(dist))
        monkeypatch.setenv("HUNTER_TAILSCALE_USER", "candidate@example.com")
        monkeypatch.setenv(
            "HUNTER_WEB_ORIGIN", "https://hunter.example.ts.net"
        )
        web_client = TestClient(
            main.app,
            base_url="https://hunter.example.ts.net",
            client=("127.0.0.1", 50000),
            headers={"Tailscale-User-Login": "candidate@example.com"},
        )

        root = web_client.get("/")
        asset = web_client.get("/assets/app-123.js")
        spa_route = web_client.get("/analytics")
        jobs_api = web_client.get("/jobs")
        missing_asset = web_client.get("/assets/missing.js")

        assert root.status_code == 200
        assert root.text == "<main>Hunter web</main>"
        assert root.headers["cache-control"] == "no-cache"
        assert asset.status_code == 200
        assert "immutable" in asset.headers["cache-control"]
        assert spa_route.status_code == 200
        assert spa_route.text == root.text
        assert jobs_api.status_code == 200
        assert jobs_api.json() == []
        assert missing_asset.status_code == 404

    def test_hosted_frontend_is_disabled_by_default(self, auth_client):
        response = auth_client.get("/")

        assert response.status_code == 404


# ── Profile CRUD roundtrip ────────────────────────────────────────────────────
class TestProfile:
    """GET/PUT /profile persistence."""

    def test_default_profile_shape(self, auth_client):
        """A fresh data dir returns the default profile skeleton."""
        body = auth_client.get("/profile").json()
        assert body["country"] == "US"
        assert isinstance(body["target_job_titles"], list)
        assert body["blacklisted_companies"] == ["Alignerr", "Outlier"]
        assert body["markdown"].startswith("# Personal Details")

    def test_profile_save_and_load_roundtrip(self, auth_client):
        """A saved profile is read back verbatim (key fields)."""
        profile = {
            "country": "IN",
            "target_job_titles": ["Software Engineer"],
            "target_locations": ["Bengaluru"],
            "blacklisted_companies": ["Example Corp"],
            "languages": ["English"],
            "visa_sponsorship_needed": False,
            "salary_expectation": {
                "min": 100000,
                "currency": "INR",
                "period": "monthly",
            },
            "markdown": "# Personal Details\nName: Test User\n",
        }
        save = auth_client.put("/profile", json=profile)
        assert save.status_code == 200
        assert save.json() == {"success": True}

        loaded = auth_client.get("/profile").json()
        assert loaded["country"] == "IN"
        assert loaded["target_job_titles"] == ["Software Engineer"]
        assert loaded["blacklisted_companies"] == ["Example Corp"]
        assert loaded["markdown"] == "# Personal Details\nName: Test User\n"
        assert "name" not in loaded

    def test_resume_upload_stores_pdf_and_updates_settings(self, auth_client, data_dir):
        import fitz

        document = fitz.open()
        document.new_page()
        pdf_bytes = document.tobytes()
        document.close()
        response = auth_client.post(
            "/profile/resume",
            content=pdf_bytes,
            headers={
                "Content-Type": "application/pdf",
                "X-Hunter-Filename": "resume.pdf",
            },
        )

        resume_path = data_dir / "resumes" / "resume.pdf"
        assert response.status_code == 200
        assert response.json() == {"success": True, "resume_path": str(resume_path)}
        assert resume_path.read_bytes() == pdf_bytes
        assert auth_client.get("/settings").json()["resume_path"] == str(resume_path)

    def test_resume_upload_rejects_non_pdf(self, auth_client):
        response = auth_client.post(
            "/profile/resume",
            content=b"plain text",
            headers={"X-Hunter-Filename": "resume.pdf"},
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_resume"

    def test_invalid_resume_does_not_replace_existing_pdf(self, auth_client, data_dir):
        resume_path = data_dir / "resumes" / "resume.pdf"
        resume_path.parent.mkdir(parents=True)
        resume_path.write_bytes(b"known-good-resume")

        response = auth_client.post(
            "/profile/resume",
            content=b"%PDF-1.4\nnot actually a PDF",
            headers={"X-Hunter-Filename": "resume.pdf"},
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_resume"
        assert resume_path.read_bytes() == b"known-good-resume"


# ── App settings CRUD ─────────────────────────────────────────────────────────
class TestAppSettings:
    """GET/PUT /settings including server-side clamping."""

    def test_settings_default_shape(self, auth_client):
        body = auth_client.get("/settings").json()
        assert "resume_path" in body
        assert "blocked_domains" in body
        assert body["browser_headless"] is False
        assert body["auto_reject_after_months"] == 1

    def test_settings_roundtrip(self, auth_client):
        settings = {"resume_path": "/tmp/resume.pdf", "blocked_domains": ["spam.com"]}
        assert auth_client.put("/settings", json=settings).json() == {"success": True}
        loaded = auth_client.get("/settings").json()
        assert loaded["resume_path"] == "/tmp/resume.pdf"
        assert loaded["blocked_domains"] == ["spam.com"]

    def test_partial_settings_update_preserves_unrelated_values(self, auth_client):
        auth_client.put(
            "/settings",
            json={
                "resume_path": "/tmp/resume.pdf",
                "blocked_domains": ["spam.com"],
                "sensitive_data": {"email": "me@example.com", "password": "secret"},
            },
        )

        assert auth_client.put("/settings", json={"theme": "dark"}).json() == {
            "success": True
        }
        loaded = auth_client.get("/settings").json()
        assert loaded["theme"] == "dark"
        assert loaded["resume_path"] == "/tmp/resume.pdf"
        assert loaded["blocked_domains"] == ["spam.com"]
        assert loaded["sensitive_data"] == {
            "email": "me@example.com",
            "password": "secret",
        }

    def test_settings_clamps_out_of_range_values(self, auth_client):
        """max_failures / stagger_delay are clamped to safe bounds on save."""
        auth_client.put("/settings", json={"max_failures": 9999, "stagger_delay": -5})
        loaded = auth_client.get("/settings").json()
        assert loaded["max_failures"] == 50  # clamped to upper bound
        assert loaded["stagger_delay"] == 0  # clamped to lower bound

        auth_client.put("/settings", json={"auto_reject_after_months": 999})
        assert auth_client.get("/settings").json()["auto_reject_after_months"] == 120

    def test_settings_strips_blank_blocked_domains(self, auth_client):
        auth_client.put("/settings", json={"blocked_domains": ["  ", "real.com", ""]})
        loaded = auth_client.get("/settings").json()
        assert loaded["blocked_domains"] == ["real.com"]


# ── LLM settings CRUD ─────────────────────────────────────────────────────────
class TestLLMSettings:
    """GET/PUT /settings/llm roundtrip."""

    def test_llm_default_shape(self, auth_client):
        body = auth_client.get("/settings/llm").json()
        assert "provider" in body

    def test_llm_settings_roundtrip(self, auth_client):
        settings = {
            "provider": "openrouter",
            "openrouter": {"api_key": "sk-xxx", "model": "meta-llama/llama-3.1-8b-instruct"},
        }
        assert auth_client.put("/settings/llm", json=settings).json() == {"success": True}
        loaded = auth_client.get("/settings/llm").json()
        assert loaded["provider"] == "openrouter"
        assert loaded["openrouter"]["model"] == "meta-llama/llama-3.1-8b-instruct"


# ── Countries ─────────────────────────────────────────────────────────────────
class TestCountries:
    def test_countries_list(self, auth_client):
        """/countries returns >= 18 country configs plus notice-period options."""
        body = auth_client.get("/countries").json()
        assert body["success"] is True
        assert len(body["countries"]) >= 18
        assert "notice_period_options" in body

    def test_get_known_country(self, auth_client):
        body = auth_client.get("/countries").json()
        code = next(iter(body["countries"]))
        resp = auth_client.get(f"/countries/{code}")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_get_unknown_country_404(self, auth_client):
        """An unsupported country code yields a structured 404."""
        resp = auth_client.get("/countries/ZZ")
        assert resp.status_code == 404
        assert resp.json()["ok"] is False
        assert resp.json()["error"]["code"] == "not_found"


# ── Plugins ───────────────────────────────────────────────────────────────────
class TestPlugins:
    EXPECTED = {"linkedin", "indeed", "seek", "naukri", "reed", "stepstone"}

    def test_plugins_list(self, auth_client):
        """/plugins returns at least the six built-in job sources with full schema."""
        body = auth_client.get("/plugins").json()
        assert body["success"] is True
        plugins = body["plugins"]
        assert len(plugins) >= 6
        names = {p["name"] for p in plugins}
        assert self.EXPECTED.issubset(names)
        sample = plugins[0]
        for field in ("name", "display_name", "version", "countries", "filters"):
            assert field in sample

    def test_plugins_filtered_by_country(self, auth_client):
        """Country filtering returns only global ('ALL') + matching-country plugins."""
        # Unknown country: only the global plugins (countries == ['ALL']) match.
        zz = {p["name"] for p in auth_client.get("/plugins", params={"country": "ZZ"}).json()["plugins"]}
        assert "indeed" in zz and "linkedin" in zz
        assert "naukri" not in zz  # naukri is India-only

        # India: global plugins plus the India-specific one.
        india = {p["name"] for p in auth_client.get("/plugins", params={"country": "IN"}).json()["plugins"]}
        assert "naukri" in india
        assert "reed" not in india  # reed is GB-only

    def test_toggle_unknown_plugin_404(self, auth_client):
        resp = auth_client.put("/plugins/does-not-exist/toggle", json={"enabled": False})
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    def test_toggle_known_plugin(self, auth_client):
        resp = auth_client.put("/plugins/linkedin/toggle", json={"enabled": True})
        assert resp.status_code == 200
        assert resp.json() == {"success": True, "name": "linkedin", "enabled": True}

    def test_import_plugin_requires_yaml_upload(self, auth_client):
        resp = auth_client.post(
            "/plugins/import",
            content=b"not yaml",
            headers={"X-Hunter-Filename": "plugin.txt"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "import_failed"

    def test_import_plugin_accepts_browser_upload(self, auth_client, monkeypatch):
        imported = []

        class FakePlugin:
            name = "acme"
            display_name = "Acme Jobs"

        class FakeRegistry:
            def import_plugin(self, path):
                uploaded = Path(path)
                imported.append((uploaded.name, uploaded.read_bytes()))
                return FakePlugin()

        monkeypatch.setattr(main, "_get_plugin_registry", lambda: FakeRegistry())
        response = auth_client.post(
            "/plugins/import",
            content=b"name: acme\n",
            headers={
                "Content-Type": "application/yaml",
                "X-Hunter-Filename": "Acme%20Jobs.yaml",
            },
        )

        assert response.status_code == 200
        assert response.json() == {
            "success": True,
            "plugin": {"name": "acme", "display_name": "Acme Jobs"},
        }
        assert imported == [("Acme Jobs.yaml", b"name: acme\n")]

    def test_reload_plugins(self, auth_client):
        resp = auth_client.post("/plugins/reload")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["count"] >= 6


# ── Jobs (CRUD over the isolated SQLite store) ───────────────────────────────
class TestJobs:
    def test_jobs_empty(self, auth_client):
        """A fresh data dir has no jobs."""
        assert auth_client.get("/jobs").json() == []

    def test_job_stats_empty(self, auth_client):
        stats = auth_client.get("/jobs/stats").json()
        assert stats["total"] == 0
        for k in ("pending", "applied", "failed", "blocked", "in_progress"):
            assert stats[k] == 0
        assert stats["category_counts"] == {
            category: 0 for category in main.JOB_CATEGORIES
        }

    def test_jobs_derive_filter_and_count_unified_categories(self, auth_client):
        from core.shared_config import write_jobs

        jobs = {
            "https://jobs.example/review": {
                "status": "pending",
                "screening": {"status": "review", "override": None},
            },
            "https://jobs.example/qualified": {
                "status": "pending",
                "screening": {"status": "qualified", "override": None},
            },
            "https://jobs.example/active": {
                "status": "in_progress",
                "screening": {"status": "qualified", "override": None},
            },
            "https://jobs.example/unqualified": {
                "status": "pending",
                "screening": {"status": "rejected", "override": None},
            },
            "https://jobs.example/legacy-applied": {"status": "applied"},
            "https://jobs.example/interview": {
                "status": "applied",
                "application_status": "interview",
            },
            "https://jobs.example/failed": {"status": "failed"},
            "https://jobs.example/blocked": {"status": "blocked"},
        }
        write_jobs(jobs)

        qualified = auth_client.get(
            "/jobs", params={"category": "qualified"}
        ).json()
        assert {job["url"] for job in qualified} == {
            "https://jobs.example/qualified",
            "https://jobs.example/active",
        }
        assert all(job["category"] == "qualified" for job in qualified)

        stats = auth_client.get("/jobs/stats").json()
        assert stats["category_counts"]["qualified"] == 2
        assert stats["category_counts"]["unqualified"] == 1
        assert stats["category_counts"]["applied"] == 1
        assert stats["category_counts"]["interview"] == 1
        assert sum(stats["category_counts"].values()) == len(jobs)

        invalid = auth_client.get("/jobs", params={"category": "unknown"})
        assert invalid.status_code == 400
        assert invalid.json()["error"]["code"] == "invalid_category"

    def test_job_category_transitions_preserve_application_evidence(
        self, auth_client
    ):
        from core.shared_config import read_jobs, write_jobs

        url = "https://jobs.example/applied-to-qualified"
        write_jobs(
            {
                url: {
                    "url": url,
                    "title": "Engineer",
                    "status": "applied",
                    "application_status": "interview",
                    "application_status_updated_at": "2026-01-02T00:00:00Z",
                    "applied_at": "2026-01-01T00:00:00Z",
                    "tailored_resume_path": "/tmp/resume.pdf",
                    "cover_letter_path": "/tmp/letter.txt",
                    "screening": {"status": "rejected", "override": None},
                }
            }
        )

        response = auth_client.put(
            "/jobs/category", json={"url": url, "category": "qualified"}
        )

        assert response.status_code == 200
        assert response.json()["job"]["category"] == "qualified"
        saved = read_jobs()[url]
        assert saved["status"] == "pending"
        assert saved["screening"]["override"] == "qualified"
        assert saved["application_status"] is None
        assert saved["application_status_updated_at"] is None
        assert saved["applied_at"] == "2026-01-01T00:00:00Z"
        assert saved["tailored_resume_path"] == "/tmp/resume.pdf"
        assert saved["cover_letter_path"] == "/tmp/letter.txt"

    def test_job_category_supports_all_transition_groups(self, auth_client):
        from core.shared_config import read_jobs, write_jobs

        url = "https://jobs.example/movable"
        write_jobs({url: {"url": url, "status": "pending", "title": "Engineer"}})

        expected = {
            "review": ("pending", "review", None),
            "qualified": ("pending", "qualified", None),
            "unqualified": ("pending", "rejected", None),
            "applied": ("applied", None, "applied"),
            "rejected": ("applied", None, "rejected"),
            "interview": ("applied", None, "interview"),
            "offer": ("applied", None, "offer"),
            "accepted": ("applied", None, "accepted"),
            "refused": ("applied", None, "refused"),
            "failed": ("failed", None, None),
            "blocked": ("blocked", None, None),
        }
        for category, (status, override, application_status) in expected.items():
            response = auth_client.put(
                "/jobs/category", json={"url": url, "category": category}
            )
            assert response.status_code == 200
            assert response.json()["job"]["category"] == category
            saved = read_jobs()[url]
            assert saved["status"] == status
            if override is not None:
                assert saved["screening"]["override"] == override
            assert saved.get("application_status") == application_status

        assert read_jobs()[url]["applied_at"]

    def test_job_category_rejects_active_application(self, auth_client):
        from core.shared_config import write_jobs

        url = "https://jobs.example/active"
        write_jobs({url: {"url": url, "status": "in_progress"}})

        response = auth_client.put(
            "/jobs/category", json={"url": url, "category": "qualified"}
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "application_in_progress"

    def test_job_categories_bulk_move_failed_jobs_to_blocked(
        self, auth_client, monkeypatch
    ):
        from core.shared_config import read_jobs, write_jobs

        class AutomationManagerStub:
            def __init__(self):
                self.discarded = []

            def discard_url(self, url):
                self.discarded.append(url)

        urls = [
            "https://jobs.example/failed-one",
            "https://jobs.example/failed-two",
        ]
        write_jobs({url: {"url": url, "status": "failed"} for url in urls})
        manager = AutomationManagerStub()
        monkeypatch.setattr(main, "_automation_manager", manager)

        response = auth_client.put(
            "/jobs/categories",
            json={"urls": urls, "category": "blocked"},
        )

        assert response.status_code == 200
        assert response.json() == {"success": True, "updated": 2, "missing": []}
        assert all(read_jobs()[url]["status"] == "blocked" for url in urls)
        assert manager.discarded == urls

    def test_job_categories_rejects_batch_containing_active_job(self, auth_client):
        from core.shared_config import read_jobs, write_jobs

        failed_url = "https://jobs.example/failed"
        active_url = "https://jobs.example/active"
        write_jobs(
            {
                failed_url: {"url": failed_url, "status": "failed"},
                active_url: {"url": active_url, "status": "in_progress"},
            }
        )

        response = auth_client.put(
            "/jobs/categories",
            json={"urls": [failed_url, active_url], "category": "blocked"},
        )

        assert response.status_code == 409
        assert read_jobs()[failed_url]["status"] == "failed"

    def test_manual_unqualified_job_is_removed_from_autonomous_queue(
        self, auth_client, monkeypatch
    ):
        from core.shared_config import write_jobs

        class AutomationManagerStub:
            def __init__(self):
                self.discarded = []

            def discard_url(self, url):
                self.discarded.append(url)

        url = "https://jobs.example/manual-unqualified"
        write_jobs({url: {"url": url, "status": "pending", "title": "Senior Engineer"}})
        manager = AutomationManagerStub()
        monkeypatch.setattr(main, "_automation_manager", manager)

        response = auth_client.put(
            "/jobs/category", json={"url": url, "category": "unqualified"}
        )

        assert response.status_code == 200
        assert manager.discarded == [url]

    def test_add_job_and_list(self, auth_client):
        """A manually added job appears in listing and stats."""
        url = "https://www.linkedin.com/jobs/view/123456"
        resp = auth_client.post("/jobs/add", json={"url": url, "title": "Dev", "company": "Acme"})
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        jobs = auth_client.get("/jobs").json()
        assert any(j["url"] == url for j in jobs)
        assert auth_client.get("/jobs/stats").json()["pending"] == 1

    def test_jobs_filter_and_count_effective_screening_folders(self, auth_client):
        from core.shared_config import update_job

        urls = {
            "qualified": "https://www.linkedin.com/jobs/view/qualified",
            "review": "https://www.linkedin.com/jobs/view/review",
            "rejected": "https://www.linkedin.com/jobs/view/rejected",
        }
        for screening_status, url in urls.items():
            auth_client.post("/jobs/add", json={"url": url, "title": screening_status})
            update_job(
                url,
                screening={"status": screening_status, "override": None},
            )

        rejected = auth_client.get(
            "/jobs", params={"status": "pending", "screening_status": "rejected"}
        ).json()
        assert [job["url"] for job in rejected] == [urls["rejected"]]

        stats = auth_client.get("/jobs/stats").json()
        assert stats["qualified"] == 1
        assert stats["review"] == 1
        assert stats["rejected"] == 1

    def test_job_stats_count_each_application_outcome(self, auth_client):
        from core.shared_config import update_job

        outcomes = (
            "applied",
            "online_assessment",
            "rejected",
            "interview",
            "offer",
            "accepted",
            "refused",
        )
        for outcome in outcomes:
            url = f"https://www.linkedin.com/jobs/view/outcome-{outcome}"
            auth_client.post("/jobs/add", json={"url": url, "title": outcome})
            fields = {"status": "applied"}
            if outcome != "applied":
                fields["application_status"] = outcome
            update_job(url, **fields)

        stats = auth_client.get("/jobs/stats").json()
        assert stats["applied"] == len(outcomes)
        for outcome in outcomes:
            assert stats[f"application_{outcome}"] == 1

    def test_add_job_missing_url_400(self, auth_client):
        resp = auth_client.post("/jobs/add", json={"title": "Dev"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "missing_field"

    def test_export_jobs_csv_empty(self, auth_client):
        """Export on an empty data dir returns a CSV with only the header row."""
        resp = auth_client.get("/jobs/export")
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]
        assert "attachment" in resp.headers["content-disposition"]
        assert "langhire-jobs.csv" in resp.headers["content-disposition"]
        lines = [ln for ln in resp.text.splitlines() if ln.strip()]
        assert len(lines) == 1  # header only
        assert "Job Title" in lines[0]
        assert "Company" in lines[0]
        assert "URL" in lines[0]

    def test_export_jobs_csv_with_data(self, auth_client):
        """Added jobs appear as CSV rows with the expected columns."""
        url = "https://www.linkedin.com/jobs/view/555000"
        auth_client.post("/jobs/add", json={"url": url, "title": "Engineer", "company": "Globex", "location": "Remote"})
        resp = auth_client.get("/jobs/export")
        assert resp.status_code == 200
        body = resp.text
        assert "Engineer" in body
        assert "Globex" in body
        assert "Remote" in body
        assert url in body
        # header + at least one data row
        assert len([ln for ln in body.splitlines() if ln.strip()]) >= 2

    def test_export_jobs_csv_requires_auth(self, client):
        """Export endpoint is behind auth like the rest of the API."""
        resp = client.get("/jobs/export")  # no Authorization header
        assert resp.status_code == 401

    def test_add_duplicate_job_409(self, auth_client):
        url = "https://www.linkedin.com/jobs/view/777"
        auth_client.post("/jobs/add", json={"url": url})
        resp = auth_client.post("/jobs/add", json={"url": url})
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "duplicate"

    def test_import_linkedin_job_saves_and_queues_classification(
        self, auth_client, monkeypatch
    ):
        from core.shared_config import read_jobs
        from sources import jobspy_collector

        canonical_url = "https://www.linkedin.com/jobs/view/4428487670"
        imported_job = {
            "url": canonical_url,
            "source": "linkedin",
            "source_id": "li-4428487670",
            "title": "AI Security Researcher",
            "company": "Opera",
            "location": "Warsaw, Poland",
            "description": "Research agentic AI systems.",
            "status": "pending",
        }
        queued = []
        monkeypatch.setattr(
            jobspy_collector, "import_linkedin_job", lambda url: imported_job
        )
        monkeypatch.setattr(
            main,
            "_enqueue_classification",
            lambda urls: queued.extend(urls) or len(urls),
        )

        response = auth_client.post(
            "/jobs/import/linkedin",
            json={
                "url": "https://www.linkedin.com/jobs/search-results/?currentJobId=4428487670"
            },
        )

        assert response.status_code == 200
        assert response.json()["url"] == canonical_url
        assert response.json()["classification_queued"] is True
        assert read_jobs()[canonical_url]["company"] == "Opera"
        assert queued == [canonical_url]

    def test_import_linkedin_job_rejects_invalid_url(self, auth_client):
        response = auth_client.post(
            "/jobs/import/linkedin", json={"url": "https://example.com/jobs/123"}
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_linkedin_url"

    def test_import_linkedin_job_skips_blacklisted_company(
        self, auth_client, monkeypatch
    ):
        from sources import jobspy_collector

        monkeypatch.setattr(
            jobspy_collector,
            "import_linkedin_job",
            lambda _url: {
                "url": "https://www.linkedin.com/jobs/view/4428487670",
                "company": "outlier",
                "status": "pending",
            },
        )

        response = auth_client.post(
            "/jobs/import/linkedin",
            json={"url": "https://www.linkedin.com/jobs/view/4428487670"},
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "blacklisted_company"

    def test_import_linkedin_job_deduplicates_url_variants(self, auth_client):
        from core.shared_config import write_jobs

        existing_url = (
            "https://www.linkedin.com/jobs/search-results/?currentJobId=4428487670"
        )
        write_jobs({existing_url: {"url": existing_url, "status": "pending"}})

        response = auth_client.post(
            "/jobs/import/linkedin",
            json={"url": "https://www.linkedin.com/jobs/view/ai-researcher-4428487670/"},
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "duplicate"

    def test_update_status_unknown_job_404(self, auth_client):
        resp = auth_client.put(
            "/jobs/status",
            json={"url": "https://example.com/nope", "status": "applied"},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    def test_update_status_invalid_status_400(self, auth_client):
        url = "https://www.linkedin.com/jobs/view/888"
        auth_client.post("/jobs/add", json={"url": url})
        resp = auth_client.put("/jobs/status", json={"url": url, "status": "bogus"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_status"

    def test_update_status_success(self, auth_client):
        url = "https://www.linkedin.com/jobs/view/999"
        auth_client.post("/jobs/add", json={"url": url})
        resp = auth_client.put("/jobs/status", json={"url": url, "status": "applied"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "applied"
        assert auth_client.get("/jobs/stats").json()["applied"] == 1
        job = auth_client.get("/jobs").json()[0]
        assert [entry["status"] for entry in job["status_history"]] == [
            "review",
            "applied",
        ]
        assert job["status_history"][-1]["source"] == "manual"

    def test_update_application_status_success(self, auth_client):
        url = "https://www.linkedin.com/jobs/view/1001"
        auth_client.post("/jobs/add", json={"url": url})

        resp = auth_client.put(
            "/jobs/application-status",
            json={"url": url, "status": "interview"},
        )

        assert resp.status_code == 200
        assert resp.json()["status"] == "interview"
        job = auth_client.get("/jobs", params={"status": "pending"}).json()[0]
        assert job["application_status"] == "interview"
        assert job["application_status_updated_at"] == resp.json()["updated_at"]
        assert job["application_status_source"] == "manual"
        assert job["application_status_evidence"] is None
        assert job["status"] == "pending"

    def test_update_application_status_rejects_invalid_value(self, auth_client):
        url = "https://www.linkedin.com/jobs/view/1002"
        auth_client.post("/jobs/add", json={"url": url})

        resp = auth_client.put(
            "/jobs/application-status",
            json={"url": url, "status": "maybe"},
        )

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_status"

    def test_update_application_status_unknown_job_404(self, auth_client):
        resp = auth_client.put(
            "/jobs/application-status",
            json={"url": "https://example.com/nope", "status": "offer"},
        )

        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    def test_delete_jobs(self, auth_client):
        url = "https://www.linkedin.com/jobs/view/555"
        auth_client.post("/jobs/add", json={"url": url})
        resp = auth_client.request("DELETE", "/jobs", json={"urls": [url]})
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 1

    def test_delete_jobs_missing_array_400(self, auth_client):
        resp = auth_client.request("DELETE", "/jobs", json={})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "missing_field"

    def test_jobs_status_filter(self, auth_client):
        """The ?status filter excludes non-matching jobs."""
        url = "https://www.linkedin.com/jobs/view/4242"
        auth_client.post("/jobs/add", json={"url": url})  # pending
        assert auth_client.get("/jobs", params={"status": "applied"}).json() == []
        assert len(auth_client.get("/jobs", params={"status": "pending"}).json()) == 1


# ── Memory / Q&A / dashboard (SQLite-backed, empty store) ─────────────────────
class TestMemoryAndQA:
    def test_memory_stats(self, auth_client):
        stats = auth_client.get("/memory/stats").json()
        assert stats["total_memories"] == 0
        assert "by_category" in stats

    def test_memory_domains_empty(self, auth_client):
        assert auth_client.get("/memory/domains").json() == []

    def test_memory_search_blank_query(self, auth_client):
        """A blank query returns an empty list rather than erroring."""
        assert auth_client.get("/memory/search", params={"q": ""}).json() == []

    def test_memory_export(self, auth_client):
        assert auth_client.get("/memory/export").json() == []

    def test_critical_memory_settings_are_persisted(self, auth_client):
        defaults = auth_client.get("/memory/critical-settings")
        assert defaults.status_code == 200
        assert defaults.json() == {
            "auto_rerank": True,
            "max_count": 5,
            "max_tokens": 500,
        }

        updated = auth_client.put(
            "/memory/critical-settings",
            json={"auto_rerank": False, "max_count": 7, "max_tokens": 900},
        )

        assert updated.status_code == 200
        assert updated.json()["selected"] == 0
        assert auth_client.get("/memory/critical-settings").json() == {
            "auto_rerank": False,
            "max_count": 7,
            "max_tokens": 900,
        }

    def test_critical_memory_settings_validate_bounds(self, auth_client):
        response = auth_client.put(
            "/memory/critical-settings",
            json={"auto_rerank": True, "max_count": 21, "max_tokens": 50},
        )

        assert response.status_code == 422

    def test_memory_rerank_requires_saved_openai_key(self, auth_client, monkeypatch):
        main._get_memory_store().add(
            "Use the second Apply button", "greenhouse.io", "navigation"
        )
        monkeypatch.setattr(main, "load_llm_settings", lambda: {"openai": {}})

        response = auth_client.post("/memory/rerank-critical")

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "openai_key_missing"

    def test_memory_rerank_applies_classifier_selection(self, auth_client, monkeypatch):
        from memory import ranker

        store = main._get_memory_store()
        store.add("Use the second Apply button", "greenhouse.io", "navigation")
        memory_id = store.export_all()[0]["id"]
        monkeypatch.setattr(
            main,
            "load_llm_settings",
            lambda: {"openai": {"api_key": "test-key"}},
        )

        async def fake_rank(memories, *, api_key, **_kwargs):
            assert api_key == "test-key"
            assert memories[0]["id"] == memory_id
            return ranker.CriticalMemoryRanking(
                rankings=[ranker.ScopeRanking(
                    scope="greenhouse",
                    choices=[ranker.CriticalMemoryChoice(
                        memory_id=memory_id,
                        reason="Prevents the wrong navigation path",
                    )],
                )]
            )

        monkeypatch.setattr(ranker, "rank_critical_memories", fake_rank)

        response = auth_client.post("/memory/rerank-critical")

        assert response.status_code == 200
        assert response.json()["selected"] == 1
        assert main._get_memory_store().export_all()[0]["critical"] is True

    def test_memory_decay(self, auth_client):
        resp = auth_client.post("/memory/decay", json={"days": 30, "factor": 0.9})
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_memory_decay_validation_error(self, auth_client):
        """factor > 1.0 violates the pydantic model -> 422."""
        resp = auth_client.post("/memory/decay", json={"factor": 5.0})
        assert resp.status_code == 422

    def test_memory_cleanup(self, auth_client):
        resp = auth_client.post("/memory/cleanup", json={"threshold": 0.3})
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_qa_empty(self, auth_client):
        assert auth_client.get("/qa").json() == []

    def test_qa_stats(self, auth_client):
        stats = auth_client.get("/qa/stats").json()
        assert stats == {
            "total": 0,
            "answered": 0,
            "unanswered": 0,
            "reviewed": 0,
            "to_review": 0,
        }

    def test_qa_review_folders_and_update(self, auth_client):
        store = main._get_memory_store()
        row = store.qa_add("Review this?", answer="Yes")

        assert [item["id"] for item in auth_client.get(
            "/qa", params={"folder": "to_review"}
        ).json()] == [row["id"]]

        response = auth_client.put(
            f"/qa/{row['id']}", json={"reviewed": True}
        )

        assert response.status_code == 200
        assert auth_client.get("/qa", params={"folder": "to_review"}).json() == []
        assert [item["id"] for item in auth_client.get(
            "/qa", params={"folder": "reviewed"}
        ).json()] == [row["id"]]

    def test_qa_auto_squash(self, auth_client):
        resp = auth_client.post("/qa/auto-squash")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_dashboard_aggregates(self, auth_client):
        """/dashboard merges jobs + memory (+ metrics) into one payload."""
        body = auth_client.get("/dashboard").json()
        assert "jobs" in body
        assert "memory" in body
        assert "metrics" in body
        assert "gmail_outcomes" in body
        assert body["jobs"]["total"] == 0


# ── Metrics & logs (empty store) ──────────────────────────────────────────────
class TestMetricsAndLogs:
    def test_metric_runs_empty(self, auth_client):
        assert auth_client.get("/metrics/runs").json() == []

    def test_metric_domains_empty(self, auth_client):
        assert auth_client.get("/metrics/domains").json() == []

    def test_recent_logs_empty(self, auth_client):
        assert auth_client.get("/logs/recent").json() == []

    def test_runs_with_logs_empty(self, auth_client):
        assert auth_client.get("/logs/runs").json() == []

    def test_previews_application_attempt_storage(self, auth_client, monkeypatch):
        captured = {}

        def report(**options):
            captured.update(options)
            return {
                "total_dossiers": 12,
                "total_bytes": 1000,
                "reclaimable_dossiers": 3,
                "reclaimable_bytes": 400,
            }

        monkeypatch.setattr(main, "application_attempt_storage_report", report)

        response = auth_client.get(
            "/application-attempts/storage",
            params={"video_retention_days": 14, "dossier_retention_days": 45},
        )

        assert response.status_code == 200
        assert response.json()["reclaimable_bytes"] == 400
        assert captured == {
            "video_retention_days": 14,
            "dossier_retention_days": 45,
        }

    def test_cleans_up_application_attempt_storage(self, auth_client, monkeypatch):
        captured = {}

        def compact(**options):
            captured.update(options)
            return {
                "success": True,
                "compacted_dossiers": 2,
                "deleted_dossiers": 1,
                "removed_bytes": 500,
                "removed_files": 6,
                "storage": {},
            }

        monkeypatch.setattr(main, "cleanup_application_attempts", compact)

        response = auth_client.post(
            "/application-attempts/retention/cleanup",
            json={"video_retention_days": 14, "dossier_retention_days": 60},
        )

        assert response.status_code == 200
        assert response.json()["removed_bytes"] == 500
        assert captured == {
            "video_retention_days": 14,
            "dossier_retention_days": 60,
        }

    def test_rejects_invalid_application_attempt_retention(self, auth_client):
        response = auth_client.post(
            "/application-attempts/retention/cleanup",
            json={"video_retention_days": 30, "dossier_retention_days": 14},
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_retention"

    def test_reads_application_attempt_dossier(
        self, auth_client, monkeypatch, tmp_path
    ):
        directory = tmp_path / "attempt"
        (directory / "screenshots").mkdir(parents=True)
        (directory / "materials").mkdir()
        (directory / "screenshots" / "final.png").write_bytes(b"png")
        (directory / "materials" / "resume.pdf").write_bytes(b"pdf")
        (directory / "materials" / "generation-audit.json").write_text(
            json.dumps({"resume": {"positioning": "Lead with backend delivery"}})
        )
        (directory / "job.json").write_text(
            json.dumps({"description": "Archived original listing text"})
        )
        (directory / "recording.mp4").write_bytes(b"video")
        (directory / "summary.md").write_text("# Summary")
        manifest = {
            "attempt_id": "attempt-123",
            "status": "failed",
            "artifacts": {
                "final_screenshot": "screenshots/final.png",
                "screenshots": ["screenshots/final.png"],
                "resume": {"path": "materials/resume.pdf"},
                "generation_audit": "materials/generation-audit.json",
                "recording": {"path": "recording.mp4"},
            },
            "files": {
                "materials/resume.pdf": {"size_bytes": 3},
                "summary.md": {"size_bytes": 9},
                "materials/generation-audit.json": {"size_bytes": 64},
            },
        }
        (directory / "manifest.json").write_text(json.dumps(manifest))
        (directory / "history.json").write_text(
            json.dumps(
                {
                    "history": [
                        {
                            "state": {
                                "url": "https://jobs.example/apply",
                                "screenshot_path": "screenshots/final.png",
                            },
                            "model_output": {
                                "evaluation_previous_goal": "Form opened",
                                "next_goal": "Submit application",
                                "action": [{"click": {"index": 4}}],
                            },
                            "metadata": {"step_start_time": 10.0},
                            "result": [{"extracted_content": "Ready to submit"}],
                        }
                    ]
                }
            )
        )
        monkeypatch.setattr(main, "find_attempt_directory", lambda _attempt_id: directory)

        response = auth_client.get("/application-attempts/attempt-123")

        assert response.status_code == 200
        body = response.json()
        assert body["manifest"]["status"] == "failed"
        assert body["screenshots"] == ["screenshots/final.png"]
        assert body["recording"] == "recording.mp4"
        assert body["files"][0] == {
            "label": "Tailored CV",
            "path": "materials/resume.pdf",
            "size_bytes": 3,
        }
        assert body["steps"][0]["next_goal"] == "Submit application"
        assert body["generation_audit"]["resume"]["positioning"] == "Lead with backend delivery"
        assert body["original_listing_text"] == "Archived original listing text"
        assert any(file["label"] == "Document generation audit" for file in body["files"])
        assert body["steps"][0]["results"] == [
            {"kind": "result", "text": "Ready to submit"}
        ]

    def test_serves_only_files_inside_application_attempt(
        self, auth_client, monkeypatch, tmp_path
    ):
        directory = tmp_path / "attempt"
        directory.mkdir()
        evidence = directory / "evidence.txt"
        evidence.write_text("application evidence")
        outside = tmp_path / "outside.txt"
        outside.write_text("private")
        monkeypatch.setattr(main, "find_attempt_directory", lambda _attempt_id: directory)

        response = auth_client.get(
            "/application-attempts/attempt-123/files/evidence.txt"
        )
        traversal = auth_client.get(
            "/application-attempts/attempt-123/files/%2E%2E/outside.txt"
        )

        assert response.status_code == 200
        assert response.text == "application evidence"
        assert traversal.status_code == 404


# ── Setup status & onboarding ─────────────────────────────────────────────────
class TestSetup:
    def test_setup_status_fresh(self, auth_client):
        """A fresh install reports nothing configured / done."""
        body = auth_client.get("/setup/status").json()
        assert body["profile"] is False
        assert body["resume"] is False
        assert body["onboarding_completed"] is False
        assert body["all_required_done"] is False

    def test_complete_onboarding_persists(self, auth_client):
        assert auth_client.post("/setup/complete-onboarding").json() == {"success": True}
        body = auth_client.get("/setup/status").json()
        assert body["onboarding_completed"] is True

    def test_setup_status_llm_done_for_gemini(self, auth_client):
        """A configured Gemini key marks the LLM step done (#40 provider)."""
        auth_client.put("/settings/llm", json={"provider": "gemini", "gemini": {"api_key": "AIza-x", "model": "gemini-2.5-pro"}})
        assert auth_client.get("/setup/status").json()["llm"] is True

    def test_setup_status_llm_done_for_openai_compatible(self, auth_client):
        """A configured base_url marks the LLM step done for openai_compatible."""
        auth_client.put("/settings/llm", json={"provider": "openai_compatible", "openai_compatible": {"base_url": "https://api.together.xyz/v1", "api_key": "", "model": "m"}})
        assert auth_client.get("/setup/status").json()["llm"] is True

    def test_setup_status_llm_not_done_when_gemini_key_blank(self, auth_client):
        auth_client.put("/settings/llm", json={"provider": "gemini", "gemini": {"api_key": "", "model": "gemini-2.5-pro"}})
        assert auth_client.get("/setup/status").json()["llm"] is False


# ── Status endpoints for the long-running workers (no work triggered) ─────────
class TestWorkerStatus:
    def test_collection_status_idle(self, auth_client):
        body = auth_client.get("/jobs/collect/status").json()
        assert body["running"] is False
        assert body["classification_running"] is False
        assert body["classification_total"] == 0
        assert isinstance(body["log"], list)

    def test_apply_status_idle(self, auth_client):
        body = auth_client.get("/apply/status").json()
        assert body["running"] is False
        assert body["workers"] == 1


class TestGmailOutcomeAPI:
    class FakeManager:
        def __init__(self):
            self.connected = False
            self.started = None
            self.dismissed = []
            self.syncs = 0

        def status(self):
            return {
                "client_configured": True,
                "connected": self.connected,
                "enabled": self.connected,
                "account_email": "candidate@example.com" if self.connected else None,
                "last_success_at": None,
                "last_error": None,
                "last_sync_summary": None,
                "review_required_count": 0,
                "review_required": [],
                "recent_events": [],
                "telegram_configured": True,
            }

        def start_authorization(self, **kwargs):
            self.started = kwargs
            return "https://accounts.google.com/o/oauth2/auth?state=test-state"

        def complete_authorization(self, **_kwargs):
            self.connected = True

        async def sync_once(self):
            self.syncs += 1
            return {
                "scanned": 1,
                "updated": 1,
                "review_required": 0,
                "completed_at": "2026-08-20T12:00:00+00:00",
            }

        async def sync_safely(self):
            return await self.sync_once()

        def disconnect(self):
            self.connected = False

        def dismiss_review(self, message_id):
            self.dismissed.append(message_id)
            return message_id == "message-1"

        def _record_error(self, _exc):
            pass

    def test_connect_status_sync_disconnect_and_review(self, auth_client, monkeypatch):
        manager = self.FakeManager()
        monkeypatch.setattr(main, "_get_gmail_outcome_manager", lambda: manager)

        assert auth_client.get("/gmail/outcomes/status").json()["connected"] is False
        connected = auth_client.post(
            "/gmail/outcomes/connect",
            json={
                "client_id": "client-id",
                "client_secret": "client-secret",
            },
        )
        assert connected.status_code == 200
        assert connected.json()["authorization_url"].startswith("https://accounts.google.com/")
        assert manager.started["client_secret"] == "client-secret"
        assert manager.started["redirect_uri"] == (
            "http://127.0.0.1:8743/gmail/oauth/callback"
        )

        synced = auth_client.post("/gmail/outcomes/sync")
        assert synced.status_code == 200
        assert synced.json()["updated"] == 1

        reviewed = auth_client.post(
            "/gmail/outcomes/reviewed", json={"message_id": "message-1"}
        )
        assert reviewed.status_code == 200
        assert manager.dismissed == ["message-1"]

        disconnected = auth_client.post("/gmail/outcomes/disconnect")
        assert disconnected.status_code == 200
        assert manager.connected is False

    def test_connect_uses_configured_hosted_callback(self, auth_client, monkeypatch):
        manager = self.FakeManager()
        monkeypatch.setattr(main, "_get_gmail_outcome_manager", lambda: manager)
        monkeypatch.setenv("HUNTER_WEB_ORIGIN", "https://hunter.example.ts.net")

        response = auth_client.post(
            "/gmail/outcomes/connect",
            json={"client_id": "client-id", "client_secret": "client-secret"},
        )

        assert response.status_code == 200
        assert manager.started["redirect_uri"] == (
            "https://hunter.example.ts.net/gmail/oauth/callback"
        )

    def test_oauth_callback_is_public_but_state_guarded_by_manager(self, client, monkeypatch):
        manager = self.FakeManager()
        monkeypatch.setattr(main, "_get_gmail_outcome_manager", lambda: manager)

        response = client.get("/gmail/oauth/callback?state=test-state&code=test-code")

        assert response.status_code == 200
        assert "Gmail connected" in response.text
        assert "/#/settings?section=gmail" in response.text
        assert manager.connected is True


class TestAutomationAPI:
    class FakeManager:
        def __init__(self):
            self.config = {
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
            self.started = False
            self.stopped = False

        def snapshot(self):
            return {
                "config": dict(self.config),
                "status": {
                    "state": "running" if self.started else "stopped",
                    "phase": "waiting" if self.started else "idle",
                    "queued_count": 0,
                    "today_started": 0,
                    "today_limit": self.config["daily_job_limit"],
                    "captcha_count": 0,
                    "pause_reason": None,
                    "log": [],
                },
            }

        def save_config(self, config):
            self.config = {**self.config, **config}
            return dict(self.config)

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True
            self.started = False

    def test_get_and_update_automation_config(self, auth_client, monkeypatch):
        manager = self.FakeManager()
        monkeypatch.setattr(main, "_get_automation_manager", lambda: manager)

        assert auth_client.get("/automation").json()["config"]["daily_job_limit"] == 10
        response = auth_client.put(
            "/automation/config",
            json={
                **manager.config,
                "sources": ["linkedin"],
                "interval_minutes": 15,
                "daily_job_limit": 20,
                "daily_company_limit": 5,
                "max_applications_per_cycle": 3,
                "automation_min_score": 75,
                "linkedin_outreach_enabled": False,
            },
        )

        assert response.status_code == 200
        assert response.json()["config"]["sources"] == ["linkedin"]
        assert response.json()["config"]["interval_minutes"] == 15
        assert response.json()["config"]["daily_job_limit"] == 20
        assert response.json()["config"]["max_applications_per_cycle"] == 3
        assert response.json()["config"]["automation_min_score"] == 75
        assert response.json()["config"]["linkedin_outreach_enabled"] is False

    def test_qualified_queue_candidates_ignore_autonomous_completion_marker(self):
        from core.shared_config import write_jobs

        write_jobs(
            {
                "eligible": {
                    "url": "eligible",
                    "status": "pending",
                    "autonomous_completed_at": "2026-08-20T10:00:00+00:00",
                    "screening": {"status": "qualified", "override": None},
                },
                "manual-qualified": {
                    "url": "manual-qualified",
                    "status": "pending",
                    "screening": {"status": "review", "override": "qualified"},
                },
                "review": {
                    "url": "review",
                    "status": "pending",
                    "screening": {"status": "review", "override": None},
                },
                "failed": {
                    "url": "failed",
                    "status": "failed",
                    "screening": {"status": "qualified", "override": None},
                },
            }
        )

        assert set(main._automation_qualified_urls()) == {
            "eligible",
            "manual-qualified",
        }

    @pytest.mark.asyncio
    async def test_automation_linkedin_collection_forwards_scan_boundaries(
        self, monkeypatch
    ):
        captured = {}

        async def fake_start_collection(body):
            captured["body"] = body
            return {"success": True}

        monkeypatch.setattr(main, "start_collection", fake_start_collection)
        monkeypatch.setattr(main, "_collection_thread", None)
        monkeypatch.setattr(main, "_collection_status", {"added_urls": []})

        await main._automation_collect_source(
            "linkedin",
            {
                **self.FakeManager().config,
                "linkedin_search_url": "https://www.linkedin.com/jobs/search/?keywords=data",
                "_posted_after": "2026-08-16T10:00:00+00:00",
                "_max_results_to_inspect": 25,
            },
        )

        body = captured["body"]
        assert body.max_jobs == 5
        assert body.filters["posted_after"] == "2026-08-16T10:00:00+00:00"
        assert body.filters["max_results_to_inspect"] == 25

    def test_update_automation_rejects_invalid_linkedin_url(
        self, auth_client, monkeypatch
    ):
        manager = self.FakeManager()
        monkeypatch.setattr(main, "_get_automation_manager", lambda: manager)

        response = auth_client.put(
            "/automation/config",
            json={**manager.config, "linkedin_search_url": "https://example.com/jobs"},
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_automation_config"

    def test_start_reports_readiness_errors_without_starting(
        self, auth_client, monkeypatch
    ):
        manager = self.FakeManager()
        monkeypatch.setattr(main, "_get_automation_manager", lambda: manager)
        monkeypatch.setattr(
            main,
            "_automation_preflight",
            lambda _config: ["Configure required application settings"],
        )

        response = auth_client.post("/automation/start")

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "automation_not_ready"
        assert manager.started is False

    def test_start_and_stop_automation(self, auth_client, monkeypatch):
        manager = self.FakeManager()
        monkeypatch.setattr(main, "_get_automation_manager", lambda: manager)
        monkeypatch.setattr(main, "_automation_preflight", lambda _config: [])

        started = auth_client.post("/automation/start")
        stopped = auth_client.post("/automation/stop")

        assert started.status_code == 200
        assert started.json()["status"]["state"] == "running"
        assert stopped.status_code == 200
        assert stopped.json()["status"]["state"] == "stopped"
        assert manager.stopped is True


# ── Auth/login status (cookie inspection, no browser launched) ────────────────
class TestAuthStatus:
    def test_auth_status_no_cookies(self, auth_client):
        """With no browser profile, both services report logged_in=False."""
        body = auth_client.get("/auth/status").json()
        assert body["linkedin"]["logged_in"] is False
        assert body["gmail"]["logged_in"] is False

    def test_login_unknown_service(self, auth_client):
        """An unknown login service is rejected without launching anything."""
        body = auth_client.post("/auth/login/myspace").json()
        assert body["success"] is False
        assert "Unknown service" in body["message"]


class TestManualBrowser:
    def test_status_reports_remote_display_configuration(self, auth_client, monkeypatch):
        monkeypatch.setattr(main.sys, "platform", "linux")
        monkeypatch.setattr(main, "_manual_browser_dependencies", lambda: [])

        body = auth_client.get("/browser/manual/status").json()

        assert body["state"] == "stopped"
        assert body["supported"] is True
        assert body["dependencies_ready"] is True
        assert body["display"] == ":99"
        assert body["vnc_port"] == 5901

    def test_start_reports_missing_remote_packages(self, auth_client, monkeypatch):
        monkeypatch.setattr(main.sys, "platform", "linux")
        monkeypatch.setattr(
            main, "_manual_browser_dependencies", lambda: ["Xvfb", "x11vnc"]
        )

        response = auth_client.post("/browser/manual/start")

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "manual_browser_dependencies_missing"
        assert main._manual_browser_status["state"] == "stopped"

    def test_start_marks_session_starting_and_dispatches_worker(
        self, auth_client, monkeypatch
    ):
        started = threading.Event()
        monkeypatch.setattr(main.sys, "platform", "linux")
        monkeypatch.setattr(main, "_manual_browser_dependencies", lambda: [])
        monkeypatch.setattr(main, "_run_manual_browser_session", started.set)

        response = auth_client.post("/browser/manual/start")

        assert response.status_code == 200
        assert response.json()["state"] == "starting"
        assert started.wait(timeout=1)

    def test_application_refuses_to_kill_active_manual_browser(
        self, auth_client, monkeypatch
    ):
        main._manual_browser_status["state"] = "running"
        killed = False

        def fake_kill():
            nonlocal killed
            killed = True

        monkeypatch.setattr(main, "_kill_browser_processes", fake_kill)

        response = auth_client.post("/apply/start", json={"mode": "all", "workers": 1})

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "browser_profile_busy"
        assert killed is False


# ── LLM-dependent endpoints: validation branches + mocked success ─────────────
class TestLLMEndpoints:
    """These endpoints call create_llm / network. We never invoke the real
    dependency: we either hit the input-validation branch, or monkeypatch
    create_llm + the LLM's ainvoke to assert the success path."""

    def test_llm_test_invalid_key_message(self, auth_client, monkeypatch):
        """/llm/test surfaces a friendly message when create_llm raises an auth error."""
        from core import llm_factory

        def _boom(_settings):
            raise RuntimeError("Invalid api_key provided")

        monkeypatch.setattr(llm_factory, "create_llm", _boom)
        resp = auth_client.post("/llm/test", json={"provider": "openai"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert "Invalid API key" in body["message"]

    def test_llm_test_success_mocked(self, auth_client, monkeypatch):
        """/llm/test returns success when create_llm + test_connection are stubbed."""
        from core import llm_factory

        async def _ok(_llm):
            return "pong"

        monkeypatch.setattr(llm_factory, "create_llm", lambda s: object())
        monkeypatch.setattr(llm_factory, "test_connection", _ok)
        resp = auth_client.post("/llm/test", json={"provider": "openai"})
        assert resp.status_code == 200
        assert resp.json() == {"success": True, "message": "pong"}

    def test_cover_letter_requires_description(self, auth_client):
        """/cover-letter/generate validates that a job description is present."""
        resp = auth_client.post("/cover-letter/generate", json={"job_title": "Dev"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "missing_field"

    def test_cover_letter_requires_llm_configured(self, auth_client):
        """With no LLM provider configured the endpoint errors clearly (no network)."""
        # Persist an LLM settings file with an empty provider.
        auth_client.put("/settings/llm", json={"provider": ""})
        resp = auth_client.post(
            "/cover-letter/generate",
            json={"job_description": "Build things.", "job_title": "Dev", "company": "Acme"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "no_llm"

    def test_cover_letter_success_mocked(self, auth_client, monkeypatch):
        """Happy path: a configured + mocked LLM yields a cover letter string."""
        auth_client.put(
            "/settings/llm",
            json={"provider": "openai", "openai": {"api_key": "sk", "model": "gpt-4o"}},
        )

        class _Resp:
            from cover_letter import CoverLetterDecision, CoverLetterSpec

            completion = CoverLetterSpec(
                text="Dear Hiring Manager, ...",
                positioning="Connect delivery experience to the role.",
                decisions=[CoverLetterDecision(
                    decision="Lead with relevant delivery experience.",
                    requirement_ids=["J003"],
                    evidence_ids=["P001"],
                )],
            )

        class _LLM:
            async def ainvoke(self, _messages, **_kwargs):
                return _Resp()

        from core import llm_factory
        from core import shared_config

        monkeypatch.setattr(llm_factory, "create_llm", lambda s: _LLM())
        saved = []
        monkeypatch.setattr(
            shared_config,
            "update_job",
            lambda url, **fields: saved.append((url, fields)),
        )
        resp = auth_client.post(
            "/cover-letter/generate",
            json={
                "job_description": "Build things.",
                "job_title": "Dev",
                "company": "Acme",
                "job_url": "https://example.com/job",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["cover_letter"].startswith("Dear Hiring Manager")
        assert body["cover_letter_path"].endswith(
            "/Candidate_Dev_Cover_Letter.txt"
        )
        assert body["cover_letter_pdf_path"].endswith(
            "/Candidate_Dev_Cover_Letter.pdf"
        )
        assert saved == [
            (
                "https://example.com/job",
                {
                    "cover_letter": "Dear Hiring Manager, ...",
                    "cover_letter_path": body["cover_letter_path"],
                    "cover_letter_pdf_path": body["cover_letter_pdf_path"],
                },
            )
        ]

    def test_serves_old_text_only_cover_letter_as_pdf(self, auth_client, monkeypatch, tmp_path):
        """Opening an older text-only letter renders and serves its PDF."""
        import cover_letter
        from core import shared_config

        text_path = tmp_path / "job.cover-letter.txt"
        text_path.write_text("Dear team", encoding="utf-8")
        pdf_path = tmp_path / "job.cover-letter.pdf"
        updates = []

        def save_pdf(_url, text, title):
            assert text == "Dear team"
            assert title == "Developer"
            pdf_path.write_bytes(b"%PDF-letter")
            return pdf_path

        monkeypatch.setattr(
            cover_letter,
            "get_cover_letter_pdf_path",
            lambda _url: None,
        )
        monkeypatch.setattr(
            cover_letter,
            "get_cover_letter_path",
            lambda _url: text_path,
        )
        monkeypatch.setattr(
            cover_letter,
            "save_cover_letter_pdf",
            save_pdf,
        )
        monkeypatch.setattr(
            shared_config,
            "read_jobs",
            lambda: {"https://example.com/job": {"title": "Developer"}},
        )
        monkeypatch.setattr(
            shared_config,
            "update_job",
            lambda url, **fields: updates.append((url, fields)),
        )

        resp = auth_client.get(
            "/cover-letter/file-by-url",
            params={"job_url": "https://example.com/job"},
        )

        assert resp.status_code == 200
        assert resp.content == b"%PDF-letter"
        assert resp.headers["content-type"].startswith("application/pdf")
        assert updates == [
            (
                "https://example.com/job",
                {"cover_letter_pdf_path": str(pdf_path)},
            )
        ]


# ── Resume tailoring: validation branch (no LLM / no tailoring invoked) ───────
class TestResumeTailor:
    def test_tailor_requires_job_urls(self, auth_client):
        resp = auth_client.post("/resume/tailor", json={})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "missing_field"

    def test_tailor_refine_requires_job_url(self, auth_client):
        resp = auth_client.post("/resume/tailor/refine", json={"instruction": "shorter"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "missing_field"

    def test_get_tailored_by_url_requires_url(self, auth_client):
        resp = auth_client.post("/resume/tailor/get-by-url", json={})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "missing_field"

    def test_get_tailored_by_hash_not_found(self, auth_client):
        resp = auth_client.get("/resume/tailor/deadbeef")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    def test_get_tailored_pdf_by_url_not_found(self, auth_client):
        resp = auth_client.get(
            "/resume/tailor/pdf-by-url",
            params={"job_url": "https://example.com/missing"},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    def test_get_tailored_pdf_by_url_serves_pdf(
        self, auth_client, monkeypatch, tmp_path
    ):
        pdf = tmp_path / "tailored.pdf"
        pdf.write_bytes(b"%PDF-1.4\n%%EOF")
        import resume.tailor as tailor

        monkeypatch.setattr(
            tailor, "get_tailored_resume_path", lambda _job_url: str(pdf)
        )
        resp = auth_client.get(
            "/resume/tailor/pdf-by-url",
            params={"job_url": "https://example.com/job"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.headers["content-disposition"].startswith("inline;")
        assert resp.content.startswith(b"%PDF")

    def test_tailor_unknown_job_reported_per_url(self, auth_client):
        """tailor returns per-URL error status for jobs not in the queue (no LLM call)."""
        resp = auth_client.post(
            "/resume/tailor", json={"job_urls": ["https://example.com/missing"]}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["results"][0]["status"] == "error"
        assert body["results"][0]["message"] == "Job not found"

    def test_tailor_always_enables_every_field(self, auth_client, monkeypatch):
        import resume.tailor as tailor

        url = "https://example.com/all-fields"
        auth_client.post(
            "/jobs/add",
            json={"url": url, "title": "Engineer", "description": "Build systems"},
        )
        captured = {}

        async def fake_tailor(job_url, description, options, job_title):
            captured.update(options)
            return {"path": "/tmp/tailored.pdf", "content": "tailored"}

        monkeypatch.setattr(tailor, "tailor_resume", fake_tailor)
        resp = auth_client.post(
            "/resume/tailor",
            json={
                "job_urls": [url],
                "options": {
                    "skills": False,
                    "title": False,
                    "overview": False,
                    "experience": False,
                    "achievements": False,
                },
            },
        )

        assert resp.status_code == 200
        assert captured == {
            "skills": True,
            "title": True,
            "overview": True,
            "experience": True,
            "achievements": True,
        }


# ── Job-collection / apply request validation (no worker thread started) ──────
class TestCollectApplyValidation:
    def test_collect_rejects_out_of_range_max_jobs(self, auth_client):
        """max_jobs > 500 violates the pydantic CollectRequest model -> 422."""
        resp = auth_client.post("/jobs/collect", json={"max_jobs": 10000})
        assert resp.status_code == 422

    def test_collect_rejects_non_european_source(self, auth_client):
        resp = auth_client.post(
            "/jobs/collect",
            json={"title": "Engineer", "source": "ziprecruiter"},
        )
        assert resp.status_code == 422

    def test_collect_requires_a_title(self, auth_client, monkeypatch):
        monkeypatch.setattr(
            main,
            "load_profile",
            lambda: {"country": "IT", "target_job_titles": [], "target_locations": []},
        )

        resp = auth_client.post("/jobs/collect", json={"source": "indeed"})

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "missing_job_titles"

    def test_collect_worker_saves_normalized_jobspy_results(
        self, auth_client, monkeypatch
    ):
        from sources import jobspy_collector

        captured = {}
        queued = []
        monkeypatch.setattr(
            main,
            "load_profile",
            lambda: {
                "country": "IT",
                "target_job_titles": ["Engineer"],
                "target_locations": ["Milan", "Rome"],
                "blacklisted_companies": ["Outlier"],
            },
        )

        def fake_collect_jobs(**kwargs):
            captured.update(kwargs)
            return jobspy_collector.CollectionResult(
                jobs=[
                    {
                        "url": "https://jobs.example/one",
                        "title": "Engineer",
                        "company": "Example",
                        "location": "Milan",
                        "source": "indeed",
                        "easy_apply": None,
                        "status": "pending",
                    }
                ],
                errors=[],
                completed_queries=2,
                stopped=False,
            )

        monkeypatch.setattr(jobspy_collector, "collect_jobs", fake_collect_jobs)
        monkeypatch.setattr(
            main,
            "_enqueue_classification",
            lambda urls, force=False, mirror_to_collection=False: queued.extend(urls)
            or len(urls),
        )

        resp = auth_client.post(
            "/jobs/collect",
            json={
                "source": "indeed",
                "max_jobs": 5,
                "filters": {"date_posted": "1"},
            },
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        main._collection_thread.join(timeout=5)
        assert main._collection_thread.is_alive() is False

        assert captured["source"] == "indeed"
        assert captured["titles"] == ["Engineer"]
        assert captured["locations"] == ["Milan", "Rome"]
        assert captured["country_code"] == "IT"
        assert captured["max_jobs"] == 5
        assert captured["hours_old"] == 24
        assert captured["job_type"] is None
        assert captured["is_remote"] is False
        assert captured["blacklisted_companies"] == ["Outlier"]

        jobs = auth_client.get("/jobs").json()
        assert len(jobs) == 1
        assert jobs[0]["url"] == "https://jobs.example/one"
        status = auth_client.get("/jobs/collect/status").json()
        assert status["running"] is False
        assert status["collected"] == 1
        assert queued == ["https://jobs.example/one"]

    def test_autonomous_collection_tags_new_jobs_and_defers_classification(
        self, auth_client, monkeypatch
    ):
        from sources import jobspy_collector

        queued = []
        monkeypatch.setattr(
            main,
            "load_profile",
            lambda: {
                "country": "DE",
                "target_job_titles": ["Engineer"],
                "target_locations": ["Berlin"],
            },
        )
        monkeypatch.setattr(
            jobspy_collector,
            "collect_jobs",
            lambda **_kwargs: jobspy_collector.CollectionResult(
                jobs=[
                    {
                        "url": "https://jobs.example/autonomous",
                        "title": "Engineer",
                        "company": "Example",
                        "source": "indeed",
                        "status": "pending",
                    }
                ],
                errors=[],
                completed_queries=1,
                stopped=False,
            ),
        )
        monkeypatch.setattr(
            main,
            "_enqueue_classification",
            lambda urls, **_kwargs: queued.extend(urls) or len(urls),
        )

        response = auth_client.post(
            "/jobs/collect",
            json={"source": "indeed", "automation": True},
        )

        assert response.status_code == 200
        main._collection_thread.join(timeout=5)
        jobs = auth_client.get("/jobs").json()
        assert jobs[0]["autonomous_discovered_at"]
        assert main._collection_status["added_urls"] == [
            "https://jobs.example/autonomous"
        ]
        assert queued == []

    def test_collect_passes_jobspy_search_scope(self, auth_client, monkeypatch):
        from sources import jobspy_collector

        captured = {}
        monkeypatch.setattr(
            main,
            "load_profile",
            lambda: {
                "country": "DE",
                "target_job_titles": ["Engineer"],
                "target_locations": ["Munich"],
            },
        )

        def fake_collect_jobs(**kwargs):
            captured.update(kwargs)
            return jobspy_collector.CollectionResult([], [], 1, False)

        monkeypatch.setattr(jobspy_collector, "collect_jobs", fake_collect_jobs)

        resp = auth_client.post(
            "/jobs/collect",
            json={
                "source": "linkedin",
                "filters": {
                    "location": "Berlin",
                    "hours_old": "0.25",
                    "job_type": "fulltime",
                    "is_remote": "true",
                },
            },
        )

        assert resp.status_code == 200
        main._collection_thread.join(timeout=5)
        assert captured["locations"] == ["Berlin"]
        assert captured["hours_old"] == 0.25
        assert captured["job_type"] == "fulltime"
        assert captured["is_remote"] is True

    def test_collect_uses_saved_session_for_linkedin_search_url(
        self, auth_client, monkeypatch, tmp_path
    ):
        from core import shared_config
        from sources import linkedin_browser_collector

        captured = {}
        queued = []
        search_url = (
            "https://www.linkedin.com/jobs/search-results/?"
            "keywords=software%20engineer&geoId=103035651&f_TPR=r86400"
        )

        async def fake_scrape(**kwargs):
            captured.update(kwargs)
            return linkedin_browser_collector.LinkedInBrowserResult(
                jobs=[
                    {
                        "id": "li-4451533722",
                        "job_url": "https://www.linkedin.com/jobs/view/4451533722",
                        "title": "Software Engineer",
                        "company": "Example",
                        "location": "Berlin",
                        "description": "Build software.",
                        "easy_apply": True,
                    }
                ],
                errors=[],
                stopped=False,
            )

        monkeypatch.setattr(
            linkedin_browser_collector, "scrape_linkedin_search_url", fake_scrape
        )
        monkeypatch.setattr(main, "_get_browser_profile_dir", lambda: str(tmp_path))
        monkeypatch.setattr(
            shared_config,
            "browser_user_agent",
            lambda **_kwargs: "headed-user-agent",
        )
        monkeypatch.setattr(
            main,
            "_enqueue_classification",
            lambda urls, force=False, mirror_to_collection=False: queued.extend(urls)
            or len(urls),
        )

        response = auth_client.post(
            "/jobs/collect",
            json={"source": "linkedin", "max_jobs": 3, "search_url": search_url},
        )

        assert response.status_code == 200
        main._collection_thread.join(timeout=5)
        assert main._collection_thread.is_alive() is False
        assert captured["search_url"] == search_url
        assert captured["max_jobs"] == 3
        assert captured["profile_dir"] == str(tmp_path)
        assert captured["user_agent"] == "headed-user-agent"
        assert captured["blacklisted_companies"] == ["Alignerr", "Outlier"]
        jobs = auth_client.get("/jobs").json()
        assert jobs[0]["url"] == "https://www.linkedin.com/jobs/view/4451533722"
        assert jobs[0]["easy_apply"] is True
        assert queued == ["https://www.linkedin.com/jobs/view/4451533722"]

    def test_collect_rejects_invalid_linkedin_search_url(self, auth_client):
        response = auth_client.post(
            "/jobs/collect",
            json={
                "source": "linkedin",
                "search_url": "https://example.com/jobs/search/?keywords=engineer",
            },
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_linkedin_search_url"

    def test_collect_indeed_omits_freshness_for_job_type(self, auth_client, monkeypatch):
        from sources import jobspy_collector

        captured = {}
        monkeypatch.setattr(
            main,
            "load_profile",
            lambda: {
                "country": "DE",
                "target_job_titles": ["Engineer"],
                "target_locations": ["Berlin"],
            },
        )

        def fake_collect_jobs(**kwargs):
            captured.update(kwargs)
            return jobspy_collector.CollectionResult([], [], 1, False)

        monkeypatch.setattr(jobspy_collector, "collect_jobs", fake_collect_jobs)

        resp = auth_client.post(
            "/jobs/collect",
            json={
                "source": "indeed",
                "filters": {
                    "hours_old": "24",
                    "job_type": "fulltime",
                    "is_remote": "true",
                },
            },
        )

        assert resp.status_code == 200
        main._collection_thread.join(timeout=5)
        assert captured["hours_old"] is None
        assert captured["job_type"] == "fulltime"
        assert captured["is_remote"] is True

    def test_apply_rejects_invalid_mode(self, auth_client):
        """An unknown apply mode is rejected by the Literal field -> 422."""
        resp = auth_client.post("/apply/start", json={"mode": "telepathy"})
        assert resp.status_code == 422

    def test_apply_rejects_too_many_workers(self, auth_client):
        resp = auth_client.post("/apply/start", json={"mode": "easy", "workers": 99})
        assert resp.status_code == 422

    def test_failed_job_retry_passes_custom_max_steps_to_worker(
        self, auth_client, monkeypatch
    ):
        from cli import apply_jobs
        from core.shared_config import write_jobs

        job_url = "https://jobs.example/retry"
        write_jobs(
            {
                job_url: {
                    "url": job_url,
                    "title": "Backend Engineer",
                    "company": "Acme",
                    "status": "failed",
                    "easy_apply": True,
                }
            }
        )
        captured = {}

        async def fake_worker(
            name,
            worker_id,
            queue,
            profile,
            qa,
            applied_labels,
            easy_apply,
            stats,
            cancel_flag=None,
            max_steps=apply_jobs.DEFAULT_MAX_STEPS,
            progress=None,
            enable_linkedin_outreach=True,
            pre_submission_attempt_limit=1,
            notify=None,
        ):
            captured["max_steps"] = max_steps
            captured["enable_linkedin_outreach"] = enable_linkedin_outreach
            captured["pre_submission_attempt_limit"] = pre_submission_attempt_limit
            captured["notify"] = notify
            captured["job"] = queue.get_nowait()
            progress("Step 1: https://jobs.example/retry | next: Continue | actions: click")

        monkeypatch.setattr(main, "_kill_browser_processes", lambda: None)
        monkeypatch.setattr(apply_jobs, "worker", fake_worker)
        main._apply_status = {
            "running": False,
            "mode": None,
            "workers": 1,
            "log": [],
        }

        resp = auth_client.post(
            "/apply/start",
            json={
                "job_url": job_url,
                "mode": "all",
                "max_steps": 125,
                "linkedin_outreach_enabled": False,
            },
        )

        assert resp.status_code == 200
        main._apply_thread.join(timeout=5)
        assert captured["max_steps"] == 125
        assert captured["enable_linkedin_outreach"] is False
        assert captured["pre_submission_attempt_limit"] == 3
        assert callable(captured["notify"])
        assert captured["job"]["url"] == job_url
        assert any(
            "Step 1: https://jobs.example/retry" in line
            for line in main._apply_status["log"]
        )
        assert main._apply_status["running"] is False

    def test_unknown_application_outcome_cannot_be_started_again(
        self, auth_client, monkeypatch
    ):
        from core.shared_config import write_jobs

        job_url = "https://jobs.example/unknown"
        write_jobs(
            {
                job_url: {
                    "url": job_url,
                    "status": "failed",
                    "last_application_outcome": {
                        "type": "unknown_outcome",
                        "retryable": False,
                    },
                }
            }
        )
        kill_calls = []
        monkeypatch.setattr(
            main, "_kill_browser_processes", lambda: kill_calls.append(True)
        )

        response = auth_client.post(
            "/apply/start", json={"job_url": job_url, "mode": "all"}
        )

        assert response.status_code == 200
        assert response.json()["success"] is False
        assert "review the saved dossier" in response.json()["message"]
        assert kill_calls == []

    def test_reviewed_non_submission_returns_job_to_qualified(self, auth_client):
        from core.shared_config import read_jobs, write_jobs

        job_url = "https://jobs.example/retry-after-review"
        previous_outcome = {
            "type": "unknown_outcome",
            "retryable": False,
            "attempt_id": "attempt-123",
        }
        write_jobs(
            {
                job_url: {
                    "url": job_url,
                    "status": "failed",
                    "last_application_outcome": previous_outcome,
                }
            }
        )

        response = auth_client.post(
            "/jobs/application-outcome/resolve",
            json={
                "url": job_url,
                "attempt_id": "attempt-123",
                "decision": "not_submitted",
            },
        )

        assert response.status_code == 200
        assert response.json()["job"]["category"] == "qualified"
        saved = read_jobs()[job_url]
        assert saved["status"] == "pending"
        assert saved["screening"]["override"] == "qualified"
        assert saved["last_application_outcome"] is None
        assert saved["last_application_outcome_review"]["decision"] == "not_submitted"
        assert saved["last_application_outcome_review"]["previous_outcome"] == previous_outcome

    def test_reviewed_submission_is_recorded_as_manual_confirmation(self, auth_client):
        from core.shared_config import read_jobs, write_jobs

        job_url = "https://jobs.example/submitted-after-review"
        write_jobs(
            {
                job_url: {
                    "url": job_url,
                    "status": "failed",
                    "last_application_outcome": {
                        "type": "unknown_outcome",
                        "retryable": False,
                        "attempt_id": "attempt-456",
                    },
                }
            }
        )

        response = auth_client.post(
            "/jobs/application-outcome/resolve",
            json={
                "url": job_url,
                "attempt_id": "attempt-456",
                "decision": "submitted",
            },
        )

        assert response.status_code == 200
        saved = read_jobs()[job_url]
        assert saved["status"] == "applied"
        assert saved["application_status"] == "applied"
        assert saved["last_application_outcome"]["type"] == "manual_confirmation"
        assert saved["last_application_outcome"]["submission_confirmed"] is True
        assert saved["last_application_outcome_review"]["decision"] == "submitted"
        assert saved["applied_at"]

    def test_outcome_review_rejects_a_stale_dossier(self, auth_client):
        from core.shared_config import write_jobs

        job_url = "https://jobs.example/newer-attempt"
        write_jobs(
            {
                job_url: {
                    "url": job_url,
                    "status": "failed",
                    "last_application_outcome": {
                        "type": "unknown_outcome",
                        "retryable": False,
                        "attempt_id": "attempt-new",
                    },
                }
            }
        )

        response = auth_client.post(
            "/jobs/application-outcome/resolve",
            json={
                "url": job_url,
                "attempt_id": "attempt-old",
                "decision": "not_submitted",
            },
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "stale_application_attempt"

    def test_stop_apply_recovers_in_progress_jobs(
        self, auth_client, monkeypatch
    ):
        from core.shared_config import read_jobs, write_jobs

        write_jobs(
            {
                "https://jobs.example/active": {
                    "url": "https://jobs.example/active",
                    "status": "in_progress",
                }
            }
        )
        main._apply_status = {
            "running": False,
            "mode": None,
            "workers": 1,
            "log": [],
        }
        monkeypatch.setattr(main, "_kill_browser_processes", lambda: None)

        resp = auth_client.post("/apply/stop")

        assert resp.status_code == 200
        assert resp.json()["success"] is True
        recovered = read_jobs()["https://jobs.example/active"]
        assert recovered["status"] == "failed"
        assert recovered["last_application_outcome"]["type"] == "unknown_outcome"
        assert recovered["last_application_outcome"]["retryable"] is False


# ── Classification and deterministic screening ───────────────────────────────
class TestClassificationAndScreening:
    @staticmethod
    def _reset_worker():
        thread = main._classification_thread
        if thread and thread.is_alive():
            thread.join(timeout=5)
        with main._classification_lock:
            main._classification_queue.clear()
            main._classification_status = {
                "running": False,
                "total": 0,
                "completed": 0,
                "failed": 0,
                "current_job": None,
                "log": [],
                "error": None,
            }

    def test_batch_selects_missing_failed_and_stale_but_not_current(
        self, auth_client, monkeypatch
    ):
        from core.shared_config import write_jobs
        import job_classifier

        self._reset_worker()
        profile = main.load_profile()
        current_job = {"url": "current", "description": "Current description"}
        current = {
            "status": "complete",
            "description_hash": job_classifier.description_hash(current_job),
            "profile_hash": job_classifier.profile_hash(profile),
        }
        write_jobs(
            {
                "missing": {"url": "missing", "description": "Missing"},
                "failed": {
                    "url": "failed",
                    "description": "Failed",
                    "classification": {"status": "failed"},
                },
                "stale": {
                    "url": "stale",
                    "description": "Changed",
                    "classification": {
                        "status": "complete",
                        "description_hash": "old",
                        "profile_hash": job_classifier.profile_hash(profile),
                    },
                },
                "current": {**current_job, "classification": current},
            }
        )
        calls = []

        async def fake_process(url, *, force=False, client=None):
            calls.append((url, force))
            return "complete"

        monkeypatch.setattr(job_classifier, "classify_and_store_job", fake_process)

        response = auth_client.post("/jobs/classify", json={})
        assert response.json() == {"success": True, "queued": 3}
        main._classification_thread.join(timeout=5)
        assert calls == [
            ("missing", False),
            ("failed", False),
            ("stale", False),
        ]
        status = auth_client.get("/jobs/classify/status").json()
        assert status["running"] is False
        assert status["total"] == 3
        assert status["completed"] == 3

        response = auth_client.post(
            "/jobs/classify", json={"job_urls": ["current"], "force": False}
        )
        assert response.json()["queued"] == 0
        response = auth_client.post(
            "/jobs/classify", json={"job_urls": ["current"], "force": True}
        )
        assert response.json()["queued"] == 1
        main._classification_thread.join(timeout=5)
        assert calls[-1] == ("current", True)

    def test_stop_waits_for_active_call_and_skips_remaining_queue(
        self, auth_client, monkeypatch
    ):
        import threading
        from core.shared_config import write_jobs
        import job_classifier

        self._reset_worker()
        write_jobs(
            {
                "one": {"url": "one", "description": "One"},
                "two": {"url": "two", "description": "Two"},
            }
        )
        started = threading.Event()
        release = threading.Event()
        calls = []

        async def slow_process(url, *, force=False, client=None):
            calls.append(url)
            started.set()
            release.wait(timeout=5)
            return "complete"

        monkeypatch.setattr(job_classifier, "classify_and_store_job", slow_process)
        response = auth_client.post(
            "/jobs/classify", json={"job_urls": ["one", "two"]}
        )
        assert response.json()["queued"] == 2
        assert started.wait(timeout=2)
        assert auth_client.post("/jobs/classify/stop").json()["success"] is True
        release.set()
        main._classification_thread.join(timeout=5)

        assert calls == ["one"]
        status = auth_client.get("/jobs/classify/status").json()
        assert status["running"] is False
        assert status["completed"] == 1
        assert status["error"] == "cancelled"

    def test_classification_heartbeat_is_mirrored_to_collection_log(
        self, monkeypatch
    ):
        import asyncio
        from core.shared_config import write_jobs
        import job_classifier

        self._reset_worker()
        main._collection_status = {"running": False, "log": []}
        monkeypatch.setattr(main, "CLASSIFICATION_HEARTBEAT_SECONDS", 0.01)
        write_jobs(
            {
                "one": {
                    "url": "one",
                    "title": "Data Analyst",
                    "company": "Acme",
                    "description": "Analyze data",
                }
            }
        )

        async def slow_process(url, *, force=False, client=None):
            await asyncio.sleep(0.03)
            return "complete"

        monkeypatch.setattr(job_classifier, "classify_and_store_job", slow_process)

        assert (
            main._enqueue_classification(
                ["one"],
                mirror_to_collection=True,
            )
            == 1
        )
        main._classification_thread.join(timeout=5)

        status_log = main._classification_status["log"]
        collection_log = main._collection_status["log"]
        assert any("Classifying 1/1: Data Analyst at Acme" in line for line in status_log)
        assert any("Still classifying 1/1: Data Analyst at Acme" in line for line in status_log)
        assert any("Classified 1/1: Data Analyst at Acme" in line for line in status_log)
        assert any(line.startswith("🤖 Still classifying") for line in collection_log)

    def test_screening_rejection_does_not_block_targeted_apply(
        self, auth_client, monkeypatch
    ):
        from types import SimpleNamespace
        from core.shared_config import read_jobs, write_jobs

        write_jobs(
            {
                "https://jobs.example/rejected": {
                    "url": "https://jobs.example/rejected",
                    "status": "pending",
                    "easy_apply": True,
                    "classification": {
                        "status": "complete",
                        "facts": {
                            "required_languages": [
                                {
                                    "value": "German",
                                    "confidence": 0.95,
                                    "evidence": ["German required"],
                                }
                            ],
                            "sponsorship": {
                                "value": "unknown",
                                "confidence": 0,
                                "evidence": [],
                            },
                            "salary": {
                                "maximum": None,
                                "confidence": 0,
                            },
                        },
                    },
                }
            }
        )
        main.save_profile(
            {
                **main.load_profile(),
                "languages": ["English"],
            }
        )

        response = auth_client.post("/jobs/screen", json={})
        assert response.json() == {"success": True, "evaluated": 1}
        assert read_jobs()["https://jobs.example/rejected"]["screening"]["status"] == "rejected"

        monkeypatch.setattr(main, "_kill_browser_processes", lambda: None)
        monkeypatch.setattr(
            main,
            "_run_async_in_thread",
            lambda factory, status: SimpleNamespace(is_alive=lambda: False),
        )
        response = auth_client.post(
            "/apply/start",
            json={"job_url": "https://jobs.example/rejected", "mode": "easy"},
        )
        assert response.status_code == 200
        assert response.json()["success"] is True
        assert read_jobs()["https://jobs.example/rejected"]["screening"]["status"] == "rejected"
        main._apply_status = {
            "running": False,
            "mode": None,
            "workers": 1,
            "log": [],
        }


# ── Generic 404 ───────────────────────────────────────────────────────────────
class TestNotFound:
    def test_unknown_route_404(self, auth_client):
        resp = auth_client.get("/this/route/does/not/exist")
        assert resp.status_code == 404

    def test_ollama_models_unreachable_server(self, auth_client):
        """/llm/ollama-models gracefully reports failure for an unreachable host."""
        resp = auth_client.post(
            "/llm/ollama-models", json={"base_url": "http://127.0.0.1:1"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert body["models"] == []
