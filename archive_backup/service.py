from __future__ import annotations

import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import ConfigStore
from .database import StateDatabase, utc_now
from .manager import ArchiveManager
from .verifier import OperationCancelled


class ArchiveService:
    def __init__(self, store: ConfigStore, database: StateDatabase) -> None:
        self.store = store
        self.database = database
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending: tuple[str, dict[str, str]] | None = None
        self._state: dict[str, Any] = {
            "running": False,
            "action": "idle",
            "detail": "等待首次检查",
            "started_at": "",
            "last_finished_at": "",
            "last_error": "",
            "next_scan_at": "",
        }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="archive-service", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._cancel.set()
        with self._condition:
            self._condition.notify_all()
        if self._thread:
            self._thread.join(timeout=15)

    def request_scan(self, *, download: bool = True) -> None:
        with self._condition:
            if self._state["running"] or self._pending:
                raise RuntimeError("已有任务正在执行或等待执行")
            self._pending = ("scan_download" if download else "scan", {})
            self._condition.notify_all()

    def request_verify(self, profile_id: str, archive_date: str) -> None:
        with self._condition:
            if self._state["running"] or self._pending:
                raise RuntimeError("已有任务正在执行或等待执行")
            self._pending = ("verify", {"profile_id": profile_id, "archive_date": archive_date})
            self._condition.notify_all()

    def request_cancel(self) -> None:
        if not self._state["running"]:
            raise RuntimeError("当前没有可取消的任务")
        self._cancel.set()

    def wake(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def status(self) -> dict[str, Any]:
        with self._condition:
            state = dict(self._state)
            state["pending"] = self._pending[0] if self._pending else ""
        config = self.store.load()
        try:
            config.archive_root.mkdir(parents=True, exist_ok=True)
            disk = shutil.disk_usage(config.archive_root)
            state["disk"] = {"total": disk.total, "used": disk.used, "free": disk.free}
            state["disk_error"] = ""
        except OSError as exc:
            state["disk"] = {"total": 0, "used": 0, "free": 0}
            state["disk_error"] = str(exc)
        state["auto_download"] = config.auto_download
        state["profile_count"] = len(config.profiles)
        state["enabled_profile_count"] = sum(1 for item in config.profiles if item.enabled)
        return state

    def _set_state(self, **fields: Any) -> None:
        with self._condition:
            self._state.update(fields)

    def _execute(self, action: str, arguments: dict[str, str]) -> None:
        config = self.store.load()
        manager = ArchiveManager(config, self.database, cancel=self._cancel)
        if action in {"scan", "scan_download"}:
            results = manager.scan_all(download=action == "scan_download")
            detail = f"已检查 {sum(item['dates'] for item in results)} 个日期"
        elif action == "verify":
            manager.verify_existing(arguments["profile_id"], arguments["archive_date"])
            detail = f"已重新验证 {arguments['archive_date']}"
        else:
            raise RuntimeError("未知后台任务")
        self._set_state(detail=detail)

    def _loop(self) -> None:
        first_run = True
        while not self._stop.is_set():
            try:
                config = self.store.load()
            except Exception as exc:
                self._set_state(
                    running=False,
                    action="idle",
                    detail="配置加载失败",
                    last_error=str(exc),
                    last_finished_at=utc_now(),
                )
                self.database.event("error", "客户端配置加载失败", detail=str(exc))
                with self._condition:
                    self._condition.wait(timeout=60)
                continue
            interval = max(60, int(config.poll_minutes) * 60)
            with self._condition:
                if self._pending:
                    action, arguments = self._pending
                    self._pending = None
                elif first_run or config.auto_download:
                    action, arguments = ("scan_download" if config.auto_download else "scan"), {}
                else:
                    action, arguments = "", {}
                first_run = False
            if action:
                self._cancel.clear()
                self._set_state(
                    running=True, action=action, detail="任务已启动",
                    started_at=utc_now(), last_error="",
                )
                try:
                    self._execute(action, arguments)
                except OperationCancelled as exc:
                    self._set_state(detail=str(exc), last_error="")
                    self.database.event("warning", "后台任务已取消", detail=str(exc))
                except Exception as exc:
                    self._set_state(detail="任务失败", last_error=str(exc))
                    self.database.event("error", "后台任务失败", detail=str(exc))
                finally:
                    next_scan = datetime.now(timezone.utc) + timedelta(seconds=interval)
                    self._set_state(
                        running=False, action="idle", last_finished_at=utc_now(),
                        next_scan_at=next_scan.isoformat().replace("+00:00", "Z"),
                    )
            deadline = time.monotonic() + interval
            with self._condition:
                while not self._stop.is_set() and not self._pending:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._condition.wait(timeout=min(remaining, 60))
