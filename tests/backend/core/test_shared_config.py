"""Unit tests for backend/core/shared_config.py.

Focuses on the pure / easily-isolated helpers: SSRF URL validation, question
normalisation, JSON load/save, jobs CRUD with file locking, credential refresh
behaviour, the memory-store singleton, the agent-history extractor, and the
memory-context builder. External boundaries (subprocess, MemoryStore) are
mocked; all file I/O lands in tmp dirs via the ``data_dir`` fixture.
"""
import json
import types

import pytest

from core import shared_config as sc


# ── validate_job_url ─────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "url",
    [
        "https://jobs.example.com/apply",
        "http://careers.acme.io",
        "https://www.linkedin.com/jobs/view/123",
    ],
)
def test_validate_job_url_allows_public_http(url):
    """Public http(s) URLs are accepted."""
    assert sc.validate_job_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/x",
        "http://127.0.0.1/x",
        "http://10.0.0.5/x",
        "http://192.168.1.1/x",
        "http://172.16.0.1/x",
        "http://169.254.1.1/x",
        "http://0.0.0.0/x",
        "ftp://example.com/x",  # non-http scheme
        "not a url",
        "",
        "https://",  # no host
    ],
)
def test_validate_job_url_rejects_private_or_invalid(url):
    """Private/internal hosts, non-http schemes, and garbage are rejected."""
    assert sc.validate_job_url(url) is False


def test_validate_job_url_ipv6_loopback_known_gap():
    """Document a source gap: bracketed IPv6 literals in the denylist never
    match what urlparse yields (it strips the brackets), so ``https://[::1]``
    is currently *accepted*. Pinned here so a future fix flips this test."""
    assert sc.validate_job_url("https://[::1]/x") is True


# ── normalize_question ───────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Hello, World!", "hello world"),
        ("  What's your name?  ", "whats your name"),
        ("Years   of   experience???", "years   of   experience"),
        ("UPPER", "upper"),
        ("", ""),
    ],
)
def test_normalize_question(raw, expected):
    """Punctuation is stripped, case lowered, and edges trimmed."""
    assert sc.normalize_question(raw) == expected


# ── load_json / save_json ────────────────────────────────────────────────────

def test_load_json_missing_default_list(tmp_path):
    """Missing file with no default returns an empty list (not dict)."""
    assert sc.load_json(tmp_path / "x.json") == []


def test_load_json_missing_explicit_default(tmp_path):
    """An explicit default is returned when the file is absent."""
    assert sc.load_json(tmp_path / "x.json", {"a": 1}) == {"a": 1}


def test_save_then_load_json_roundtrip(tmp_path):
    """Saved JSON is read back verbatim and no temp file lingers."""
    p = tmp_path / "nested" / "data.json"
    sc.save_json(p, {"k": [1, 2, 3]})
    assert sc.load_json(p) == {"k": [1, 2, 3]}
    assert not p.with_suffix(".tmp").exists()


def test_browser_headless_reads_current_setting(tmp_path, monkeypatch):
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(sc, "_settings_file", settings_file)

    settings_file.write_text(json.dumps({"browser_headless": True}))
    assert sc.browser_headless() is True

    settings_file.write_text(json.dumps({"browser_headless": False}))
    assert sc.browser_headless() is False


def test_linux_browser_prefers_user_installed_chrome(tmp_path, monkeypatch):
    user_chrome = tmp_path / ".local" / "bin" / "google-chrome"
    user_chrome.parent.mkdir(parents=True)
    user_chrome.touch()
    monkeypatch.setattr(sc.sys, "platform", "linux")
    monkeypatch.setattr(sc.Path, "home", lambda: tmp_path)

    assert sc.browser_executable_path() == str(user_chrome)


def test_browser_user_agent_matches_headed_browser_version(monkeypatch):
    monkeypatch.setattr(sc, "browser_headless", lambda: True)
    monkeypatch.setattr(sc, "_browser_major_version", lambda: "150")

    user_agent = sc.browser_user_agent()

    assert "Chrome/150.0.0.0" in user_agent
    assert "HeadlessChrome" not in user_agent


def test_browser_user_agent_is_unset_in_headed_mode(monkeypatch):
    monkeypatch.setattr(sc, "browser_headless", lambda: False)
    monkeypatch.setattr(
        sc,
        "_browser_major_version",
        lambda: pytest.fail("headed mode should not inspect the browser version"),
    )

    assert sc.browser_user_agent() is None


def test_profile_browser_uses_requested_virtual_display(tmp_path, monkeypatch):
    calls = {}

    class FakeProcess:
        def poll(self):
            return None

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_popen(command, **kwargs):
        calls["command"] = command
        calls["env"] = kwargs.get("env")
        return FakeProcess()

    monkeypatch.setattr(sc.sys, "platform", "linux")
    monkeypatch.setattr(sc, "DATA_DIR", tmp_path)
    monkeypatch.setattr(sc, "browser_executable_path", lambda: "/usr/bin/google-chrome")
    monkeypatch.setattr(sc.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(sc.urllib.request, "urlopen", lambda *_args, **_kwargs: FakeResponse())

    endpoint, process = sc.launch_profile_browser(
        tmp_path / "profile",
        headless=False,
        display=":99",
        start_url="https://www.linkedin.com/jobs/",
    )

    assert endpoint.startswith("http://127.0.0.1:")
    assert isinstance(process, FakeProcess)
    assert calls["env"]["DISPLAY"] == ":99"
    assert "--window-size=1440,900" in calls["command"]
    assert calls["command"][-1] == "https://www.linkedin.com/jobs/"


# ── jobs CRUD (read/write/update/claim) ──────────────────────────────────────

@pytest.fixture
def jobs_paths(tmp_path, monkeypatch):
    """Point the jobs file/lock at a tmp location for isolation."""
    jf = tmp_path / "jobs.json"
    monkeypatch.setattr(sc, "JOBS_FILE", jf)
    monkeypatch.setattr(sc, "JOBS_LOCK", tmp_path / "jobs.db.lock")
    return jf


def test_read_jobs_empty(jobs_paths):
    """Reading before any write yields an empty dict."""
    assert sc.read_jobs() == {}


def test_write_then_read_jobs(jobs_paths):
    """write_jobs persists and read_jobs returns it."""
    sc.write_jobs({"u1": {"status": "pending"}})
    assert sc.read_jobs() == {"u1": {"status": "pending"}}


def test_update_job_existing(jobs_paths):
    """update_job merges fields into an existing entry."""
    sc.write_jobs({"u1": {"status": "pending", "title": "Eng"}})
    sc.update_job("u1", status="done", company="Acme")
    jobs = sc.read_jobs()
    assert jobs["u1"] == {"status": "done", "title": "Eng", "company": "Acme"}


def test_update_job_missing_is_noop(jobs_paths):
    """Updating an unknown URL leaves the store unchanged."""
    sc.write_jobs({"u1": {"status": "pending"}})
    sc.update_job("ghost", status="done")
    assert sc.read_jobs() == {"u1": {"status": "pending"}}


def test_claim_job_success(jobs_paths):
    """A pending job is claimed and flipped to in_progress."""
    sc.write_jobs({"u1": {"status": "pending"}})
    assert sc.claim_job("u1") is True
    assert sc.read_jobs()["u1"]["status"] == "in_progress"


def test_claim_job_already_taken(jobs_paths):
    """A non-pending job cannot be claimed."""
    sc.write_jobs({"u1": {"status": "in_progress"}})
    assert sc.claim_job("u1") is False


def test_claim_job_unknown(jobs_paths):
    """Claiming an unknown URL returns False."""
    sc.write_jobs({})
    assert sc.claim_job("nope") is False


# ── refresh_credentials ──────────────────────────────────────────────────────

def test_refresh_credentials_no_cmd_returns_true(monkeypatch):
    """With no ADA_CMD configured, refresh is a silent success."""
    monkeypatch.setattr(sc, "ADA_CMD", ())
    assert sc.refresh_credentials() is True


def test_refresh_credentials_success(monkeypatch):
    """A successful subprocess run returns True."""
    monkeypatch.setattr(sc, "ADA_CMD", ("ada", "creds"))
    calls = {}

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(sc.subprocess, "run", fake_run)
    assert sc.refresh_credentials() is True
    assert calls["cmd"] == ("ada", "creds")


def test_refresh_credentials_failure(monkeypatch):
    """A CalledProcessError is caught and reported as False."""
    monkeypatch.setattr(sc, "ADA_CMD", ("ada", "creds"))

    def fake_run(cmd, **kwargs):
        raise sc.subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(sc.subprocess, "run", fake_run)
    assert sc.refresh_credentials() is False


def test_refresh_credentials_timeout(monkeypatch):
    """A TimeoutExpired is caught and reported as False."""
    monkeypatch.setattr(sc, "ADA_CMD", ("ada", "creds"))

    def fake_run(cmd, **kwargs):
        raise sc.subprocess.TimeoutExpired(cmd, 30)

    monkeypatch.setattr(sc.subprocess, "run", fake_run)
    assert sc.refresh_credentials() is False


# ── get_memory_store singleton ───────────────────────────────────────────────

def test_get_memory_store_is_singleton(monkeypatch):
    """get_memory_store constructs once and caches the instance."""
    instances = []

    class FakeStore:
        def __init__(self):
            instances.append(self)

    monkeypatch.setattr(sc, "MemoryStore", FakeStore)
    monkeypatch.setattr(sc, "_memory_store", None)
    first = sc.get_memory_store()
    second = sc.get_memory_store()
    assert first is second
    assert len(instances) == 1


# ── extract_from_history ─────────────────────────────────────────────────────

def _history_item(memory_text):
    """Build a fake agent-history item exposing model_output.memory."""
    model_output = types.SimpleNamespace(memory=memory_text)
    return types.SimpleNamespace(model_output=model_output)


def _result(items):
    return types.SimpleNamespace(history=items)


def test_extract_from_history_jobs_and_questions():
    """Tagged JOB_APPLIED and QUESTION markers are parsed out."""
    mem = (
        '@@JOB_APPLIED: {"title": "Engineer", "company": "Acme", "location": "NYC"}\n'
        '@@QUESTION: {"question": "Visa?", "answer": "No", "answered": true, "reason": null, "type": "radio"}'
    )
    jobs, questions = sc.extract_from_history(_result([_history_item(mem)]))
    assert jobs == ["Engineer at Acme - NYC"]
    assert questions == {"Visa?": "No"}


def test_extract_from_history_preserves_unanswered_question_without_reusable_answer():
    mem = (
        '@@QUESTION: {"question": "Why us?", "answer": null, "answered": false, '
        '"reason": "Employer prohibits AI answers", "type": "text"}'
    )

    _, questions = sc.extract_from_history(_result([_history_item(mem)]))

    assert questions == {"Why us?": ""}


def test_extract_from_history_recognizes_legacy_not_answered_text():
    mem = (
        '@@QUESTION: {"question": "Why us?", '
        '"answer": "Not answered: employer prohibits AI answers", "type": "text"}'
    )

    _, questions = sc.extract_from_history(_result([_history_item(mem)]))

    assert questions == {"Why us?": ""}


def test_extract_from_history_later_answer_fills_same_question():
    mem = (
        '@@QUESTION: {"question": "Visa?", "answer": null, "answered": false, '
        '"reason": "Not reached", "type": "radio"}\n'
        '@@QUESTION: {"question": "visa", "answer": "Yes", "answered": true, '
        '"reason": null, "type": "radio"}'
    )

    _, questions = sc.extract_from_history(_result([_history_item(mem)]))

    assert questions == {"Visa?": "Yes"}


def test_extract_from_history_dedupes_questions():
    """Questions that normalise identically are deduplicated."""
    mem = (
        '@@QUESTION: {"question": "Your Name?", "answer": "A"}\n'
        '@@QUESTION: {"question": "your name", "answer": "B"}'
    )
    _, questions = sc.extract_from_history(_result([_history_item(mem)]))
    assert questions == {"Your Name?": "A"}


def test_extract_from_history_skips_empty_and_bad_json():
    """Items with no output and malformed JSON are ignored gracefully."""
    items = [
        types.SimpleNamespace(model_output=None),
        _history_item('@@JOB_APPLIED: {bad json}'),
        _history_item('no markers here'),
    ]
    jobs, questions = sc.extract_from_history(_result(items))
    assert jobs == []
    assert questions == {}


def test_extract_from_history_fallback_phrase():
    """Free-text 'applied to X via' is captured when no JSON marker exists."""
    mem = "Successfully applied to Senior Dev via the company site."
    jobs, _ = sc.extract_from_history(_result([_history_item(mem)]))
    assert jobs == ["Senior Dev"]


def test_extract_from_history_deduplicates_jobs():
    """Duplicate job strings collapse to one (order preserved)."""
    mem = (
        '@@JOB_APPLIED: {"title": "X", "company": "Y", "location": "Z"}\n'
        '@@JOB_APPLIED: {"title": "X", "company": "Y", "location": "Z"}'
    )
    jobs, _ = sc.extract_from_history(_result([_history_item(mem)]))
    assert jobs == ["X at Y - Z"]


# ── build_memory_context ─────────────────────────────────────────────────────

@pytest.fixture
def base_profile(monkeypatch):
    """The deterministic part of the candidate profile."""
    monkeypatch.setattr(
        sc,
        "load_profile_markdown",
        lambda: "# Personal Details\nName: Ada Lovelace\nEmail: ada@example.com\n",
    )
    return {
        "country": "GB",
        "visa_sponsorship_needed": False,
        "target_job_titles": ["Software Engineer"],
        "target_locations": ["London", "Remote"],
        "languages": ["English"],
        "salary_expectation": {
            "min": 80000,
            "currency": "GBP",
            "period": "annual",
        },
    }


@pytest.fixture
def no_op_store(monkeypatch):
    """Stub the memory store so build_memory_context skips DB/website memory."""
    class FakeStore:
        def qa_get_all_for_prompt(self):
            return {}

        def get_domain_memories(self, url, limit=20):
            return []

        def extract_domain(self, url):
            return "example.com"

        def format_for_prompt(self, memories):
            return "MEM"

    monkeypatch.setattr(sc, "_memory_store", FakeStore())
    monkeypatch.setattr(
        sc,
        "load_profile_markdown",
        lambda: "# Personal Details\nName: Ada Lovelace\nEmail: ada@example.com\n",
    )
    return FakeStore


def test_build_memory_context_basic(base_profile, no_op_store):
    """The context contains only fixed settings and the Markdown profile."""
    ctx = sc.build_memory_context(base_profile, qa={})
    assert "FIXED SEARCH AND SCREENING SETTINGS:" in ctx
    assert "Name: Ada Lovelace" in ctx
    assert "GBP 80,000 minimum (annual)" in ctx
    assert "CANDIDATE MARKDOWN PROFILE:" in ctx
    assert "TRACKING INSTRUCTIONS:" not in ctx


def test_build_memory_context_passes_markdown_profile(base_profile, no_op_store):
    ctx = sc.build_memory_context(base_profile, qa={})

    assert "Country: GB" in ctx
    assert "Target job titles: Software Engineer" in ctx
    assert "Email: ada@example.com" in ctx


def test_build_memory_context_does_not_inject_qa(base_profile, no_op_store):
    ctx = sc.build_memory_context(base_profile, qa={"Q1": "A1", "blank": ""})
    assert "Q1" not in ctx
    assert "A1" not in ctx


def test_build_memory_context_does_not_inject_applied_labels(base_profile, no_op_store):
    ctx = sc.build_memory_context(base_profile, qa={}, applied_labels=["Job A", "Job B"])
    assert "Already applied" not in ctx
    assert "Job A" not in ctx


def test_build_memory_context_no_salary(base_profile, no_op_store):
    """A zero salary minimum renders 'Not specified'."""
    base_profile["salary_expectation"]["min"] = 0
    ctx = sc.build_memory_context(base_profile, qa={})
    assert "Minimum salary: Not specified" in ctx


def test_build_memory_context_injects_only_supplied_critical_memories(base_profile, monkeypatch):
    class FakeStore:
        def get_domain_memories(self, url, limit=20):
            raise AssertionError("website memories must not be queried")

    monkeypatch.setattr(sc, "_memory_store", FakeStore())
    ctx = sc.build_memory_context(
        base_profile,
        qa={},
        job_url="https://acme.io/job",
        critical_memories=[{"category": "navigation", "content": "Use the lower button"}],
    )
    assert "CRITICAL PLATFORM MEMORIES" in ctx
    assert "Use the lower button" in ctx
    assert "load_current_platform_critical_memories" in ctx
    assert "PLATFORM_INSIGHT:" in ctx
    assert "more than three" in ctx
    assert "questions or answers" in ctx


def test_build_memory_context_does_not_query_saved_qa(base_profile, monkeypatch):
    class FakeStore:
        def qa_get_all_for_prompt(self):
            raise AssertionError("saved Q&A must not be queried")

    monkeypatch.setattr(sc, "_memory_store", FakeStore())
    ctx = sc.build_memory_context(base_profile, qa={"OLDQ": "OLDA"})
    assert "DBQ" not in ctx
    assert "OLDQ" not in ctx
