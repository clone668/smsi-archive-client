from __future__ import annotations

import sqlite3

from archive_backup.database import StateDatabase
from archive_backup.manager import ArchiveManager
from archive_backup.sources import VerifiedDirectorySource
from test_manager import make_config


def test_native_workflows_ignore_remote_and_local_supplement_leftovers(tmp_path, archive_fixture, monkeypatch):
    fixture = archive_fixture()
    config, profile = make_config(tmp_path, fixture["source_root"])
    database = StateDatabase(tmp_path / "state.sqlite3")
    updates = []
    manager = ArchiveManager(config, database, progress=updates.append)
    leftovers = []
    for root in (fixture["source_root"], manager.profile_root(profile)):
        path = root / "dependencies-v1/deliveries/broken.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"invalid retired supplement")
        leftovers.append(path)
    original = VerifiedDirectorySource.read_small

    def native_read(source, key, maximum):
        assert "dependencies-v1" not in key
        return original(source, key, maximum)

    monkeypatch.setattr(VerifiedDirectorySource, "read_small", native_read)
    first = manager.scan_profile(profile, download=True, incremental=True)
    assert first["failed"] == 0 and first["completed"] == 1
    second = manager.scan_profile(profile, download=True, incremental=True, audit_verified=False)
    assert second["dates"] == 0 and second["failed"] == 0 and not second["material"]
    day = fixture["archive_date"]
    assert manager.download_specific(profile.profile_id, day)["status"] == "verified"
    assert manager.verify_existing(profile.profile_id, day)["status"] == "verified"
    assert database.day(profile.profile_id, day)["status"] == "verified"
    assert not any(event.get("phase") == "supplement" for event in updates)
    assert all(path.read_bytes() == b"invalid retired supplement" for path in leftovers)


def test_previous_database_column_does_not_affect_native_status(tmp_path):
    path = tmp_path / "state.sqlite3"
    database = StateDatabase(path)
    database.upsert_day("node", "2026-10-16", status="verified")
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE archive_days ADD COLUMN dependency_summary TEXT NOT NULL DEFAULT ''")
        connection.execute("UPDATE archive_days SET dependency_summary=?", ('{"state":"error"}',))
    reopened = StateDatabase(path)
    assert reopened.day("node", "2026-10-16")["status"] == "verified"
    assert "dependency_summary" not in reopened.days()[0]
    reopened.upsert_day("node", "2026-10-17", status="verified")
    assert len(reopened.days()) == 2
