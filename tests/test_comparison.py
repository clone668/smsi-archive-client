from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from archive_backup.config import ClientConfig, ProfileConfig
from archive_backup import comparison as comparison_module
from archive_backup.database import StateDatabase
from archive_backup.manager import ArchiveManager
from archive_backup.comparison import compare_archives


def _comparison_side(
    profile_id: str,
    collector_id: str,
    object_item: dict,
    *,
    records: int = 100,
    code_version: str = "a" * 40,
    source_epoch: str = "source-v1",
) -> dict:
    return {
        "profile_id": profile_id,
        "collector_id": collector_id,
        "reported_collector_id": "",
        "manifest_sha256": "1" * 64,
        "report": {},
        "report_present": False,
        "overall_status": "unknown",
        "report_summary": {},
        "quality_policy_sha256": "",
        "source_health": {},
        "record_count": records,
        "business_inventory": {"price_data": records},
        "collector_code_versions": [code_version],
        "source_epochs": [source_epoch],
        "objects": {
            "date=2026-08-15/business/table=price_data/day/part-00000.parquet": object_item
        },
    }


def _clone_source(
    fixture: dict,
    tmp_path: Path,
    collector_id: str,
    *,
    record_count: int = 2,
    reportless: bool = False,
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
    if reportless:
        previous_sha = hashlib.sha256(manifest_raw).hexdigest()
        report_index = next(
            index
            for index, item in enumerate(manifest["objects"])
            if item["kind"] == "runtime_report"
        )
        manifest["objects"] = [
            item for index, item in enumerate(manifest["objects"])
            if index != report_index
        ]
        manifest["object_count"] = len(manifest["objects"])
        manifest["row_count"] = sum(
            int(item["row_count"]) for item in manifest["objects"]
        )
        manifest["replicas"] = {
            name: [
                item for index, item in enumerate(proofs)
                if index != report_index
            ]
            for name, proofs in manifest["replicas"].items()
        }
        manifest["maintenance"] = {
            "contract_version": "smsi-archive-manifest-maintenance/v1",
            "action": "remove_legacy_runtime_report",
            "previous_manifest_sha256": previous_sha,
        }
        manifest_raw = json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        report_path.unlink()
    manifest_path.write_bytes(manifest_raw)
    receipt_path = day_root / ".smsi-verified.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["manifest_sha256"] = hashlib.sha256(manifest_raw).hexdigest()
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    return source_root


def _two_profile_config(
    tmp_path: Path,
    fixture: dict,
    *,
    right_records: int = 2,
    reportless: bool = False,
):
    left_source = _clone_source(
        fixture, tmp_path, "collector-a", reportless=reportless
    )
    right_source = _clone_source(
        fixture,
        tmp_path,
        "collector-b",
        record_count=right_records,
        reportless=reportless,
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
    common = {
        "collector_id": "collector-a",
        "reported_collector_id": "",
        "manifest_sha256": "1" * 64,
        "report": {},
        "report_present": False,
        "overall_status": "unknown",
        "quality_policy_sha256": "",
        "source_health": {},
        "business_inventory": {"price_data": 100},
    }
    comparison = compare_archives(
        "2026-08-07",
        {**common, "profile_id": "left", "record_count": 100},
        {
            **common,
            "profile_id": "right",
            "collector_id": "collector-b",
            "record_count": 50,
            "business_inventory": {"price_data": 50},
        },
    )
    assert comparison["status"] == "attention"
    assert comparison["record_relative_difference"] == 0.5
    assert any(
        item["code"] == "record_volume_difference"
        for item in comparison["issues"]
    )


def test_report_attention_is_separate_from_healthy_data_comparison() -> None:
    common = {
        "reported_collector_id": "",
        "manifest_sha256": "1" * 64,
        "report": {},
        "report_present": True,
        "overall_status": "attention",
        "report_summary": {
            "assessment_classification": "historical",
            "status": "attention",
        },
        "quality_policy_sha256": "2" * 64,
        "source_health": {"source-a": "healthy"},
        "business_inventory": {"price_data": 1_430_397},
    }
    comparison = compare_archives(
        "2026-08-12",
        {
            **common,
            "profile_id": "left",
            "collector_id": "collector-a",
            "reported_collector_id": "collector-a",
            "record_count": 1_430_397,
        },
        {
            **common,
            "profile_id": "right",
            "collector_id": "collector-b",
            "reported_collector_id": "collector-b",
            "record_count": 1_430_212,
            "business_inventory": {"price_data": 1_430_212},
        },
    )

    assert comparison["status"] == "healthy"
    assert comparison["data_status"] == "healthy"
    assert comparison["data_issues"] == []
    assert comparison["report_issues"] == []
    assert comparison["record_difference"] == 185
    assert comparison["record_relative_difference"] == 0.000129
    assert comparison["report_status"] == {
        "left": "attention",
        "right": "attention",
    }


def test_current_data_quality_remains_an_archive_comparison_alert() -> None:
    common = {
        "manifest_sha256": "1" * 64,
        "report": {},
        "report_present": True,
        "overall_status": "attention",
        "quality_policy_sha256": "2" * 64,
        "source_health": {"source-a": "attention"},
        "business_inventory": {"price_data": 100},
        "record_count": 100,
        "report_summary": {
            "assessment_classification": "current",
            "data_quality_status": "attention",
            "status": "attention",
        },
    }
    comparison = compare_archives(
        "2026-08-13",
        {
            **common,
            "profile_id": "left",
            "collector_id": "collector-a",
            "reported_collector_id": "collector-a",
        },
        {
            **common,
            "profile_id": "right",
            "collector_id": "collector-b",
            "reported_collector_id": "collector-b",
        },
    )

    assert comparison["status"] == "healthy"
    assert any(
        item["code"].startswith("report_data_quality:")
        for item in comparison["report_issues"]
    )


def test_scan_removes_comparison_for_deleted_archive_day(
    tmp_path: Path,
    archive_fixture,
) -> None:
    fixture = archive_fixture()
    config = _two_profile_config(tmp_path, fixture)
    database = StateDatabase(tmp_path / "state.sqlite3")
    manager = ArchiveManager(config, database)
    manager.scan_all(download=True)
    assert len(database.comparisons()) == 1

    right_profile = config.profiles[1]
    shutil.rmtree(
        Path(right_profile.verified_source_root) / f"date={fixture['archive_date']}"
    )
    shutil.rmtree(manager.final_root(right_profile, fixture["archive_date"]))

    results = manager.scan_all(download=False)

    assert results[1]["stale_removed"] == 1
    assert database.day(right_profile.profile_id, fixture["archive_date"]) is None
    assert database.comparisons() == []


def test_scan_all_reconciles_every_profile_before_inspecting_days(
    tmp_path: Path,
    archive_fixture,
    monkeypatch,
) -> None:
    fixture = archive_fixture()
    config = _two_profile_config(tmp_path, fixture)
    database = StateDatabase(tmp_path / "state.sqlite3")
    manager = ArchiveManager(config, database)
    right_profile = config.profiles[1]
    database.upsert_day(right_profile.profile_id, "2026-08-08", status="ready")

    def inspect_day(*_args, **_kwargs):
        assert database.day(right_profile.profile_id, "2026-08-08") is None
        return None

    monkeypatch.setattr(manager, "inspect_day", inspect_day)
    results = manager.scan_all(download=False)

    assert results[1]["stale_removed"] == 1


def test_reportless_archives_compare_business_data_without_warning(
    tmp_path: Path,
    archive_fixture,
) -> None:
    fixture = archive_fixture()
    config = _two_profile_config(tmp_path, fixture, reportless=True)
    database = StateDatabase(tmp_path / "state.sqlite3")
    manager = ArchiveManager(config, database)

    results = manager.scan_all(download=True)

    assert all(item["failed"] == 0 for item in results)
    comparison = database.comparisons()[0]
    assert comparison["status"] == "healthy"
    assert comparison["record_count"] == {"left": 2, "right": 2}
    assert comparison["source_health"] == []
    assert comparison["issues"] == []


def test_schema_difference_requires_review() -> None:
    common_object = {
        "kind": "business",
        "table_name": "price_data",
        "row_count": 100,
        "size_bytes": 200,
        "sha256": "1" * 64,
        "content_sha256": "2" * 64,
        "schema_sha256": "3" * 64,
    }
    left = _comparison_side("left", "collector-a", common_object)
    right = _comparison_side(
        "right",
        "collector-b",
        {**common_object, "schema_sha256": "4" * 64},
    )

    result = compare_archives("2026-08-15", left, right)

    assert result["status"] == "attention"
    assert result["comparison_contract_version"] == (
        "smsi-archive-client-comparison/v2"
    )
    assert any(
        issue["code"] == "object_schema_mismatch"
        and "price_data" in issue["detail"]
        for issue in result["data_issues"]
    )


def test_independent_capture_object_differences_are_observations() -> None:
    left_object = {
        "kind": "business",
        "table_name": "price_data",
        "row_count": 100,
        "size_bytes": 200,
        "sha256": "1" * 64,
        "content_sha256": "2" * 64,
        "schema_sha256": "3" * 64,
    }
    right_object = {
        **left_object,
        "row_count": 99,
        "size_bytes": 198,
        "sha256": "4" * 64,
        "content_sha256": "5" * 64,
    }
    left = _comparison_side("left", "collector-a", left_object)
    right = _comparison_side(
        "right", "collector-b", right_object, records=99
    )

    result = compare_archives("2026-08-15", left, right)

    assert result["status"] == "healthy"
    assert result["data_issues"] == []
    assert {
        observation["code"]: observation["count"]
        for observation in result["observed_differences"]
    } == {
        "object_checksum_difference": 1,
        "object_row_count_difference": 1,
        "object_size_difference": 1,
    }


def test_collector_release_and_source_epoch_differences_require_review() -> None:
    common_object = {
        "kind": "business",
        "table_name": "price_data",
        "row_count": 100,
        "size_bytes": 200,
        "sha256": "1" * 64,
        "content_sha256": "2" * 64,
        "schema_sha256": "3" * 64,
    }
    left = _comparison_side("left", "collector-a", common_object)
    right = _comparison_side(
        "right",
        "collector-b",
        common_object,
        code_version="b" * 40,
        source_epoch="source-v2",
    )

    result = compare_archives("2026-08-15", left, right)

    assert result["status"] == "attention"
    assert {issue["code"] for issue in result["data_issues"]} == {
        "collector_code_version_mismatch",
        "source_epoch_mismatch",
    }


def test_unchanged_manifest_pair_reuses_previous_comparison(
    tmp_path: Path,
    archive_fixture,
    monkeypatch,
) -> None:
    fixture = archive_fixture()
    config = _two_profile_config(tmp_path, fixture)
    database = StateDatabase(tmp_path / "state.sqlite3")
    manager = ArchiveManager(config, database)
    manager.scan_all(download=True)
    first = database.comparisons()[0]
    assert first["comparison_contract_version"] == (
        "smsi-archive-client-comparison/v2"
    )

    original_loader = comparison_module._load_verified_archive

    def unexpected_load(*_args, **_kwargs):
        raise AssertionError("unchanged comparison must not reopen archive evidence")

    monkeypatch.setattr(
        comparison_module, "_load_verified_archive", unexpected_load
    )
    reused = manager.refresh_comparisons()[0]
    assert reused["evaluated_at"] == first["evaluated_at"]

    calls = []

    def tracked_load(*args, **kwargs):
        calls.append((args, kwargs))
        return original_loader(*args, **kwargs)

    monkeypatch.setattr(comparison_module, "_load_verified_archive", tracked_load)
    database.upsert_day(
        "left",
        fixture["archive_date"],
        status="verified",
        manifest_sha256="f" * 64,
    )
    refreshed = manager.refresh_comparisons()[0]

    assert len(calls) == 2
    assert refreshed["inputs"]["left_manifest_sha256"] == "f" * 64
