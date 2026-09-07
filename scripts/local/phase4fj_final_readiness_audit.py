"""Compare Phase 4BP latency baseline with final measured and proposed outcomes."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

SCHEMA = "phase4fj.readiness-input.v1"
METRICS = (
    "market_to_forecast",
    "forecast_to_ranking",
    "ranking_to_risk",
    "risk_to_paper_eligibility",
    "settlement_to_evaluation",
    "operator_review",
    "ci_feedback",
    "end_to_end",
)


def _hash(v: Any) -> str:
    if isinstance(v, dict):
        v = {k: x for k, x in v.items() if k != "artifact_hash"}
    return canonical_hash(v)


def build_report(p: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(p, dict) or set(p) != {
        "schema",
        "baseline_hash",
        "environment_hash",
        "metrics",
        "artifact_hash",
    }:
        raise ValueError("PHASE4FJ_FIELDS_INVALID")
    if p["schema"] != SCHEMA or p["artifact_hash"] != _hash(p):
        raise ValueError("PHASE4FJ_HASH_INVALID")
    values = p["metrics"]
    if not isinstance(values, list) or {
        x.get("metric") for x in values if isinstance(x, dict)
    } != set(METRICS):
        raise ValueError("PHASE4FJ_METRICS_INVALID")
    rows = []
    for x in values:
        if set(x) != {"metric", "baseline_ms", "final_ms", "evidence_kind", "evidence_hash"}:
            raise ValueError("PHASE4FJ_METRIC_FIELDS_INVALID")
        if x["evidence_kind"] not in {"MEASURED", "PROPOSED"}:
            raise ValueError("PHASE4FJ_EVIDENCE_KIND_INVALID")
        for field in ("baseline_ms", "final_ms"):
            if isinstance(x[field], bool) or not isinstance(x[field], int) or x[field] < 0:
                raise ValueError("PHASE4FJ_LATENCY_INVALID")
        if not isinstance(x["evidence_hash"], str) or len(x["evidence_hash"]) != 64:
            raise ValueError("PHASE4FJ_EVIDENCE_HASH_INVALID")
        delta = x["baseline_ms"] - x["final_ms"]
        rows.append(
            {
                **x,
                "delta_ms": delta,
                "improvement_bps": 0
                if x["baseline_ms"] == 0
                else delta * 10000 // x["baseline_ms"],
                "gain_claimed": x["evidence_kind"] == "MEASURED" and delta > 0,
            }
        )
    rows.sort(key=lambda x: METRICS.index(x["metric"]))
    measured = [x for x in rows if x["evidence_kind"] == "MEASURED"]
    proposed = [x for x in rows if x["evidence_kind"] == "PROPOSED"]
    regressions = [x["metric"] for x in measured if x["delta_ms"] < 0]
    r = {
        "schema": "phase4fj.readiness-report.v1",
        "phase": "4FJ",
        "baseline_hash": p["baseline_hash"],
        "environment_hash": p["environment_hash"],
        "input_hash": p["artifact_hash"],
        "metrics": rows,
        "measured_improvements": [x["metric"] for x in measured if x["delta_ms"] > 0],
        "unchanged_measured": [x["metric"] for x in measured if x["delta_ms"] == 0],
        "proposals": [x["metric"] for x in proposed],
        "measured_regressions": regressions,
        "readiness_passed": not regressions and bool(measured),
        "proposal_counted_as_gain": False,
        "production_database_mutated": False,
        "services_controlled": False,
        "exchange_requests_made": False,
        "execution_authorized": False,
    }
    r["artifact_hash"] = _hash(r)
    return r


def publish(path: Path, p: dict[str, Any]):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, n = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(n)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as h:
            json.dump(p, h, sort_keys=True, separators=(",", ":"))
            h.write("\n")
            h.flush()
            os.fsync(h.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--input", type=Path, required=True)
    a.add_argument("--output", type=Path, required=True)
    x = a.parse_args()
    publish(x.output, build_report(json.loads(x.input.read_text())))


if __name__ == "__main__":
    main()
