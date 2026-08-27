#!/usr/bin/env python3
"""Fail when a public source snapshot contains private paths or obvious secrets."""

from __future__ import annotations

import ipaddress
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PATHS = {
    ".env",
    "AGENTS.md",
    "NOTES.md",
    "profile.md",
    "skills-lock.json",
}
FORBIDDEN_PREFIXES = (
    ".agents/",
    ".codex/",
    ".devtool/",
    ".venv/",
    "application_attempts/",
    "browser_profile/",
    "dist/",
    "node_modules/",
    "output/",
)
SECRET_PATTERNS = {
    "private key": re.compile(
        r"-----BEGIN (?:DSA|EC|OPENSSH|PGP|RSA) PRIVATE KEY-----"
    ),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    "OpenAI key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
}
IPV4_PATTERN = re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")
DEPENDENCY_METADATA = {"pyproject.toml", "uv.lock"}
VERSION_LIKE_IPS = {"150.0.0.0"}


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    return [ROOT / item.decode() for item in result.stdout.split(b"\0") if item]


def main() -> int:
    files = tracked_files()
    if not files:
        print("Public snapshot check failed: no tracked files")
        return 1

    findings: list[str] = []
    for path in files:
        relative = path.relative_to(ROOT).as_posix()
        if relative in FORBIDDEN_PATHS or relative.startswith(FORBIDDEN_PREFIXES):
            findings.append(f"forbidden public path: {relative}")
            continue
        if path.stat().st_size > 5 * 1024 * 1024:
            findings.append(f"unexpected tracked file over 5 MiB: {relative}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                findings.append(f"possible {label}: {relative}")
        for raw_ip in IPV4_PATTERN.findall(text):
            if relative in DEPENDENCY_METADATA or raw_ip in VERSION_LIKE_IPS:
                continue
            try:
                address = ipaddress.ip_address(raw_ip)
            except ValueError:
                continue
            if address.is_global:
                findings.append(f"hard-coded public IP address: {relative}")

    if findings:
        print("Public snapshot check failed:")
        for finding in sorted(set(findings)):
            print(f"- {finding}")
        return 1

    print("Public snapshot check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
