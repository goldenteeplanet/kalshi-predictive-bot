from __future__ import annotations

import hashlib
import json
from typing import Any

from kalshi_predictor.phase4cd.read_model_chain import ReadModelChainResult
from kalshi_predictor.phase4cd.read_model_compatibility import CompatibilityResult
from kalshi_predictor.phase4cd.read_model_consumer import ReadModelView
from kalshi_predictor.phase4cd.read_model_staleness import StalenessEscalation

DASHBOARD_SCHEMA_VERSION = "phase4fv-read-model-provenance-dashboard-v1"


class ProvenanceDashboardError(ValueError):
    """Stable fail-closed provenance dashboard error."""


def build_provenance_dashboard(
    *,
    view: ReadModelView | None,
    compatibility: CompatibilityResult | None,
    chain: ReadModelChainResult | None,
    staleness: StalenessEscalation | None,
    max_lane_fields: int = 32,
) -> dict[str, Any]:
    if max_lane_fields <= 0:
        raise ProvenanceDashboardError("LANE_FIELD_BOUND_INVALID")
    missing = sorted(
        name
        for name, value in {
            "chain": chain,
            "compatibility": compatibility,
            "staleness": staleness,
            "view": view,
        }.items()
        if value is None
    )
    if missing:
        return _finalize(
            {
                "schema_version": DASHBOARD_SCHEMA_VERSION,
                "mode": "PAPER_READ_ONLY",
                "execution_authorized": False,
                "status": "UNAVAILABLE",
                "action": "INSPECT_MISSING_EVIDENCE",
                "missing_components": missing,
                "provenance": None,
                "freshness": None,
                "compatibility": None,
                "chain": None,
                "lane": None,
            }
        )
    assert view is not None
    assert compatibility is not None
    assert chain is not None
    assert staleness is not None
    if len(view.evidence_lane) > max_lane_fields:
        raise ProvenanceDashboardError("LANE_FIELD_BOUND_EXCEEDED")
    if view.source_database_identity_hash != chain.source_identity_hash:
        raise ProvenanceDashboardError("SOURCE_IDENTITY_MISMATCH")
    if compatibility.producer_schema != view.schema_version:
        raise ProvenanceDashboardError("PRODUCER_SCHEMA_MISMATCH")

    if compatibility.decision != "COMPATIBLE":
        status = "BLOCKED"
        action = "REVIEW_SCHEMA_COMPATIBILITY"
    elif staleness.status == "LINEAGE_FAILURE":
        status = "LINEAGE_FAILURE"
        action = staleness.action
    elif staleness.status in {"STALE", "STALLED"}:
        status = staleness.status
        action = staleness.action
    elif staleness.status == "WARNING":
        status = "WARNING"
        action = staleness.action
    else:
        status = "HEALTHY"
        action = "NO_ACTION"
    return _finalize(
        {
            "schema_version": DASHBOARD_SCHEMA_VERSION,
            "mode": "PAPER_READ_ONLY",
            "execution_authorized": False,
            "status": status,
            "action": action,
            "missing_components": [],
            "provenance": {
                "source_identity_hash": view.source_database_identity_hash,
                "source_watermark": view.source_watermark,
                "payload_hash": view.payload_hash,
                "manifest_hash": view.manifest_hash,
            },
            "freshness": {
                "status": staleness.status,
                "snapshot_age_seconds": staleness.snapshot_age_seconds,
                "progress_age_seconds": staleness.progress_age_seconds,
                "reason": staleness.reason,
            },
            "compatibility": {
                "producer_schema": compatibility.producer_schema,
                "consumer_schema": compatibility.consumer_schema,
                "decision": compatibility.decision,
                "reason": compatibility.reason,
                "matrix_hash": compatibility.matrix_hash,
            },
            "chain": {
                "node_count": chain.node_count,
                "genesis_hash": chain.genesis_hash,
                "head_hash": chain.head_hash,
                "first_sequence": chain.first_sequence,
                "last_sequence": chain.last_sequence,
                "progress": chain.progress,
            },
            "lane": dict(sorted(view.evidence_lane.items())),
        }
    )


def validate_provenance_dashboard(payload: Any) -> None:
    required = {
        "schema_version",
        "mode",
        "execution_authorized",
        "status",
        "action",
        "missing_components",
        "provenance",
        "freshness",
        "compatibility",
        "chain",
        "lane",
        "dashboard_hash",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise ProvenanceDashboardError("DASHBOARD_FIELDS_INVALID")
    if payload["schema_version"] != DASHBOARD_SCHEMA_VERSION:
        raise ProvenanceDashboardError("DASHBOARD_SCHEMA_UNSUPPORTED")
    if payload["mode"] != "PAPER_READ_ONLY" or payload["execution_authorized"] is not False:
        raise ProvenanceDashboardError("DASHBOARD_SAFETY_BOUNDARY_INVALID")
    unhashed = {key: value for key, value in payload.items() if key != "dashboard_hash"}
    if payload["dashboard_hash"] != _hash(unhashed):
        raise ProvenanceDashboardError("DASHBOARD_HASH_MISMATCH")
    if payload["status"] == "UNAVAILABLE":
        if not payload["missing_components"]:
            raise ProvenanceDashboardError("DASHBOARD_UNAVAILABLE_REASON_MISSING")
    elif payload["missing_components"]:
        raise ProvenanceDashboardError("DASHBOARD_COMPONENT_STATE_INVALID")


def _finalize(payload: dict[str, Any]) -> dict[str, Any]:
    payload["dashboard_hash"] = _hash(payload)
    validate_provenance_dashboard(payload)
    return payload


def _hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
