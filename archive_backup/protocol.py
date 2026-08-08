from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping


DATE_RE = re.compile(r"^20\d{2}-\d{2}-\d{2}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
MANIFEST_CONTRACT = "smsi-long-term-archive-manifest/v3"
PROGRESS_CONTRACT = "smsi-archive-progress/v1"
PROGRESS_STAGES = {
    "preparing",
    "parquet_generation",
    "remote_upload",
    "manifest_publication",
    "verified",
    "failed",
}


@dataclass(frozen=True)
class ProgressSnapshot:
    archive_date: str
    status: str
    stage: str
    error: str = ""


@dataclass(frozen=True)
class ManifestSnapshot:
    archive_date: str
    manifest: dict[str, Any]
    raw: bytes
    sha256: str

    @property
    def objects(self) -> list[dict[str, Any]]:
        return list(self.manifest["objects"])

    @property
    def object_count(self) -> int:
        return int(self.manifest["object_count"])

    @property
    def row_count(self) -> int:
        return int(self.manifest["row_count"])


def validate_relative_key(value: Any, archive_date: str) -> str:
    text = str(value or "").strip("/")
    path = PurePosixPath(text)
    expected = f"date={archive_date}"
    if (
        not text
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in text
        or not path.parts
        or path.parts[0] != expected
        or len(path.parts) < 2
    ):
        raise RuntimeError(f"归档对象路径无效: {text or '--'}")
    return text


def parse_progress(raw: bytes, archive_date: str) -> ProgressSnapshot:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("归档进度文件格式无效") from exc
    if not isinstance(payload, Mapping):
        raise RuntimeError("归档进度文件格式无效")
    status = str(payload.get("status") or "")
    stage = str(payload.get("stage") or "")
    if payload.get("contract_version") != PROGRESS_CONTRACT:
        raise RuntimeError("归档进度协议不受支持")
    if str(payload.get("archive_date") or "") != archive_date:
        raise RuntimeError("归档进度日期不匹配")
    if status not in {"running", "verified", "failed"}:
        raise RuntimeError("归档进度状态无效")
    if stage not in PROGRESS_STAGES:
        raise RuntimeError("归档进度阶段无效")
    if (
        (status == "running" and stage in {"verified", "failed"})
        or (status == "verified" and stage != "verified")
        or (status == "failed" and stage != "failed")
    ):
        raise RuntimeError("归档进度终态不一致")
    return ProgressSnapshot(
        archive_date=archive_date,
        status=status,
        stage=stage,
        error=str(payload.get("error") or "")[:2000],
    )


def parse_manifest(raw: bytes, archive_date: str) -> ManifestSnapshot:
    if not DATE_RE.fullmatch(archive_date):
        raise RuntimeError("归档日期无效")
    if len(raw) > 32 * 1024 * 1024:
        raise RuntimeError("manifest 超过安全大小")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("manifest 格式无效") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("manifest 格式无效")
    if payload.get("contract_version") != MANIFEST_CONTRACT:
        raise RuntimeError("manifest 协议不受支持")
    if payload.get("status") != "verified":
        raise RuntimeError("manifest 尚未验证完成")
    if str(payload.get("archive_date") or "") != archive_date:
        raise RuntimeError("manifest 日期不匹配")
    if payload.get("retention_delete_allowed") is not True:
        raise RuntimeError("manifest 未通过远端清理门禁")
    objects = payload.get("objects")
    if not isinstance(objects, list):
        raise RuntimeError("manifest 对象列表无效")
    if int(payload.get("object_count") or 0) != len(objects):
        raise RuntimeError("manifest 对象总数不一致")
    keys: set[str] = set()
    rows = 0
    for item in objects:
        if not isinstance(item, dict):
            raise RuntimeError("manifest 对象记录无效")
        key = validate_relative_key(item.get("relative_key"), archive_date)
        if key in keys:
            raise RuntimeError(f"manifest 对象路径重复: {key}")
        keys.add(key)
        if int(item.get("size_bytes") or -1) < 0:
            raise RuntimeError(f"manifest 对象大小无效: {key}")
        if not SHA_RE.fullmatch(str(item.get("sha256") or "")):
            raise RuntimeError(f"manifest 对象摘要无效: {key}")
        if not SHA_RE.fullmatch(str(item.get("schema_sha256") or "")):
            raise RuntimeError(f"manifest schema 摘要无效: {key}")
        if item.get("kind") == "business" and not SHA_RE.fullmatch(
            str(item.get("content_sha256") or "")
        ):
            raise RuntimeError(f"manifest 业务内容摘要无效: {key}")
        row_count = int(item.get("row_count") or 0)
        if row_count < 0:
            raise RuntimeError(f"manifest 对象行数无效: {key}")
        rows += row_count
    if int(payload.get("row_count") or 0) != rows:
        raise RuntimeError("manifest 总行数不一致")
    replicas = payload.get("replicas")
    if not isinstance(replicas, Mapping) or "google_drive" not in replicas:
        raise RuntimeError("manifest 缺少 Google Drive 验证证据")
    drive_results = replicas.get("google_drive")
    if not isinstance(drive_results, list) or len(drive_results) != len(objects):
        raise RuntimeError("Google Drive 验证对象数不一致")
    if any(not isinstance(item, Mapping) or item.get("read_verified") is not True for item in drive_results):
        raise RuntimeError("Google Drive 完整读回证据不完整")
    return ManifestSnapshot(
        archive_date=archive_date,
        manifest=payload,
        raw=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
    )
