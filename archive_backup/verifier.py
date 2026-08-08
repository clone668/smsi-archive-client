from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path, PurePosixPath
from threading import Event
from typing import Any, Callable, Mapping

from .protocol import ManifestSnapshot, validate_relative_key


VerifyProgress = Callable[[str, int, int], None]


class OperationCancelled(RuntimeError):
    pass


def raise_if_cancelled(cancel: Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise OperationCancelled("操作已取消，未完成结果不会发布")


def sha256_file(path: Path, cancel: Event | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            raise_if_cancelled(cancel)
            digest.update(chunk)
    raise_if_cancelled(cancel)
    return digest.hexdigest()


def canonical_value(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {str(key): canonical_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [canonical_value(item) for item in value]
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        raise ValueError("归档包含非有限浮点数")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def update_content_digest(digest: Any, row: Mapping[str, Any]) -> None:
    payload = json.dumps(
        canonical_value(dict(row)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)


def verify_parquet(path: Path, item: Mapping[str, Any], cancel: Event | None = None) -> dict[str, Any]:
    raise_if_cancelled(cancel)
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("缺少 pyarrow，无法执行完整 Parquet 校验") from exc
    parquet = pq.ParquetFile(path)
    try:
        row_count = int(parquet.metadata.num_rows)
        expected_rows = int(item.get("row_count") or 0)
        if row_count != expected_rows:
            raise RuntimeError(f"Parquet 行数不一致: {path.name}")
        schema_sha256 = hashlib.sha256(str(parquet.schema_arrow).encode("utf-8")).hexdigest()
        expected_schema = str(item.get("schema_sha256") or "")
        if expected_schema and schema_sha256 != expected_schema:
            raise RuntimeError(f"Parquet schema 不一致: {path.name}")
        content_sha256: str | None = None
        if item.get("kind") == "business":
            expected_content = str(item.get("content_sha256") or "")
            if len(expected_content) != 64:
                raise RuntimeError(f"业务内容摘要缺失: {path.name}")
            digest = hashlib.sha256()
            for batch in parquet.iter_batches(batch_size=10_000):
                raise_if_cancelled(cancel)
                for index, row in enumerate(batch.to_pylist()):
                    if index % 256 == 0:
                        raise_if_cancelled(cancel)
                    update_content_digest(digest, row)
            content_sha256 = digest.hexdigest()
            if content_sha256 != expected_content:
                raise RuntimeError(f"业务内容摘要不一致: {path.name}")
    finally:
        parquet.close()
    return {"row_count": row_count, "schema_sha256": schema_sha256, "content_sha256": content_sha256}


def local_object_path(day_root: Path, relative_key: str, archive_date: str) -> Path:
    safe = validate_relative_key(relative_key, archive_date)
    path = PurePosixPath(safe)
    candidate = (day_root / Path(*path.parts[1:])).resolve()
    root = day_root.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("本地归档对象路径越界") from exc
    return candidate


def verify_local_day(
    day_root: Path,
    snapshot: ManifestSnapshot,
    progress: VerifyProgress | None = None,
    cancel: Event | None = None,
) -> dict[str, Any]:
    verified: list[dict[str, Any]] = []
    total_rows = 0
    tree_material: list[dict[str, Any]] = []
    for index, item in enumerate(snapshot.objects, start=1):
        raise_if_cancelled(cancel)
        key = validate_relative_key(item.get("relative_key"), snapshot.archive_date)
        path = local_object_path(day_root, key, snapshot.archive_date)
        expected_size = int(item["size_bytes"])
        expected_sha = str(item["sha256"])
        if not path.is_file():
            raise RuntimeError(f"本地归档对象缺失: {key}")
        if path.stat().st_size != expected_size:
            raise RuntimeError(f"本地归档对象大小不一致: {key}")
        if sha256_file(path, cancel) != expected_sha:
            raise RuntimeError(f"本地归档对象 SHA-256 不一致: {key}")
        parquet = verify_parquet(path, item, cancel)
        total_rows += int(parquet["row_count"])
        entry = {
            "relative_key": key,
            "kind": item.get("kind"),
            "table_name": item.get("table_name"),
            "size_bytes": expected_size,
            "sha256": expected_sha,
            **parquet,
        }
        verified.append(entry)
        tree_material.append({
            "relative_key": key,
            "size_bytes": expected_size,
            "sha256": expected_sha,
            "row_count": parquet["row_count"],
        })
        if progress:
            progress(key, index, snapshot.object_count)
    if len(verified) != snapshot.object_count or total_rows != snapshot.row_count:
        raise RuntimeError("本地归档对象数或总行数不一致")
    tree_sha256 = hashlib.sha256(json.dumps(
        canonical_value(tree_material), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    return {
        "contract_version": "smsi-local-archive-verification/v1",
        "status": "verified",
        "archive_date": snapshot.archive_date,
        "manifest_sha256": snapshot.sha256,
        "object_count": len(verified),
        "row_count": total_rows,
        "tree_sha256": tree_sha256,
        "verified_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "retention_delete_allowed": False,
        "objects": verified,
    }
