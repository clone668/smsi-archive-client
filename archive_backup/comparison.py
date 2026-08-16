from __future__ import annotations

import json
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import ClientConfig, ProfileConfig
from .protocol import parse_manifest
from .reporting import runtime_report_summary
from .verifier import local_object_path, verify_runtime_report


STATUS_ORDER = {"critical": 0, "attention": 1, "unknown": 2, "healthy": 3}
COMPARISON_CONTRACT = "smsi-archive-client-comparison/v2"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({str(item).strip() for item in value if str(item).strip()})


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取验证证据: {path.name}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"验证证据格式无效: {path.name}")
    return value


def _load_verified_archive(
    config: ClientConfig,
    profile: ProfileConfig,
    archive_date: str,
) -> dict[str, Any]:
    root = config.archive_root / f"collector={profile.collector_id}" / f"date={archive_date}"
    manifest_path = root / "manifest.json"
    receipt_path = root / ".smsi-verified.json"
    if not manifest_path.is_file() or not receipt_path.is_file():
        raise RuntimeError("本地恢复验证证据不完整")
    manifest_raw = manifest_path.read_bytes()
    snapshot = parse_manifest(manifest_raw, archive_date)
    receipt = _read_json(receipt_path)
    if (
        receipt.get("contract_version") != "smsi-local-archive-verification/v1"
        or receipt.get("status") != "verified"
        or str(receipt.get("archive_date") or "") != archive_date
        or str(receipt.get("manifest_sha256") or "") != snapshot.sha256
        or int(receipt.get("object_count") or -1) != snapshot.object_count
        or int(receipt.get("row_count") or -1) != snapshot.row_count
    ):
        raise RuntimeError("本地恢复验证凭据与 manifest 不一致")
    inventory: dict[str, int] = {}
    objects: dict[str, dict[str, Any]] = {}
    for item in snapshot.objects:
        relative_key = str(item.get("relative_key") or "")
        kind = str(item.get("kind") or "")
        table_name = str(item.get("table_name") or "")
        objects[relative_key] = {
            "kind": kind,
            "table_name": table_name,
            "row_count": int(item.get("row_count") or 0),
            "size_bytes": int(item.get("size_bytes") or 0),
            "sha256": str(item.get("sha256") or ""),
            "content_sha256": str(item.get("content_sha256") or ""),
            "schema_sha256": str(item.get("schema_sha256") or ""),
        }
        if kind == "business":
            inventory[table_name or "unknown"] = inventory.get(
                table_name or "unknown", 0
            ) + int(item.get("row_count") or 0)
    report_items = [
        item for item in snapshot.objects if item.get("kind") == "runtime_report"
    ]
    report: dict[str, Any] = {}
    if len(report_items) == 1:
        report_item = report_items[0]
        report_path = local_object_path(
            root,
            str(report_item["relative_key"]),
            archive_date,
        )
        verify_runtime_report(report_path, report_item, archive_date)
        report = _read_json(report_path)
    source_health = {
        str(item.get("source_id") or "unknown"): str(
            (item.get("quality") or {}).get("status") or "unknown"
        )
        for item in (report.get("collection_sources") or {}).get("sources") or []
        if isinstance(item, Mapping)
    }
    report_summary = runtime_report_summary(report) if report else {}
    ledger_counts = snapshot.manifest.get("ledger_counts")
    if not isinstance(ledger_counts, Mapping):
        ledger_counts = {}
    return {
        "profile_id": profile.profile_id,
        "collector_id": profile.collector_id,
        "reported_collector_id": str(report.get("collector_node_id") or ""),
        "manifest_sha256": snapshot.sha256,
        "manifest": snapshot,
        "report": report,
        "report_present": bool(report),
        "overall_status": str(report_summary.get("status") or "unknown"),
        "report_summary": report_summary,
        "quality_policy_sha256": str(
            ((report.get("collection_sources") or {}).get("quality_policy") or {}).get(
                "sha256"
            )
            or ""
        ),
        "source_health": source_health,
        "record_count": sum(inventory.values()),
        "business_inventory": dict(sorted(inventory.items())),
        "collector_code_versions": _string_list(
            ledger_counts.get("collector_code_versions")
        ),
        "source_epochs": _string_list(ledger_counts.get("source_epochs")),
        "objects": objects,
    }


def _issue(
    severity: str,
    code: str,
    detail: str,
    **evidence: Any,
) -> dict[str, Any]:
    return {"severity": severity, "code": code, "detail": detail, **evidence}


def _object_labels(keys: Sequence[str], objects: Mapping[str, Mapping[str, Any]]) -> str:
    labels = [str(objects.get(key, {}).get("table_name") or key) for key in keys]
    unique = list(dict.fromkeys(labels))
    preview = "、".join(unique[:6])
    if len(unique) > 6:
        preview += f" 等 {len(unique)} 项"
    return preview


def compare_archives(
    archive_date: str,
    left: dict[str, Any],
    right: dict[str, Any],
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    data_issues: list[dict[str, Any]] = []
    report_issues: list[dict[str, Any]] = []
    for side in (left, right):
        if side["report_present"] and side["reported_collector_id"] != side["collector_id"]:
            issue = _issue(
                "critical",
                f"collector_identity:{side['profile_id']}",
                f"配置 {side['collector_id']}，报告 {side['reported_collector_id'] or '--'}",
            )
            issues.append(issue)
            data_issues.append(issue)
        report_summary = side.get("report_summary") or {}
        data_quality_status = str(report_summary.get("data_quality_status") or "")
        if (
            side["report_present"]
            and report_summary.get("assessment_classification") == "current"
            and data_quality_status != "healthy"
        ):
            issue = _issue(
                data_quality_status,
                f"report_data_quality:{side['profile_id']}",
                f"当日数据质量为 {data_quality_status}",
            )
            issues.append(issue)
            report_issues.append(issue)
    if (
        left["report_present"]
        and right["report_present"]
        and left["reported_collector_id"] == right["reported_collector_id"]
    ):
        issue = _issue("critical", "collector_identity_duplicate", "两份报告使用相同节点标识")
        issues.append(issue)
        data_issues.append(issue)

    left_policy = left["quality_policy_sha256"]
    right_policy = right["quality_policy_sha256"]
    both_reports_current = (
        left.get("report_summary", {}).get("assessment_classification") == "current"
        and right.get("report_summary", {}).get("assessment_classification") == "current"
    )
    if both_reports_current and left_policy != right_policy:
        issue = _issue("attention", "quality_policy_mismatch", "两台服务器使用的质量策略不同")
        issues.append(issue)
        report_issues.append(issue)

    for field, code, detail in (
        (
            "collector_code_versions",
            "collector_code_version_mismatch",
            "两台服务器的采集器代码版本集合不同",
        ),
        (
            "source_epochs",
            "source_epoch_mismatch",
            "两台服务器的来源 Epoch 集合不同",
        ),
    ):
        left_value = left.get(field) or []
        right_value = right.get(field) or []
        if left_value != right_value:
            issue = _issue(
                "attention",
                code,
                detail,
                left=left_value,
                right=right_value,
            )
            issues.append(issue)
            data_issues.append(issue)

    source_health = []
    compared_source_ids = (
        sorted(set(left["source_health"]) | set(right["source_health"]))
        if left["report_present"] and right["report_present"]
        else []
    )
    for source_id in compared_source_ids:
        left_status = left["source_health"].get(source_id, "missing")
        right_status = right["source_health"].get(source_id, "missing")
        source_health.append(
            {"source_id": source_id, "left": left_status, "right": right_status}
        )
        if not both_reports_current:
            continue
        if "critical" in {left_status, right_status}:
            severity = "critical"
        elif left_status != right_status or "attention" in {left_status, right_status}:
            severity = "attention"
        elif "unknown" in {left_status, right_status} or "missing" in {
            left_status,
            right_status,
        }:
            severity = "unknown"
        else:
            continue
        issue = _issue(
            severity,
            f"source_health:{source_id}",
            f"{left_status} / {right_status}",
        )
        issues.append(issue)
        report_issues.append(issue)

    left_tables = set(left["business_inventory"])
    right_tables = set(right["business_inventory"])
    if left_tables != right_tables:
        issue = _issue(
            "attention",
            "business_inventory_mismatch",
            "业务表清单不一致",
        )
        issues.append(issue)
        data_issues.append(issue)
    left_records = int(left["record_count"])
    right_records = int(right["record_count"])
    denominator = max(left_records, right_records)
    relative_difference = (
        abs(left_records - right_records) / denominator if denominator else 0.0
    )
    if relative_difference > 0.20:
        issue = _issue(
            "attention",
            "record_volume_difference",
            f"总记录量相对差异 {relative_difference:.1%}，超过 20%",
        )
        issues.append(issue)
        data_issues.append(issue)

    left_objects = left.get("objects") or {}
    right_objects = right.get("objects") or {}
    left_keys = set(left_objects)
    right_keys = set(right_objects)
    left_only = sorted(left_keys - right_keys)
    right_only = sorted(right_keys - left_keys)
    if left_only or right_only:
        issue = _issue(
            "attention",
            "object_set_mismatch",
            f"归档对象集合不同：左侧独有 {len(left_only)}，右侧独有 {len(right_only)}",
            left_only=left_only,
            right_only=right_only,
        )
        issues.append(issue)
        data_issues.append(issue)

    metadata_keys: list[str] = []
    schema_keys: list[str] = []
    checksum_keys: list[str] = []
    row_count_keys: list[str] = []
    size_keys: list[str] = []
    for key in sorted(left_keys & right_keys):
        left_object = left_objects[key]
        right_object = right_objects[key]
        identity_specific = left_object.get("kind") == "runtime_report"
        if (
            left_object.get("kind"),
            left_object.get("table_name"),
        ) != (
            right_object.get("kind"),
            right_object.get("table_name"),
        ):
            metadata_keys.append(key)
        if left_object.get("schema_sha256") != right_object.get("schema_sha256"):
            schema_keys.append(key)
        if identity_specific:
            continue
        if left_object.get("sha256") != right_object.get("sha256"):
            checksum_keys.append(key)
        if left_object.get("row_count") != right_object.get("row_count"):
            row_count_keys.append(key)
        if left_object.get("size_bytes") != right_object.get("size_bytes"):
            size_keys.append(key)

    if metadata_keys:
        issue = _issue(
            "attention",
            "object_metadata_mismatch",
            "对象类型或业务表标识不同："
            + _object_labels(metadata_keys, left_objects),
            objects=metadata_keys,
        )
        issues.append(issue)
        data_issues.append(issue)
    if schema_keys:
        issue = _issue(
            "attention",
            "object_schema_mismatch",
            "Schema 不一致：" + _object_labels(schema_keys, left_objects),
            objects=schema_keys,
        )
        issues.append(issue)
        data_issues.append(issue)

    observed_differences: list[dict[str, Any]] = []
    for code, keys in (
        ("object_checksum_difference", checksum_keys),
        ("object_row_count_difference", row_count_keys),
        ("object_size_difference", size_keys),
    ):
        if keys:
            observed_differences.append(
                {"code": code, "count": len(keys), "objects": keys}
            )
    data_status = min(
        (item["severity"] for item in data_issues),
        key=lambda value: STATUS_ORDER[value],
        default="healthy",
    )
    return {
        "comparison_contract_version": COMPARISON_CONTRACT,
        "evaluated_at": _utc_now(),
        "archive_date": archive_date,
        "pair_id": "__".join(sorted((left["profile_id"], right["profile_id"]))),
        "left_profile_id": left["profile_id"],
        "right_profile_id": right["profile_id"],
        "status": data_status,
        "data_status": data_status,
        "data_issues": data_issues,
        "report_issues": report_issues,
        "restore_verification": {"left": "verified", "right": "verified"},
        "left_collector_id": left["collector_id"],
        "right_collector_id": right["collector_id"],
        "quality_policy": {"left": left_policy, "right": right_policy},
        "source_health": source_health,
        "record_count": {"left": left_records, "right": right_records},
        "record_difference": abs(left_records - right_records),
        "record_relative_difference": round(relative_difference, 6),
        "report_status": {
            "left": left["overall_status"] if left["report_present"] else "missing",
            "right": right["overall_status"] if right["report_present"] else "missing",
        },
        "report_summary": {
            "left": left.get("report_summary") or {},
            "right": right.get("report_summary") or {},
        },
        "business_inventory": {
            "left": left["business_inventory"],
            "right": right["business_inventory"],
        },
        "collector_code_versions": {
            "left": left.get("collector_code_versions") or [],
            "right": right.get("collector_code_versions") or [],
        },
        "source_epochs": {
            "left": left.get("source_epochs") or [],
            "right": right.get("source_epochs") or [],
        },
        "object_summary": {
            "common": len(left_keys & right_keys),
            "left_only": len(left_only),
            "right_only": len(right_only),
        },
        "observed_differences": observed_differences,
        "issues": issues,
    }


def build_archive_comparisons(
    config: ClientConfig,
    days: Sequence[Mapping[str, Any]],
    previous: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    profiles = [profile for profile in config.profiles if profile.enabled]
    verified_dates = {
        profile.profile_id: {
            str(item.get("archive_date") or "")
            for item in days
            if item.get("profile_id") == profile.profile_id
            and item.get("status") == "verified"
        }
        for profile in profiles
    }
    results: list[dict[str, Any]] = []
    day_rows = {
        (str(item.get("profile_id") or ""), str(item.get("archive_date") or "")): item
        for item in days
    }
    previous_results = {
        (str(item.get("archive_date") or ""), str(item.get("pair_id") or "")): item
        for item in previous
    }
    for left_profile, right_profile in combinations(profiles, 2):
        dates = sorted(
            verified_dates[left_profile.profile_id]
            & verified_dates[right_profile.profile_id],
            reverse=True,
        )
        for archive_date in dates:
            pair_id = "__".join(
                sorted((left_profile.profile_id, right_profile.profile_id))
            )
            left_day = day_rows[(left_profile.profile_id, archive_date)]
            right_day = day_rows[(right_profile.profile_id, archive_date)]
            inputs = {
                "left_profile_id": left_profile.profile_id,
                "right_profile_id": right_profile.profile_id,
                "left_collector_id": left_profile.collector_id,
                "right_collector_id": right_profile.collector_id,
                "left_manifest_sha256": str(left_day.get("manifest_sha256") or ""),
                "right_manifest_sha256": str(right_day.get("manifest_sha256") or ""),
            }
            previous_result = previous_results.get((archive_date, pair_id))
            if (
                previous_result
                and previous_result.get("comparison_contract_version")
                == COMPARISON_CONTRACT
                and previous_result.get("status") != "unknown"
                and previous_result.get("inputs") == inputs
            ):
                reused = dict(previous_result)
                reused.pop("compared_at", None)
                results.append(reused)
                continue
            try:
                left = _load_verified_archive(config, left_profile, archive_date)
                right = _load_verified_archive(config, right_profile, archive_date)
                result = compare_archives(archive_date, left, right)
            except RuntimeError as exc:
                result = {
                    "archive_date": archive_date,
                    "comparison_contract_version": COMPARISON_CONTRACT,
                    "evaluated_at": _utc_now(),
                    "pair_id": pair_id,
                    "left_profile_id": left_profile.profile_id,
                    "right_profile_id": right_profile.profile_id,
                    "status": "unknown",
                    "restore_verification": {"left": "unknown", "right": "unknown"},
                    "issues": [
                        _issue("unknown", "comparison_evidence_unavailable", str(exc))
                    ],
                }
            result["inputs"] = inputs
            results.append(result)
    return results
