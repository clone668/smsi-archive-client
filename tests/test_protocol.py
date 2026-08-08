from __future__ import annotations

import json

import pytest

from archive_backup.protocol import parse_manifest, parse_progress


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
