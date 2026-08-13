from __future__ import annotations

from collections import Counter
from typing import Any, Mapping


OUTCOMES_CONTRACT = "smsi-runtime-health-outcomes/v1"
ASSESSMENT_ENGINE_VERSION = "smsi-runtime-health-assessment/v4"
HEALTH_STATUSES = frozenset({"healthy", "attention", "critical", "unknown"})


def _health_status(value: Any) -> str:
    status = str(value or "")
    return status if status in HEALTH_STATUSES else "unknown"


def runtime_report_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    """Return a display-only summary of a manifest-bound runtime report.

    The client verifies report bytes and protocol elsewhere.  This function
    deliberately does not re-assess health: health rules belong to the
    collector that created the evidence.
    """
    collection = report.get("collection_sources")
    collection = collection if isinstance(collection, Mapping) else {}
    sources = collection.get("sources")
    sources = sources if isinstance(sources, list) else []
    source_counts = Counter(
        str((item.get("quality") or {}).get("status") or "unknown")
        for item in sources
        if isinstance(item, Mapping) and isinstance(item.get("quality"), Mapping)
    )
    summary = report.get("summary")
    summary = summary if isinstance(summary, Mapping) else {}
    raw_issues = summary.get("top_issues")
    raw_issues = raw_issues if isinstance(raw_issues, list) else []
    issue_counts = summary.get("issue_counts")
    assessment = report.get("assessment")
    assessment = assessment if isinstance(assessment, Mapping) else {}
    outcomes = assessment.get("outcomes")
    outcomes = outcomes if isinstance(outcomes, Mapping) else {}
    outcomes_current = (
        assessment.get("engine_version") == ASSESSMENT_ENGINE_VERSION
        and
        outcomes.get("contract_version") == OUTCOMES_CONTRACT
        and isinstance(outcomes.get("data_quality"), Mapping)
        and isinstance(outcomes.get("operational"), Mapping)
    )
    data_quality = outcomes.get("data_quality") if outcomes_current else {}
    operational = outcomes.get("operational") if outcomes_current else {}
    observations = outcomes.get("observations") if outcomes_current else {}
    classification = "current" if outcomes_current else "historical"
    return {
        "status": _health_status(report.get("overall_status")),
        "issue_count": int(summary.get("issue_count") or 0),
        "issue_counts": dict(issue_counts) if isinstance(issue_counts, Mapping) else {},
        "source_counts": dict(source_counts),
        "top_issues": [
            {
                "severity": str(item.get("severity") or "unknown"),
                "title": str(item.get("title") or item.get("code") or ""),
                "action": str(item.get("action") or ""),
            }
            for item in raw_issues[:3]
            if isinstance(item, Mapping)
        ],
        "generated_at": str(report.get("generated_at") or ""),
        "assessment_classification": classification,
        "assessment_engine_version": str(assessment.get("engine_version") or ""),
        "data_quality_status": (
            _health_status(data_quality.get("status")) if outcomes_current else ""
        ),
        "data_quality_issue_count": (
            int(data_quality.get("issue_count") or 0) if outcomes_current else 0
        ),
        "operational_status": (
            _health_status(operational.get("status")) if outcomes_current else ""
        ),
        "operational_issue_count": (
            int(operational.get("issue_count") or 0) if outcomes_current else 0
        ),
        "observation_count": (
            int(observations.get("count") or 0)
            if isinstance(observations, Mapping)
            else 0
        ),
    }
