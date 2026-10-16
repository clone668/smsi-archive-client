"""Receive immutable business-day supplements without changing native archives.

This module and dependency_contract.py are shared verbatim by both clients.
The public delivery file is installed last, after local verification succeeds.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from .dependency_contract import (
    DependencyError, MAX_JSON_BYTES, PREFIX, canonical_bytes, digest, local_path,
    manifest_key, parse_json, read_json, safe_key, validate_catalog,
    validate_catalog_page, validate_manifest, write_atomic,
)

DELIVERY = re.compile(r"([0-9]{20})-([0-9a-f]{64})\.json\Z")
LOCAL_CONTRACT = "smsi-local-dependency-verification/v1"
MAX_DELIVERIES = 10_000


def receipt_key(sha: str) -> str:
    manifest_key(sha)  # validate before constructing a local or remote path
    return f"{PREFIX}/verified/{sha}.json"


def summary_label(summary: dict[str, Any]) -> str:
    state = summary.get("state", "unchecked")
    if state == "verified":
        return "补充包已验证 · 材料仍有缺项" if summary.get("dependency_status") == "partial" else "补充包已验证 · 材料齐全"
    return {
        "unchecked": "补充包尚未检查", "unpublished": "补充包尚未发布",
        "ready": "补充包待下载", "waiting_native": "补充包等待原日包验证",
        "error": "补充包检查失败", "cancelled": "补充包已暂停，等待续传",
    }.get(state, "补充包处理中")


class DependencyReceiver:
    """source supplies list_deliveries(), read_small(key, limit), download(key, path, size, progress)."""

    def __init__(self, root: Path, node: str, source: Any, *,
                 checkpoint: Callable[[], None] = lambda: None,
                 progress: Callable[[int, int, str], None] = lambda *_: None,
                 minimum_free_bytes: int = 0, workers: int = 1,
                 require_upstream_receipt: bool = False):
        self.root, self.node, self.source = root, node, source
        self.checkpoint, self.progress = checkpoint, progress
        self.minimum_free_bytes, self.workers = minimum_free_bytes, max(1, min(workers, 16))
        self.require_upstream_receipt = require_upstream_receipt

    def _read(self, key: str, sha: str) -> bytes:
        self.checkpoint()
        safe_key(key)
        cache = local_path(self.root, f"{PREFIX}/cache/{sha}.json")
        if cache.exists():
            with cache.open("rb") as stream:
                raw = stream.read(MAX_JSON_BYTES + 1)
        elif local_path(self.root, key).is_file():
            with local_path(self.root, key).open("rb") as stream:
                raw = stream.read(MAX_JSON_BYTES + 1)
        elif self.source is None:
            raise DependencyError("dependency_local_metadata_missing")
        else:
            raw = self.source.read_small(key, MAX_JSON_BYTES)
        if raw is None or len(raw) > MAX_JSON_BYTES or hashlib.sha256(raw).hexdigest() != sha:
            raise DependencyError("dependency_metadata_missing_or_hash_mismatch")
        parse_json(raw)
        write_atomic(cache, raw, immutable=True)
        return raw

    def _catalog(self, key: str) -> tuple[dict, list[dict], dict[str, bytes]]:
        safe_key(key)
        match = DELIVERY.fullmatch(key.removeprefix(f"{PREFIX}/deliveries/"))
        if not match or not key.startswith(f"{PREFIX}/deliveries/"):
            raise DependencyError("dependency_delivery_name_invalid")
        generation, sha = int(match[1]), match[2]
        raw = self._read(key, sha)
        catalog = validate_catalog(parse_json(raw), node=self.node)
        if catalog["generation"] != generation:
            raise DependencyError("dependency_generation_binding_mismatch")
        seen = local_path(self.root, f"{PREFIX}/state/seen/{generation:020d}.json")
        write_atomic(seen, canonical_bytes({"catalog_sha256": sha}), immutable=True)
        metadata, entries = {key: raw}, []
        identities = set()
        for page in catalog["pages"]:
            page_raw = self._read(page["relative_key"], page["sha256"])
            metadata[page["relative_key"]] = page_raw
            for entry in validate_catalog_page(page_raw, expected_sha256=page["sha256"]):
                identity = (entry["business_date"], entry["base_manifest_sha256"])
                if identity in identities:
                    raise DependencyError("dependency_duplicate_day_binding")
                identities.add(identity)
                entries.append(entry)
        # The publisher commits one business day per generation. Keep that
        # boundary until multi-day publication has a complete-receipt protocol.
        if len(entries) != 1:
            raise DependencyError("dependency_delivery_shape_unsupported")
        return catalog, entries, metadata

    def discover(self) -> dict[tuple[str, str], dict[str, Any]]:
        keys = self.source.list_deliveries() if self.source is not None else []
        if len(keys) > MAX_DELIVERIES or len(keys) != len(set(keys)):
            raise DependencyError("dependency_inventory_limit_or_duplicate")
        # Remember locally published generations when a source temporarily
        # lists older material; never silently select an earlier delivery.
        public = local_path(self.root, f"{PREFIX}/deliveries")
        known = {f"{PREFIX}/deliveries/{p.name}" for p in public.glob("*.json")}
        keys = sorted(set(keys) | known)
        if len(keys) > MAX_DELIVERIES:
            raise DependencyError("dependency_inventory_limit")
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for key in keys:
            catalog, entries, _ = self._catalog(key)
            for entry in entries:
                identity = (entry["business_date"], entry["base_manifest_sha256"])
                latest[identity] = {**entry, "generation": catalog["generation"], "catalog_key": key}
        return latest

    def _prepare(self, entry: dict) -> tuple[dict, dict[str, bytes]]:
        catalog, entries, metadata = self._catalog(entry["catalog_key"])
        selected = {key: entry[key] for key in entries[0]}
        if selected != entries[0] or entry["generation"] != catalog["generation"]:
            raise DependencyError("dependency_selected_delivery_changed")
        raw = self._read(entry["manifest_key"], entry["manifest_sha256"])
        manifest = validate_manifest(parse_json(raw), node=self.node,
            business_day=entry["business_date"], base_sha256=entry["base_manifest_sha256"])
        if manifest["root_scope"] != "business_day" or manifest["dependency_status"] != entry["dependency_status"]:
            raise DependencyError("dependency_manifest_scope_or_status_mismatch")
        metadata[entry["manifest_key"]] = raw
        readback_raw = self._read(catalog["readback_receipt_key"], catalog["readback_receipt_sha256"])
        readback = parse_json(readback_raw)
        replicas = readback.get("replicas")
        if (readback.get("contract_version") != "smsi-business-day-replica-readbacks/v1"
                or readback.get("collector_node_id") != self.node
                or readback.get("manifest_sha256") != entry["manifest_sha256"]
                or readback.get("required_replicas") != catalog["required_replicas"]
                or not isinstance(replicas, dict) or set(replicas) != set(catalog["required_replicas"])):
            raise DependencyError("dependency_readback_binding_mismatch")
        expected = [(item["sha256"], item["size_bytes"]) for item in manifest["objects"]]
        expected.append((entry["manifest_sha256"], len(raw)))
        for name, proofs in replicas.items():
            if not isinstance(proofs, list) or len(proofs) != len(expected):
                raise DependencyError("dependency_readback_incomplete")
            for proof, (sha, size) in zip(proofs, expected):
                if (not isinstance(proof, dict) or proof.get("name") != name
                        or proof.get("read_verified") is not True or proof.get("sha256") != sha
                        or type(proof.get("size_bytes")) is not int or proof["size_bytes"] != size
                        or not proof.get("failure_domain")):
                    raise DependencyError("dependency_readback_invalid")
        metadata[catalog["readback_receipt_key"]] = readback_raw
        if self.require_upstream_receipt:
            upstream = self.source.read_small(receipt_key(entry["manifest_sha256"]), MAX_JSON_BYTES)
            if upstream is None or not self._receipt_matches(parse_json(upstream), entry, manifest):
                raise DependencyError("dependency_upstream_not_verified")
        return manifest, metadata

    def _receipt_matches(self, receipt: dict, entry: dict, manifest: dict) -> bool:
        return (receipt.get("contract_version") == LOCAL_CONTRACT and receipt.get("status") == "verified"
            and receipt.get("collector_node_id") == self.node
            and all(receipt.get(key) == entry[key] for key in (
                "business_date", "base_manifest_sha256", "manifest_sha256", "catalog_key", "generation"))
            and receipt.get("objects_sha256") == manifest["objects_sha256"]
            and receipt.get("dependency_status") == manifest["dependency_status"])

    @staticmethod
    def _signature(path: Path) -> list[int]:
        stat = path.stat()
        return [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino]

    def _object_receipt(self, item: dict) -> Path:
        return local_path(self.root, f"{PREFIX}/state/objects/{digest(item)}.json")

    def _cached_object(self, item: dict) -> bool:
        path = local_path(self.root, item["relative_key"])
        receipt = self._object_receipt(item)
        try:
            return bool(path.is_file() and receipt.is_file()
                        and read_json(receipt).get("signature") == self._signature(path))
        except (OSError, ValueError):
            return False  # A damaged cache requires verification, never trust.

    def _verified(self, entry: dict, manifest: dict, metadata: dict[str, bytes]) -> bool:
        receipt = local_path(self.root, receipt_key(entry["manifest_sha256"]))
        try:
            if not receipt.is_file() or not self._receipt_matches(read_json(receipt), entry, manifest):
                return False
            for key, raw in metadata.items():
                public = local_path(self.root, key)
                if not public.is_file() or public.stat().st_size != len(raw) or public.read_bytes() != raw:
                    return False
            return all(self._cached_object(item) for item in manifest["objects"])
        except (OSError, ValueError):
            return False

    def verify(self, entry: dict) -> dict[str, Any]:
        """Explicit local recheck ignores cached object verification signatures."""
        manifest, _ = self._prepare(entry)
        if not local_path(self.root, entry["catalog_key"]).is_file():
            raise DependencyError("dependency_local_delivery_unpublished")
        for item in manifest["objects"]:
            self._verify_object(item)
        result = self.inspect(entry)
        if result["state"] != "verified":
            raise DependencyError("dependency_local_receipt_invalid")
        return result

    def inspect(self, entry: dict) -> dict[str, Any]:
        manifest, metadata = self._prepare(entry)
        return {**entry, "state": "verified" if self._verified(entry, manifest, metadata) else "ready",
            "object_count": len(manifest["objects"]), "missing_count": len(manifest["missing"]),
            "bytes_total": sum(item["size_bytes"] for item in manifest["objects"])}

    def _verify_object(self, item: dict, *, path: Path | None = None, record: bool = True) -> None:
        import pyarrow.parquet as pq
        self.checkpoint()
        path = path or local_path(self.root, item["relative_key"])
        before = self._signature(path)
        observed = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                self.checkpoint()
                observed.update(chunk)
        if before[0] != item["size_bytes"] or observed.hexdigest() != item["sha256"]:
            raise DependencyError("dependency_object_hash_mismatch")
        with pq.ParquetFile(path) as parquet:
            if (parquet.metadata.num_rows != item["row_count"]
                    or hashlib.sha256(parquet.schema_arrow.serialize().to_pybytes()).hexdigest() != item["schema_sha256"]):
                raise DependencyError("dependency_parquet_metadata_mismatch")
        if self._signature(path) != before:
            raise DependencyError("dependency_object_changed_during_verification")
        if record:
            write_atomic(self._object_receipt(item), canonical_bytes({"signature": before}))

    def sync(self, entry: dict) -> dict[str, Any]:
        manifest, metadata = self._prepare(entry)
        day_root = self.root / ("date=" + entry["business_date"])
        native_raw = (day_root / "manifest.json").read_bytes()
        native_receipt = read_json(day_root / ".smsi-verified.json")
        if (hashlib.sha256(native_raw).hexdigest() != entry["base_manifest_sha256"]
                or native_receipt.get("contract_version") != "smsi-local-archive-verification/v1"
                or native_receipt.get("status") != "verified"
                or native_receipt.get("manifest_sha256") != entry["base_manifest_sha256"]
                or native_receipt.get("archive_date") != entry["business_date"]):
            raise DependencyError("dependency_native_not_verified")
        if self._verified(entry, manifest, metadata):
            return {**self.inspect(entry), "downloaded_bytes": 0}
        pending = [item for item in manifest["objects"] if not self._cached_object(item)]
        required = sum(item["size_bytes"] for item in pending)
        self.root.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self.root).free < required + self.minimum_free_bytes:
            raise DependencyError("补充包下载空间不足，已保留现有文件")
        total = sum(item["size_bytes"] for item in manifest["objects"])
        pending_keys = {item["relative_key"] for item in pending}
        counters = {item["relative_key"]: item["size_bytes"] for item in manifest["objects"]
                    if item["relative_key"] not in pending_keys}
        done_total = sum(counters.values())
        progress_lock = Lock()
        # Create shared parents before worker threads resolve paths. On Python
        # 3.10/Windows, resolving a missing parent while another thread creates
        # it can transiently return a different path spelling.
        for key in (f"{PREFIX}/objects", f"{PREFIX}/state/objects"):
            local_path(self.root, key).mkdir(parents=True, exist_ok=True)

        def transfer(item: dict) -> int:
            self.checkpoint()
            key = item["relative_key"]
            path = local_path(self.root, key)
            temporary = local_path(self.root, key + ".downloading")
            def progress(size: int) -> None:
                nonlocal done_total
                self.checkpoint()
                with progress_lock:
                    current = max(0, min(size, item["size_bytes"]))
                    done_total += current - counters.get(key, 0)
                    counters[key] = current
                    self.progress(done_total, total, key)
            if path.is_file():
                try:
                    self._verify_object(item)
                except DependencyError:
                    pass
                else:
                    progress(item["size_bytes"])
                    return 0
            self.source.download(key, temporary, item["size_bytes"], progress)
            self.checkpoint()
            try:
                self._verify_object(item, path=temporary, record=False)
            except DependencyError as exc:
                # A full-length corrupt resume file must not poison every retry.
                temporary.unlink()
                raise DependencyError("dependency_download_verification_failed: " + str(exc)) from exc
            with temporary.open("r+b") as stream:
                os.fsync(stream.fileno())
            path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary, path)
            if os.name != "nt":
                directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            write_atomic(self._object_receipt(item), canonical_bytes({"signature": self._signature(path)}))
            progress(item["size_bytes"])
            return item["size_bytes"]

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            downloaded = sum(pool.map(transfer, pending))
        self.checkpoint()
        # Re-read the upstream verification receipt before making the delivery
        # visible. A transfer never activates unfinished Ubuntu material.
        self._prepare(entry)
        self.checkpoint()
        for key, raw in metadata.items():
            if key != entry["catalog_key"]:
                write_atomic(local_path(self.root, key), raw, immutable=True)
        receipt = {**entry, "contract_version": LOCAL_CONTRACT, "status": "verified",
            "collector_node_id": self.node, "objects_sha256": manifest["objects_sha256"],
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "object_count": len(manifest["objects"]), "missing_count": len(manifest["missing"])}
        write_atomic(local_path(self.root, receipt_key(entry["manifest_sha256"])), canonical_bytes(receipt))
        write_atomic(local_path(self.root, entry["catalog_key"]), metadata[entry["catalog_key"]], immutable=True)
        return {**self.inspect(entry), "downloaded_bytes": downloaded}
