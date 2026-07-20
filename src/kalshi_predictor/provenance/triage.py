from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SEVERITY = {
    "OBSERVATION_REFERENCE_MISSING": 100,
    "SNAPSHOT_REFERENCE_MISSING": 95,
    "OBSERVATION_TIMESTAMP_INVALID": 90,
    "SNAPSHOT_TIMESTAMP_INVALID": 85,
    "OBSERVATION_STALE": 70,
    "SNAPSHOT_STALE": 65,
}
REPAIR = {
    "OBSERVATION_REFERENCE_MISSING": "repair exact future observation-reference capture",
    "SNAPSHOT_REFERENCE_MISSING": "repair exact future snapshot-reference capture",
    "OBSERVATION_TIMESTAMP_INVALID": "export the exact observation timestamp",
    "SNAPSHOT_TIMESTAMP_INVALID": "export the exact snapshot captured_at timestamp",
    "OBSERVATION_STALE": "inspect observation refresh cadence without changing thresholds",
    "SNAPSHOT_STALE": "inspect snapshot capture cadence without changing thresholds",
}


def build_offline_provenance_triage(report: Mapping[str, Any]) -> dict[str, Any]:
    rows = report.get("rows")
    if not isinstance(rows, list):
        raise ValueError("regression report must contain a rows list")
    cause_counts: Counter[str] = Counter()
    model_counts: Counter[str] = Counter()
    revision_counts: Counter[str] = Counter()
    ticker_counts: Counter[str] = Counter()
    age_buckets = {"observation": Counter(), "snapshot": Counter()}
    affected: dict[str, set[str]] = defaultdict(set)
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"row {index} must be an object")
        failures = row.get("failures")
        if not isinstance(failures, list):
            raise ValueError(f"row {index} failures must be a list")
        model = str(row.get("model_name") or "UNKNOWN")
        version = str(row.get("model_version") or "UNKNOWN")
        ticker = str(row.get("ticker") or "UNKNOWN")
        for failure in sorted({str(value) for value in failures}):
            cause_counts[failure] += 1
            model_counts[model] += 1
            revision_counts[f"{model}@{version}"] += 1
            ticker_counts[ticker] += 1
            affected[failure].add(ticker)
        age_buckets["observation"][_age_bucket(row.get("observation_age_seconds"))] += 1
        age_buckets["snapshot"][_age_bucket(row.get("snapshot_age_seconds"))] += 1
    priorities = [
        {
            "rank": 0,
            "cause": cause,
            "severity": SEVERITY.get(cause, 50),
            "affected_rows": count,
            "affected_tickers": sorted(affected[cause]),
            "recommended_repair": REPAIR.get(cause, "inspect exact exported attribution fields"),
        }
        for cause, count in cause_counts.items()
    ]
    priorities.sort(key=lambda row: (-row["severity"], -row["affected_rows"], row["cause"]))
    for rank, row in enumerate(priorities, 1):
        row["rank"] = rank
    return {
        "phase": "PROV-15D",
        "mode": "OFFLINE_PROVENANCE_FAILURE_TRIAGE",
        "source_phase": report.get("phase"),
        "database_access": False,
        "execution_enabled": False,
        "summary": {
            "rows_examined": len(rows),
            "failed_rows": sum(bool(row.get("failures")) for row in rows),
            "distinct_causes": len(cause_counts),
            "distinct_affected_tickers": len([
                ticker for ticker in ticker_counts if ticker != "UNKNOWN"
            ]),
        },
        "groups": {
            "by_cause": dict(sorted(cause_counts.items())),
            "by_model": dict(sorted(model_counts.items())),
            "by_revision": dict(sorted(revision_counts.items())),
            "by_ticker": dict(sorted(ticker_counts.items())),
            "observation_age_buckets": dict(sorted(age_buckets["observation"].items())),
            "snapshot_age_buckets": dict(sorted(age_buckets["snapshot"].items())),
        },
        "priorities": priorities,
        "guardrails": {
            "thresholds_changed": False,
            "source_export_modified": False,
            "database_writes": 0,
            "execution_enabled": False,
        },
    }


def write_offline_provenance_triage(report: Mapping[str, Any], output_path: Path) -> Path:
    payload = build_offline_provenance_triage(report)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output_path)
    return output_path


def _age_bucket(value: Any) -> str:
    if value is None:
        return "missing"
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return "invalid"
    if seconds < 0:
        return "future"
    if seconds <= 60:
        return "000-060s"
    if seconds <= 300:
        return "061-300s"
    if seconds <= 900:
        return "301-900s"
    return "901s+"
