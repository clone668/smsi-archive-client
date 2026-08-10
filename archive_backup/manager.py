from __future__ import annotations

import json
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .config import ClientConfig, ProfileConfig
from .comparison import build_archive_comparisons
from .database import StateDatabase
from .protocol import DATE_RE, ManifestSnapshot, parse_manifest, parse_progress
from .sources import ArchiveSource, build_source, split_bandwidth_limit
from .verifier import (
    OperationCancelled,
    local_object_path,
    raise_if_cancelled,
    sha256_file,
    verify_local_day,
)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def read_json(path: Path, maximum_bytes: int = 32 * 1024 * 1024) -> dict[str, Any] | None:
    if not path.is_file() or path.stat().st_size > maximum_bytes:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


class ArchiveManager:
    def __init__(
        self,
        config: ClientConfig,
        database: StateDatabase,
        *,
        cancel: threading.Event | None = None,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self.cancel = cancel or threading.Event()
        self._progress_lock = threading.Lock()
        self._progress_callback = progress

    def _emit_progress(self, payload: dict[str, Any]) -> None:
        if self._progress_callback is None:
            return
        try:
            self._progress_callback(payload)
        except Exception:
            # Progress reporting must never interrupt archive verification.
            return

    def profile_root(self, profile: ProfileConfig) -> Path:
        return self.config.archive_root / f"collector={profile.collector_id}"

    def final_root(self, profile: ProfileConfig, archive_date: str) -> Path:
        return self.profile_root(profile) / f"date={archive_date}"

    def staging_root(self, profile: ProfileConfig, archive_date: str) -> Path:
        return self.profile_root(profile) / ".partial" / f"date={archive_date}"

    def quarantine_root(self, profile: ProfileConfig) -> Path:
        return self.profile_root(profile) / ".quarantine"

    def _quarantine(self, profile: ProfileConfig, path: Path, reason: str) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = self.quarantine_root(profile) / f"{path.name}.{stamp}.{reason}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        counter = 1
        while destination.exists():
            destination = destination.with_name(f"{destination.name}.{counter}")
            counter += 1
        os.replace(path, destination)
        return destination

    def _eligible_dates(self, source: ArchiveSource) -> list[str]:
        today = datetime.now(timezone.utc).date()
        cutoff = today - timedelta(days=self.config.history_days)
        dates = []
        for value in source.list_dates():
            if not DATE_RE.fullmatch(value):
                continue
            try:
                parsed = date.fromisoformat(value)
            except ValueError:
                continue
            if cutoff <= parsed <= today:
                dates.append(value)
        return sorted(dates)

    def _remote_snapshot(
        self,
        source: ArchiveSource,
        profile: ProfileConfig,
        archive_date: str,
    ) -> tuple[ManifestSnapshot | None, str, str]:
        progress_key = f"date={archive_date}/_smsi-archive-progress.json"
        progress_raw = source.read_small(progress_key, 1024 * 1024)
        if progress_raw is not None:
            progress = parse_progress(progress_raw, archive_date)
            if progress.status == "running":
                return None, "remote_running", progress.stage
            if progress.status == "failed":
                return None, "remote_failed", progress.error or progress.stage
        manifest_raw = source.read_small(f"date={archive_date}/manifest.json", 32 * 1024 * 1024)
        if manifest_raw is None:
            return None, "waiting_manifest", "归档尚未发布 manifest"
        snapshot = parse_manifest(manifest_raw, archive_date)
        return snapshot, "ready", "远端归档已验证"

    def _verified_receipt(
        self,
        profile: ProfileConfig,
        archive_date: str,
        snapshot: ManifestSnapshot,
    ) -> dict[str, Any] | None:
        final = self.final_root(profile, archive_date)
        receipt = read_json(final / ".smsi-verified.json")
        manifest_path = final / "manifest.json"
        if (
            not receipt
            or receipt.get("contract_version") != "smsi-local-archive-verification/v1"
            or receipt.get("status") != "verified"
            or receipt.get("archive_date") != archive_date
            or not manifest_path.is_file()
            or (final / ".smsi-verification-failed.json").exists()
        ):
            return None
        if receipt.get("manifest_sha256") != snapshot.sha256:
            raise RuntimeError("远端 manifest 已变化，拒绝覆盖已有验证归档")
        if manifest_path.stat().st_size != len(snapshot.raw) or sha256_file(manifest_path) != snapshot.sha256:
            return None
        if int(receipt.get("object_count") or -1) != snapshot.object_count:
            raise RuntimeError("本地验证凭据对象数不一致")
        for item in snapshot.objects:
            path = local_object_path(final, str(item["relative_key"]), archive_date)
            if not path.is_file() or path.stat().st_size != int(item["size_bytes"]):
                return None
        return receipt

    def _reserve_disk(self, required_bytes: int) -> None:
        self.config.archive_root.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.config.archive_root).free
        required = max(0, int(required_bytes)) + int(self.config.minimum_free_bytes)
        if free < required:
            raise RuntimeError(
                f"磁盘空间不足：可用 {free} 字节，需要至少 {required} 字节"
            )

    def _prepare_stage(
        self,
        profile: ProfileConfig,
        snapshot: ManifestSnapshot,
    ) -> Path:
        stage = self.staging_root(profile, snapshot.archive_date)
        if stage.exists():
            staged_manifest = stage / "manifest.json"
            if not staged_manifest.is_file() or staged_manifest.read_bytes() != snapshot.raw:
                self._quarantine(profile, stage, "manifest-changed")
        stage.mkdir(parents=True, exist_ok=True)
        manifest_path = stage / "manifest.json"
        temporary = manifest_path.with_suffix(".json.tmp")
        temporary.write_bytes(snapshot.raw)
        temporary.replace(manifest_path)
        return stage

    def _download_one(
        self,
        source: ArchiveSource,
        stage: Path,
        snapshot: ManifestSnapshot,
        item: dict[str, Any],
        bandwidth_limit: str,
        progress: Callable[[str, int, bool], None] | None = None,
    ) -> tuple[str, int, int]:
        raise_if_cancelled(self.cancel)
        key = str(item["relative_key"])
        expected_size = int(item["size_bytes"])
        expected_sha = str(item["sha256"])
        destination = local_object_path(stage, key, snapshot.archive_date)
        if destination.is_file() and destination.stat().st_size == expected_size:
            if sha256_file(destination, self.cancel) == expected_sha:
                if progress is not None:
                    progress(key, expected_size, True)
                return key, expected_size, 0
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".downloading")
        if temporary.exists():
            temporary.unlink()
        try:
            def source_progress(current: int) -> None:
                if progress is not None:
                    progress(key, min(max(int(current), 0), expected_size), False)

            observed_size = source.download(
                key,
                temporary,
                self.cancel,
                progress=source_progress,
                bandwidth_limit=bandwidth_limit,
            )
            raise_if_cancelled(self.cancel)
            if observed_size != expected_size or temporary.stat().st_size != expected_size:
                raise RuntimeError(f"下载对象大小不一致: {key}")
            if sha256_file(temporary, self.cancel) != expected_sha:
                raise RuntimeError(f"下载对象 SHA-256 不一致: {key}")
            os.replace(temporary, destination)
            if progress is not None:
                progress(key, expected_size, True)
        finally:
            if temporary.exists():
                temporary.unlink()
        return key, expected_size, expected_size

    def download_day(
        self,
        profile: ProfileConfig,
        source: ArchiveSource,
        snapshot: ManifestSnapshot,
    ) -> dict[str, Any]:
        archive_date = snapshot.archive_date
        existing = self._verified_receipt(profile, archive_date, snapshot)
        if existing:
            self.database.upsert_day(
                profile.profile_id,
                archive_date,
                status="verified",
                manifest_sha256=snapshot.sha256,
                object_count=snapshot.object_count,
                objects_done=snapshot.object_count,
                row_count=snapshot.row_count,
                bytes_total=sum(int(item["size_bytes"]) for item in snapshot.objects),
                bytes_done=sum(int(item["size_bytes"]) for item in snapshot.objects),
                detail="本地恢复验证已通过",
                error="",
            )
            return existing

        final = self.final_root(profile, archive_date)
        if final.exists():
            quarantined = self._quarantine(profile, final, "unverified")
            self.database.event(
                "warning", "未验证目录已隔离", profile_id=profile.profile_id,
                archive_date=archive_date, detail=str(quarantined),
            )
        stage = self._prepare_stage(profile, snapshot)
        bytes_total = sum(int(item["size_bytes"]) for item in snapshot.objects)
        self._reserve_disk(bytes_total)
        self.database.upsert_day(
            profile.profile_id,
            archive_date,
            status="downloading",
            manifest_sha256=snapshot.sha256,
            object_count=snapshot.object_count,
            objects_done=0,
            row_count=snapshot.row_count,
            bytes_total=bytes_total,
            bytes_done=0,
            detail="正在逐对象下载与校验",
            error="",
        )
        objects_done = 0
        bytes_done = 0
        network_bytes = 0
        download_workers = max(1, min(self.config.download_workers, max(snapshot.object_count, 1)))
        per_worker_bandwidth = split_bandwidth_limit(
            self.config.bandwidth_limit, download_workers
        )
        object_bytes: dict[str, int] = {}
        completed_keys: set[str] = set()
        active_keys: set[str] = set()
        object_sizes = {
            str(value["relative_key"]): int(value["size_bytes"])
            for value in snapshot.objects
        }
        started_monotonic = time.monotonic()

        def publish_progress(key: str, current: int, completed: bool) -> None:
            with self._progress_lock:
                item_size = object_sizes[key]
                object_bytes[key] = min(max(int(current), 0), item_size)
                if completed:
                    completed_keys.add(key)
                    active_keys.discard(key)
                else:
                    active_keys.add(key)
                bytes_transferred = sum(object_bytes.values())
                completed_bytes = sum(object_sizes[key] for key in completed_keys)
                elapsed = max(time.monotonic() - started_monotonic, 0.001)
                speed = bytes_transferred / elapsed
                remaining = max(bytes_total - bytes_transferred, 0)
                self._emit_progress({
                    "phase": "downloading",
                    "profile_id": profile.profile_id,
                    "archive_date": archive_date,
                    "object_count": snapshot.object_count,
                    "objects_done": len(completed_keys),
                    "bytes_total": bytes_total,
                    "bytes_done": completed_bytes,
                    "bytes_transferred": bytes_transferred,
                    "current_object": key,
                    "current_object_bytes": object_bytes[key],
                    "current_object_total": item_size,
                    "active_transfers": len(active_keys),
                    "download_workers": download_workers,
                    "bandwidth_limit": self.config.bandwidth_limit.strip(),
                    "speed_bytes_per_second": int(speed),
                    "eta_seconds": int(remaining / speed) if speed > 0 else None,
                })

        self._emit_progress({
            "phase": "downloading",
            "profile_id": profile.profile_id,
            "archive_date": archive_date,
            "object_count": snapshot.object_count,
            "objects_done": 0,
            "bytes_total": bytes_total,
            "bytes_done": 0,
            "bytes_transferred": 0,
            "current_object": "",
            "current_object_bytes": 0,
            "current_object_total": 0,
            "active_transfers": 0,
            "download_workers": download_workers,
            "bandwidth_limit": self.config.bandwidth_limit.strip(),
            "speed_bytes_per_second": 0,
            "eta_seconds": None,
        })

        with ThreadPoolExecutor(max_workers=download_workers) as executor:
            futures = [
                executor.submit(
                    self._download_one,
                    source,
                    stage,
                    snapshot,
                    item,
                    per_worker_bandwidth,
                    publish_progress,
                )
                for item in snapshot.objects
            ]
            for future in as_completed(futures):
                raise_if_cancelled(self.cancel)
                _key, completed_bytes, downloaded_bytes = future.result()
                with self._progress_lock:
                    objects_done += 1
                    bytes_done += completed_bytes
                    network_bytes += downloaded_bytes
                    self.database.upsert_day(
                        profile.profile_id,
                        archive_date,
                        status="downloading",
                        objects_done=objects_done,
                        bytes_done=bytes_done,
                        detail=f"已完成 {objects_done}/{snapshot.object_count} 个对象",
                    )

        self._emit_progress({
            "phase": "verifying",
            "profile_id": profile.profile_id,
            "archive_date": archive_date,
            "object_count": snapshot.object_count,
            "objects_done": snapshot.object_count,
            "bytes_total": bytes_total,
            "bytes_done": bytes_total,
            "bytes_transferred": bytes_total,
            "current_object": "",
            "current_object_bytes": 0,
            "current_object_total": 0,
            "active_transfers": 0,
            "download_workers": download_workers,
            "bandwidth_limit": self.config.bandwidth_limit.strip(),
            "speed_bytes_per_second": 0,
            "eta_seconds": None,
        })

        self.database.upsert_day(
            profile.profile_id,
            archive_date,
            status="verifying",
            objects_done=snapshot.object_count,
            bytes_done=bytes_total,
            detail="正在执行恢复验证：SHA-256、Parquet/JSON、schema、行数与业务摘要",
        )

        def verification_progress(_key: str, current: int, total: int) -> None:
            self.database.upsert_day(
                profile.profile_id,
                archive_date,
                status="verifying",
                detail=f"完整校验 {current}/{total}",
            )
            self._emit_progress({
                "phase": "verifying",
                "profile_id": profile.profile_id,
                "archive_date": archive_date,
                "object_count": snapshot.object_count,
                "objects_done": current,
                "bytes_total": bytes_total,
                "bytes_done": bytes_total,
                "bytes_transferred": bytes_total,
                "current_object": _key,
                "current_object_bytes": 0,
                "current_object_total": 0,
                "active_transfers": 0,
                "download_workers": download_workers,
                "bandwidth_limit": self.config.bandwidth_limit.strip(),
                "speed_bytes_per_second": 0,
                "eta_seconds": None,
            })

        report = verify_local_day(stage, snapshot, verification_progress, self.cancel)
        latest_snapshot, latest_state, latest_detail = self._remote_snapshot(
            source, profile, archive_date
        )
        if latest_snapshot is None:
            raise RuntimeError(
                f"完整校验后远端状态不再可发布: {latest_state} · {latest_detail}"
            )
        if latest_snapshot.sha256 != snapshot.sha256:
            raise RuntimeError("下载期间远端 manifest 已变化，本次结果不予发布")
        report.update({
            "profile_id": profile.profile_id,
            "collector_id": profile.collector_id,
            "source_type": profile.source_type,
            "source_name": source.name,
            "downloaded_bytes_this_run": network_bytes,
        })
        write_json_atomic(stage / ".smsi-verified.json", report)
        raise_if_cancelled(self.cancel)
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(stage, final)
        partial_parent = stage.parent
        if partial_parent.exists() and not any(partial_parent.iterdir()):
            partial_parent.rmdir()
        self.database.upsert_day(
            profile.profile_id,
            archive_date,
            status="verified",
            objects_done=snapshot.object_count,
            bytes_done=bytes_total,
            detail="下载完成，恢复验证通过",
            error="",
        )
        self.database.event(
            "info", "归档下载与恢复验证完成", profile_id=profile.profile_id,
            archive_date=archive_date,
            detail=f"{snapshot.object_count} 个对象，{snapshot.row_count} 行",
        )
        self._emit_progress({
            "phase": "verified",
            "profile_id": profile.profile_id,
            "archive_date": archive_date,
            "object_count": snapshot.object_count,
            "objects_done": snapshot.object_count,
            "bytes_total": bytes_total,
            "bytes_done": bytes_total,
            "bytes_transferred": bytes_total,
            "current_object": "",
            "current_object_bytes": 0,
            "current_object_total": 0,
            "active_transfers": 0,
            "download_workers": download_workers,
            "bandwidth_limit": self.config.bandwidth_limit.strip(),
            "speed_bytes_per_second": 0,
            "eta_seconds": 0,
        })
        return report

    def inspect_day(
        self,
        profile: ProfileConfig,
        source: ArchiveSource,
        archive_date: str,
        *,
        download: bool,
    ) -> dict[str, Any] | None:
        raise_if_cancelled(self.cancel)
        snapshot, state, detail = self._remote_snapshot(source, profile, archive_date)
        if snapshot is None:
            self.database.upsert_day(
                profile.profile_id, archive_date, status=state, detail=detail, error="",
            )
            return None
        try:
            receipt = self._verified_receipt(profile, archive_date, snapshot)
        except RuntimeError as exc:
            self.database.upsert_day(
                profile.profile_id, archive_date, status="manifest_changed",
                manifest_sha256=snapshot.sha256, detail="需要人工复核", error=str(exc),
            )
            self.database.event(
                "error", "远端 manifest 发生变化", profile_id=profile.profile_id,
                archive_date=archive_date, detail=str(exc),
            )
            return None
        if receipt:
            self.database.upsert_day(
                profile.profile_id, archive_date, status="verified",
                manifest_sha256=snapshot.sha256, object_count=snapshot.object_count,
                objects_done=snapshot.object_count, row_count=snapshot.row_count,
                bytes_total=sum(int(item["size_bytes"]) for item in snapshot.objects),
                bytes_done=sum(int(item["size_bytes"]) for item in snapshot.objects),
                detail="本地已完整验证", error="",
            )
            return receipt
        self.database.upsert_day(
            profile.profile_id, archive_date, status="ready",
            manifest_sha256=snapshot.sha256, object_count=snapshot.object_count,
            row_count=snapshot.row_count,
            bytes_total=sum(int(item["size_bytes"]) for item in snapshot.objects),
            detail="可下载", error="",
        )
        return self.download_day(profile, source, snapshot) if download else None

    def day_detail(self, profile_id: str, archive_date: str) -> dict[str, Any]:
        """Return a click-triggered remote manifest/local inventory snapshot."""
        profile = next(
            (item for item in self.config.profiles if item.profile_id == profile_id),
            None,
        )
        if profile is None:
            raise RuntimeError("配置不存在")
        try:
            date.fromisoformat(archive_date)
        except ValueError as exc:
            raise RuntimeError("归档日期无效") from exc
        source = build_source(self.config, profile)
        snapshot, remote_state, remote_detail = self._remote_snapshot(
            source, profile, archive_date
        )
        final = self.final_root(profile, archive_date)
        stage = self.staging_root(profile, archive_date)
        row = self.database.day(profile_id, archive_date) or {}
        objects: list[dict[str, Any]] = []
        remote_bytes = 0
        if snapshot is not None:
            remote_bytes = sum(int(item["size_bytes"]) for item in snapshot.objects)
            for item in snapshot.objects:
                key = str(item["relative_key"])
                final_path = local_object_path(final, key, archive_date)
                stage_path = local_object_path(stage, key, archive_date)
                temporary_path = stage_path.with_name(stage_path.name + ".downloading")
                expected_size = int(item["size_bytes"])
                if final_path.is_file():
                    local_state = (
                        "present"
                        if final_path.stat().st_size == expected_size
                        else "mismatch"
                    )
                    local_bytes = final_path.stat().st_size
                elif temporary_path.is_file():
                    local_state = "downloading"
                    local_bytes = temporary_path.stat().st_size
                elif stage_path.is_file():
                    local_state = "staged"
                    local_bytes = stage_path.stat().st_size
                else:
                    local_state = "missing"
                    local_bytes = 0
                objects.append({
                    "relative_key": key,
                    "name": PurePosixPath(key).name,
                    "size_bytes": expected_size,
                    "remote_state": "available",
                    "local_state": local_state,
                    "local_bytes": local_bytes,
                })
        local_present = sum(
            item["local_state"] in {"present", "downloading", "staged"}
            for item in objects
        )
        local_bytes = sum(
            int(item["local_bytes"])
            for item in objects
            if item["local_state"] in {"present", "downloading", "staged"}
        )
        return {
            "profile_id": profile_id,
            "archive_date": archive_date,
            "source_type": profile.source_type,
            "remote": {
                "state": "ready" if snapshot is not None else remote_state,
                "detail": remote_detail,
                "object_count": snapshot.object_count if snapshot else 0,
                "bytes_total": remote_bytes,
                "manifest_sha256": snapshot.sha256 if snapshot else "",
            },
            "local": {
                "state": str(row.get("status") or "missing"),
                "detail": str(row.get("detail") or ""),
                "object_count": local_present,
                "bytes_done": local_bytes,
                "bytes_total": remote_bytes,
                "final_exists": final.is_dir(),
                "staging_exists": stage.is_dir(),
            },
            "objects": objects,
        }

    def browse_dates(self, profile_id: str, *, scope: str) -> dict[str, Any]:
        """List remote or local archive dates on explicit user request."""
        if scope not in {"remote", "local"}:
            raise RuntimeError("文件范围无效")
        profile = next(
            (item for item in self.config.profiles if item.profile_id == profile_id),
            None,
        )
        if profile is None:
            raise RuntimeError("配置不存在")
        root = self.profile_root(profile)
        local_dates = {
            item.name.removeprefix("date=")
            for item in root.glob("date=*")
            if item.is_dir() and DATE_RE.fullmatch(item.name.removeprefix("date="))
        }
        partial_root = root / ".partial"
        partial_dates = {
            item.name.removeprefix("date=")
            for item in partial_root.glob("date=*")
            if item.is_dir() and DATE_RE.fullmatch(item.name.removeprefix("date="))
        } if partial_root.is_dir() else set()
        remote_dates: set[str] = set()
        if scope == "remote":
            source = build_source(self.config, profile)
            remote_dates = {
                value for value in source.list_dates() if DATE_RE.fullmatch(value)
            }
        rows = {
            str(item["archive_date"]): item
            for item in self.database.days(5000)
            if item.get("profile_id") == profile_id
        }
        dates = []
        available_dates = (
            remote_dates if scope == "remote" else local_dates | partial_dates
        )
        for archive_date in sorted(available_dates, reverse=True):
            row = rows.get(archive_date) or {}
            dates.append({
                "archive_date": archive_date,
                "remote": archive_date in remote_dates,
                "local": archive_date in local_dates,
                "partial": archive_date in partial_dates,
                "status": str(row.get("status") or "unknown"),
                "object_count": int(row.get("object_count") or 0),
                "row_count": int(row.get("row_count") or 0),
                "bytes_total": int(row.get("bytes_total") or 0),
                "updated_at": str(row.get("updated_at") or ""),
            })
        return {
            "scope": scope,
            "profile_id": profile_id,
            "source_type": profile.source_type,
            "dates": dates,
        }

    def browse_files(
        self,
        profile_id: str,
        archive_date: str,
        *,
        scope: str,
        path: str = "",
    ) -> dict[str, Any]:
        """Browse one directory level without recursively enumerating children."""
        if scope not in {"remote", "local"}:
            raise RuntimeError("文件范围无效")
        profile = next(
            (item for item in self.config.profiles if item.profile_id == profile_id),
            None,
        )
        if profile is None:
            raise RuntimeError("配置不存在")
        try:
            date.fromisoformat(archive_date)
        except ValueError as exc:
            raise RuntimeError("归档日期无效") from exc
        directory = str(path or "").strip("/")
        if directory:
            directory_path = PurePosixPath(directory)
            if (
                directory_path.is_absolute()
                or ".." in directory_path.parts
                or "." in directory_path.parts
                or "\\" in directory
            ):
                raise RuntimeError("浏览目录路径无效")
            directory = directory_path.as_posix()
        final = self.final_root(profile, archive_date)
        stage = self.staging_root(profile, archive_date)
        if scope == "remote":
            source = build_source(self.config, profile)
            snapshot, remote_state, remote_detail = self._remote_snapshot(
                source, profile, archive_date
            )
            entries: dict[str, dict[str, Any]] = {}
            browse_index: list[dict[str, Any]] = []
            if snapshot is not None:
                prefix = f"date={archive_date}/"
                for item in snapshot.objects:
                    key = str(item["relative_key"])
                    relative = key.removeprefix(prefix)
                    local_path = local_object_path(final, key, archive_date)
                    staged_path = local_object_path(stage, key, archive_date)
                    expected_size = int(item["size_bytes"])
                    if local_path.is_file():
                        local_state = "present" if local_path.stat().st_size == expected_size else "mismatch"
                    elif staged_path.is_file():
                        local_state = "staged" if staged_path.stat().st_size == expected_size else "mismatch"
                    elif staged_path.with_name(staged_path.name + ".downloading").is_file():
                        local_state = "downloading"
                    else:
                        local_state = "missing"
                    browse_index.append({
                        "type": "file", "path": relative, "name": PurePosixPath(relative).name,
                        "size_bytes": expected_size, "row_count": int(item.get("row_count") or 0),
                        "kind": str(item.get("kind") or ""), "table_name": str(item.get("table_name") or ""),
                        "sha256": str(item.get("sha256") or ""), "local_state": local_state,
                    })
                    if directory:
                        if not relative.startswith(directory + "/"):
                            continue
                        remainder = relative[len(directory) + 1 :]
                    else:
                        remainder = relative
                    if not remainder:
                        continue
                    first, separator, _rest = remainder.partition("/")
                    child_path = f"{directory}/{first}" if directory else first
                    if separator:
                        entry = entries.setdefault(
                            child_path,
                            {"type": "directory", "path": child_path, "name": first, "entry_count": 0},
                        )
                        entry["entry_count"] += 1
                        continue
                    entries[child_path] = {
                        "type": "file", "path": child_path, "name": first,
                        "size_bytes": expected_size,
                        "row_count": int(item.get("row_count") or 0),
                        "kind": str(item.get("kind") or ""),
                        "table_name": str(item.get("table_name") or ""),
                        "sha256": str(item.get("sha256") or ""),
                        "local_state": local_state,
                    }
            ordered = sorted(
                entries.values(),
                key=lambda item: (item["type"] != "directory", str(item["name"]).casefold()),
            )
            files = [item for item in ordered if item["type"] == "file"]
            return {
                "scope": scope,
                "profile_id": profile_id,
                "archive_date": archive_date,
                "path": directory,
                "parent_path": "/".join(PurePosixPath(directory).parts[:-1]) if directory else "",
                "state": "ready" if snapshot is not None else remote_state,
                "detail": remote_detail,
                "entry_count": len(ordered),
                "object_count": len(files),
                "bytes_total": sum(int(item["size_bytes"]) for item in files),
                "row_count": sum(int(item.get("row_count") or 0) for item in files),
                "total_object_count": snapshot.object_count if snapshot else 0,
                "total_bytes": sum(int(item["size_bytes"]) for item in snapshot.objects) if snapshot else 0,
                "total_row_count": snapshot.row_count if snapshot else 0,
                "browse_index": browse_index,
                "entries": ordered,
                "files": files,
            }

        expected: dict[str, dict[str, Any]] = {}
        local_snapshot: ManifestSnapshot | None = None
        manifest_error = ""
        for manifest_path in (final / "manifest.json", stage / "manifest.json"):
            if not manifest_path.is_file():
                continue
            try:
                if manifest_path.stat().st_size > 32 * 1024 * 1024:
                    raise RuntimeError("本地 manifest 超过安全大小")
                local_snapshot = parse_manifest(
                    manifest_path.read_bytes(), archive_date
                )
            except (OSError, RuntimeError) as exc:
                manifest_error = str(exc)
                continue
            break
        if local_snapshot is not None:
            prefix = f"date={archive_date}/"
            for item in local_snapshot.objects:
                key = str(item["relative_key"])
                expected[key.removeprefix(prefix)] = item
        control_files = {
            "manifest.json",
            ".smsi-verified.json",
            ".smsi-verification-failed.json",
        }
        entries: dict[str, dict[str, Any]] = {}
        for location, root in (("verified", final), ("partial", stage)):
            current_root = root / Path(*PurePosixPath(directory).parts) if directory else root
            if not current_root.is_dir():
                continue
            try:
                current_root.resolve().relative_to(root.resolve())
            except ValueError as exc:
                raise RuntimeError("浏览目录路径越界") from exc
            for path in sorted(current_root.iterdir(), key=lambda item: item.name.casefold()):
                if not path.is_file() or path.is_symlink():
                    continue
                if len(entries) >= 5000:
                    raise RuntimeError("本地文件超过 5000 个，请缩小浏览范围")
                relative = path.relative_to(root).as_posix()
                file_state = "downloading" if path.name.endswith(".downloading") else location
                manifest_relative = (
                    relative.removesuffix(".downloading")
                    if file_state == "downloading"
                    else relative
                )
                item = expected.get(manifest_relative) or {}
                entry = entries.get(relative)
                if entry is not None:
                    entry["locations"].append(location)
                    if location == "verified":
                        entry.update({"location": location, "state": file_state})
                    continue
                entries[relative] = {
                    "type": "file", "path": relative, "name": path.name,
                    "size_bytes": path.stat().st_size,
                    "modified_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
                    "location": location, "locations": [location], "state": file_state,
                    "remote_state": (
                        "listed" if item
                        else "control" if manifest_relative in control_files
                        else "local_only"
                    ),
                    "expected_size": int(item.get("size_bytes") or 0),
                }
            for path in sorted(current_root.iterdir(), key=lambda item: item.name.casefold()):
                if not path.is_dir() or path.is_symlink():
                    continue
                relative = path.relative_to(root).as_posix()
                entry = entries.get(relative)
                if entry is None:
                    entries[relative] = {
                        "type": "directory", "path": relative, "name": path.name,
                        "location": location, "locations": [location], "entry_count": 0,
                    }
                elif location not in entry["locations"]:
                    entry["locations"].append(location)
        ordered = sorted(
            entries.values(),
            key=lambda item: (item["type"] != "directory", str(item["name"]).casefold()),
        )
        files = [item for item in ordered if item["type"] == "file"]
        return {
            "scope": scope,
            "profile_id": profile_id,
            "archive_date": archive_date,
            "path": directory,
            "parent_path": "/".join(PurePosixPath(directory).parts[:-1]) if directory else "",
            "state": str((self.database.day(profile_id, archive_date) or {}).get("status") or "unknown"),
            "detail": (
                f"本地 manifest 无法解析: {manifest_error}"
                if manifest_error and local_snapshot is None
                else "本地已验证目录与暂存目录"
            ),
            "entry_count": len(ordered),
            "object_count": len(files),
            "bytes_total": sum(int(item["size_bytes"]) for item in files),
            "row_count": local_snapshot.row_count if local_snapshot else 0,
            "entries": ordered,
            "files": files,
        }

    def scan_profile(self, profile: ProfileConfig, *, download: bool) -> dict[str, Any]:
        source = build_source(self.config, profile)
        dates = self._eligible_dates(source)
        completed = 0
        failed = 0
        for archive_date in dates:
            raise_if_cancelled(self.cancel)
            try:
                report = self.inspect_day(profile, source, archive_date, download=download)
                if report:
                    completed += 1
            except OperationCancelled:
                row = self.database.day(profile.profile_id, archive_date) or {}
                if row.get("status") in {"downloading", "verifying"}:
                    self.database.upsert_day(
                        profile.profile_id,
                        archive_date,
                        status="cancelled",
                        detail="任务已取消，已完成部分保留，下次可继续",
                        error="",
                    )
                    self._emit_progress({
                        "phase": "cancelled",
                        "profile_id": profile.profile_id,
                        "archive_date": archive_date,
                        "object_count": int(row.get("object_count") or 0),
                        "objects_done": int(row.get("objects_done") or 0),
                        "bytes_total": int(row.get("bytes_total") or 0),
                        "bytes_done": int(row.get("bytes_done") or 0),
                        "bytes_transferred": int(row.get("bytes_done") or 0),
                        "current_object": "",
                        "current_object_bytes": 0,
                        "current_object_total": 0,
                        "active_transfers": 0,
                        "download_workers": self.config.download_workers,
                        "bandwidth_limit": self.config.bandwidth_limit.strip(),
                        "speed_bytes_per_second": 0,
                        "eta_seconds": None,
                    })
                raise
            except Exception as exc:
                failed += 1
                self.database.upsert_day(
                    profile.profile_id, archive_date, status="error", error=str(exc),
                    detail="本次处理失败，已保留现有数据",
                )
                self.database.event(
                    "error", "归档处理失败", profile_id=profile.profile_id,
                    archive_date=archive_date, detail=str(exc),
                )
        return {"profile_id": profile.profile_id, "dates": len(dates), "completed": completed, "failed": failed}

    def scan_all(self, *, download: bool) -> list[dict[str, Any]]:
        results = []
        for profile in self.config.profiles:
            if profile.enabled:
                try:
                    results.append(self.scan_profile(profile, download=download))
                except OperationCancelled:
                    raise
                except Exception as exc:
                    results.append({
                        "profile_id": profile.profile_id,
                        "dates": 0,
                        "completed": 0,
                        "failed": 1,
                    })
                    self.database.event(
                        "error", "归档来源检查失败",
                        profile_id=profile.profile_id, detail=str(exc),
                    )
        self.refresh_comparisons()
        return results

    def refresh_comparisons(self) -> list[dict[str, Any]]:
        comparisons = build_archive_comparisons(self.config, self.database.days(5000))
        self.database.replace_comparisons(comparisons)
        return comparisons

    def verify_existing(self, profile_id: str, archive_date: str) -> dict[str, Any]:
        profile = next((item for item in self.config.profiles if item.profile_id == profile_id), None)
        if profile is None:
            raise RuntimeError("配置不存在")
        source = build_source(self.config, profile)
        snapshot, state, detail = self._remote_snapshot(source, profile, archive_date)
        if snapshot is None:
            raise RuntimeError(f"远端归档不可验证: {state} · {detail}")
        root = self.final_root(profile, archive_date)
        if not root.is_dir():
            raise RuntimeError("本地归档目录不存在")
        self.database.upsert_day(profile_id, archive_date, status="verifying", detail="正在重新完整校验")
        try:
            report = verify_local_day(root, snapshot, cancel=self.cancel)
        except OperationCancelled:
            raise
        except Exception as exc:
            failure = {
                "contract_version": "smsi-local-verification-failure/v1",
                "status": "failed",
                "archive_date": archive_date,
                "error": str(exc),
                "failed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            write_json_atomic(root / ".smsi-verification-failed.json", failure)
            self.database.upsert_day(
                profile_id, archive_date, status="error",
                detail="本地完整校验失败，原目录已保留", error=str(exc),
            )
            self.database.event(
                "error", "本地归档重新验证失败",
                profile_id=profile_id, archive_date=archive_date, detail=str(exc),
            )
            raise
        latest_snapshot, latest_state, latest_detail = self._remote_snapshot(
            source, profile, archive_date
        )
        if latest_snapshot is None or latest_snapshot.sha256 != snapshot.sha256:
            error = (
                f"重新校验期间远端归档发生变化: {latest_state} · {latest_detail}"
            )
            self.database.upsert_day(
                profile_id, archive_date, status="error",
                detail="重新校验未发布结果", error=error,
            )
            self.database.event(
                "error", "重新校验期间远端归档变化",
                profile_id=profile_id, archive_date=archive_date, detail=error,
            )
            raise RuntimeError(error)
        report.update({
            "profile_id": profile.profile_id,
            "collector_id": profile.collector_id,
            "source_type": profile.source_type,
            "source_name": source.name,
            "downloaded_bytes_this_run": 0,
        })
        write_json_atomic(root / ".smsi-verified.json", report)
        self.database.upsert_day(profile_id, archive_date, status="verified", detail="重新恢复验证通过", error="")
        self.database.event("info", "本地归档重新恢复验证完成", profile_id=profile_id, archive_date=archive_date)
        self.refresh_comparisons()
        return report
