#!/usr/bin/env python3
"""Run Hunter remotely and alert Telegram on crashes, health loss, or pauses."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent
BACKEND = PROJECT_DIR / ".venv" / "bin" / "python"
BACKEND_COMMAND = [str(BACKEND), "backend/main.py"]
HEALTH_URL = "http://127.0.0.1:8743/health"
AUTOMATION_URL = "http://127.0.0.1:8743/automation"
CHECK_SECONDS = 15
HEALTH_FAILURE_LIMIT = 4


def notify(message: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Telegram notification skipped: missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        return False
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return 200 <= response.status < 300
    except (OSError, urllib.error.URLError) as exc:
        print(f"Telegram notification failed: {type(exc).__name__}", file=sys.stderr)
        return False


def fetch_json(url: str, token: str = "") -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-notification", action="store_true")
    args = parser.parse_args()
    if args.test_notification:
        return 0 if notify("Hunter remote watchdog is configured.") else 1

    if not BACKEND.is_file():
        notify(f"Hunter failed to start: Python environment is missing on {os.uname().nodename}.")
        return 1

    intentional_stop = False
    child: subprocess.Popen | None = None

    def stop_child(_signum, _frame):
        nonlocal intentional_stop
        intentional_stop = True
        if child is not None and child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGINT, stop_child)
    signal.signal(signal.SIGTERM, stop_child)

    child = subprocess.Popen(BACKEND_COMMAND, cwd=PROJECT_DIR, env=os.environ.copy())
    health_failures = 0
    last_pause_reason = None
    started_at = time.monotonic()
    token = os.environ.get("JOB_APPLICANT_TOKEN", "").strip()

    while child.poll() is None:
        time.sleep(CHECK_SECONDS)
        if time.monotonic() - started_at < 20:
            continue
        try:
            fetch_json(HEALTH_URL)
            health_failures = 0
        except (OSError, ValueError, urllib.error.URLError):
            health_failures += 1
            if health_failures >= HEALTH_FAILURE_LIMIT:
                notify(
                    f"Hunter health checks failed {health_failures} times on "
                    f"{os.uname().nodename}; the autonomous runner was stopped."
                )
                child.terminate()
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill()
                return 1
            continue

        try:
            automation = fetch_json(AUTOMATION_URL, token)
            status = automation.get("status") or {}
            reason = status.get("pause_reason") if status.get("state") == "paused" else None
            if reason and reason != last_pause_reason:
                notify(f"Hunter autonomy paused on {os.uname().nodename}: {reason}")
                last_pause_reason = reason
            elif not reason:
                last_pause_reason = None
        except (OSError, ValueError, urllib.error.URLError):
            pass

    return_code = child.returncode or 0
    if not intentional_stop:
        notify(
            f"Hunter crashed on {os.uname().nodename} with exit code {return_code}. "
            "Autonomous work is stopped."
        )
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
