"""Small, read-only snapshot of the machine running Hunter."""

from __future__ import annotations

import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil

from core.config import get_data_dir


_HUNTER_DATA_CACHE_SECONDS = 60.0
_hunter_data_cache: tuple[Path, float, int] | None = None


def _usage_payload(usage: Any) -> dict[str, float | int]:
    return {
        "percent": round(float(usage.percent), 1),
        "used_bytes": int(usage.used),
        "total_bytes": int(usage.total),
    }


def _directory_size_bytes(root: Path) -> int:
    """Best-effort size of regular files below root without following symlinks."""
    total = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _hunter_data_bytes(data_dir: Path) -> int:
    """Measure Hunter's application data at most once per minute."""
    global _hunter_data_cache

    now = time.monotonic()
    if (
        _hunter_data_cache is not None
        and _hunter_data_cache[0] == data_dir
        and now - _hunter_data_cache[1] < _HUNTER_DATA_CACHE_SECONDS
    ):
        return _hunter_data_cache[2]

    size = _directory_size_bytes(data_dir)
    _hunter_data_cache = (data_dir, now, size)
    return size


def _temperature_payload() -> dict[str, bool | float | str | None]:
    try:
        groups = psutil.sensors_temperatures()
    except (AttributeError, NotImplementedError, OSError):
        groups = {}

    hottest: tuple[str, Any] | None = None
    for group, readings in groups.items():
        for reading in readings:
            current = getattr(reading, "current", None)
            if current is None or not math.isfinite(float(current)):
                continue
            if not -20 <= float(current) <= 150:
                continue
            if hottest is None or float(current) > float(hottest[1].current):
                hottest = (group, reading)

    if hottest is None:
        return {
            "available": False,
            "celsius": None,
            "sensor": None,
            "high_celsius": None,
            "critical_celsius": None,
        }

    group, reading = hottest
    label = str(getattr(reading, "label", "") or "").strip()
    return {
        "available": True,
        "celsius": round(float(reading.current), 1),
        "sensor": label or group,
        "high_celsius": _optional_temperature(getattr(reading, "high", None)),
        "critical_celsius": _optional_temperature(getattr(reading, "critical", None)),
    }


def _optional_temperature(value: Any) -> float | None:
    if value is None:
        return None
    number = float(value)
    return round(number, 1) if math.isfinite(number) and 0 < number <= 200 else None


def _headroom(
    cpu_percent: float,
    memory: dict[str, float | int],
    swap: dict[str, float | int],
    disk: dict[str, float | int],
    temperature: dict[str, bool | float | str | None],
) -> dict[str, str]:
    checks = [
        ("CPU", cpu_percent, 85.0, 98.0, "%"),
        ("Memory", float(memory["percent"]), 85.0, 95.0, "%"),
        ("Disk", float(disk["percent"]), 85.0, 95.0, "%"),
    ]
    if int(swap["total_bytes"]) > 0:
        checks.append(("Swap", float(swap["percent"]), 50.0, 85.0, "%"))
    if temperature["available"]:
        high = float(temperature["high_celsius"] or 80.0)
        critical = float(temperature["critical_celsius"] or 95.0)
        checks.append(
            (
                "Temperature",
                float(temperature["celsius"] or 0.0),
                min(high, 80.0),
                min(critical, 95.0),
                "°C",
            )
        )

    for label, value, _watch, critical, suffix in checks:
        if value >= critical:
            return {
                "state": "critical",
                "summary": f"{label} is critically high at {value:g}{suffix}.",
            }
    for label, value, watch, _critical, suffix in checks:
        if value >= watch:
            return {
                "state": "watch",
                "summary": f"{label} is running high at {value:g}{suffix}.",
            }
    return {
        "state": "healthy",
        "summary": "Resources are within a safe range for browser work.",
    }


def collect_system_status() -> dict[str, Any]:
    """Return one current host snapshot without retaining historical metrics."""
    data_dir = get_data_dir()
    cpu_percent = round(float(psutil.cpu_percent(interval=0.1)), 1)
    memory_stats = psutil.virtual_memory()
    swap_stats = psutil.swap_memory()
    disk_stats = psutil.disk_usage(str(data_dir))

    memory = {
        **_usage_payload(memory_stats),
        "used_bytes": int(memory_stats.total - memory_stats.available),
        "available_bytes": int(memory_stats.available),
    }
    swap = _usage_payload(swap_stats)
    disk = {
        **_usage_payload(disk_stats),
        "free_bytes": int(disk_stats.free),
        "hunter_data_bytes": _hunter_data_bytes(data_dir),
    }
    temperature = _temperature_payload()

    return {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "cpu": {"percent": cpu_percent},
        "memory": memory,
        "swap": swap,
        "disk": disk,
        "temperature": temperature,
        "headroom": _headroom(cpu_percent, memory, swap, disk, temperature),
    }
