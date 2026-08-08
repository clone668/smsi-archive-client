from __future__ import annotations

import json
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import ClientConfig, ProfileConfig
from .database import StateDatabase
from .protocol import DATE_RE, ManifestSnapshot, parse_manifest, parse_progress
from .sources import ArchiveSource, build_source
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
    ) -> None:
        self.config = config
        self.database = database
        self.cancel = cancel or threading.Event()
        self._progress_lock = threading.Lock()

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
    ) -> tuple[str, int, int]:
        raise_if_cancelled(self.cancel)
        key = str(item["relative_key"])
        expected_size = int(item["size_bytes"])
        expected_sha = str(item["sha256"])
        destination = local_object_path(stage, key, snapshot.archive_date)
        if destination.is_file() and destination.stat().st_size == expected_size:
            if sha256_file(destination, self.cancel) == expected_sha:
                return key, expected_size, 0
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".downloading")
        if temporary.exists():
            temporary.unlink()
        try:
            observed_size = source.download(key, temporary, self.cancel)
            raise_if_cancelled(self.cancel)
            if observed_size != expected_size or temporary.stat().st_size != expected_size:
                raise RuntimeError(f"下载对象大小不一致: {key}")
            if sha256_file(temporary, self.cancel) != expected_sha:
                raise RuntimeError(f"下载对象 SHA-256 不一致: {key}")
            os.replace(temporary, destination)
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
                detail="本地已完整验证",
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
        with ThreadPoolExecutor(max_workers=self.config.download_workers) as executor:
            futures = [
                executor.submit(self._download_one, source, stage, snapshot, item)
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

        self.database.upsert_day(
            profile.profile_id,
            archive_date,
            status="verifying",
            objects_done=snapshot.object_count,
            bytes_done=bytes_total,
            detail="正在校验 Parquet schema、行数与业务内容摘要",
        )

        def verification_progress(_key: str, current: int, total: int) -> None:
            self.database.upsert_day(
                profile.profile_id,
                archive_date,
                status="verifying",
                detail=f"完整校验 {current}/{total}",
            )

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
            detail="下载与完整校验完成",
            error="",
        )
        self.database.event(
            "info", "归档下载验证完成", profile_id=profile.profile_id,
            archive_date=archive_date,
            detail=f"{snapshot.object_count} 个对象，{snapshot.row_count} 行",
        )
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
        return results

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
        self.database.upsert_day(profile_id, archive_date, status="verified", detail="重新完整校验通过", error="")
        self.database.event("info", "本地归档重新验证完成", profile_id=profile_id, archive_date=archive_date)
        return report
