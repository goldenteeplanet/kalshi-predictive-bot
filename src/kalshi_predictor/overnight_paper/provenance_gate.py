"""Gate 9: original-artifact lineage, never a model-skill attestation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from kalshi_predictor.advanced_risk.engine import AdvancedRiskDecision
from kalshi_predictor.overnight_paper.provenance import (
    Artifact,
    Verification,
    validate_source_visibility,
    verify_full_provenance,
)
from kalshi_predictor.overnight_paper.source_health import MAX_FORECAST_AGE_SECONDS, aware
from kalshi_predictor.position_sizing.sizer import PositionSizingDecision


@dataclass(frozen=True)
class ProvenanceContext:
    """Original inputs supplied by the coordinator, not serialized PASS reports.

    Expected code SHA comes from the runtime release; expected rule version comes
    from the independently verified rule. This gate checks their binding only.
    """

    artifacts: dict[str, Artifact]
    source_artifacts: tuple[Artifact, ...]
    training_artifacts: tuple[Artifact, ...]
    model_code: bytes
    features_artifact: Artifact
    expected_code_sha: str
    expected_rule_version: str


def verify_complete_provenance(
    *,
    decision: dict[str, Any],
    decision_id: str,
    context: ProvenanceContext | None,
    now: datetime,
    phase3m: PositionSizingDecision | None,
    phase3n: AdvancedRiskDecision | None,
) -> Verification:
    """Recompute original hashes, visibility, freshness and exact decision lineage.

    Features contain identity, source_hashes, generated_at, available_at and a
    nonempty records list. Each record names source_sha256, observed_at,
    available_at, name and value. No clock can be supplied by a PASS attestation.
    """
    if not isinstance(context, ProvenanceContext):
        return Verification(False, ("ORIGINAL_PROVENANCE_CONTEXT_REQUIRED",))
    base = verify_full_provenance(
        decision=decision,
        decision_id=decision_id,
        artifacts=context.artifacts,
        source_artifacts=context.source_artifacts,
        training_artifacts=context.training_artifacts,
        model_code=context.model_code,
        now=now,
        phase3m=phase3m,
        phase3n=phase3n,
    )
    if not base.passed:
        return base
    try:
        rows = {role: artifact.decode() for role, artifact in context.artifacts.items()}
        forecast, model, snapshot = (rows[key] for key in ("forecast", "model", "snapshot"))
        at, reference = aware(decision["decision_at"]), aware(now)
        if not re.fullmatch(r"[0-9a-f]{40}", context.expected_code_sha):
            raise ValueError("RUNTIME_CODE_SHA_REQUIRED")
        if not context.expected_rule_version.strip():
            raise ValueError("VERIFIED_RULE_VERSION_REQUIRED")
        for key, expected in (
            ("code_sha", context.expected_code_sha),
            ("rule_version", context.expected_rule_version),
        ):
            if decision.get(key) != expected or forecast.get(key) != expected:
                raise ValueError("PROVENANCE_BINDING_MISMATCH:" + key)
        if not model.get("name") or any(
            row.get("model_name") != model["name"] for row in (decision, forecast)
        ):
            raise ValueError("MODEL_NAME_MISMATCH")
        if model.get("model_kind", "trained") != "fixed_heuristic" and any(
            aware(row["training_cutoff"]) != aware(model["training_cutoff"])
            for row in (decision, forecast)
        ):
            raise ValueError("TRAINING_CUTOFF_BINDING_MISMATCH")
        features = context.features_artifact.decode()
        if any(
            row.get("features_artifact_sha256") != context.features_artifact.sha256
            for row in (decision, forecast)
        ):
            raise ValueError("FEATURE_ARTIFACT_BINDING_MISMATCH")
        if any(features.get(key) != decision[key] for key in ("ticker", "event_id", "series")):
            raise ValueError("FEATURE_IDENTITY_MISMATCH")
        if features.get("source_hashes") != decision["source_hashes"]:
            raise ValueError("FEATURE_SOURCE_BINDING_MISMATCH")
        generated, available = aware(features["generated_at"]), aware(features["available_at"])
        if not generated <= available <= aware(forecast["generated_at"]) <= at:
            raise ValueError("FEATURE_VISIBILITY_INVALID")
        sources = {source.sha256: source.decode() for source in context.source_artifacts}
        manifest = [
            {
                "sha256": sha,
                **{
                    key: source[key]
                    for key in (
                        "provider_updated_at",
                        "provider_generated_at",
                        "available_at",
                        "received_at",
                    )
                },
                **({"clock_basis": source["clock_basis"]} if "clock_basis" in source else {}),
            }
            for sha, source in sources.items()
        ]
        if decision.get("source_timestamps") != manifest:
            raise ValueError("SOURCE_TIMESTAMP_BINDING_MISMATCH")
        for source in sources.values():
            validate_source_visibility(source, decision_at=at, now=reference)
            if source.get("clock_basis") == "public_rest_receipt":
                continue
            if any(
                not 0
                <= (reference - aware(source[key])).total_seconds()
                <= MAX_FORECAST_AGE_SECONDS
                for key in ("provider_updated_at", "provider_generated_at")
            ):
                raise ValueError("PROVIDER_CLOCK_STALE")
        if not 0 <= (reference - aware(snapshot["captured_at"])).total_seconds() <= 60:
            raise ValueError("SNAPSHOT_CLOCK_STALE")
        records = features.get("records")
        if not isinstance(records, list) or not records:
            raise ValueError("ORIGINAL_FEATURE_RECORDS_REQUIRED")
        clocks = []
        for record in records:
            source = sources[record["source_sha256"]]
            observed, visible = aware(record["observed_at"]), aware(record["available_at"])
            if not (
                record.get("name")
                and "value" in record
                and observed <= visible <= generated
                and aware(source["received_at"]) <= visible
            ):
                raise ValueError("FEATURE_RECORD_VISIBILITY_INVALID")
            clocks.append(
                {
                    key: record[key]
                    for key in ("name", "source_sha256", "observed_at", "available_at")
                }
            )
        if decision.get("feature_timestamps") != clocks:
            raise ValueError("FEATURE_TIMESTAMP_BINDING_MISMATCH")
        return Verification(True, (), base.verified_hashes + (context.features_artifact.sha256,))
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError) as exc:
        return Verification(False, (str(exc),))
