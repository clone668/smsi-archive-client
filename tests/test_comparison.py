from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from archive_backup.config import ClientConfig, ProfileConfig
from archive_backup.database import StateDatabase
from archive_backup.manager import ArchiveManager


def _clone_source(
    fixture: dict,
    tmp_path: Path,
    collector_id: str,
    *,
    record_count: int = 2,
) -> Path:
    source_root = tmp_path / f"source-{collector_id}" / f"collector={collector_id}"
    day_root = source_root / f"date={fixture['archive_date']}"
    shutil.copytree(fixture["day_root"], day_root)
    report_path = day_root / "runtime-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["collector_node_id"] = collector_id
    report["database"]["writes"]["record_count"] = record_count
    report_raw = json.dumps(
        report, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    report_path.write_bytes(report_raw)

    manifest_path = day_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    report_item = next(
        item for item in manifest["objects"] if item["kind"] == "runtime_report"
    )
    report_item["size_bytes"] = len(report_raw)
    report_item["sha256"] = hashlib.sha256(report_raw).hexdigest()
    manifest_raw = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    manifest_path.write_bytes(manifest_raw)
    receipt_path = day_root / ".smsi-verified.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["manifest_sha256"] = hashlib.sha256(manifest_raw).hexdigest()
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    return source_root


def _two_profile_config(tmp_path: Path, fixture: dict, *, right_records: int = 2):
    left_source = _clone_source(fixture, tmp_path, "collector-a")
    right_source = _clone_source(
        fixture, tmp_path, "collector-b", record_count=right_records
    )
    profiles = [
        ProfileConfig(
            profile_id="left",
            display_name="Left",
            collector_id="collector-a",
            source_type="verified_directory",
            verified_source_root=str(left_source),
        ),
        ProfileConfig(
            profile_id="right",
            display_name="Right",
            collector_id="collector-b",
            source_type="verified_directory",
            verified_source_root=str(right_source),
        ),
    ]
    return ClientConfig(
        local_root=str(tmp_path / "local"),
        minimum_free_bytes=1024**3,
        history_days=3650,
        profiles=profiles,
    )


def test_scan_all_persists_healthy_two_server_restore_comparison(
    tmp_path: Path,
    archive_fixture,
) -> None:
    fixture = archive_fixture()
    config = _two_profile_config(tmp_path, fixture)
    database = StateDatabase(tmp_path / "state.sqlite3")
    manager = ArchiveManager(config, database)

    results = manager.scan_all(download=True)

    assert all(item["failed"] == 0 for item in results)
    comparisons = database.comparisons()
    assert len(comparisons) == 1
    comparison = comparisons[0]
    assert comparison["status"] == "healthy"
    assert comparison["restore_verification"] == {
        "left": "verified",
        "right": "verified",
    }
    assert comparison["record_relative_difference"] == 0
    assert comparison["issues"] == []


def test_two_server_comparison_flags_large_record_volume_difference(
    tmp_path: Path,
    archive_fixture,
) -> None:
    fixture = archive_fixture()
    config = _two_profile_config(tmp_path, fixture, right_records=1)
    database = StateDatabase(tmp_path / "state.sqlite3")
    manager = ArchiveManager(config, database)

    manager.scan_all(download=True)

    comparison = database.comparisons()[0]
    assert comparison["status"] == "attention"
    assert comparison["record_relative_difference"] == 0.5
    assert any(
        item["code"] == "record_volume_difference"
        for item in comparison["issues"]
    )
