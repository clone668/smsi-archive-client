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
        report_path = day_root / "runtime-report.json"
        report = {
            "contract_version": "smsi-runtime-health-report/v1",
            "archive_date": archive_date,
            "collector_node_id": "collector-a",
            "overall_status": "healthy",
            "summary": {"top_issues": []},
            "archive": {
                "status": "data_objects_verified",
                "all_data_objects_read_verified": True,
            },
            "collection_sources": {
                "quality_policy": {"sha256": "1" * 64},
                "sources": [
                    {
                        "source_id": "binance_market_events",
                        "quality": {"status": "healthy"},
                    }
                ],
            },
            "database": {"writes": {"record_count": len(rows)}},
            "warning_error_logs": {"record_count": 0},
        }
        report_raw = json.dumps(
            report, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        report_path.write_bytes(report_raw)
        report_key = f"date={archive_date}/runtime-report.json"
        report_item = {
            "kind": "runtime_report",
            "table_name": "runtime_health",
            "format": "json",
            "relative_key": report_key,
            "row_count": 1,
            "size_bytes": len(report_raw),
            "sha256": hashlib.sha256(report_raw).hexdigest(),
            "report_contract_version": "smsi-runtime-health-report/v1",
        }
        manifest = {
            "contract_version": "smsi-long-term-archive-manifest/v3",
            "status": "verified",
            "archive_date": archive_date,
            "object_count": 2,
            "row_count": len(rows) + 1,
            "retention_delete_allowed": True,
            "objects": [item, report_item],
            "replicas": {
                "google_drive": [
                    {"relative_key": relative_key, "read_verified": True},
                    {"relative_key": report_key, "read_verified": True},
                ]
            },
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
            "report_path": report_path,
            "manifest": manifest,
            "manifest_raw": manifest_raw,
            "relative_key": relative_key,
            "archive_date": archive_date,
        }
    return build
