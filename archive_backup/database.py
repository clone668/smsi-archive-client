from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class StateDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS archive_days (
                    profile_id TEXT NOT NULL,
                    archive_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    manifest_sha256 TEXT NOT NULL DEFAULT '',
                    object_count INTEGER NOT NULL DEFAULT 0,
                    objects_done INTEGER NOT NULL DEFAULT 0,
                    row_count INTEGER NOT NULL DEFAULT 0,
                    bytes_total INTEGER NOT NULL DEFAULT 0,
                    bytes_done INTEGER NOT NULL DEFAULT 0,
                    report_summary TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (profile_id, archive_date)
                );
                CREATE INDEX IF NOT EXISTS idx_archive_days_updated
                    ON archive_days(updated_at DESC);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level TEXT NOT NULL,
                    event TEXT NOT NULL,
                    profile_id TEXT NOT NULL DEFAULT '',
                    archive_date TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_created
                    ON events(created_at DESC);
                CREATE TABLE IF NOT EXISTS runtime_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS archive_comparisons (
                    archive_date TEXT NOT NULL,
                    pair_id TEXT NOT NULL,
                    left_profile_id TEXT NOT NULL,
                    right_profile_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    compared_at TEXT NOT NULL,
                    PRIMARY KEY (archive_date, pair_id)
                );
                CREATE INDEX IF NOT EXISTS idx_archive_comparisons_date
                    ON archive_comparisons(archive_date DESC, pair_id);
                CREATE TABLE IF NOT EXISTS archive_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    requested_by TEXT NOT NULL DEFAULT 'manual',
                    profile_id TEXT NOT NULL DEFAULT '',
                    archive_date TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL DEFAULT 'queued',
                    manifest_sha256 TEXT NOT NULL DEFAULT '',
                    object_count INTEGER NOT NULL DEFAULT 0,
                    objects_done INTEGER NOT NULL DEFAULT 0,
                    bytes_total INTEGER NOT NULL DEFAULT 0,
                    bytes_done INTEGER NOT NULL DEFAULT 0,
                    speed_bytes_per_second INTEGER NOT NULL DEFAULT 0,
                    eta_seconds INTEGER,
                    current_object TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_archive_jobs_status
                    ON archive_jobs(status, id);
                CREATE INDEX IF NOT EXISTS idx_archive_jobs_updated
                    ON archive_jobs(updated_at DESC, id DESC);
                CREATE TABLE IF NOT EXISTS archive_job_items (
                    job_id INTEGER NOT NULL,
                    relative_key TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    bytes_done INTEGER NOT NULL DEFAULT 0,
                    sha256 TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (job_id, relative_key),
                    FOREIGN KEY (job_id) REFERENCES archive_jobs(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_archive_job_items_status
                    ON archive_job_items(job_id, status, relative_key);
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(archive_days)").fetchall()
            }
            if "report_summary" not in columns:
                connection.execute(
                    "ALTER TABLE archive_days ADD COLUMN report_summary TEXT NOT NULL DEFAULT ''"
                )

    def create_job(
        self,
        action: str,
        *,
        requested_by: str = "manual",
        profile_id: str = "",
        archive_date: str = "",
    ) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO archive_jobs(
                    action, requested_by, profile_id, archive_date, status,
                    phase, detail, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(action)[:32],
                    str(requested_by)[:16],
                    str(profile_id)[:64],
                    str(archive_date)[:10],
                    "queued",
                    "queued",
                    "等待后台任务执行",
                    now,
                    now,
                ),
            )
            job_id = int(cursor.lastrowid)
        return self.job(job_id) or {"id": job_id}

    def job(self, job_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM archive_jobs WHERE id=?", (int(job_id),)
            ).fetchone()
        return dict(row) if row else None

    def jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 500))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM archive_jobs ORDER BY id DESC LIMIT ?", (bounded,)
            ).fetchall()
        return [dict(row) for row in rows]

    def active_job(self) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM archive_jobs "
                "WHERE status IN ('queued','running','cancelling') "
                "ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'cancelling' THEN 1 ELSE 2 END, id "
                "LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def next_queued_job(self) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM archive_jobs WHERE status='queued' ORDER BY id LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def update_job(self, job_id: int, **fields: Any) -> None:
        allowed = {
            "profile_id", "archive_date", "status", "phase", "manifest_sha256",
            "object_count", "objects_done", "bytes_total", "bytes_done",
            "speed_bytes_per_second", "eta_seconds", "current_object", "detail",
            "error", "cancel_requested", "started_at", "finished_at",
        }
        values = {key: value for key, value in fields.items() if key in allowed}
        if not values:
            return
        values["updated_at"] = utc_now()
        assignments = ", ".join(f"{key}=?" for key in values)
        with self._lock, self._connect() as connection:
            connection.execute(
                f"UPDATE archive_jobs SET {assignments} WHERE id=?",
                (*values.values(), int(job_id)),
            )

    def request_job_cancel(self, job_id: int) -> None:
        job = self.job(job_id)
        if not job or job["status"] not in {"queued", "running", "cancelling"}:
            raise RuntimeError("当前任务不能取消")
        now = utc_now()
        if job["status"] == "queued":
            self.update_job(
                job_id,
                status="cancelled",
                phase="cancelled",
                cancel_requested=1,
                detail="任务在开始前已取消",
                finished_at=now,
            )
            return
        self.update_job(
            job_id,
            status="cancelling",
            phase="cancelling",
            cancel_requested=1,
            detail="正在停止任务，已完成对象会保留",
        )

    def recover_interrupted_jobs(self) -> int:
        now = utc_now()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE archive_jobs
                SET status='queued', phase='recovering', cancel_requested=0,
                    detail='客户端重启，任务将从已完成对象继续', error='', updated_at=?
                WHERE status IN ('running','cancelling')
                """,
                (now,),
            )
            connection.execute(
                """
                UPDATE archive_days
                SET status='interrupted',
                    detail='客户端重启，等待从暂存对象继续',
                    updated_at=?
                WHERE status IN ('downloading','verifying')
                """,
                (now,),
            )
            return int(cursor.rowcount)

    def replace_job_items(self, job_id: int, items: list[dict[str, Any]]) -> None:
        now = utc_now()
        normalized = [
            {
                "relative_key": str(item.get("relative_key") or "")[:2000],
                "size_bytes": int(item.get("size_bytes") or 0),
                "sha256": str(item.get("sha256") or "")[:64],
            }
            for item in items
            if str(item.get("relative_key") or "")
        ]
        incoming_keys = {item["relative_key"] for item in normalized}
        with self._lock, self._connect() as connection:
            existing_keys = {
                str(row["relative_key"])
                for row in connection.execute(
                    "SELECT relative_key FROM archive_job_items WHERE job_id=?",
                    (int(job_id),),
                ).fetchall()
            }
            stale_keys = existing_keys - incoming_keys
            if stale_keys:
                connection.executemany(
                    "DELETE FROM archive_job_items WHERE job_id=? AND relative_key=?",
                    [(int(job_id), relative_key) for relative_key in stale_keys],
                )
            connection.executemany(
                """
                INSERT INTO archive_job_items(
                    job_id, relative_key, status, size_bytes, bytes_done,
                    sha256, updated_at
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(job_id, relative_key) DO UPDATE SET
                    status=CASE
                        WHEN archive_job_items.size_bytes=excluded.size_bytes
                         AND archive_job_items.sha256=excluded.sha256
                        THEN archive_job_items.status ELSE 'queued' END,
                    bytes_done=CASE
                        WHEN archive_job_items.size_bytes=excluded.size_bytes
                         AND archive_job_items.sha256=excluded.sha256
                        THEN archive_job_items.bytes_done ELSE 0 END,
                    error=CASE
                        WHEN archive_job_items.size_bytes=excluded.size_bytes
                         AND archive_job_items.sha256=excluded.sha256
                        THEN archive_job_items.error ELSE '' END,
                    finished_at=CASE
                        WHEN archive_job_items.size_bytes=excluded.size_bytes
                         AND archive_job_items.sha256=excluded.sha256
                        THEN archive_job_items.finished_at ELSE '' END,
                    size_bytes=excluded.size_bytes,
                    sha256=excluded.sha256,
                    updated_at=excluded.updated_at
                """,
                [
                    (
                        int(job_id),
                        item["relative_key"],
                        "queued",
                        item["size_bytes"],
                        0,
                        item["sha256"],
                        now,
                    )
                    for item in normalized
                ],
            )

    def update_job_item(self, job_id: int, relative_key: str, **fields: Any) -> None:
        allowed = {
            "status", "size_bytes", "bytes_done", "sha256", "attempts", "error",
            "started_at", "finished_at",
        }
        values = {key: value for key, value in fields.items() if key in allowed}
        if not values:
            return
        values["updated_at"] = utc_now()
        assignments = ", ".join(f"{key}=?" for key in values)
        with self._lock, self._connect() as connection:
            connection.execute(
                f"UPDATE archive_job_items SET {assignments} "
                "WHERE job_id=? AND relative_key=?",
                (*values.values(), int(job_id), str(relative_key)),
            )

    def job_items(self, job_id: int, limit: int = 1000) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 10000))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM archive_job_items WHERE job_id=? "
                "ORDER BY CASE status WHEN 'downloading' THEN 0 WHEN 'verifying' THEN 1 "
                "WHEN 'failed' THEN 2 WHEN 'queued' THEN 3 ELSE 4 END, relative_key LIMIT ?",
                (int(job_id), bounded),
            ).fetchall()
        return [dict(row) for row in rows]

    def finish_job_items(self, job_id: int, status: str, error: str = "") -> None:
        now = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE archive_job_items
                SET status=?,
                    bytes_done=CASE WHEN ?='completed' THEN size_bytes ELSE bytes_done END,
                    error=?, finished_at=?, updated_at=?
                WHERE job_id=?
                  AND (?='completed' OR status!='completed')
                """,
                (
                    str(status)[:16], str(status), str(error)[:4000], now, now,
                    int(job_id), str(status),
                ),
            )

    def upsert_day(self, profile_id: str, archive_date: str, **fields: Any) -> None:
        allowed = {
            "status",
            "manifest_sha256",
            "object_count",
            "objects_done",
            "row_count",
            "bytes_total",
            "bytes_done",
            "report_summary",
            "detail",
            "error",
        }
        values = {key: value for key, value in fields.items() if key in allowed}
        status = str(values.pop("status", "unknown"))
        now = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO archive_days(profile_id, archive_date, status, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(profile_id, archive_date) DO UPDATE SET
                    status=excluded.status, updated_at=excluded.updated_at
                """,
                (profile_id, archive_date, status, now),
            )
            for key, value in values.items():
                connection.execute(
                    f"UPDATE archive_days SET {key}=?, updated_at=? "
                    "WHERE profile_id=? AND archive_date=?",
                    (value, now, profile_id, archive_date),
                )

    def day(self, profile_id: str, archive_date: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM archive_days WHERE profile_id=? AND archive_date=?",
                (profile_id, archive_date),
            ).fetchone()
        return self._decode_day(row) if row else None

    def days(self, limit: int = 500) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 5000))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM archive_days ORDER BY archive_date DESC, profile_id LIMIT ?",
                (bounded,),
            ).fetchall()
        return [self._decode_day(row) for row in rows]

    def delete_days(self, profile_id: str, archive_dates: set[str]) -> int:
        dates = sorted({str(value)[:10] for value in archive_dates if value})
        if not dates:
            return 0
        with self._lock, self._connect() as connection:
            cursor = connection.executemany(
                "DELETE FROM archive_days WHERE profile_id=? AND archive_date=?",
                [(str(profile_id)[:64], archive_date) for archive_date in dates],
            )
            return int(cursor.rowcount)

    @staticmethod
    def _decode_day(row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        raw_summary = payload.get("report_summary")
        if isinstance(raw_summary, str) and raw_summary:
            try:
                parsed = json.loads(raw_summary)
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed = {}
            payload["report_summary"] = parsed if isinstance(parsed, dict) else {}
        else:
            payload["report_summary"] = {}
        return payload

    def event(
        self,
        level: str,
        event: str,
        *,
        profile_id: str = "",
        archive_date: str = "",
        detail: str = "",
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO events(level,event,profile_id,archive_date,detail,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    str(level)[:16],
                    str(event)[:80],
                    str(profile_id)[:64],
                    str(archive_date)[:10],
                    str(detail)[:4000],
                    utc_now(),
                ),
            )

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 500))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (bounded,)
            ).fetchall()
        return [dict(row) for row in rows]

    def replace_comparisons(self, values: list[dict[str, Any]]) -> None:
        now = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM archive_comparisons")
            connection.executemany(
                """
                INSERT INTO archive_comparisons(
                    archive_date, pair_id, left_profile_id, right_profile_id,
                    status, detail_json, compared_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                [
                    (
                        str(item.get("archive_date") or "")[:10],
                        str(item.get("pair_id") or "")[:140],
                        str(item.get("left_profile_id") or "")[:64],
                        str(item.get("right_profile_id") or "")[:64],
                        str(item.get("status") or "unknown")[:16],
                        json.dumps(item, ensure_ascii=False, sort_keys=True),
                        now,
                    )
                    for item in values
                ],
            )

    def comparisons(self, limit: int = 180) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 1000))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM archive_comparisons "
                "ORDER BY archive_date DESC, pair_id LIMIT ?",
                (bounded,),
            ).fetchall()
        results = []
        for row in rows:
            value = dict(row)
            try:
                detail = json.loads(str(value.pop("detail_json")))
            except json.JSONDecodeError:
                detail = {}
            if not isinstance(detail, dict):
                detail = {}
            detail.update(value)
            results.append(detail)
        return results

    def set_runtime(self, key: str, value: Any) -> None:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO runtime_state(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, encoded, utc_now()),
            )

    def get_runtime(self, key: str, default: Any = None) -> Any:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM runtime_state WHERE key=?", (key,)
            ).fetchone()
        if not row:
            return default
        try:
            return json.loads(str(row["value"]))
        except json.JSONDecodeError:
            return default
