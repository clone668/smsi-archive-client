from __future__ import annotations

from archive_backup.desktop import (
    bytes_text,
    cached_directory_result,
    duration_text,
    preflight,
)
from archive_backup.config import ConfigStore


def test_desktop_formatters() -> None:
    assert bytes_text(0) == "0 B"
    assert bytes_text(1536) == "1.5 KiB"
    assert bytes_text(15 * 1024**2) == "15 MiB"
    assert duration_text(8) == "8 秒"
    assert duration_text(125) == "2 分 5 秒"
    assert duration_text(7500) == "2 小时 5 分"


def test_cached_directory_navigation_keeps_one_level_only() -> None:
    index = [
        {"type": "file", "path": "business/market/trades.parquet", "size_bytes": 100, "row_count": 5},
        {"type": "file", "path": "business/orders.parquet", "size_bytes": 200, "row_count": 8},
        {"type": "file", "path": "control/manifest.json", "size_bytes": 50, "row_count": 0},
    ]
    meta = {"archive_date": "2026-08-09", "download_eligible": True}

    root = cached_directory_result(index, meta, "")
    assert [(item["type"], item["name"]) for item in root["entries"]] == [
        ("directory", "business"),
        ("directory", "control"),
    ]

    business = cached_directory_result(index, meta, "business")
    assert [(item["type"], item["name"]) for item in business["entries"]] == [
        ("directory", "market"),
        ("file", "orders.parquet"),
    ]
    assert business["bytes_total"] == 200
    assert business["row_count"] == 8
    assert business["parent_path"] == ""


def test_desktop_preflight_does_not_create_a_window(tmp_path) -> None:
    result = preflight(ConfigStore(tmp_path / "state"))

    assert result["version"] == "3.1.0"
    assert result["profiles"] == 0
    assert result["tk"]
    assert (tmp_path / "state" / "config.json").exists()
    assert (tmp_path / "state" / "state.sqlite3").exists()
