from __future__ import annotations

import threading

from archive_backup.config import ConfigStore
from archive_backup.database import StateDatabase
from archive_backup.service import ArchiveService
from archive_backup.verifier import OperationCancelled


def test_archive_day_report_summary_round_trips_as_object(tmp_path) -> None:
    database = StateDatabase(tmp_path / "state.sqlite3")
    database.upsert_day(
        "collector-a",
        "2026-08-09",
        status="verified",
        report_summary='{"issue_count":1,"status":"attention"}',
    )

    day = database.day("collector-a", "2026-08-09")
    assert day["report_summary"] == {
        "issue_count": 1,
        "status": "attention",
    }


def test_job_lifecycle_and_object_progress_are_persistent(tmp_path) -> None:
    database = StateDatabase(tmp_path / "state.sqlite3")
    job = database.create_job(
        "download", profile_id="collector-a", archive_date="2026-08-09"
    )
    database.update_job(
        job["id"],
        status="running",
        phase="downloading",
        started_at="2026-08-10T00:00:00Z",
        object_count=1,
        bytes_total=100,
    )
    database.replace_job_items(job["id"], [{
        "relative_key": "date=2026-08-09/business/a.parquet",
        "size_bytes": 100,
        "sha256": "a" * 64,
    }])
    database.update_job_item(
        job["id"],
        "date=2026-08-09/business/a.parquet",
        status="downloading",
        bytes_done=40,
    )

    active = database.active_job()
    items = database.job_items(job["id"])
    assert active["id"] == job["id"]
    assert active["phase"] == "downloading"
    assert items[0]["bytes_done"] == 40
    assert items[0]["status"] == "downloading"


def test_interrupted_job_is_queued_for_safe_resume(tmp_path) -> None:
    database = StateDatabase(tmp_path / "state.sqlite3")
    job = database.create_job(
        "download", profile_id="collector-a", archive_date="2026-08-09"
    )
    database.update_job(job["id"], status="running", phase="verifying")
    database.upsert_day(
        "collector-a", "2026-08-09", status="verifying", detail="running"
    )

    assert database.recover_interrupted_jobs() == 1
    recovered = database.job(job["id"])
    day = database.day("collector-a", "2026-08-09")
    assert recovered["status"] == "queued"
    assert recovered["phase"] == "recovering"
    assert day["status"] == "interrupted"


def test_cancel_keeps_completed_items_completed(tmp_path) -> None:
    database = StateDatabase(tmp_path / "state.sqlite3")
    job = database.create_job("scan_download")
    database.replace_job_items(job["id"], [
        {"relative_key": "date=2026-08-09/a", "size_bytes": 10, "sha256": "a" * 64},
        {"relative_key": "date=2026-08-09/b", "size_bytes": 20, "sha256": "b" * 64},
    ])
    database.update_job_item(
        job["id"], "date=2026-08-09/a", status="completed", bytes_done=10
    )
    database.finish_job_items(job["id"], "cancelled")

    items = {item["relative_key"]: item for item in database.job_items(job["id"])}
    assert items["date=2026-08-09/a"]["status"] == "completed"
    assert items["date=2026-08-09/b"]["status"] == "cancelled"


def test_replacing_job_manifest_removes_stale_items_and_keeps_matching_progress(
    tmp_path,
) -> None:
    database = StateDatabase(tmp_path / "state.sqlite3")
    job = database.create_job("download")
    database.replace_job_items(job["id"], [
        {"relative_key": "old", "size_bytes": 10, "sha256": "a" * 64},
        {"relative_key": "keep", "size_bytes": 20, "sha256": "b" * 64},
    ])
    database.update_job_item(
        job["id"], "keep", status="completed", bytes_done=20
    )

    database.replace_job_items(job["id"], [
        {"relative_key": "keep", "size_bytes": 20, "sha256": "b" * 64},
        {"relative_key": "new", "size_bytes": 30, "sha256": "c" * 64},
    ])

    items = {item["relative_key"]: item for item in database.job_items(job["id"])}
    assert set(items) == {"keep", "new"}
    assert items["keep"]["status"] == "completed"
    assert items["keep"]["bytes_done"] == 20
    assert items["new"]["status"] == "queued"

    database.replace_job_items(job["id"], [
        {"relative_key": "keep", "size_bytes": 25, "sha256": "d" * 64},
    ])
    changed = database.job_items(job["id"])[0]
    assert changed["status"] == "queued"
    assert changed["bytes_done"] == 0


def test_verification_progress_does_not_downgrade_completed_object(tmp_path) -> None:
    store = ConfigStore(tmp_path / "state")
    store.load()
    database = StateDatabase(store.root / "state.sqlite3")
    service = ArchiveService(store, database)
    job = database.create_job("download")
    relative_key = "date=2026-08-09/business/a.parquet"
    database.replace_job_items(job["id"], [{
        "relative_key": relative_key,
        "size_bytes": 100,
        "sha256": "a" * 64,
    }])
    database.update_job_item(
        job["id"], relative_key, status="completed", bytes_done=100
    )

    service._update_progress(job["id"], {
        "phase": "verifying",
        "current_object": relative_key,
        "object_count": 1,
        "objects_done": 1,
        "bytes_total": 100,
        "bytes_done": 100,
    })

    item = database.job_items(job["id"])[0]
    assert item["status"] == "completed"
    assert item["bytes_done"] == 100


def test_queued_job_can_be_cancelled_before_worker_starts(tmp_path) -> None:
    store = ConfigStore(tmp_path / "state")
    store.load()
    database = StateDatabase(store.root / "state.sqlite3")
    service = ArchiveService(store, database)

    job = service.request_scan(download=False)
    service.request_cancel()

    cancelled = database.job(job["id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["phase"] == "cancelled"
    assert service.status()["current_job"] is None


def test_completed_object_is_persisted_even_when_job_updates_are_throttled(
    tmp_path,
) -> None:
    store = ConfigStore(tmp_path / "state")
    store.load()
    database = StateDatabase(store.root / "state.sqlite3")
    service = ArchiveService(store, database)
    job = database.create_job(
        "download", profile_id="collector-a", archive_date="2026-08-09"
    )
    relative_key = "date=2026-08-09/business/a.parquet"
    service._update_progress(job["id"], {
        "phase": "downloading",
        "items": [{
            "relative_key": relative_key,
            "size_bytes": 100,
            "sha256": "a" * 64,
        }],
        "object_count": 1,
        "bytes_total": 100,
        "manifest_sha256": "f" * 64,
    })
    service._update_progress(job["id"], {
        "phase": "downloading",
        "current_object": relative_key,
        "current_object_bytes": 100,
        "current_object_total": 100,
        "object_count": 1,
        "objects_done": 1,
        "bytes_total": 100,
        "bytes_transferred": 100,
    })

    item = database.job_items(job["id"])[0]
    persisted_job = database.job(job["id"])
    assert item["status"] == "completed"
    assert item["bytes_done"] == 100
    assert persisted_job["manifest_sha256"] == "f" * 64


def test_archive_service_can_start_again_after_a_safe_stop(tmp_path) -> None:
    store = ConfigStore(tmp_path / "state")
    store.load()
    database = StateDatabase(store.root / "state.sqlite3")
    service = ArchiveService(store, database)

    service.start()
    assert service._thread is not None and service._thread.is_alive()
    assert service.stop() is True

    service.start()
    try:
        assert service._thread is not None and service._thread.is_alive()
        assert not service._stop.is_set()
        assert not service._cancel.is_set()
    finally:
        assert service.stop() is True


def test_safe_stop_queues_running_job_for_resume(tmp_path) -> None:
    store = ConfigStore(tmp_path / "state")
    store.load()
    database = StateDatabase(store.root / "state.sqlite3")
    service = ArchiveService(store, database)
    executing = threading.Event()

    def execute(_job_id, _action, arguments):
        database.upsert_day(
            arguments["profile_id"],
            arguments["archive_date"],
            status="downloading",
            detail="正在下载",
        )
        executing.set()
        assert service._cancel.wait(timeout=2)
        raise OperationCancelled("操作已取消，未完成结果不会发布")

    service._execute = execute
    job = service.request_download("collector-a", "2026-08-09")
    service.start()

    assert executing.wait(timeout=2)
    assert service.stop(timeout=2) is True
    recovered = database.job(job["id"])
    day = database.day("collector-a", "2026-08-09")
    assert recovered["status"] == "queued"
    assert recovered["phase"] == "recovering"
    assert recovered["cancel_requested"] == 0
    assert day["status"] == "interrupted"
    assert service._thread is None
