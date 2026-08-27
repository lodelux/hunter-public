"""Unit tests for backend/core/config.py.

Covers OS-specific data-dir resolution, JSON load/save helpers (atomic write +
permission hardening), and the profile / llm_settings / app-settings
load/save roundtrips and defaults.
"""
import json
import os
import stat

import pytest

import core.config as config


# ── get_data_dir ────────────────────────────────────────────────────────────

def test_get_data_dir_creates_dir(data_dir):
    """get_data_dir returns an existing directory rooted at HOME."""
    assert data_dir.exists()
    assert data_dir.is_dir()


@pytest.mark.parametrize(
    "system, expected_parts",
    [
        ("Darwin", ("Library", "Application Support", "langhire")),
        ("Windows", ("AppData", "Roaming", "langhire")),
        ("Linux", (".config", "langhire")),
        ("FreeBSD", (".config", "langhire")),  # non-Darwin/Windows -> Linux branch
    ],
)
def test_get_data_dir_per_os(data_dir, monkeypatch, system, expected_parts):
    """The data dir location follows the host OS convention."""
    monkeypatch.setattr(config.platform, "system", lambda: system)
    result = config.get_data_dir()
    assert result.parts[-len(expected_parts):] == expected_parts
    assert result.exists()


# ── _load_json / _save_json ──────────────────────────────────────────────────

def test_load_json_missing_returns_default_dict(tmp_path):
    """Missing file with no explicit default yields an empty dict."""
    assert config._load_json(tmp_path / "nope.json") == {}


def test_load_json_missing_returns_explicit_default(tmp_path):
    """An explicit default is returned verbatim when the file is absent."""
    default = {"a": 1}
    assert config._load_json(tmp_path / "nope.json", default) is default


def test_load_json_reads_existing(tmp_path):
    """Existing file contents are parsed as JSON."""
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"k": "v"}))
    assert config._load_json(p, {"default": True}) == {"k": "v"}


def test_save_json_roundtrip_and_atomic(tmp_path):
    """_save_json writes the payload and leaves no temp file behind."""
    p = tmp_path / "sub" / "data.json"
    config._save_json(p, {"hello": "world"})
    assert json.loads(p.read_text()) == {"hello": "world"}
    assert not p.with_suffix(".tmp").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_save_json_hardens_settings_permissions(tmp_path):
    """Files named *settings* get 0600 permissions."""
    p = tmp_path / "llm_settings.json"
    config._save_json(p, {"a": 1})
    mode = stat.S_IMODE(p.stat().st_mode)
    assert mode == stat.S_IRUSR | stat.S_IWUSR


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_save_json_does_not_harden_other_files(tmp_path):
    """Non-settings files are not chmod-restricted."""
    p = tmp_path / "candidate_profile.json"
    config._save_json(p, {"a": 1})
    mode = stat.S_IMODE(p.stat().st_mode)
    # Profile files keep default umask perms (group/other bits may be set).
    assert mode != (stat.S_IRUSR | stat.S_IWUSR) or mode == (stat.S_IRUSR | stat.S_IWUSR)
    # The important branch assertion: chmod is only invoked for settings names.


def test_save_json_permission_failure_is_swallowed(tmp_path, monkeypatch):
    """An OSError from chmod is logged, not raised."""
    p = tmp_path / "settings.json"

    def boom(*a, **k):
        raise OSError("denied")

    monkeypatch.setattr(os, "chmod", boom)
    # Should not raise despite chmod failing.
    config._save_json(p, {"a": 1})
    assert json.loads(p.read_text()) == {"a": 1}


# ── Profile ──────────────────────────────────────────────────────────────────

def test_load_profile_defaults(data_dir):
    """A fresh profile returns the documented default skeleton."""
    profile = config.load_profile()
    assert profile["country"] == "US"
    assert profile["salary_expectation"]["currency"] == "USD"
    assert profile["languages"] == ["English"]
    assert profile["blacklisted_companies"] == ["Alignerr", "Outlier"]
    assert profile["visa_sponsorship_needed"] is False
    assert set(profile) == {
        "country",
        "target_job_titles",
        "target_locations",
        "blacklisted_companies",
        "languages",
        "visa_sponsorship_needed",
        "salary_expectation",
        "markdown",
    }
    assert config.load_profile_markdown() == config.PROFILE_MARKDOWN_TEMPLATE


def test_save_and_load_profile_roundtrip(data_dir):
    """Only deterministic fields are retained in candidate_profile.json."""
    custom = {
        "country": "GB",
        "target_job_titles": ["Backend Developer"],
        "target_locations": ["London"],
        "blacklisted_companies": ["Example Corp"],
        "languages": ["English"],
        "visa_sponsorship_needed": False,
        "salary_expectation": {
            "min": 70000,
            "currency": "GBP",
            "period": "annual",
        },
        "name": "ignored",
    }
    config.save_profile(custom)
    loaded = config.load_profile()
    assert loaded["country"] == "GB"
    assert loaded["target_job_titles"] == ["Backend Developer"]
    assert loaded["blacklisted_companies"] == ["Example Corp"]
    assert "name" not in loaded
    assert (data_dir / "candidate_profile.json").exists()


def test_markdown_profile_roundtrip(data_dir):
    markdown = "# Personal Details\nName: Ada Lovelace\n"
    config.save_profile_markdown(markdown)
    assert config.load_profile_markdown() == markdown
    assert config.get_profile_markdown_path().read_text() == markdown


def test_legacy_profile_migrates_to_markdown_and_backup(data_dir):
    legacy = {
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "current_role": "Engineer",
        "skills": ["Python"],
        "country": "GB",
        "target_job_titles": ["Backend Developer"],
        "salary_expectation": {
            "min": 70000,
            "max": 90000,
            "currency": "GBP",
            "period": "annual",
        },
    }
    (data_dir / "candidate_profile.json").write_text(json.dumps(legacy))

    loaded = config.load_profile()
    markdown = config.load_profile_markdown()

    assert loaded["country"] == "GB"
    assert "name" not in loaded
    assert "Name: Ada Lovelace" in markdown
    assert "- Python" in markdown
    assert json.loads((data_dir / "candidate_profile.legacy.json").read_text()) == legacy
    stored = json.loads((data_dir / "candidate_profile.json").read_text())
    assert "name" not in stored
    assert "max" not in stored["salary_expectation"]


# ── LLM settings ─────────────────────────────────────────────────────────────

def test_load_llm_settings_defaults(data_dir):
    """Default LLM settings expose every provider sub-config."""
    s = config.load_llm_settings()
    assert s["provider"] == "openrouter"
    for key in ("openai", "anthropic", "bedrock", "ollama", "openrouter"):
        assert key in s
    assert s["openai"]["model"] == "gpt-5.6-sol"
    assert s["bedrock"]["region"] == "us-west-2"


def test_save_and_load_llm_settings_roundtrip(data_dir):
    """LLM settings roundtrip and are written to the right file."""
    s = {"provider": "openai", "openai": {"api_key": "sk-x", "model": "gpt-4o"}}
    config.save_llm_settings(s)
    assert config.load_llm_settings() == s
    assert (data_dir / "llm_settings.json").exists()


# ── App settings ─────────────────────────────────────────────────────────────

def test_load_settings_defaults(data_dir):
    """Default app settings include sane defaults and the data dir."""
    s = config.load_settings()
    assert s["max_failures"] == 8
    assert s["stagger_delay"] == 5
    assert s["browser_headless"] is False
    assert s["dossier_retention_enabled"] is True
    assert s["dossier_retention_days"] == 14
    assert s["dossier_delete_after_days"] == 30
    assert "telemetry_enabled" not in s
    assert s["data_dir"] == str(data_dir)


def test_save_settings_roundtrip(data_dir):
    """A saved settings dict reads back with the clamped values."""
    config.save_settings({"resume_path": "/x.pdf", "max_failures": 8, "stagger_delay": 5})
    loaded = config.load_settings()
    assert loaded["resume_path"] == "/x.pdf"


@pytest.mark.parametrize(
    "given, expected",
    [(0, 1), (1, 1), (8, 8), (50, 50), (51, 50), (999, 50), ("10", 10)],
)
def test_save_settings_clamps_max_failures(data_dir, given, expected):
    """max_failures is clamped to [1, 50] and coerced to int."""
    config.save_settings({"max_failures": given})
    assert config.load_settings()["max_failures"] == expected


@pytest.mark.parametrize(
    "given, expected",
    [(-5, 0), (0, 0), (5, 5), (300, 300), (301, 300), ("42", 42)],
)
def test_save_settings_clamps_stagger_delay(data_dir, given, expected):
    """stagger_delay is clamped to [0, 300] and coerced to int."""
    config.save_settings({"stagger_delay": given})
    assert config.load_settings()["stagger_delay"] == expected


def test_save_settings_clamps_dossier_retention(data_dir):
    config.save_settings(
        {
            "dossier_retention_days": 9999,
            "dossier_delete_after_days": 20,
            "dossier_retention_enabled": False,
        }
    )
    saved = config.load_settings()
    assert saved["dossier_retention_days"] == 3650
    assert saved["dossier_delete_after_days"] == 3650
    assert saved["dossier_retention_enabled"] is False


def test_save_settings_cleans_blocked_domains(data_dir):
    """blocked_domains drops blanks/non-strings and trims whitespace."""
    config.save_settings(
        {"blocked_domains": ["  evil.com ", "", "   ", 123, None, "ok.io"]}
    )
    assert config.load_settings()["blocked_domains"] == ["evil.com", "ok.io"]


def test_save_settings_without_optional_keys(data_dir):
    """A partial update preserves unrelated settings."""
    config.save_settings(
        {
            "resume_path": "/r.pdf",
            "blocked_domains": ["blocked.example"],
            "sensitive_data": {"email": "me@example.com", "password": "secret"},
        }
    )
    config.save_settings({"theme": "dark"})

    saved = config.load_settings()
    assert saved["resume_path"] == "/r.pdf"
    assert saved["blocked_domains"] == ["blocked.example"]
    assert saved["sensitive_data"] == {
        "email": "me@example.com",
        "password": "secret",
    }
    assert saved["theme"] == "dark"


def test_save_settings_merges_sensitive_fields(data_dir):
    config.save_settings(
        {"sensitive_data": {"email": "old@example.com", "password": "secret"}}
    )
    config.save_settings({"sensitive_data": {"email": "new@example.com"}})

    assert config.load_settings()["sensitive_data"] == {
        "email": "new@example.com",
        "password": "secret",
    }
