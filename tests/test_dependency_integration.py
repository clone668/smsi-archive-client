from __future__ import annotations

import hashlib
from pathlib import Path

from archive_backup.database import StateDatabase
from archive_backup.dependencies import DependencyReceiver
from archive_backup.manager import ArchiveManager
from test_dependencies import Source, publish
from test_manager import make_config


def test_incremental_scan_downloads_late_supplements_for_verified_day(tmp_path, archive_fixture):
    fixture = archive_fixture()
    config, profile = make_config(tmp_path, fixture["source_root"])
    database = StateDatabase(tmp_path / "state.sqlite3")
    manager = ArchiveManager(config, database)
    first = manager.scan_profile(profile, download=True, incremental=True)
    assert first["failed"] == 0
    day = fixture["archive_date"]
    base = hashlib.sha256(fixture["manifest_raw"]).hexdigest()
    before = {path.relative_to(config.archive_root).as_posix(): path.read_bytes()
              for path in (config.archive_root / "collector=collector-a" / f"date={day}").rglob("*") if path.is_file()}
    cloud = tmp_path / "cloud"
    entry, objects = publish(cloud, base, day=day)
    DependencyReceiver(fixture["source_root"], "collector-a", Source(cloud)).sync(entry)
    second = manager.scan_profile(profile, download=True, incremental=True, audit_verified=False)
    assert second["dates"] == 0  # No native-day download or audit.
    assert second["failed"] == 0
    assert second["supplements_completed"] == 1
    row = database.day(profile.profile_id, day)
    assert row["status"] == "verified"
    assert row["dependency_summary"]["state"] == "verified"
    assert row["dependency_summary"]["dependency_status"] == "partial"
    for key, raw in before.items():
        assert (config.archive_root / key).read_bytes() == raw
    assert manager.scan_profile(profile, download=True, incremental=True, audit_verified=False)["material"] is False
    newer, _ = publish(cloud, base, day=day, generation=2, rows=(1, 2, 3))
    DependencyReceiver(fixture["source_root"], "collector-a", Source(cloud)).sync(newer)
    assert manager.scan_profile(profile, download=True, incremental=True)["supplements_completed"] == 1
    assert database.day(profile.profile_id, day)["dependency_summary"]["generation"] == 2


def test_supplement_failure_preserves_verified_native_state(tmp_path, archive_fixture):
    fixture = archive_fixture()
    config, profile = make_config(tmp_path, fixture["source_root"])
    database = StateDatabase(tmp_path / "state.sqlite3")
    manager = ArchiveManager(config, database)
    manager.scan_profile(profile, download=True)
    base = hashlib.sha256(fixture["manifest_raw"]).hexdigest()
    cloud = tmp_path / "cloud"
    entry, objects = publish(cloud, base, day=fixture["archive_date"])
    DependencyReceiver(fixture["source_root"], "collector-a", Source(cloud)).sync(entry)
    (fixture["source_root"] / objects[0]["relative_key"]).write_bytes(b"corrupt")
    result = manager.scan_profile(profile, download=True, incremental=True)
    assert result["failed"] == 1
    row = database.day(profile.profile_id, fixture["archive_date"])
    assert row["status"] == "verified"
    assert row["dependency_summary"]["state"] == "error"
