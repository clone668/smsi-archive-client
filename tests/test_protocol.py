from __future__ import annotations

import json

import pytest

from archive_backup.protocol import parse_manifest, parse_progress
from archive_backup.verifier import verify_runtime_report


def test_running_progress_is_not_terminal() -> None:
    raw = json.dumps({
        "contract_version": "smsi-archive-progress/v1",
        "archive_date": "2026-08-07",
        "status": "running",
        "stage": "remote_upload",
    }).encode()
    progress = parse_progress(raw, "2026-08-07")
    assert progress.status == "running"
    assert progress.stage == "remote_upload"


def test_manifest_rejects_incomplete_drive_readback(archive_fixture) -> None:
    fixture = archive_fixture()
    fixture["manifest"]["replicas"]["google_drive"][0]["read_verified"] = False
    raw = json.dumps(fixture["manifest"]).encode()
    with pytest.raises(RuntimeError, match="完整读回证据不完整"):
        parse_manifest(raw, fixture["archive_date"])


def test_manifest_rejects_unsafe_object_path(archive_fixture) -> None:
    fixture = archive_fixture()
    fixture["manifest"]["objects"][0]["relative_key"] = "date=2026-08-07/../secret"
    raw = json.dumps(fixture["manifest"]).encode()
    with pytest.raises(RuntimeError, match="路径无效"):
        parse_manifest(raw, fixture["archive_date"])


def test_manifest_requires_schema_and_business_content_digests(archive_fixture) -> None:
    fixture = archive_fixture()
    fixture["manifest"]["objects"][0]["schema_sha256"] = ""
    raw = json.dumps(fixture["manifest"]).encode()
    with pytest.raises(RuntimeError, match="schema 摘要无效"):
        parse_manifest(raw, fixture["archive_date"])


def test_manifest_accepts_json_runtime_report_without_parquet_schema(
    archive_fixture,
) -> None:
    fixture = archive_fixture()

    snapshot = parse_manifest(fixture["manifest_raw"], fixture["archive_date"])
    item = next(value for value in snapshot.objects if value["kind"] == "runtime_report")
    result = verify_runtime_report(
        fixture["report_path"], item, fixture["archive_date"]
    )

    assert result["row_count"] == 1
    assert result["collector_node_id"] == "collector-a"
    assert result["overall_status"] == "healthy"


def test_runtime_report_rejects_incomplete_archive_evidence(archive_fixture) -> None:
    fixture = archive_fixture()
    report = json.loads(fixture["report_path"].read_text(encoding="utf-8"))
    report["archive"]["all_data_objects_read_verified"] = False
    fixture["report_path"].write_text(json.dumps(report), encoding="utf-8")
    item = next(
        value
        for value in fixture["manifest"]["objects"]
        if value["kind"] == "runtime_report"
    )

    with pytest.raises(RuntimeError, match="归档或采集证据不完整"):
        verify_runtime_report(fixture["report_path"], item, fixture["archive_date"])
