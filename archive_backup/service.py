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


ACTIVE_JOB_STATES = {"queued", "running", "cancelling"}


class ArchiveService:
    def __init__(self, store: ConfigStore, database: StateDatabase) -> None:
        self.store = store
        self.database = database
        self._condition = threading.Condition()
        self._persistence_lock = threading.Lock()
        self._stop = threading.Event()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._resume_thread: threading.Thread | None = None
        self._pending: tuple[int, str, dict[str, str]] | None = None
        self._current_job_id = 0
        self._last_progress_write = 0.0
        recovered = self.database.recover_interrupted_jobs()
        self._state: dict[str, Any] = {
            "running": False,
            "action": "idle",
            "detail": (
                f"发现 {recovered} 个被重启打断的任务，等待恢复"
                if recovered
                else "等待首次检查"
            ),
            "started_at": "",
            "last_finished_at": "",
            "last_error": "",
            "next_scan_at": "",
            "progress": {},
        }

    def start(self) -> None:
        with self._condition:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._cancel.clear()
            self._thread = threading.Thread(
                target=self._loop, name="archive-service", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 30) -> bool:
        self._stop.set()
        self._cancel.set()
        with self._condition:
            self._condition.notify_all()
            thread = self._thread
        if thread:
            thread.join(timeout=timeout)
        stopped = thread is None or not thread.is_alive()
        if stopped:
            with self._condition:
                if self._thread is thread:
                    self._thread = None
        return stopped

    def resume_when_stopped(self) -> None:
        with self._condition:
            thread = self._thread
            if thread is None or not thread.is_alive():
                self.start()
                return
            if not self._stop.is_set():
                return
            if self._resume_thread and self._resume_thread.is_alive():
                return
            resume_thread = threading.Thread(
                target=self._resume_after,
                args=(thread,),
                name="archive-service-resume",
                daemon=True,
            )
            self._resume_thread = resume_thread
            resume_thread.start()

    def _resume_after(self, thread: threading.Thread) -> None:
        thread.join()
        with self._condition:
            self._resume_thread = None
        self.start()

    def _queue_job(
        self,
        action: str,
        arguments: dict[str, str] | None = None,
        *,
        requested_by: str = "manual",
    ) -> dict[str, Any]:
        arguments = dict(arguments or {})
        with self._condition:
            active = self.database.active_job()
            if self._state["running"] or self._pending or (
                active and active.get("status") in ACTIVE_JOB_STATES
            ):
                raise RuntimeError("已有任务正在执行或等待执行")
            job = self.database.create_job(
                action,
                requested_by=requested_by,
                profile_id=arguments.get("profile_id", ""),
                archive_date=arguments.get("archive_date", ""),
            )
            self._pending = (int(job["id"]), action, arguments)
            self._condition.notify_all()
        return job

    def request_scan(self, *, download: bool = True) -> dict[str, Any]:
        return self._queue_job("scan_download" if download else "scan")

    def request_download(self, profile_id: str, archive_date: str) -> dict[str, Any]:
        return self._queue_job(
            "download",
            {"profile_id": profile_id, "archive_date": archive_date},
        )

    def request_verify(self, profile_id: str, archive_date: str) -> dict[str, Any]:
        return self._queue_job(
            "verify",
            {"profile_id": profile_id, "archive_date": archive_date},
        )

    def request_cancel(self) -> None:
        with self._condition:
            if self._state["running"] and self._current_job_id:
                self.database.request_job_cancel(self._current_job_id)
                self._state["detail"] = "正在停止任务，已完成对象会保留"
                self._cancel.set()
                return
            active = self.database.active_job()
            if not active or active.get("status") != "queued":
                raise RuntimeError("当前没有可取消的任务")
            self.database.request_job_cancel(int(active["id"]))
            if self._pending and self._pending[0] == int(active["id"]):
                self._pending = None
            self._state["detail"] = "排队任务已取消"
            self._condition.notify_all()

    def wake(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def status(self) -> dict[str, Any]:
        with self._condition:
            state = dict(self._state)
            state["progress"] = dict(self._state.get("progress") or {})
            state["pending"] = self._pending[1] if self._pending else ""
            current_job_id = self._current_job_id
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
        state["enabled_profile_count"] = sum(
            1 for item in config.profiles if item.enabled
        )
        state["current_job"] = (
            self.database.job(current_job_id)
            if current_job_id
            else self.database.active_job()
        )
        return state

    def _set_state(self, **fields: Any) -> None:
        with self._condition:
            self._state.update(fields)

    def _execute(self, job_id: int, action: str, arguments: dict[str, str]) -> str:
        config = self.store.load()
        manager = ArchiveManager(
            config,
            self.database,
            cancel=self._cancel,
            progress=lambda progress: self._update_progress(job_id, progress),
        )
        if action in {"scan", "scan_download"}:
            results = manager.scan_all(download=action == "scan_download")
            removed = sum(item.get("stale_removed", 0) for item in results)
            detail = f"已检查 {sum(item['dates'] for item in results)} 个日期"
            return f"{detail}，清理 {removed} 条陈旧状态" if removed else detail
        if action == "download":
            manager.download_specific(
                arguments["profile_id"], arguments["archive_date"]
            )
            return f"已下载并验证 {arguments['archive_date']}"
        if action == "verify":
            manager.verify_existing(
                arguments["profile_id"], arguments["archive_date"]
            )
            return f"已重新验证 {arguments['archive_date']}"
        raise RuntimeError("未知后台任务")

    def _update_progress(self, job_id: int, progress: dict[str, Any]) -> None:
        public_progress = {
            key: value for key, value in progress.items() if key != "items"
        }
        self._set_state(progress=public_progress)
        items = progress.get("items")
        now_monotonic = time.monotonic()
        terminal_phase = str(progress.get("phase") or "") in {
            "verified", "cancelled", "failed"
        }
        with self._persistence_lock:
            if isinstance(items, list):
                self.database.replace_job_items(job_id, items)
            phase = str(progress.get("phase") or "running")
            current_object = str(progress.get("current_object") or "")
            if current_object and phase == "downloading":
                current = int(progress.get("current_object_bytes") or 0)
                total = int(progress.get("current_object_total") or 0)
                item_status = (
                    "completed"
                    if total and current >= total
                    else "downloading"
                )
                self.database.update_job_item(
                    job_id,
                    current_object,
                    status=item_status,
                    bytes_done=current,
                    finished_at=utc_now() if item_status == "completed" else "",
                )
            if not terminal_phase and now_monotonic - self._last_progress_write < 0.75:
                return
            self._last_progress_write = now_monotonic
            bytes_done = int(
                progress.get("bytes_transferred")
                or progress.get("bytes_done")
                or 0
            )
            job_fields: dict[str, Any] = {
                "status": "running",
                "phase": phase,
                "profile_id": str(progress.get("profile_id") or ""),
                "archive_date": str(progress.get("archive_date") or ""),
                "object_count": int(progress.get("object_count") or 0),
                "objects_done": int(progress.get("objects_done") or 0),
                "bytes_total": int(progress.get("bytes_total") or 0),
                "bytes_done": bytes_done,
                "speed_bytes_per_second": int(
                    progress.get("speed_bytes_per_second") or 0
                ),
                "eta_seconds": progress.get("eta_seconds"),
                "current_object": current_object,
                "detail": {
                    "discovering": "正在读取远端归档日期清单",
                    "scanning": "正在检查远端归档日期与本地状态",
                    "downloading": "正在下载并逐对象校验",
                    "verifying": "正在执行完整恢复校验",
                    "verified": "归档已经完整验证",
                }.get(phase, str(self._state.get("detail") or "任务执行中")),
                "error": "",
            }
            if progress.get("manifest_sha256"):
                job_fields["manifest_sha256"] = str(
                    progress["manifest_sha256"]
                )
            self.database.update_job(
                job_id,
                **job_fields,
            )

    @staticmethod
    def _job_arguments(job: dict[str, Any]) -> dict[str, str]:
        return {
            "profile_id": str(job.get("profile_id") or ""),
            "archive_date": str(job.get("archive_date") or ""),
        }

    def _wait_for_work(self, interval: int) -> None:
        deadline = time.monotonic() + interval
        with self._condition:
            while not self._stop.is_set() and not self._pending:
                if self.database.next_queued_job():
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(timeout=min(remaining, 60))

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
            pending: tuple[int, str, dict[str, str]] | None = None
            with self._condition:
                if self._pending:
                    pending = self._pending
                    self._pending = None
            if pending is None:
                queued = self.database.next_queued_job()
                if queued:
                    pending = (
                        int(queued["id"]),
                        str(queued["action"]),
                        self._job_arguments(queued),
                    )
                elif first_run or config.auto_download:
                    action = "scan_download" if config.auto_download else "scan"
                    job = self.database.create_job(action, requested_by="automatic")
                    pending = (int(job["id"]), action, {})
            first_run = False
            if pending:
                job_id, action, arguments = pending
                started_at = utc_now()
                with self._condition:
                    queued_job = self.database.job(job_id)
                    if not queued_job or queued_job.get("status") != "queued":
                        pending = None
                    else:
                        starting_detail = {
                            "scan": "正在检查网盘归档状态",
                            "scan_download": "正在检查归档状态与可同步日期",
                            "download": "正在准备指定日期下载",
                            "verify": "正在准备重新校验",
                        }.get(action, "任务已启动")
                        self._cancel.clear()
                        self._current_job_id = job_id
                        self.database.update_job(
                            job_id,
                            status="running",
                            phase="starting",
                            started_at=started_at,
                            detail=starting_detail,
                            error="",
                            cancel_requested=0,
                        )
                        self._state.update(
                            running=True,
                            action=action,
                            detail=starting_detail,
                            started_at=started_at,
                            last_error="",
                            progress={},
                        )
                if pending is None:
                    self._wait_for_work(interval)
                    continue
                try:
                    detail = self._execute(job_id, action, arguments)
                    self._set_state(detail=detail)
                    self.database.update_job(
                        job_id,
                        status="completed",
                        phase="completed",
                        detail=detail,
                        error="",
                        speed_bytes_per_second=0,
                        eta_seconds=0,
                        current_object="",
                        finished_at=utc_now(),
                    )
                    self.database.finish_job_items(job_id, "completed")
                except OperationCancelled as exc:
                    if self._stop.is_set():
                        detail = "客户端停止，任务将在下次启动时恢复"
                        progress = dict(self._state.get("progress") or {})
                        profile_id = str(
                            arguments.get("profile_id")
                            or progress.get("profile_id")
                            or ""
                        )
                        archive_date = str(
                            arguments.get("archive_date")
                            or progress.get("archive_date")
                            or ""
                        )
                        if profile_id and archive_date:
                            self.database.upsert_day(
                                profile_id,
                                archive_date,
                                status="interrupted",
                                detail="客户端重启，等待从暂存对象继续",
                                error="",
                            )
                        self.database.update_job(
                            job_id,
                            status="queued",
                            phase="recovering",
                            cancel_requested=0,
                            detail=detail,
                            error="",
                        )
                    else:
                        detail = str(exc)
                        self.database.update_job(
                            job_id,
                            status="cancelled",
                            phase="cancelled",
                            detail=detail,
                            error="",
                            finished_at=utc_now(),
                        )
                        self.database.finish_job_items(job_id, "cancelled")
                        self.database.event("warning", "后台任务已取消", detail=detail)
                    self._set_state(detail=detail, last_error="")
                except Exception as exc:
                    error = str(exc)
                    self._set_state(detail="任务失败", last_error=error)
                    self.database.update_job(
                        job_id,
                        status="failed",
                        phase="failed",
                        detail="任务执行失败",
                        error=error,
                        speed_bytes_per_second=0,
                        finished_at=utc_now(),
                    )
                    self.database.finish_job_items(job_id, "failed", error)
                    self.database.event("error", "后台任务失败", detail=error)
                finally:
                    next_scan = datetime.now(timezone.utc) + timedelta(seconds=interval)
                    self._current_job_id = 0
                    self._set_state(
                        running=False,
                        action="idle",
                        last_finished_at=utc_now(),
                        next_scan_at=next_scan.isoformat().replace("+00:00", "Z"),
                    )
            self._wait_for_work(interval)
