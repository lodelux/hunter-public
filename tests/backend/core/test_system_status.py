from types import SimpleNamespace

from core import system_status


def _usage(*, percent, used, total, available=None, free=None):
    return SimpleNamespace(
        percent=percent,
        used=used,
        total=total,
        available=available,
        free=free,
    )


def test_collect_system_status_reports_hottest_sensor_and_safe_headroom(monkeypatch):
    monkeypatch.setattr(system_status, "_hunter_data_bytes", lambda _path: 6_000)
    monkeypatch.setattr(system_status.psutil, "cpu_percent", lambda interval: 24.5)
    monkeypatch.setattr(
        system_status.psutil,
        "virtual_memory",
        lambda: _usage(percent=50, used=4_000, total=8_000, available=4_000),
    )
    monkeypatch.setattr(
        system_status.psutil,
        "swap_memory",
        lambda: _usage(percent=10, used=100, total=1_000),
    )
    monkeypatch.setattr(
        system_status.psutil,
        "disk_usage",
        lambda _path: _usage(percent=25, used=25_000, total=100_000, free=75_000),
    )
    monkeypatch.setattr(
        system_status.psutil,
        "sensors_temperatures",
        lambda: {
            "coretemp": [
                SimpleNamespace(label="Core 0", current=52, high=90, critical=100),
                SimpleNamespace(label="CPU package", current=61, high=90, critical=100),
            ]
        },
        raising=False,
    )

    snapshot = system_status.collect_system_status()

    assert snapshot["cpu"] == {"percent": 24.5}
    assert snapshot["memory"]["available_bytes"] == 4_000
    assert snapshot["disk"]["free_bytes"] == 75_000
    assert snapshot["disk"]["hunter_data_bytes"] == 6_000
    assert snapshot["temperature"] == {
        "available": True,
        "celsius": 61.0,
        "sensor": "CPU package",
        "high_celsius": 90.0,
        "critical_celsius": 100.0,
    }
    assert snapshot["headroom"]["state"] == "healthy"


def test_collect_system_status_handles_missing_sensors_and_memory_pressure(monkeypatch):
    monkeypatch.setattr(system_status, "_hunter_data_bytes", lambda _path: 6_000)
    monkeypatch.setattr(system_status.psutil, "cpu_percent", lambda interval: 20)
    monkeypatch.setattr(
        system_status.psutil,
        "virtual_memory",
        lambda: _usage(percent=96, used=7_680, total=8_000, available=320),
    )
    monkeypatch.setattr(
        system_status.psutil,
        "swap_memory",
        lambda: _usage(percent=0, used=0, total=0),
    )
    monkeypatch.setattr(
        system_status.psutil,
        "disk_usage",
        lambda _path: _usage(percent=25, used=25_000, total=100_000, free=75_000),
    )
    monkeypatch.setattr(
        system_status.psutil,
        "sensors_temperatures",
        lambda: {},
        raising=False,
    )

    snapshot = system_status.collect_system_status()

    assert snapshot["temperature"]["available"] is False
    assert snapshot["headroom"] == {
        "state": "critical",
        "summary": "Memory is critically high at 96%.",
    }


def test_directory_size_counts_nested_files_without_following_symlinks(tmp_path):
    (tmp_path / "root.bin").write_bytes(b"1234")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "child.bin").write_bytes(b"123456")
    (tmp_path / "linked.bin").symlink_to(nested / "child.bin")

    assert system_status._directory_size_bytes(tmp_path) == 10
