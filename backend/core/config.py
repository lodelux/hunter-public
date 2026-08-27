"""
Configuration management for Hunter.
All settings stored in OS-appropriate app data directory.
No hardcoded values.
"""
import json
import os
import platform
from pathlib import Path

try:
    from sources.company_filter import DEFAULT_BLACKLISTED_COMPANIES
except ImportError:
    from backend.sources.company_filter import DEFAULT_BLACKLISTED_COMPANIES


def get_data_dir() -> Path:
    """Get OS-appropriate data directory for the app."""
    system = platform.system()
    if system == "Darwin":
        base = Path.home() / "Library" / "Application Support" / "langhire"
    elif system == "Windows":
        base = Path.home() / "AppData" / "Roaming" / "langhire"
    else:  # Linux
        base = Path.home() / ".config" / "langhire"
    base.mkdir(parents=True, exist_ok=True)
    return base


def get_profile_markdown_path() -> Path:
    """Use the checkout's tracked profile when Hunter runs from a repository."""
    configured_root = os.environ.get("HUNTER_PROJECT_ROOT")
    if configured_root:
        return Path(configured_root).expanduser() / "profile.md"

    for root in (Path.cwd(), Path(__file__).resolve().parents[2]):
        if (root / ".git").exists() or (
            (root / "pyproject.toml").exists() and (root / "backend").is_dir()
        ):
            return root / "profile.md"

    return get_data_dir() / "profile.md"


def _load_json(path: Path, default=None):
    if path.exists():
        return json.loads(path.read_text())
    return default if default is not None else {}


def _save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)
    if any(s in path.name for s in ("llm_settings", "settings")):
        try:
            import os, stat
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            import logging
            logging.getLogger("config").warning(
                f"Could not set restrictive permissions on {path.name}"
            )


# ── Profile ───────────────────────────────────────────────────────────────

PROFILE_DEFAULTS = {
    "country": "US",
    "target_job_titles": [],
    "target_locations": [],
    "blacklisted_companies": DEFAULT_BLACKLISTED_COMPANIES,
    "languages": ["English"],
    "visa_sponsorship_needed": False,
    "salary_expectation": {
        "min": 50000,
        "currency": "USD",
        "period": "annual",
    },
}

PROFILE_MARKDOWN_TEMPLATE = """# Personal Details
Name:
Email:
Phone:
Date of birth:
Nationality:
Address:

# Professional Profile
Current role:
Years of experience:
Summary:

# Experience

# Education

# Skills

# Work Preferences
Work authorization:
Preferred work mode:
Willing to relocate:
Notice period:

# Application Materials
## Cover Letter Instructions

# Additional Notes
"""


def _structured_profile(profile: dict) -> dict:
    """Keep only values required by deterministic collection and screening."""
    salary = profile.get("salary_expectation")
    if not isinstance(salary, dict):
        salary = {}

    def _list(name: str, default: list[str]) -> list[str]:
        value = profile.get(name, default)
        if not isinstance(value, list):
            return list(default)
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]

    minimum = salary.get("min", PROFILE_DEFAULTS["salary_expectation"]["min"])
    if not isinstance(minimum, (int, float)):
        minimum = PROFILE_DEFAULTS["salary_expectation"]["min"]

    return {
        "country": str(profile.get("country") or PROFILE_DEFAULTS["country"]),
        "target_job_titles": _list("target_job_titles", []),
        "target_locations": _list("target_locations", []),
        "blacklisted_companies": _list(
            "blacklisted_companies",
            DEFAULT_BLACKLISTED_COMPANIES,
        ),
        "languages": _list("languages", ["English"]),
        "visa_sponsorship_needed": profile.get("visa_sponsorship_needed") is True,
        "salary_expectation": {
            "min": minimum,
            "currency": str(
                salary.get("currency")
                or PROFILE_DEFAULTS["salary_expectation"]["currency"]
            ),
            "period": str(
                salary.get("period")
                or PROFILE_DEFAULTS["salary_expectation"]["period"]
            ),
        },
    }


def _legacy_profile_to_markdown(profile: dict) -> str:
    """Render the old free-form profile fields into the new Markdown document."""
    if not profile:
        return PROFILE_MARKDOWN_TEMPLATE

    address = profile.get("address") if isinstance(profile.get("address"), dict) else {}
    education = (
        profile.get("education") if isinstance(profile.get("education"), dict) else {}
    )
    skills = profile.get("skills") if isinstance(profile.get("skills"), list) else []
    phone = " ".join(
        part for part in (
            str(profile.get("phone_country_code") or "").strip(),
            str(profile.get("phone") or "").strip(),
        ) if part
    )
    address_text = ", ".join(
        str(address.get(field) or "").strip()
        for field in ("street", "city", "state", "zip", "country")
        if str(address.get(field) or "").strip()
    )
    education_text = ", ".join(
        str(education.get(field) or "").strip()
        for field in ("degree", "school", "graduation")
        if str(education.get(field) or "").strip()
    )
    skill_text = "\n".join(
        f"- {skill.strip()}"
        for skill in skills
        if isinstance(skill, str) and skill.strip()
    )

    return f"""# Personal Details
Name: {profile.get("name", "")}
Email: {profile.get("email", "")}
Phone: {phone}
Date of birth: {profile.get("date_of_birth", "")}
Nationality: {profile.get("nationality", "")}
Address: {address_text}

# Professional Profile
Current role: {profile.get("current_role", "")}
Years of experience: {profile.get("years_of_experience", "")}
Summary:

# Experience

# Education
{education_text}

# Skills
{skill_text}

# Work Preferences
Work authorization: {profile.get("work_authorization", "")}
Preferred work mode: {profile.get("preferred_work_mode", "")}
Willing to relocate: {profile.get("willing_to_relocate", "")}
Notice period: {profile.get("notice_period", "")}

# Application Materials
## Cover Letter Instructions

# Additional Notes
{profile.get("notes", "")}
"""


def _save_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _migrate_legacy_profile() -> dict:
    """Create profile.md once and reduce the legacy JSON to deterministic fields."""
    data_dir = get_data_dir()
    profile_path = data_dir / "candidate_profile.json"
    markdown_path = get_profile_markdown_path()
    raw = _load_json(profile_path, {})
    if not isinstance(raw, dict):
        raw = {}
    structured = _structured_profile(raw)

    if not markdown_path.exists():
        _save_text(markdown_path, _legacy_profile_to_markdown(raw))

    if profile_path.exists() and raw != structured:
        backup_path = data_dir / "candidate_profile.legacy.json"
        if not backup_path.exists():
            _save_json(backup_path, raw)
        _save_json(profile_path, structured)

    return structured


def load_profile() -> dict:
    structured = _migrate_legacy_profile()
    return {
        **structured,
        "markdown": get_profile_markdown_path().read_text(encoding="utf-8"),
    }


def save_profile(profile: dict):
    _migrate_legacy_profile()
    _save_json(
        get_data_dir() / "candidate_profile.json",
        _structured_profile(profile),
    )


def load_profile_markdown() -> str:
    _migrate_legacy_profile()
    return get_profile_markdown_path().read_text(encoding="utf-8")


def save_profile_markdown(markdown: str):
    _migrate_legacy_profile()
    _save_text(get_profile_markdown_path(), str(markdown))


# ── LLM Settings ──────────────────────────────────────────────────────────

def load_llm_settings() -> dict:
    return _load_json(get_data_dir() / "llm_settings.json", {
        "provider": "openrouter",
        "openai": {"api_key": "", "model": "gpt-5.6-sol"},
        "anthropic": {"api_key": "", "model": "claude-sonnet-4-5"},
        "bedrock": {"access_key": "", "secret_key": "", "region": "us-west-2", "model": "us.anthropic.claude-sonnet-4-6"},
        "gemini": {"api_key": "", "model": "gemini-2.5-pro"},
        "ollama": {"base_url": "http://localhost:11434", "model": ""},
        "openrouter": {"api_key": "", "model": "qwen/qwen3.6-plus"},
    })


def save_llm_settings(settings: dict):
    _save_json(get_data_dir() / "llm_settings.json", settings)


# ── App Settings ──────────────────────────────────────────────────────────

SETTINGS_DEFAULTS = {
    "resume_path": "",
    "blocked_domains": [],
    "sensitive_data": {"email": "", "password": ""},
    "max_failures": 8,
    "stagger_delay": 5,
    "browser_headless": False,
    "theme": "system",
    "dossier_retention_enabled": True,
    "dossier_retention_days": 14,
    "dossier_delete_after_days": 30,
    "auto_reject_after_months": 1,
    "memory_auto_rerank": True,
    "critical_memory_max_count": 5,
    "critical_memory_max_tokens": 500,
}


def load_settings() -> dict:
    saved = _load_json(get_data_dir() / "settings.json", {})
    if not isinstance(saved, dict):
        saved = {}
    settings = {**SETTINGS_DEFAULTS, **saved}
    sensitive = saved.get("sensitive_data")
    settings["sensitive_data"] = {
        **SETTINGS_DEFAULTS["sensitive_data"],
        **(sensitive if isinstance(sensitive, dict) else {}),
    }
    settings["data_dir"] = str(get_data_dir())
    return settings


def save_settings(updates: dict):
    """Merge a partial settings update without discarding unrelated fields."""
    updates = dict(updates)
    if "max_failures" in updates:
        updates["max_failures"] = max(1, min(int(updates["max_failures"]), 50))
    if "stagger_delay" in updates:
        updates["stagger_delay"] = max(0, min(int(updates["stagger_delay"]), 300))
    if "dossier_retention_days" in updates:
        updates["dossier_retention_days"] = max(
            1, min(int(updates["dossier_retention_days"]), 3650)
        )
    if "dossier_delete_after_days" in updates:
        updates["dossier_delete_after_days"] = max(
            1, min(int(updates["dossier_delete_after_days"]), 3650)
        )
    if "dossier_retention_enabled" in updates:
        updates["dossier_retention_enabled"] = bool(
            updates["dossier_retention_enabled"]
        )
    if "auto_reject_after_months" in updates:
        updates["auto_reject_after_months"] = max(
            1, min(int(updates["auto_reject_after_months"]), 120)
        )
    if "memory_auto_rerank" in updates:
        updates["memory_auto_rerank"] = bool(updates["memory_auto_rerank"])
    if "critical_memory_max_count" in updates:
        updates["critical_memory_max_count"] = max(
            1, min(int(updates["critical_memory_max_count"]), 20)
        )
    if "critical_memory_max_tokens" in updates:
        updates["critical_memory_max_tokens"] = max(
            100, min(int(updates["critical_memory_max_tokens"]), 4000)
        )
    if "blocked_domains" in updates:
        updates["blocked_domains"] = [
            str(d).strip() for d in updates["blocked_domains"]
            if isinstance(d, str) and d.strip()
        ]
    settings = load_settings()
    if isinstance(updates.get("sensitive_data"), dict):
        updates["sensitive_data"] = {
            **settings["sensitive_data"],
            **updates["sensitive_data"],
        }
    settings.update(updates)
    settings["dossier_delete_after_days"] = max(
        int(settings["dossier_retention_days"]),
        int(settings["dossier_delete_after_days"]),
    )
    settings.pop("data_dir", None)
    _save_json(get_data_dir() / "settings.json", settings)
