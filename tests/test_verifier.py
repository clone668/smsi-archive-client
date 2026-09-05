from __future__ import annotations

import hashlib
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from archive_backup.verifier import reader_version, verify_parquet


def _object(tmp_path: Path) -> tuple[Path, dict]:
    """One Parquet object, plus a manifest entry that matches it exactly."""
    path = tmp_path / "part-00000.parquet"
    table = pa.table({"symbol": ["BTCUSDT"], "price": [100.5]})
    pq.write_table(table, path, compression="zstd")
    parquet = pq.ParquetFile(path)
    schema_sha256 = hashlib.sha256(str(parquet.schema_arrow).encode("utf-8")).hexdigest()
    parquet.close()
    item = {
        "kind": "control",
        "relative_key": "date=2026-08-09/control/part-00000.parquet",
        "row_count": 1,
        "size_bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "schema_sha256": schema_sha256,
    }
    return path, item


def test_an_object_matching_its_manifest_entry_passes(tmp_path: Path) -> None:
    path, item = _object(tmp_path)

    report = verify_parquet(path, item)

    assert report["row_count"] == 1
    assert report["schema_sha256"] == item["schema_sha256"]


def test_a_fingerprint_we_cannot_reproduce_is_not_reported_as_damage(
    tmp_path: Path,
) -> None:
    """By this point the file's bytes already matched the manifest.

    The schema fingerprint is derived from those same bytes, so a number we
    cannot reproduce says something about this machine's reader, not about the
    archive.  Calling it damage would send the operator hunting for corruption
    in the one place that has none - and this copy is what the retention
    decision upstream is allowed to rely on.
    """
    path, item = _object(tmp_path)
    item["schema_sha256"] = "b" * 64

    with pytest.raises(RuntimeError) as failure:
        verify_parquet(path, item)

    message = str(failure.value)
    assert "无法复现" in message
    assert "副本没有损坏" in message
    assert reader_version() in message


def test_a_business_content_digest_we_cannot_reproduce_names_the_reader(
    tmp_path: Path,
) -> None:
    """The deepest check is decoded row by row, so it is reader-derived too."""
    path, item = _object(tmp_path)
    item.update({"kind": "business", "content_sha256": "c" * 64})

    with pytest.raises(RuntimeError) as failure:
        verify_parquet(path, item)

    message = str(failure.value)
    assert "无法复现" in message
    assert "副本没有损坏" in message
    assert reader_version() in message


def test_a_row_count_mismatch_is_still_a_plain_inconsistency(tmp_path: Path) -> None:
    """Row count comes from Parquet's own metadata, not from a text rendering.

    Nothing about a reader version explains it away, so it keeps the blunt
    wording - the distinction is the point.
    """
    path, item = _object(tmp_path)
    item["row_count"] = 2

    with pytest.raises(RuntimeError, match="行数不一致"):
        verify_parquet(path, item)
