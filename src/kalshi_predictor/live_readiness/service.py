from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import (
    LiveReadinessCertificate,
    LiveReadinessCertificateEvent,
    ReadinessControlResult,
    ReadinessDecisionRecord,
    ReadinessEvidenceManifest,
    ReadinessReviewRecord,
)
from kalshi_predictor.live_readiness.catalog import CONTROL_BY_ID, CONTROL_CATALOG, catalog_summary
from kalshi_predictor.live_readiness.contracts import (
    BLOCKING_STATUSES,
    CERTIFICATE_SCHEMA_VERSION,
    CONTROL_CATALOG_VERSION,
    CONTROL_STATUSES,
    DECISION_CONDITIONAL_GO,
    DECISION_GO,
    DECISION_INCOMPLETE,
    DECISION_NO_GO,
    EVALUATOR_VERSION,
    EVIDENCE_MANIFEST_SCHEMA_VERSION,
    READINESS_DECISION_SCHEMA_VERSION,
    REASON_CANCEL_ONLY,
    REASON_CERTIFICATE_DISABLED,
    REASON_CERTIFICATE_EXPIRED,
    REASON_CERTIFICATE_INVALID,
    REASON_CERTIFICATE_REVOKED,
    REASON_CERTIFICATE_SCOPE_MISMATCH,
    REASON_CERTIFICATE_STAGE_MISMATCH,
    REASON_CRITICAL_CONTROL_FAILED,
    REASON_HIGH_CONTROL_FAILED,
    REASON_MANDATORY_CONTROL_NOT_TESTED,
    REASON_ORDER_EXCEEDS_ENVELOPE,
    REASON_REQUIRED_APPROVAL_MISSING,
    SEVERITY_CRITICAL,
    SEVERITY_HIGH,
    STAGE_CONSTRAINED,
    STAGE_FULL,
    STAGE_MICRO,
    STATUS_NA,
    STATUS_NOT_TESTED,
    STATUS_PASS,
    TARGET_ENVIRONMENT,
    TARGET_STAGES,
    LiveReadinessConfig,
    canonical_json,
    parse_required_roles,
    sha256_json,
    sha256_text,
    stable_id,
)
from kalshi_predictor.utils.time import parse_datetime, utc_now

CONTROL_WEIGHTS = {
    SEVERITY_CRITICAL: 5,
    SEVERITY_HIGH: 3,
    "MEDIUM": 2,
    "LOW": 1,
}


def config_from_settings(settings: Settings | None = None) -> LiveReadinessConfig:
    resolved = settings or get_settings()
    config = LiveReadinessConfig(
        enabled=resolved.phase_3v_live_readiness_enabled,
        mode=resolved.phase_3v_mode,
        default_target_stage=resolved.phase_3v_default_target_stage.upper(),
        certificate_issuance_enabled=resolved.phase_3v_certificate_issuance_enabled,
        certificate_max_lifetime_hours=resolved.phase_3v_certificate_max_lifetime_hours,
        evidence_stale_after_days=resolved.phase_3v_evidence_stale_after_days,
        required_approval_roles=parse_required_roles(resolved.phase_3v_required_approval_roles),
        micro_max_contracts_per_order=resolved.phase_3v_micro_max_contracts_per_order,
        constrained_max_contracts_per_order=(
            resolved.phase_3v_constrained_max_contracts_per_order
        ),
        full_max_contracts_per_order=resolved.phase_3v_full_max_contracts_per_order,
    )
    config.validate()
    return config


def evaluate_live_readiness(
    session: Session,
    *,
    settings: Settings | None = None,
    target_stage: str | None = None,
    control_status_overrides: dict[str, str] | None = None,
    evidence_items: list[dict[str, Any]] | None = None,
    approvals: list[dict[str, Any]] | None = None,
    exceptions: list[dict[str, Any]] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    resolved_settings = settings or get_settings()
    config = config_from_settings(resolved_settings)
    stage = (target_stage or config.default_target_stage).upper()
    if stage not in TARGET_STAGES:
        raise ValueError("target_stage must be MICRO, CONSTRAINED, or FULL.")

    generated_at = utc_now()
    overrides = _normalized_overrides(control_status_overrides)
    control_results = [
        _control_result(control.control_id, overrides) for control in CONTROL_CATALOG
    ]
    scope = build_review_scope(resolved_settings, target_stage=stage)
    scope_hash = sha256_json(scope)
    review_id = stable_id("rev", generated_at.isoformat(), scope_hash)
    manifest = build_evidence_manifest(
        review_id=review_id,
        scope_hash=scope_hash,
        evidence_items=evidence_items,
        generated_at=generated_at,
    )
    score = diagnostic_score(control_results)
    reason_codes = _decision_reason_codes(control_results, approvals)
    decision = _decision_from_controls(control_results, approvals, config)
    launch_envelope = None
    certificate_ref = None

    if decision in {DECISION_GO, DECISION_CONDITIONAL_GO}:
        launch_envelope = build_launch_envelope(config, target_stage=stage)
        if config.certificate_issuance_enabled:
            certificate_ref = stable_id("cert", review_id, scope_hash, canonical_json(approvals))
        else:
            decision = DECISION_NO_GO
            reason_codes.append(REASON_CERTIFICATE_DISABLED)
            launch_envelope = None
            certificate_ref = None

    decision_id = stable_id("dec", review_id, decision, canonical_json(control_results))
    decision_payload = {
        "schema_version": READINESS_DECISION_SCHEMA_VERSION,
        "decision_id": decision_id,
        "review_id": review_id,
        "created_at": generated_at.isoformat(),
        "target_environment": TARGET_ENVIRONMENT,
        "target_stage": stage,
        "decision": decision,
        "scope": scope,
        "scope_sha256": scope_hash,
        "catalog_version": CONTROL_CATALOG_VERSION,
        "evaluator_version": EVALUATOR_VERSION,
        "evidence_summary": _evidence_summary(manifest),
        "diagnostic_score": score,
        "gates": _gates(control_results),
        "control_results": control_results,
        "findings": _findings(control_results),
        "exceptions": exceptions or [],
        "approvals": approvals or [],
        "reason_codes": _unique(reason_codes),
        "launch_envelope": launch_envelope,
        "certificate_ref": certificate_ref,
        "re_review_triggers": _re_review_triggers(),
        "report_ref": "reports/live_readiness_report.md",
        "lineage": {
            "evidence_manifest_id": manifest["manifest_id"],
            "evidence_manifest_sha256": manifest["manifest_sha256"],
            "catalog_summary": catalog_summary(),
        },
        "safety": {
            "enables_live_trading": False,
            "enables_demo_execution": False,
            "places_orders": False,
            "changes_risk_limits": False,
            "score_is_decision_authority": False,
        },
    }

    if persist:
        _persist_review(
            session,
            review_id=review_id,
            scope=scope,
            scope_hash=scope_hash,
            manifest=manifest,
            decision_payload=decision_payload,
            control_results=control_results,
            generated_at=generated_at,
        )

    return {
        "review": decision_payload,
        "manifest": manifest,
        "summary": live_readiness_summary_from_decision(decision_payload),
    }


def build_review_scope(settings: Settings, *, target_stage: str) -> dict[str, Any]:
    config_payload = settings.model_dump(mode="json")
    source_hashes = {}
    for path in (
        Path("pyproject.toml"),
        Path("alembic.ini"),
        Path("src/kalshi_predictor/cli.py"),
        Path("src/kalshi_predictor/data/schema.py"),
    ):
        if path.exists():
            source_hashes[str(path)] = sha256_text(path.read_text(encoding="utf-8"))
    return {
        "scope_id": stable_id("scope", target_stage, sha256_json(config_payload), source_hashes),
        "bot_package": "kalshi-predictive-bot",
        "target_environment": TARGET_ENVIRONMENT,
        "target_stage": target_stage,
        "kalshi_env_setting": settings.kalshi_env,
        "database_url_hash": sha256_text(settings.kalshi_db_url),
        "config_hash": sha256_json(config_payload),
        "source_hashes": source_hashes,
        "review_boundaries": {
            "does_not_enable_live_trading": True,
            "does_not_enable_demo_execution": True,
            "does_not_change_trading_behavior": True,
        },
    }


def build_evidence_manifest(
    *,
    review_id: str,
    scope_hash: str,
    evidence_items: list[dict[str, Any]] | None,
    generated_at,
) -> dict[str, Any]:
    items = evidence_items or [
        {
            "evidence_id": stable_id("evi", review_id, "local_empty_manifest"),
            "control_ids": [],
            "status": "MISSING",
            "quality": "UNVERIFIED",
            "contains_secrets": False,
            "description": "No external live-readiness evidence supplied.",
        }
    ]
    normalized = []
    for index, item in enumerate(items, start=1):
        payload = dict(item)
        payload.setdefault("evidence_id", stable_id("evi", review_id, index, payload))
        payload.setdefault("captured_at", generated_at.isoformat())
        payload.setdefault("source_type", "local")
        payload.setdefault("contains_secrets", False)
        payload.setdefault("hash", sha256_json(payload))
        normalized.append(payload)
    manifest = {
        "schema_version": EVIDENCE_MANIFEST_SCHEMA_VERSION,
        "manifest_id": stable_id("evm", review_id, scope_hash, canonical_json(normalized)),
        "review_id": review_id,
        "generated_at": generated_at.isoformat(),
        "frozen_at": generated_at.isoformat(),
        "target_environment": TARGET_ENVIRONMENT,
        "scope_sha256": scope_hash,
        "items": normalized,
    }
    manifest["manifest_sha256"] = sha256_json(manifest)
    return manifest


def diagnostic_score(control_results: list[dict[str, Any]]) -> dict[str, Any]:
    numerator = 0
    denominator = 0
    for row in control_results:
        weight = CONTROL_WEIGHTS[row["severity"]]
        denominator += weight
        if row["status"] in {STATUS_PASS, STATUS_NA}:
            numerator += weight
    value = round((numerator / denominator) * 100, 2) if denominator else 0
    return {
        "score": value,
        "maximum": 100,
        "interpretation": "Diagnostic only. Mandatory hard-veto controls override score.",
    }


def build_launch_envelope(config: LiveReadinessConfig, *, target_stage: str) -> dict[str, Any]:
    caps = {
        STAGE_MICRO: config.micro_max_contracts_per_order,
        STAGE_CONSTRAINED: config.constrained_max_contracts_per_order,
        STAGE_FULL: config.full_max_contracts_per_order,
    }
    max_contracts = min(caps[target_stage], 5)
    return {
        "target_stage": target_stage,
        "max_contracts_per_order": max_contracts,
        "max_total_live_contracts": max_contracts,
        "new_risk_requires_active_certificate": True,
        "phase_3n_final_authority_required": True,
        "cancel_only_on_invalid_certificate": True,
        "raise_risk_limits_allowed": False,
        "auto_reentry_allowed": False,
    }


def issue_live_readiness_certificate(
    decision_payload: dict[str, Any],
    *,
    issuer: str,
    signature_reference: str,
    valid_hours: int = 4,
) -> dict[str, Any]:
    if decision_payload["decision"] not in {DECISION_GO, DECISION_CONDITIONAL_GO}:
        raise ValueError("Only GO or CONDITIONAL_GO decisions can produce a certificate.")
    if not decision_payload.get("launch_envelope"):
        raise ValueError("Certificate requires a launch envelope.")

    issued_at = utc_now()
    certificate = {
        "schema_version": CERTIFICATE_SCHEMA_VERSION,
        "certificate_id": stable_id(
            "cert",
            decision_payload["decision_id"],
            issuer,
            signature_reference,
            issued_at.isoformat(),
        ),
        "review_id": decision_payload["review_id"],
        "decision_id": decision_payload["decision_id"],
        "status": "ACTIVE",
        "target_environment": TARGET_ENVIRONMENT,
        "target_stage": decision_payload["target_stage"],
        "issued_at": issued_at.isoformat(),
        "valid_from": issued_at.isoformat(),
        "expires_at": (issued_at + timedelta(hours=valid_hours)).isoformat(),
        "scope_sha256": decision_payload["scope_sha256"],
        "launch_envelope": decision_payload["launch_envelope"],
        "issuer_safety": {
            "contains_secret_material": False,
            "contains_exchange_write_credentials": False,
            "enables_exchange_writes": False,
        },
        "gateway_policy": {
            "require_active_certificate": True,
            "require_final_phase_3n_allow": True,
            "cancel_only_when_invalid": True,
            "reduce_only_without_fresh_reconciliation": True,
            "no_auto_reentry": True,
        },
        "revocation": {
            "online_required": True,
            "material_changes_invalidate": True,
        },
        "signature": {
            "issuer": issuer,
            "signature_reference": signature_reference,
            "payload_sha256": sha256_json(decision_payload),
        },
    }
    certificate["certificate_sha256"] = sha256_json(certificate)
    return certificate


def verify_certificate_for_order(
    certificate_payload: dict[str, Any] | None,
    *,
    order_intent: dict[str, Any] | None = None,
    expected_scope_sha256: str | None = None,
    expected_stage: str | None = None,
    now=None,
) -> dict[str, Any]:
    evaluated_at = parse_datetime(now) or utc_now()
    intent = order_intent or {}
    reason_codes: list[str] = []
    if not certificate_payload:
        reason_codes.extend([REASON_CERTIFICATE_INVALID, REASON_CANCEL_ONLY])
        return _guard_result(False, True, reason_codes)

    if certificate_payload.get("status") == "REVOKED":
        reason_codes.append(REASON_CERTIFICATE_REVOKED)
    elif certificate_payload.get("status") != "ACTIVE":
        reason_codes.append(REASON_CERTIFICATE_INVALID)

    expires_at = parse_datetime(certificate_payload.get("expires_at"))
    valid_from = parse_datetime(certificate_payload.get("valid_from"))
    if valid_from is None or expires_at is None or not (valid_from <= evaluated_at <= expires_at):
        reason_codes.append(REASON_CERTIFICATE_EXPIRED)

    if expected_scope_sha256 and certificate_payload.get("scope_sha256") != expected_scope_sha256:
        reason_codes.append(REASON_CERTIFICATE_SCOPE_MISMATCH)
    if expected_stage and certificate_payload.get("target_stage") != expected_stage:
        reason_codes.append(REASON_CERTIFICATE_STAGE_MISMATCH)

    envelope = certificate_payload.get("launch_envelope") or {}
    quantity = int(intent.get("quantity") or 0)
    max_contracts = int(envelope.get("max_contracts_per_order") or 0)
    if quantity > max_contracts:
        reason_codes.append(REASON_ORDER_EXCEEDS_ENVELOPE)

    allowed = not reason_codes
    if not allowed:
        reason_codes.append(REASON_CANCEL_ONLY)
    return _guard_result(allowed, True, _unique(reason_codes))


def persist_certificate(session: Session, certificate_payload: dict[str, Any]) -> None:
    row = LiveReadinessCertificate(
        certificate_id=certificate_payload["certificate_id"],
        review_id=certificate_payload["review_id"],
        decision_id=certificate_payload["decision_id"],
        status=certificate_payload["status"],
        target_environment=certificate_payload["target_environment"],
        target_stage=certificate_payload["target_stage"],
        issued_at=parse_datetime(certificate_payload["issued_at"]),
        valid_from=parse_datetime(certificate_payload["valid_from"]),
        expires_at=parse_datetime(certificate_payload["expires_at"]),
        revoked_at=None,
        scope_sha256=certificate_payload["scope_sha256"],
        envelope_sha256=sha256_json(certificate_payload["launch_envelope"]),
        signature_payload_sha256=certificate_payload["signature"]["payload_sha256"],
        certificate_json=canonical_json(certificate_payload),
        raw_json=canonical_json(certificate_payload),
    )
    event = LiveReadinessCertificateEvent(
        event_id=stable_id("cert_evt", row.certificate_id, "ISSUED", row.issued_at),
        certificate_id=row.certificate_id,
        event_type="ISSUED",
        created_at=row.issued_at,
        reason="Certificate issued from signed readiness decision.",
        raw_json=canonical_json({"certificate_id": row.certificate_id, "event_type": "ISSUED"}),
    )
    session.add(row)
    session.add(event)


def live_readiness_summary_from_decision(decision_payload: dict[str, Any]) -> dict[str, Any]:
    control_counts = Counter(row["status"] for row in decision_payload["control_results"])
    critical_blockers = [
        row
        for row in decision_payload["control_results"]
        if row["severity"] == SEVERITY_CRITICAL and row["status"] != STATUS_PASS
    ]
    return {
        "decision": decision_payload["decision"],
        "target_stage": decision_payload["target_stage"],
        "diagnostic_score": decision_payload["diagnostic_score"]["score"],
        "control_count": len(decision_payload["control_results"]),
        "critical_blockers": len(critical_blockers),
        "pass_count": control_counts.get(STATUS_PASS, 0),
        "not_tested_count": control_counts.get(STATUS_NOT_TESTED, 0),
        "reason_codes": decision_payload["reason_codes"],
        "latest_review_id": decision_payload["review_id"],
        "certificate_ref": decision_payload["certificate_ref"],
        "paper_only": True,
    }


def live_readiness_card(session: Session, *, settings: Settings | None = None) -> dict[str, Any]:
    resolved = settings or get_settings()
    latest = session.scalar(
        select(ReadinessDecisionRecord).order_by(desc(ReadinessDecisionRecord.created_at)).limit(1)
    )
    if latest is None:
        return {
            "decision": "INCOMPLETE",
            "mode": resolved.phase_3v_mode.upper(),
            "target_stage": resolved.phase_3v_default_target_stage.upper(),
            "diagnostic_score": "n/a",
            "control_count": catalog_summary()["control_count"],
            "critical_blockers": "unknown",
            "latest_review": "none",
            "certificate": "none",
            "paper_only": True,
            "next_action": "Run kalshi-bot live-readiness-review.",
        }
    payload = canonical_json_to_dict(latest.decision_json)
    summary = live_readiness_summary_from_decision(payload)
    return {
        "decision": summary["decision"],
        "mode": resolved.phase_3v_mode.upper(),
        "target_stage": summary["target_stage"],
        "diagnostic_score": summary["diagnostic_score"],
        "control_count": summary["control_count"],
        "critical_blockers": summary["critical_blockers"],
        "latest_review": summary["latest_review_id"],
        "certificate": summary["certificate_ref"] or "none",
        "paper_only": True,
        "next_action": _next_action(summary["decision"]),
    }


def latest_live_readiness_payload(session: Session) -> dict[str, Any] | None:
    latest = session.scalar(
        select(ReadinessDecisionRecord).order_by(desc(ReadinessDecisionRecord.created_at)).limit(1)
    )
    return canonical_json_to_dict(latest.decision_json) if latest else None


def live_readiness_status(session: Session, *, settings: Settings | None = None) -> dict[str, Any]:
    card = live_readiness_card(session, settings=settings)
    reviews = int(session.scalar(select(func.count()).select_from(ReadinessReviewRecord)) or 0)
    decisions = int(session.scalar(select(func.count()).select_from(ReadinessDecisionRecord)) or 0)
    certificates = int(
        session.scalar(select(func.count()).select_from(LiveReadinessCertificate)) or 0
    )
    return {
        **card,
        "review_count": reviews,
        "decision_count": decisions,
        "certificate_count": certificates,
    }


def live_readiness_panel(session: Session, *, settings: Settings | None = None) -> dict[str, Any]:
    card = live_readiness_card(session, settings=settings)
    return {
        **card,
        "read_only": True,
        "allow_live_execution": False,
        "allow_demo_execution": False,
        "allow_order_create": False,
        "guard_policy": (
            "new risk requires external active certificate; invalid cert is cancel-only"
        ),
    }


def canonical_json_to_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = __import__("json").loads(value)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _control_result(control_id: str, overrides: dict[str, str]) -> dict[str, Any]:
    control = CONTROL_BY_ID[control_id]
    status = overrides.get(control_id, STATUS_NOT_TESTED)
    reason_codes = _control_reason_codes(control, status)
    return {
        **control.as_payload(),
        "status": status,
        "observed_result": _observed_result(status),
        "evidence_ids": [],
        "reason_codes": reason_codes,
        "owner": "unassigned",
        "reviewer": "unassigned",
    }


def _control_reason_codes(control, status: str) -> list[str]:
    if status == STATUS_NOT_TESTED and control.severity in {SEVERITY_CRITICAL, SEVERITY_HIGH}:
        return [REASON_MANDATORY_CONTROL_NOT_TESTED]
    if status in BLOCKING_STATUSES and control.severity == SEVERITY_CRITICAL:
        return [REASON_CRITICAL_CONTROL_FAILED]
    if status in BLOCKING_STATUSES and control.severity == SEVERITY_HIGH:
        return [REASON_HIGH_CONTROL_FAILED]
    return []


def _observed_result(status: str) -> str:
    if status == STATUS_PASS:
        return "Evidence indicates the control passed."
    if status == STATUS_NA:
        return "Control marked not applicable with explicit rationale required."
    if status == STATUS_NOT_TESTED:
        return "No current evidence was supplied for this control."
    return f"Control status is {status}; readiness must fail closed."


def _normalized_overrides(overrides: dict[str, str] | None) -> dict[str, str]:
    normalized = {}
    for control_id, status in (overrides or {}).items():
        if control_id not in CONTROL_BY_ID:
            raise ValueError(f"Unknown readiness control: {control_id}")
        status_value = status.upper()
        if status_value not in CONTROL_STATUSES:
            raise ValueError(f"Unsupported readiness status: {status}")
        normalized[control_id] = status_value
    return normalized


def _decision_from_controls(
    control_results: list[dict[str, Any]],
    approvals: list[dict[str, Any]] | None,
    config: LiveReadinessConfig,
) -> str:
    if any(
        row["severity"] == SEVERITY_CRITICAL and row["status"] in BLOCKING_STATUSES
        for row in control_results
    ):
        return DECISION_NO_GO
    if any(
        row["severity"] == SEVERITY_HIGH and row["status"] in BLOCKING_STATUSES
        for row in control_results
    ):
        return DECISION_NO_GO
    if any(
        row["severity"] in {SEVERITY_CRITICAL, SEVERITY_HIGH}
        and row["status"] == STATUS_NOT_TESTED
        for row in control_results
    ):
        return DECISION_INCOMPLETE
    if not _approval_roles_met(approvals, config.required_approval_roles):
        return DECISION_NO_GO
    if any(row["status"] != STATUS_PASS for row in control_results):
        return DECISION_CONDITIONAL_GO
    return DECISION_GO


def _decision_reason_codes(
    control_results: list[dict[str, Any]],
    approvals: list[dict[str, Any]] | None,
) -> list[str]:
    codes: list[str] = []
    for row in control_results:
        codes.extend(row["reason_codes"])
    if not approvals:
        codes.append(REASON_REQUIRED_APPROVAL_MISSING)
    return _unique(codes)


def _approval_roles_met(
    approvals: list[dict[str, Any]] | None,
    required_roles: tuple[str, ...],
) -> bool:
    if not approvals:
        return False
    approved_roles = {
        str(row.get("role", "")).lower()
        for row in approvals
        if str(row.get("status", "APPROVED")).upper() == "APPROVED"
    }
    return all(role.lower() in approved_roles for role in required_roles)


def _gates(control_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in control_results:
        groups[row["family"]].append(row)
    gates = []
    for family, rows in groups.items():
        status = STATUS_PASS if all(row["status"] == STATUS_PASS for row in rows) else "BLOCKED"
        gates.append(
            {
                "gate_id": stable_id("gate", family),
                "family": family,
                "status": status,
                "critical": any(row["severity"] == SEVERITY_CRITICAL for row in rows),
                "control_ids": [row["control_id"] for row in rows],
                "reason_codes": _unique(
                    code for row in rows for code in row.get("reason_codes", [])
                ),
            }
        )
    return gates


def _findings(control_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings = []
    for row in control_results:
        if row["severity"] not in {SEVERITY_CRITICAL, SEVERITY_HIGH}:
            continue
        if row["status"] == STATUS_PASS:
            continue
        findings.append(
            {
                "finding_id": stable_id("find", row["control_id"], row["status"]),
                "control_id": row["control_id"],
                "severity": row["severity"],
                "status": row["status"],
                "title": row["title"],
                "message": row["observed_result"],
                "owner": row["owner"],
                "due_at": None,
            }
        )
    return findings


def _evidence_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "manifest_id": manifest["manifest_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "item_count": len(manifest["items"]),
        "secret_free": all(not item.get("contains_secrets") for item in manifest["items"]),
    }


def _re_review_triggers() -> list[str]:
    return [
        "Any code, config, model, account, credential, risk limit, or target stage change.",
        "Any failed or stale critical/high evidence item.",
        "Any certificate revocation, expiry, or scope mismatch.",
        "Any incident, exchange API rule change, or data quality fault.",
    ]


def _persist_review(
    session: Session,
    *,
    review_id: str,
    scope: dict[str, Any],
    scope_hash: str,
    manifest: dict[str, Any],
    decision_payload: dict[str, Any],
    control_results: list[dict[str, Any]],
    generated_at,
) -> None:
    session.add(
        ReadinessReviewRecord(
            review_id=review_id,
            lifecycle_state="DECIDED",
            target_environment=TARGET_ENVIRONMENT,
            target_stage=decision_payload["target_stage"],
            created_at=generated_at,
            frozen_at=generated_at,
            decided_at=generated_at,
            scope_json=canonical_json(scope),
            scope_sha256=scope_hash,
            decision=decision_payload["decision"],
            diagnostic_score=str(decision_payload["diagnostic_score"]["score"]),
            reason_codes_json=canonical_json(decision_payload["reason_codes"]),
            raw_json=canonical_json(decision_payload),
        )
    )
    session.add(
        ReadinessEvidenceManifest(
            manifest_id=manifest["manifest_id"],
            review_id=review_id,
            generated_at=parse_datetime(manifest["generated_at"]),
            frozen_at=parse_datetime(manifest["frozen_at"]),
            scope_sha256=scope_hash,
            manifest_sha256=manifest["manifest_sha256"],
            items_json=canonical_json(manifest["items"]),
            raw_json=canonical_json(manifest),
        )
    )
    for row in control_results:
        session.add(
            ReadinessControlResult(
                review_id=review_id,
                control_id=row["control_id"],
                family=row["family"],
                severity=row["severity"],
                status=row["status"],
                evidence_ids_json=canonical_json(row["evidence_ids"]),
                reason_codes_json=canonical_json(row["reason_codes"]),
                observed_result=row["observed_result"],
                owner=row["owner"],
                reviewer=row["reviewer"],
                created_at=generated_at,
                raw_json=canonical_json(row),
            )
        )
    session.add(
        ReadinessDecisionRecord(
            decision_id=decision_payload["decision_id"],
            review_id=review_id,
            decision=decision_payload["decision"],
            created_at=generated_at,
            target_stage=decision_payload["target_stage"],
            diagnostic_score=str(decision_payload["diagnostic_score"]["score"]),
            reason_codes_json=canonical_json(decision_payload["reason_codes"]),
            launch_envelope_json=canonical_json(decision_payload["launch_envelope"]),
            certificate_ref=decision_payload["certificate_ref"],
            decision_json=canonical_json(decision_payload),
            report_path=decision_payload["report_ref"],
            raw_json=canonical_json(decision_payload),
        )
    )


def _guard_result(
    allow_new_or_increasing_risk: bool,
    allow_cancel_only: bool,
    reason_codes: list[str],
) -> dict[str, Any]:
    return {
        "allow_new_or_increasing_risk": allow_new_or_increasing_risk,
        "allow_cancel_only": allow_cancel_only,
        "reason_codes": reason_codes,
        "paper_only_unchanged": True,
        "message": (
            "Certificate valid for new risk."
            if allow_new_or_increasing_risk
            else "Live readiness guard blocks new risk; cancel-only remains allowed."
        ),
    }


def _next_action(decision: str) -> str:
    if decision == DECISION_GO:
        return "Confirm active short-lived certificate and human launch approval."
    if decision == DECISION_CONDITIONAL_GO:
        return "Resolve approved exceptions before increasing stage."
    if decision == DECISION_NO_GO:
        return "Resolve failed mandatory controls and rerun live-readiness-review."
    return "Collect missing evidence and rerun live-readiness-review."


def _unique(values) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
