from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from archive_backup.dependencies import DependencyReceiver, receipt_key, summary_label
from archive_backup.dependency_contract import (
    CONTRACT, CATALOG_CONTRACT, DependencyError, canonical_bytes, digest, manifest_key, object_key,
)

NODE, DAY = "collector-a", "2026-10-12"


class Source:
    def __init__(self, root):
        self.root, self.downloads, self.reads = root, [], []
        self.fail = ""

    def list_deliveries(self):
        return [p.relative_to(self.root).as_posix() for p in (self.root / "dependencies-v1/deliveries").glob("*.json")]

    def read_small(self, key, maximum):
        self.reads.append(key)
        path = self.root / key
        return path.read_bytes() if path.exists() else None

    def download(self, key, destination, size, progress):
        self.downloads.append(key)
        if key == self.fail:
            raise RuntimeError("transfer interrupted")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.root / key, destination)
        progress(size)


def native(root, day=DAY, raw=None):
    raw = raw or canonical_bytes({"archive_date": day, "objects": []})
    directory = root / ("date=" + day)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_bytes(raw)
    sha = hashlib.sha256(raw).hexdigest()
    (directory / ".smsi-verified.json").write_bytes(canonical_bytes({
        "contract_version": "smsi-local-archive-verification/v1", "status": "verified",
        "archive_date": day, "manifest_sha256": sha,
    }))
    return sha


def publish(root, base, *, day=DAY, node=NODE, generation=1, rows=(1, 2), partial=True, bad_readback=False):
    def put(key, data):
        path = root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    objects = []
    for row in rows:
        table = pa.table({"record_id": [f"id-{row}"], "payload": [str(row)]})
        sink = pa.BufferOutputStream()
        pq.write_table(table, sink)
        data = sink.getvalue().to_pybytes()
        sha = hashlib.sha256(data).hexdigest()
        key = object_key(sha)
        put(key, data)
        objects.append({"relative_key": key, "sha256": sha, "size_bytes": len(data),
            "row_count": 1, "schema_sha256": hashlib.sha256(table.schema.serialize().to_pybytes()).hexdigest(),
            "kind": "evidence", "table_name": "evidence", "native_archive_date": "2026-10-13"})
    manifest = {"contract_version": CONTRACT, "collector_node_id": node, "business_date": day,
        "base_manifest_sha256": base, "resolver_contract": "smsi-business-day-dependency-resolver/v1",
        "resolver_code_sha256": "1" * 64, "base_business_objects_sha256": "2" * 64,
        "root_scope": "business_day", "root_count": 2, "root_set_sha256": "3" * 64,
        "dependency_status": "partial" if partial else "complete",
        "missing": [{"reason": "original_trade_inputs_not_persisted"}] if partial else [],
        "objects": objects, "objects_sha256": digest(objects), "object_count": len(objects)}
    raw = canonical_bytes(manifest)
    sha = hashlib.sha256(raw).hexdigest()
    put(manifest_key(sha), raw)
    proofs = [{"name": "google_drive", "failure_domain": "gdrive", "read_verified": not bad_readback,
               "sha256": obj["sha256"], "size_bytes": obj["size_bytes"]} for obj in objects]
    proofs.append({"name": "google_drive", "failure_domain": "gdrive", "read_verified": True,
                   "sha256": sha, "size_bytes": len(raw)})
    readback = {"contract_version": "smsi-business-day-replica-readbacks/v1", "collector_node_id": node,
        "manifest_sha256": sha, "required_replicas": ["google_drive"], "replicas": {"google_drive": proofs}}
    receipt_sha = digest(readback)
    readback_key = f"dependencies-v1/replica-readbacks/{receipt_sha}.json"
    put(readback_key, canonical_bytes(readback))
    entry = {"business_date": day, "base_manifest_sha256": base, "manifest_key": manifest_key(sha),
             "manifest_sha256": sha, "dependency_status": manifest["dependency_status"]}
    page = {"contract_version": CATALOG_CONTRACT, "entries": [entry]}
    page_sha = digest(page)
    page_key = f"dependencies-v1/catalog/{page_sha}.json"
    put(page_key, canonical_bytes(page))
    catalog = {"contract_version": CATALOG_CONTRACT, "collector_node_id": node, "generation": generation,
        "pages": [{"relative_key": page_key, "sha256": page_sha}], "required_replicas": ["google_drive"],
        "readback_receipt_key": readback_key, "readback_receipt_sha256": receipt_sha}
    catalog_key = f"dependencies-v1/deliveries/{generation:020d}-{digest(catalog)}.json"
    put(catalog_key, canonical_bytes(catalog))
    return {**entry, "generation": generation, "catalog_key": catalog_key}, objects


def test_two_hops_late_generation_reuses_objects_and_preserves_missing(tmp_path):
    cloud, ubuntu, windows = [tmp_path / name for name in ("cloud", "ubuntu", "windows")]
    base = native(ubuntu)
    native(windows)
    source = Source(cloud)
    first = DependencyReceiver(ubuntu, NODE, source, workers=2)
    assert first.discover() == {}
    entry, objects = publish(cloud, base)
    assert first.discover()[(DAY, base)] == entry
    received = first.sync(entry)
    assert received["state"] == "verified" and received["dependency_status"] == "partial"
    assert "缺项" in summary_label(received)
    second_source = Source(ubuntu)
    second = DependencyReceiver(windows, NODE, second_source, require_upstream_receipt=True)
    second.sync(second.discover()[(DAY, base)])
    assert len(second_source.downloads) == 2
    source.downloads.clear()
    second_source.downloads.clear()
    newer, _ = publish(cloud, base, generation=2, rows=(1, 2, 3), partial=False)
    first.sync(first.discover()[(DAY, base)])
    second.sync(second.discover()[(DAY, base)])
    assert len(source.downloads) == len(second_source.downloads) == 1
    assert second.inspect(newer)["state"] == "verified"
    assert second.inspect(newer)["dependency_status"] == "complete"
    source.downloads.clear()
    first.sync(newer)
    assert source.downloads == []
    assert hashlib.sha256((windows / f"date={DAY}/manifest.json").read_bytes()).hexdigest() == base


def test_interrupted_transfer_never_publishes_and_resumes_completed_objects(tmp_path):
    target, cloud = tmp_path / "target", tmp_path / "cloud"
    base = native(target)
    entry, objects = publish(cloud, base)
    source = Source(cloud)
    source.fail = objects[1]["relative_key"]
    receiver = DependencyReceiver(target, NODE, source)
    with pytest.raises(RuntimeError, match="interrupted"):
        receiver.sync(entry)
    assert not (target / entry["catalog_key"]).exists()
    source.fail, source.downloads = "", []
    receiver.sync(entry)
    assert source.downloads == [objects[1]["relative_key"]]


@pytest.mark.parametrize("failure", ["object", "base", "readback", "node", "upstream"])
def test_invalid_material_is_never_activated(tmp_path, failure):
    target, cloud = tmp_path / "target", tmp_path / "cloud"
    base = native(target)
    entry, objects = publish(cloud, "f" * 64 if failure == "base" else base,
        node="wrong-node" if failure == "node" else NODE, bad_readback=failure == "readback")
    if failure == "object":
        (cloud / objects[0]["relative_key"]).write_bytes(b"corrupt")
    receiver = DependencyReceiver(target, NODE, Source(cloud), require_upstream_receipt=failure == "upstream")
    with pytest.raises(DependencyError):
        receiver.sync(entry)
    assert not (target / entry["catalog_key"]).exists()


def test_conflicting_generation_is_rejected_even_after_restart(tmp_path):
    target, cloud = tmp_path / "target", tmp_path / "cloud"
    base = native(target)
    entry, _ = publish(cloud, base)
    DependencyReceiver(target, NODE, Source(cloud)).discover()
    (cloud / entry["catalog_key"]).unlink()
    publish(cloud, base, rows=(9,))
    with pytest.raises(DependencyError, match="immutable_conflict"):
        DependencyReceiver(target, NODE, Source(cloud)).discover()


def test_corrupt_resume_file_is_removed_and_retry_succeeds(tmp_path):
    target, cloud = tmp_path / "target", tmp_path / "cloud"
    base = native(target)
    entry, objects = publish(cloud, base, rows=(1,))
    source = Source(cloud)
    real_download = source.download
    def corrupt(key, destination, size, progress):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"X" * size)
    source.download = corrupt
    receiver = DependencyReceiver(target, NODE, source)
    with pytest.raises(DependencyError, match="download_verification_failed"):
        receiver.sync(entry)
    assert not (target / (objects[0]["relative_key"] + ".downloading")).exists()
    source.download = real_download
    receiver.sync(entry)
    assert receiver.inspect(entry)["state"] == "verified"


def test_missing_local_object_is_repaired_without_redownloading_native(tmp_path):
    target, cloud = tmp_path / "target", tmp_path / "cloud"
    base = native(target)
    entry, objects = publish(cloud, base)
    source = Source(cloud)
    receiver = DependencyReceiver(target, NODE, source)
    receiver.sync(entry)
    (target / objects[0]["relative_key"]).unlink()
    source.downloads.clear()
    assert receiver.inspect(entry)["state"] == "ready"
    receiver.sync(entry)
    assert source.downloads == [objects[0]["relative_key"]]


def test_manual_verification_rehashes_files_without_using_network(tmp_path):
    target, cloud = tmp_path / "target", tmp_path / "cloud"
    base = native(target)
    entry, objects = publish(cloud, base)
    DependencyReceiver(target, NODE, Source(cloud)).sync(entry)
    local = DependencyReceiver(target, NODE, None)
    assert local.verify(entry)["state"] == "verified"
    path = target / objects[0]["relative_key"]
    path.write_bytes(b"X" * path.stat().st_size)
    with pytest.raises(DependencyError, match="object_hash"):
        local.verify(entry)


def test_cancel_before_publication_keeps_completed_files_for_resume(tmp_path):
    target, cloud = tmp_path / "target", tmp_path / "cloud"
    base = native(target)
    entry, objects = publish(cloud, base, rows=(1,))
    source = Source(cloud)
    cancelled = False
    def checkpoint():
        if cancelled:
            raise RuntimeError("cancelled")
    receiver = DependencyReceiver(target, NODE, source, checkpoint=checkpoint)
    original = receiver._verify_object
    def verify_then_cancel(item, **kwargs):
        nonlocal cancelled
        original(item, **kwargs)
        cancelled = True
    receiver._verify_object = verify_then_cancel
    with pytest.raises(RuntimeError, match="cancelled"):
        receiver.sync(entry)
    assert not (target / entry["catalog_key"]).exists()
    assert (target / objects[0]["relative_key"]).exists()
    cancelled = False
    source.downloads.clear()
    DependencyReceiver(target, NODE, source).sync(entry)
    assert source.downloads == []


def test_low_disk_space_stops_before_transfer(tmp_path):
    target, cloud = tmp_path / "target", tmp_path / "cloud"
    base = native(target)
    entry, _ = publish(cloud, base)
    source = Source(cloud)
    receiver = DependencyReceiver(target, NODE, source, minimum_free_bytes=10**30)
    with pytest.raises(DependencyError, match="空间不足"):
        receiver.sync(entry)
    assert source.downloads == []


def test_missing_published_metadata_is_restored_without_object_transfer(tmp_path):
    target, cloud = tmp_path / "target", tmp_path / "cloud"
    entry, _ = publish(cloud, native(target))
    source = Source(cloud)
    receiver = DependencyReceiver(target, NODE, source)
    receiver.sync(entry)
    (target / entry["manifest_key"]).unlink()
    assert receiver.inspect(entry)["state"] == "ready"
    source.downloads.clear()
    assert receiver.sync(entry)["state"] == "verified"
    assert source.downloads == []
    assert (target / entry["manifest_key"]).is_file()


def test_corrupt_verification_cache_rehashes_existing_object(tmp_path):
    target, cloud = tmp_path / "target", tmp_path / "cloud"
    entry, objects = publish(cloud, native(target), rows=(1,))
    source = Source(cloud)
    receiver = DependencyReceiver(target, NODE, source)
    receiver.sync(entry)
    receiver._object_receipt(objects[0]).write_bytes(b"broken")
    source.downloads.clear()
    assert receiver.sync(entry)["state"] == "verified"
    assert source.downloads == []


def test_actual_collector_v6139_delivery_crosses_both_hops(tmp_path):
    # Golden output generated by smsiv5's real BundleWriter + publish_bundle.
    # Do not regenerate using the client-side fixture builder above: that would
    # hide differences between producer and receiver schema/hash algorithms.
    import base64
    fixture = json.loads((Path(__file__).parent / "fixtures/dependency_delivery_v1.json").read_text(encoding="utf-8"))
    cloud, ubuntu, windows = [tmp_path / name for name in ("cloud", "ubuntu", "windows")]
    for key, encoded in fixture["files"].items():
        path = cloud / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(encoded))
    raw = base64.b64decode(fixture["native_manifest_base64"])
    base = native(ubuntu, raw=raw)
    native(windows, raw=raw)
    first = DependencyReceiver(ubuntu, NODE, Source(cloud), workers=2)
    entry = first.discover()[(DAY, base)]
    assert first.sync(entry)["state"] == "verified"
    second = DependencyReceiver(windows, NODE, Source(ubuntu), require_upstream_receipt=True)
    assert second.sync(second.discover()[(DAY, base)])["state"] == "verified"
    assert second.verify(entry)["dependency_status"] == "partial"
