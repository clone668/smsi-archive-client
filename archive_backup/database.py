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
                """
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
        return dict(row) if row else None

    def days(self, limit: int = 500) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 5000))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM archive_days ORDER BY archive_date DESC, profile_id LIMIT ?",
                (bounded,),
            ).fetchall()
        return [dict(row) for row in rows]

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
