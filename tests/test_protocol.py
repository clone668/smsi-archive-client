from __future__ import annotations

import json

import pytest

from archive_backup.protocol import parse_manifest, parse_progress, progress_stage_label
from archive_backup.verifier import verify_runtime_report


def _progress(stage: str, status: str = "running") -> bytes:
    return json.dumps({
        "contract_version": "smsi-archive-progress/v1",
        "archive_date": "2026-08-07",
        "status": status,
        "stage": stage,
    }).encode()


def test_running_progress_is_not_terminal() -> None:
    progress = parse_progress(_progress("remote_upload"), "2026-08-07")
    assert progress.status == "running"
    assert progress.stage == "remote_upload"


def test_progress_accepts_stages_the_collector_adds_later() -> None:
    """The stage is a display label, so an unknown name is not a data fault.

    The collector publishes runtime_report_generation and runtime_report_upload;
    an allowlist that predates them logged a red "归档处理失败" for a healthy run.
    """
    for stage in (
        "preparing",
        "parquet_generation",
        "remote_upload",
        "runtime_report_generation",
        "runtime_report_upload",
        "manifest_publication",
        "some_stage_invented_in_2027",
    ):
        assert parse_progress(_progress(stage), "2026-08-07").stage == stage

    assert progress_stage_label("runtime_report_generation") == "生成运行报告"
    assert progress_stage_label("some_stage_invented_in_2027") == "some_stage_invented_in_2027"


def test_progress_still_refuses_garbage_and_inconsistent_terminals() -> None:
    for stage in ("", "Remote Upload", "../etc", "x" * 65):
        with pytest.raises(RuntimeError, match="阶段无效"):
            parse_progress(_progress(stage), "2026-08-07")
    with pytest.raises(RuntimeError, match="终态不一致"):
        parse_progress(_progress("remote_upload", status="verified"), "2026-08-07")
    with pytest.raises(RuntimeError, match="终态不一致"):
        parse_progress(_progress("verified", status="running"), "2026-08-07")


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
