from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from archive_backup.verifier import update_content_digest


@pytest.fixture
def archive_fixture(tmp_path: Path):
    def build(archive_date: str = "2026-08-07") -> dict[str, Any]:
        source_root = tmp_path / "source" / "collector=collector-a"
        day_root = source_root / f"date={archive_date}"
        object_path = day_root / "business" / "table=price_data" / "day" / "part-00000.parquet"
        object_path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {"timestamp": 1, "symbol": "BTCUSDT", "price": 100.5},
            {"timestamp": 2, "symbol": "ETHUSDT", "price": 50.25},
        ]
        table = pa.Table.from_pylist(rows)
        pq.write_table(table, object_path, compression="zstd")
        parquet = pq.ParquetFile(object_path)
        schema_sha = hashlib.sha256(str(parquet.schema_arrow).encode("utf-8")).hexdigest()
        parquet.close()
        content = hashlib.sha256()
        for row in rows:
            update_content_digest(content, row)
        relative_key = f"date={archive_date}/business/table=price_data/day/part-00000.parquet"
        item = {
            "kind": "business",
            "table_name": "price_data",
            "relative_key": relative_key,
            "row_count": len(rows),
            "size_bytes": object_path.stat().st_size,
            "sha256": hashlib.sha256(object_path.read_bytes()).hexdigest(),
            "schema_sha256": schema_sha,
            "content_sha256": content.hexdigest(),
        }
        manifest = {
            "contract_version": "smsi-long-term-archive-manifest/v3",
            "status": "verified",
            "archive_date": archive_date,
            "object_count": 1,
            "row_count": len(rows),
            "retention_delete_allowed": True,
            "objects": [item],
            "replicas": {"google_drive": [{"relative_key": relative_key, "read_verified": True}]},
        }
        manifest_raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        (day_root / "manifest.json").write_bytes(manifest_raw)
        progress = {
            "contract_version": "smsi-archive-progress/v1",
            "archive_date": archive_date,
            "status": "verified",
            "stage": "verified",
        }
        (day_root / "_smsi-archive-progress.json").write_text(json.dumps(progress), encoding="utf-8")
        (day_root / ".smsi-verified.json").write_text(json.dumps({
            "contract_version": "smsi-local-archive-verification/v1",
            "status": "verified",
            "archive_date": archive_date,
            "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        }), encoding="utf-8")
        return {
            "source_root": source_root,
            "day_root": day_root,
            "object_path": object_path,
            "manifest": manifest,
            "manifest_raw": manifest_raw,
            "relative_key": relative_key,
            "archive_date": archive_date,
        }
    return build
