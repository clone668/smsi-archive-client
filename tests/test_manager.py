from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from archive_backup.config import ClientConfig, ProfileConfig
from archive_backup.database import StateDatabase
from archive_backup.manager import ArchiveManager
from archive_backup.protocol import parse_manifest
from archive_backup.sources import VerifiedDirectorySource
from archive_backup.verifier import OperationCancelled


def make_config(tmp_path: Path, source_root: Path) -> tuple[ClientConfig, ProfileConfig]:
    profile = ProfileConfig(
        profile_id="collector-a",
        display_name="Collector A",
        collector_id="collector-a",
        source_type="verified_directory",
        verified_source_root=str(source_root),
    )
    config = ClientConfig(
        local_root=str(tmp_path / "local"),
        minimum_free_bytes=1024**3,
        history_days=3650,
        profiles=[profile],
    )
    return config, profile


def test_verified_directory_downloads_and_publishes_atomically(tmp_path, archive_fixture) -> None:
    fixture = archive_fixture()
    config, profile = make_config(tmp_path, fixture["source_root"])
    database = StateDatabase(tmp_path / "state.sqlite3")
    manager = ArchiveManager(config, database)

    result = manager.scan_profile(profile, download=True)

    assert result["failed"] == 0
    final = Path(config.local_root) / "collector=collector-a" / "date=2026-08-07"
    assert (final / ".smsi-verified.json").is_file()
    assert (final / "manifest.json").read_bytes() == fixture["manifest_raw"]
    target = final / "business" / "table=price_data" / "day" / "part-00000.parquet"
    assert hashlib.sha256(target.read_bytes()).hexdigest() == fixture["manifest"]["objects"][0]["sha256"]
    day = database.day("collector-a", "2026-08-07")
    assert day["status"] == "verified"
    assert day["report_summary"]["status"] == "healthy"
    assert day["report_summary"]["source_counts"] == {"healthy": 1}


def test_runtime_report_summary_ignores_report_that_no_longer_matches_manifest(
    tmp_path, archive_fixture
) -> None:
    fixture = archive_fixture()
    config, _profile = make_config(tmp_path, fixture["source_root"])
    database = StateDatabase(tmp_path / "state.sqlite3")
    manager = ArchiveManager(config, database)
    report = json.loads(fixture["report_path"].read_text(encoding="utf-8"))
    report["overall_status"] = "critical"
    fixture["report_path"].write_text(json.dumps(report), encoding="utf-8")

    snapshot = parse_manifest(fixture["manifest_raw"], fixture["archive_date"])
    result = manager._runtime_report_summary(fixture["day_root"], snapshot)

    assert result == {}


def test_download_reports_byte_progress_without_changing_verification_gate(tmp_path, archive_fixture) -> None:
    fixture = archive_fixture()
    config, profile = make_config(tmp_path, fixture["source_root"])
    database = StateDatabase(tmp_path / "state.sqlite3")
    updates: list[dict] = []
    manager = ArchiveManager(config, database, progress=updates.append)

    manager.scan_profile(profile, download=True)

    assert any(item["phase"] == "downloading" and item["bytes_transferred"] > 0 for item in updates)
    assert updates[-1]["phase"] == "verified"
    assert updates[-1]["bytes_done"] == updates[-1]["bytes_total"]


def test_verified_directory_ignores_unverified_source_day(tmp_path, archive_fixture) -> None:
    fixture = archive_fixture()
    (fixture["day_root"] / ".smsi-verified.json").unlink()
    source = VerifiedDirectorySource(ProfileConfig(
        profile_id="collector-a", display_name="A", collector_id="collector-a",
        source_type="verified_directory", verified_source_root=str(fixture["source_root"]),
    ))
    assert source.list_dates() == set()


def test_verified_directory_rejects_receipt_for_different_manifest(tmp_path, archive_fixture) -> None:
    fixture = archive_fixture()
    receipt_path = fixture["day_root"] / ".smsi-verified.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["manifest_sha256"] = "0" * 64
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    source = VerifiedDirectorySource(ProfileConfig(
        profile_id="collector-a", display_name="A", collector_id="collector-a",
        source_type="verified_directory", verified_source_root=str(fixture["source_root"]),
    ))
    assert source.list_dates() == set()


def test_manifest_change_does_not_overwrite_verified_day(tmp_path, archive_fixture) -> None:
    fixture = archive_fixture()
    config, profile = make_config(tmp_path, fixture["source_root"])
    database = StateDatabase(tmp_path / "state.sqlite3")
    manager = ArchiveManager(config, database)
    manager.scan_profile(profile, download=True)
    final = manager.final_root(profile, fixture["archive_date"])
    original = (final / "manifest.json").read_bytes()

    changed = dict(fixture["manifest"])
    changed["generated_at"] = "later"
    changed_raw = json.dumps(changed, sort_keys=True, separators=(",", ":")).encode()
    (fixture["day_root"] / "manifest.json").write_bytes(changed_raw)
    source_receipt_path = fixture["day_root"] / ".smsi-verified.json"
    source_receipt = json.loads(source_receipt_path.read_text(encoding="utf-8"))
    source_receipt["manifest_sha256"] = hashlib.sha256(changed_raw).hexdigest()
    source_receipt_path.write_text(json.dumps(source_receipt), encoding="utf-8")
    manager.scan_profile(profile, download=True)

    assert (final / "manifest.json").read_bytes() == original
    row = database.day("collector-a", fixture["archive_date"])
    assert row["status"] == "manifest_changed"


class RunningSource:
    name = "test"

    def read_small(self, relative_key: str, maximum_bytes: int):
        if relative_key.endswith("_smsi-archive-progress.json"):
            return json.dumps({
                "contract_version": "smsi-archive-progress/v1",
                "archive_date": "2026-08-07",
                "status": "running",
                "stage": "remote_upload",
            }).encode()
        raise AssertionError("运行中的归档不应读取 manifest")


def test_running_remote_archive_never_reads_manifest(tmp_path) -> None:
    config, profile = make_config(tmp_path, tmp_path / "source")
    database = StateDatabase(tmp_path / "state.sqlite3")
    manager = ArchiveManager(config, database)
    result = manager.inspect_day(profile, RunningSource(), "2026-08-07", download=True)
    assert result is None
    assert database.day("collector-a", "2026-08-07")["status"] == "remote_running"


class ChangedAfterDownloadSource:
    name = "test"

    def __init__(self, fixture) -> None:
        self.fixture = fixture

    def read_small(self, relative_key: str, maximum_bytes: int):
        if relative_key.endswith("_smsi-archive-progress.json"):
            return json.dumps({
                "contract_version": "smsi-archive-progress/v1",
                "archive_date": self.fixture["archive_date"],
                "status": "verified",
                "stage": "verified",
            }).encode()
        if relative_key.endswith("manifest.json"):
            changed = dict(self.fixture["manifest"])
            changed["generated_at"] = "changed-during-download"
            return json.dumps(changed, sort_keys=True, separators=(",", ":")).encode()
        return None

    def download(self, relative_key: str, destination: Path, cancel=None, progress=None, bandwidth_limit=None) -> int:
        source = (
            self.fixture["report_path"]
            if relative_key.endswith("runtime-report.json")
            else self.fixture["object_path"]
        )
        shutil.copyfile(source, destination)
        if progress:
            progress(destination.stat().st_size)
        return destination.stat().st_size


def test_manifest_change_during_download_is_not_published(tmp_path, archive_fixture) -> None:
    fixture = archive_fixture()
    config, profile = make_config(tmp_path, fixture["source_root"])
    database = StateDatabase(tmp_path / "state.sqlite3")
    manager = ArchiveManager(config, database)
    snapshot = parse_manifest(fixture["manifest_raw"], fixture["archive_date"])

    with pytest.raises(RuntimeError, match="下载期间远端 manifest 已变化"):
        manager.download_day(profile, ChangedAfterDownloadSource(fixture), snapshot)

    assert not manager.final_root(profile, fixture["archive_date"]).exists()
    assert manager.staging_root(profile, fixture["archive_date"]).exists()


def test_failed_local_recheck_is_recorded_and_recovered_without_erasing_evidence(
    tmp_path, archive_fixture
) -> None:
    fixture = archive_fixture()
    config, profile = make_config(tmp_path, fixture["source_root"])
    database = StateDatabase(tmp_path / "state.sqlite3")
    manager = ArchiveManager(config, database)
    manager.scan_profile(profile, download=True)
    final = manager.final_root(profile, fixture["archive_date"])
    local_object = final / "business" / "table=price_data" / "day" / "part-00000.parquet"
    local_object.write_bytes(b"corrupted")

    with pytest.raises(RuntimeError):
        manager.verify_existing(profile.profile_id, fixture["archive_date"])

    assert (final / ".smsi-verification-failed.json").is_file()
    assert database.day(profile.profile_id, fixture["archive_date"])["status"] == "error"

    result = manager.scan_profile(profile, download=True)
    assert result["failed"] == 0
    assert database.day(profile.profile_id, fixture["archive_date"])["status"] == "verified"
    quarantined = list(manager.quarantine_root(profile).glob("date=2026-08-07.*.unverified"))
    assert len(quarantined) == 1
    assert (quarantined[0] / ".smsi-verification-failed.json").is_file()


class CancelDuringDownloadSource:
    name = "test"

    def __init__(self, fixture, cancel) -> None:
        self.fixture = fixture
        self.cancel = cancel

    def list_dates(self):
        return {self.fixture["archive_date"]}

    def read_small(self, relative_key: str, maximum_bytes: int):
        if relative_key.endswith("manifest.json"):
            return self.fixture["manifest_raw"]
        if relative_key.endswith("_smsi-archive-progress.json"):
            return json.dumps({
                "contract_version": "smsi-archive-progress/v1",
                "archive_date": self.fixture["archive_date"],
                "status": "verified",
                "stage": "verified",
            }).encode()
        return None

    def download(self, relative_key, destination, cancel=None, progress=None, bandwidth_limit=None):
        source = (
            self.fixture["report_path"]
            if relative_key.endswith("runtime-report.json")
            else self.fixture["object_path"]
        )
        shutil.copyfile(source, destination)
        if progress:
            progress(destination.stat().st_size)
        self.cancel.set()
        raise OperationCancelled("下载已取消，未完成结果不会发布")


def test_cancel_marks_day_cancelled_and_keeps_staging(tmp_path, archive_fixture, monkeypatch) -> None:
    fixture = archive_fixture()
    config, profile = make_config(tmp_path, fixture["source_root"])
    database = StateDatabase(tmp_path / "state.sqlite3")
    manager = ArchiveManager(config, database)
    source = CancelDuringDownloadSource(fixture, manager.cancel)
    monkeypatch.setattr("archive_backup.manager.build_source", lambda _config, _profile: source)

    with pytest.raises(OperationCancelled):
        manager.scan_profile(profile, download=True)

    row = database.day(profile.profile_id, fixture["archive_date"])
    assert row["status"] == "cancelled"
    assert "下次可继续" in row["detail"]
    assert manager.staging_root(profile, fixture["archive_date"]).exists()
    assert not manager.final_root(profile, fixture["archive_date"]).exists()


def test_cancel_specific_download_does_not_leave_day_running(
    tmp_path, archive_fixture, monkeypatch
) -> None:
    fixture = archive_fixture()
    config, profile = make_config(tmp_path, fixture["source_root"])
    database = StateDatabase(tmp_path / "state.sqlite3")
    manager = ArchiveManager(config, database)
    source = CancelDuringDownloadSource(fixture, manager.cancel)
    monkeypatch.setattr(
        "archive_backup.manager.build_source", lambda _config, _profile: source
    )

    with pytest.raises(OperationCancelled):
        manager.download_specific(profile.profile_id, fixture["archive_date"])

    row = database.day(profile.profile_id, fixture["archive_date"])
    assert row["status"] == "cancelled"
    assert "下次可继续" in row["detail"]


def test_cancel_recheck_restores_previous_verified_state(
    tmp_path, archive_fixture
) -> None:
    fixture = archive_fixture()
    config, profile = make_config(tmp_path, fixture["source_root"])
    database = StateDatabase(tmp_path / "state.sqlite3")
    manager = ArchiveManager(config, database)
    manager.scan_profile(profile, download=True)
    manager.cancel.set()

    with pytest.raises(OperationCancelled):
        manager.verify_existing(profile.profile_id, fixture["archive_date"])

    row = database.day(profile.profile_id, fixture["archive_date"])
    assert row["status"] == "verified"
    assert "保留上次验证结果" in row["detail"]


def test_remote_file_browser_uses_verified_manifest(tmp_path, archive_fixture) -> None:
    fixture = archive_fixture()
    config, profile = make_config(tmp_path, fixture["source_root"])
    manager = ArchiveManager(config, StateDatabase(tmp_path / "state.sqlite3"))

    dates = manager.browse_dates(profile.profile_id, scope="remote")
    result = manager.browse_files(
        profile.profile_id, fixture["archive_date"], scope="remote"
    )

    assert dates["dates"][0]["archive_date"] == fixture["archive_date"]
    assert result["state"] == "ready"
    assert result["entry_count"] == 2
    assert result["entries"][0]["type"] == "directory"
    assert len(result["browse_index"]) == 2
    assert result["browse_index"][0]["path"] == "business/table=price_data/day/part-00000.parquet"
    assert result["browse_index"][1]["path"] == "runtime-report.json"
    nested = manager.browse_files(
        profile.profile_id,
        fixture["archive_date"],
        scope="remote",
        path="business/table=price_data/day",
    )
    assert nested["object_count"] == 1
    assert nested["files"][0]["path"] == "business/table=price_data/day/part-00000.parquet"
    assert nested["files"][0]["local_state"] == "missing"


def test_remote_file_browser_blocks_running_archive(
    tmp_path, monkeypatch
) -> None:
    config, profile = make_config(tmp_path, tmp_path / "source")
    manager = ArchiveManager(config, StateDatabase(tmp_path / "state.sqlite3"))
    monkeypatch.setattr(
        "archive_backup.manager.build_source",
        lambda _config, _profile: RunningSource(),
    )

    result = manager.browse_files(
        profile.profile_id, "2026-08-07", scope="remote"
    )

    assert result["state"] == "remote_running"
    assert result["download_eligible"] is False
    assert result["browse_index"] == []


def test_remote_file_browser_blocks_already_verified_day(
    tmp_path, archive_fixture
) -> None:
    fixture = archive_fixture()
    config, profile = make_config(tmp_path, fixture["source_root"])
    manager = ArchiveManager(config, StateDatabase(tmp_path / "state.sqlite3"))
    manager.scan_profile(profile, download=True)

    result = manager.browse_files(
        profile.profile_id, fixture["archive_date"], scope="remote"
    )

    assert result["state"] == "verified"
    assert result["download_eligible"] is False
    assert result["download_block_reason"] == "本地归档已经完整验证"


def test_remote_file_browser_surfaces_manifest_change(
    tmp_path, archive_fixture
) -> None:
    fixture = archive_fixture()
    config, profile = make_config(tmp_path, fixture["source_root"])
    manager = ArchiveManager(config, StateDatabase(tmp_path / "state.sqlite3"))
    manager.scan_profile(profile, download=True)
    changed = dict(fixture["manifest"])
    changed["generated_at"] = "changed-after-local-verification"
    changed_raw = json.dumps(
        changed, sort_keys=True, separators=(",", ":")
    ).encode()
    (fixture["day_root"] / "manifest.json").write_bytes(changed_raw)
    source_receipt_path = fixture["day_root"] / ".smsi-verified.json"
    source_receipt = json.loads(source_receipt_path.read_text(encoding="utf-8"))
    source_receipt["manifest_sha256"] = hashlib.sha256(changed_raw).hexdigest()
    source_receipt_path.write_text(json.dumps(source_receipt), encoding="utf-8")

    result = manager.browse_files(
        profile.profile_id, fixture["archive_date"], scope="remote"
    )

    assert result["state"] == "manifest_changed"
    assert result["download_eligible"] is False
    assert "manifest 已变化" in result["download_block_reason"]


def test_local_file_browser_does_not_access_remote(
    tmp_path, archive_fixture, monkeypatch
) -> None:
    fixture = archive_fixture()
    config, profile = make_config(tmp_path, tmp_path / "unavailable-source")
    manager = ArchiveManager(config, StateDatabase(tmp_path / "state.sqlite3"))
    stage = manager.staging_root(profile, fixture["archive_date"])
    target = stage / "business" / "table=price_data" / "day"
    target.mkdir(parents=True)
    (stage / "manifest.json").write_bytes(fixture["manifest_raw"])
    downloading = target / "part-00000.parquet.downloading"
    downloading.write_bytes(b"partial")
    monkeypatch.setattr(
        "archive_backup.manager.build_source",
        lambda *_args: (_ for _ in ()).throw(AssertionError("remote access")),
    )

    dates = manager.browse_dates(profile.profile_id, scope="local")
    result = manager.browse_files(
        profile.profile_id, fixture["archive_date"], scope="local"
    )

    assert dates["dates"][0]["partial"] is True
    assert result["entry_count"] == 2
    assert result["object_count"] == 1
    assert any(
        item["path"].endswith("part-00000.parquet.downloading")
        for item in result["browse_index"]
    )
    nested = manager.browse_files(
        profile.profile_id,
        fixture["archive_date"],
        scope="local",
        path="business/table=price_data/day",
    )
    assert any(item["state"] == "downloading" for item in nested["files"])
    assert any(item["remote_state"] == "control" for item in result["files"])
