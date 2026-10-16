"""Versioned, additive business-day dependency delivery.

This module has no collector imports, environment loading, database writes or
network I/O. Copies in the receiving applications share this exact contract.
The native V3 manifest and its retention permission are never amended here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

CONTRACT = "smsi-business-day-dependencies/v1"
CATALOG_CONTRACT = "smsi-business-day-delivery-catalog/v1"
RECEIPT_CONTRACT = "smsi-business-day-dependency-receipt/v1"
PREFIX = "dependencies-v1"
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_OBJECTS = 100_000
HEX = re.compile(r"[0-9a-f]{64}\Z")
NODE = re.compile(r"[a-z0-9][a-z0-9._-]{1,62}[a-z0-9]\Z")
KINDS = frozenset({"evidence", "raw", "batch_commit", "control", "business"})


class DependencyError(ValueError):
    """An invalid or incomplete supplement must not become a trusted input."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_file(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def checked_hash(value: Any) -> str:
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise DependencyError("dependency_hash_invalid")
    return value


def checked_day(value: Any) -> str:
    if not isinstance(value, str):
        raise DependencyError("dependency_date_invalid")
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError(value)
    except ValueError as exc:
        raise DependencyError("dependency_date_invalid") from exc
    return value


def checked_node(value: Any) -> str:
    if not isinstance(value, str) or not NODE.fullmatch(value):
        raise DependencyError("dependency_node_invalid")
    return value


def safe_key(value: Any, *, prefix: str = PREFIX) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise DependencyError("dependency_path_invalid")
    parts = value.split("/")
    if (parts[0] != prefix or any(part in {"", ".", ".."} for part in parts)
            or any(part.endswith((".", " ")) for part in parts)
            or any(any(ord(char) < 32 for char in part) for part in parts)):
        raise DependencyError("dependency_path_invalid")
    if PurePosixPath(value).as_posix() != value:
        raise DependencyError("dependency_path_invalid")
    return value


def is_filesystem_link(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    # Python 3.10 lacks Path.is_junction(). Reparse-point inspection is needed
    # on the supported Windows runtime as well as symlink checks on Linux.
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def local_path(root: Path, key: str) -> Path:
    parts = safe_key(key).split("/")
    root = root.resolve()
    path = root.joinpath(*parts)
    current = root
    for part in parts:
        current = current / part
        # Also reject Windows junctions, whose target may be inside the root.
        if is_filesystem_link(current):
            raise DependencyError("dependency_link_forbidden")
    if not path.resolve().is_relative_to(root):
        raise DependencyError("dependency_path_escape")
    return path


def parse_json(raw: bytes, *, maximum: int = MAX_JSON_BYTES) -> dict[str, Any]:
    if len(raw) > maximum:
        raise DependencyError("dependency_json_size_limit")
    def unique(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise DependencyError("dependency_json_duplicate_key")
            result[key] = value
        return result
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                           parse_constant=lambda _value: (_ for _ in ()).throw(
                               DependencyError("dependency_json_nonfinite")))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise DependencyError("dependency_json_invalid") from exc
    if not isinstance(value, dict):
        raise DependencyError("dependency_json_object_required")
    return value


def read_json(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        raw = stream.read(MAX_JSON_BYTES + 1)
    return parse_json(raw)


def write_atomic(path: Path, raw: bytes, *, immutable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if immutable and path.exists():
        if path.stat().st_size != len(raw) or path.read_bytes() != raw:
            raise DependencyError("dependency_immutable_conflict")
        return
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if immutable:
            # An exclusive hard link installs an immutable name without a
            # check/replace race. The temporary is always on the same volume.
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.stat().st_size != len(raw) or path.read_bytes() != raw:
                    raise DependencyError("dependency_immutable_conflict")
        else:
            os.replace(temporary, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def object_key(sha256: str) -> str:
    return f"{PREFIX}/objects/{checked_hash(sha256)}.parquet"


def manifest_key(sha256: str) -> str:
    return f"{PREFIX}/manifests/{checked_hash(sha256)}.json"


def _integer(value: Any, code: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise DependencyError(code)
    return value


def validate_manifest(value: Mapping[str, Any], *, node: str,
                      business_day: str, base_sha256: str) -> dict[str, Any]:
    if (value.get("contract_version") != CONTRACT
            or value.get("collector_node_id") != checked_node(node)
            or value.get("business_date") != checked_day(business_day)
            or value.get("base_manifest_sha256") != checked_hash(base_sha256)):
        raise DependencyError("dependency_manifest_binding_mismatch")
    if "retention_delete_allowed" in value:
        raise DependencyError("dependency_retention_permission_forbidden")
    if value.get("dependency_status") not in {"complete", "partial"}:
        raise DependencyError("dependency_status_invalid")
    missing = value.get("missing")
    if not isinstance(missing, list) or (not missing) != (value["dependency_status"] == "complete"):
        raise DependencyError("dependency_status_missing_mismatch")
    if any(not isinstance(item, dict) or not str(item.get("reason") or "") for item in missing):
        raise DependencyError("dependency_missing_invalid")
    if value.get("resolver_contract") != "smsi-business-day-dependency-resolver/v1":
        raise DependencyError("dependency_resolver_unsupported")
    checked_hash(value.get("resolver_code_sha256"))
    checked_hash(value.get("base_business_objects_sha256"))
    if value.get("root_scope") not in {"selected_roots", "business_day"}:
        raise DependencyError("dependency_root_scope_invalid")
    if value["root_scope"] == "selected_roots" and not any(
            item["reason"] == "business_day_scope_not_exhaustive" for item in missing):
        raise DependencyError("dependency_subset_scope_marker_missing")
    checked_hash(value.get("root_set_sha256"))
    _integer(value.get("root_count"), "dependency_root_count_invalid")
    objects = value.get("objects")
    if not isinstance(objects, list) or len(objects) > MAX_OBJECTS:
        raise DependencyError("dependency_object_limit")
    keys: set[str] = set()
    for item in objects:
        if not isinstance(item, dict):
            raise DependencyError("dependency_object_invalid")
        key = safe_key(item.get("relative_key"))
        if key != object_key(item.get("sha256")) or key in keys:
            raise DependencyError("dependency_object_identity_invalid")
        keys.add(key)
        if item.get("kind") not in KINDS or not str(item.get("table_name") or ""):
            raise DependencyError("dependency_object_kind_invalid")
        checked_day(item.get("native_archive_date"))
        checked_hash(item.get("schema_sha256"))
        _integer(item.get("row_count"), "dependency_row_count_invalid", minimum=1)
        _integer(item.get("size_bytes"), "dependency_object_size_invalid", minimum=1)
    if value.get("object_count") != len(objects):
        raise DependencyError("dependency_object_count_mismatch")
    if value.get("objects_sha256") != digest(objects):
        raise DependencyError("dependency_object_set_mismatch")
    return dict(value)


def validate_catalog(value: Mapping[str, Any], *, node: str) -> dict[str, Any]:
    if (value.get("contract_version") != CATALOG_CONTRACT
            or value.get("collector_node_id") != checked_node(node)):
        raise DependencyError("dependency_catalog_binding_mismatch")
    _integer(value.get("generation"), "dependency_catalog_generation_invalid", minimum=1)
    replicas = value.get("required_replicas")
    if (not isinstance(replicas, list) or not replicas
            or any(not isinstance(name, str) or not name for name in replicas)
            or len(replicas) != len(set(replicas))):
        raise DependencyError("dependency_catalog_replica_policy_invalid")
    sha = checked_hash(value.get("readback_receipt_sha256"))
    if value.get("readback_receipt_key") != f"{PREFIX}/replica-readbacks/{sha}.json":
        raise DependencyError("dependency_catalog_receipt_binding_invalid")
    pages = value.get("pages")
    if not isinstance(pages, list) or not pages or len(pages) > 4096:
        raise DependencyError("dependency_catalog_pages_invalid")
    seen: set[str] = set()
    for page in pages:
        if not isinstance(page, dict):
            raise DependencyError("dependency_catalog_page_invalid")
        sha = checked_hash(page.get("sha256"))
        key = safe_key(page.get("relative_key"))
        if key != f"{PREFIX}/catalog/{sha}.json" or key in seen:
            raise DependencyError("dependency_catalog_page_invalid")
        seen.add(key)
    return dict(value)


def validate_catalog_page(raw: bytes, *, expected_sha256: str) -> list[dict[str, Any]]:
    if hashlib.sha256(raw).hexdigest() != checked_hash(expected_sha256):
        raise DependencyError("dependency_catalog_page_hash_mismatch")
    value = parse_json(raw)
    entries = value.get("entries")
    if value.get("contract_version") != CATALOG_CONTRACT or not isinstance(entries, list) or len(entries) > 128:
        raise DependencyError("dependency_catalog_page_invalid")
    seen: set[tuple[str, str]] = set()
    for item in entries:
        if not isinstance(item, dict):
            raise DependencyError("dependency_catalog_entry_invalid")
        identity = (checked_day(item.get("business_date")), checked_hash(item.get("base_manifest_sha256")))
        if identity in seen or item.get("manifest_key") != manifest_key(item.get("manifest_sha256")):
            raise DependencyError("dependency_catalog_entry_invalid")
        seen.add(identity)
        if item.get("dependency_status") not in {"complete", "partial"}:
            raise DependencyError("dependency_catalog_entry_invalid")
    return entries


def verify_object(root: Path, item: Mapping[str, Any]) -> Path:
    path = local_path(root, str(item["relative_key"]))
    if path.stat().st_size != item["size_bytes"] or sha256_file(path) != item["sha256"]:
        raise DependencyError("dependency_object_content_mismatch")
    return path
