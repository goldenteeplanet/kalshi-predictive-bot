from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.self_evaluation.contracts import (
    JOURNAL_STATUS_FINAL,
    JOURNAL_STATUS_NO_ACTIVITY,
    JOURNAL_STATUS_PROVISIONAL,
    SCHEMA_VERSION,
    checksum_payload,
    stable_phase_3p_id,
    validate_journal_payload,
)
from kalshi_predictor.self_evaluation.dataset import EvaluationDataset, build_evaluation_dataset
from kalshi_predictor.self_evaluation.findings import FindingSet, build_findings
from kalshi_predictor.self_evaluation.metrics import MetricRecord, build_metric_records
from kalshi_predictor.self_evaluation.renderer import render_journal_markdown
from kalshi_predictor.self_evaluation.repository import (
    existing_evaluation_run,
    latest_journal_for_session,
    load_journal_payload,
    persist_evaluation_run,
    persist_finding_records,
    persist_journal_record,
    persist_metric_records,
)
from kalshi_predictor.self_evaluation.sessions import (
    normalize_evaluation_as_of,
    resolve_trading_session,
)
from kalshi_predictor.utils.time import utc_now


@dataclass(frozen=True)
class SelfEvaluationResult:
    evaluation_run_id: str
    journal_id: str
    journal_revision: int
    journal_status: str
    markdown_path: Path | None
    json_path: Path | None
    payload: dict[str, Any]
    markdown: str
    idempotent: bool = False


def run_self_evaluation(
    session: Session,
    *,
    settings: Settings | None = None,
    session_date: str | date | None = None,
    evaluation_as_of: datetime | str | None = None,
    run_type: str = "NIGHTLY",
    output_path: str | Path | None = None,
    json_output_path: str | Path | None = None,
) -> SelfEvaluationResult:
    resolved = settings or get_settings()
    as_of = normalize_evaluation_as_of(evaluation_as_of, settings=resolved)
    trading_session = resolve_trading_session(
        session_date=session_date,
        evaluation_as_of=as_of,
        settings=resolved,
    )
    dataset = build_evaluation_dataset(
        session,
        trading_session=trading_session,
        evaluation_as_of=as_of,
        data_mode=resolved.phase_3p_data_mode,
    )
    existing = existing_evaluation_run(
        session,
        trading_session_id=trading_session.trading_session_id,
        evaluation_as_of=as_of,
        policy_id=resolved.phase_3p_evaluation_policy_id,
        policy_version=resolved.phase_3p_evaluation_policy_version,
        data_mode=resolved.phase_3p_data_mode,
        source_manifest_hash=str(dataset.manifest["manifest_hash"]),
    )
    if existing and existing.journal_id:
        latest = latest_journal_for_session(
            session,
            trading_session_id=trading_session.trading_session_id,
            policy_id=resolved.phase_3p_evaluation_policy_id,
            policy_version=resolved.phase_3p_evaluation_policy_version,
        )
        payload = load_journal_payload(latest) or {}
        markdown = render_journal_markdown(payload) if payload else ""
        written_md = _write_text(output_path, markdown) if output_path else None
        written_json = (
            _write_text(json_output_path, _json_text(payload))
            if json_output_path
            else None
        )
        return SelfEvaluationResult(
            evaluation_run_id=existing.evaluation_run_id,
            journal_id=existing.journal_id,
            journal_revision=existing.journal_revision,
            journal_status=existing.status,
            markdown_path=written_md,
            json_path=written_json,
            payload=payload,
            markdown=markdown,
            idempotent=True,
        )

    evaluation_run_id = stable_phase_3p_id(
        "run",
        trading_session.trading_session_id,
        as_of.isoformat(),
        resolved.phase_3p_evaluation_policy_id,
        resolved.phase_3p_evaluation_policy_version,
        dataset.manifest["manifest_hash"],
    )
    metrics = build_metric_records(
        session,
        dataset,
        evaluation_run_id=evaluation_run_id,
        settings=resolved,
    )
    finding_set = build_findings(metrics, evaluation_run_id=evaluation_run_id, settings=resolved)
    status = _journal_status(dataset)
    latest = latest_journal_for_session(
        session,
        trading_session_id=trading_session.trading_session_id,
        policy_id=resolved.phase_3p_evaluation_policy_id,
        policy_version=resolved.phase_3p_evaluation_policy_version,
    )
    revision = (latest.journal_revision + 1) if latest else 1
    journal_id = stable_phase_3p_id(
        "journal",
        trading_session.trading_session_id,
        resolved.phase_3p_evaluation_policy_id,
        resolved.phase_3p_evaluation_policy_version,
        revision,
    )
    generated_at = utc_now()
    payload = build_journal_payload(
        dataset=dataset,
        metrics=metrics,
        finding_set=finding_set,
        evaluation_run_id=evaluation_run_id,
        journal_id=journal_id,
        journal_revision=revision,
        journal_status=status,
        generated_at=generated_at,
        settings=resolved,
        supersedes_journal_id=latest.journal_id if latest else None,
    )
    validate_journal_payload(payload)
    markdown = render_journal_markdown(payload)
    written_md = _write_text(output_path, markdown) if output_path else None
    written_json = _write_text(json_output_path, _json_text(payload)) if json_output_path else None
    payload_checksum = checksum_payload(payload)
    markdown_checksum = checksum_payload(markdown)
    completed_at = utc_now()
    persist_evaluation_run(
        session,
        evaluation_run_id=evaluation_run_id,
        trading_session={
            "trading_session_id": trading_session.trading_session_id,
            "session_label": trading_session.session_label,
            "session_timezone": trading_session.session_timezone,
            "session_open_at": trading_session.session_open_at,
            "session_close_at": trading_session.session_close_at,
        },
        evaluation_as_of=as_of,
        generated_at=generated_at,
        completed_at=completed_at,
        run_type=run_type,
        status=status,
        policy_id=resolved.phase_3p_evaluation_policy_id,
        policy_version=resolved.phase_3p_evaluation_policy_version,
        data_mode=resolved.phase_3p_data_mode,
        manifest=dataset.manifest,
        journal_id=journal_id,
        journal_revision=revision,
        summary=payload["coverage_summary"],
    )
    persist_metric_records(
        session,
        evaluation_run_id=evaluation_run_id,
        trading_session_id=trading_session.trading_session_id,
        metrics=metrics,
    )
    persist_finding_records(
        session,
        evaluation_run_id=evaluation_run_id,
        trading_session_id=trading_session.trading_session_id,
        findings=_all_findings(finding_set),
    )
    persist_journal_record(
        session,
        journal_id=journal_id,
        evaluation_run_id=evaluation_run_id,
        trading_session_id=trading_session.trading_session_id,
        journal_revision=revision,
        journal_status=status,
        schema_version=SCHEMA_VERSION,
        policy_id=resolved.phase_3p_evaluation_policy_id,
        policy_version=resolved.phase_3p_evaluation_policy_version,
        generated_at=generated_at,
        evaluation_as_of=as_of,
        payload_checksum=payload_checksum,
        markdown_checksum=markdown_checksum,
        markdown_path=str(written_md) if written_md else None,
        payload=payload,
        supersedes_journal_id=latest.journal_id if latest else None,
        revision_reason_codes=_revision_reason_codes(latest, dataset),
    )
    return SelfEvaluationResult(
        evaluation_run_id=evaluation_run_id,
        journal_id=journal_id,
        journal_revision=revision,
        journal_status=status,
        markdown_path=written_md,
        json_path=written_json,
        payload=payload,
        markdown=markdown,
        idempotent=False,
    )


def build_journal_payload(
    *,
    dataset: EvaluationDataset,
    metrics: list[MetricRecord],
    finding_set: FindingSet,
    evaluation_run_id: str,
    journal_id: str,
    journal_revision: int,
    journal_status: str,
    generated_at: datetime,
    settings: Settings,
    supersedes_journal_id: str | None,
) -> dict[str, Any]:
    metric_payloads = [metric.as_payload() for metric in metrics]
    worked = [finding.as_payload() for finding in finding_set.what_worked]
    failed = [finding.as_payload() for finding in finding_set.what_failed]
    changed = [finding.as_payload() for finding in finding_set.what_changed]
    watch = [finding.as_payload() for finding in finding_set.watch_items]
    data_quality = [finding.as_payload() for finding in finding_set.data_quality_items]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "journal_id": journal_id,
        "evaluation_run_id": evaluation_run_id,
        "journal_revision": journal_revision,
        "journal_status": journal_status,
        "trading_session": dataset.trading_session.as_payload(),
        "evaluation_as_of": dataset.evaluation_as_of.isoformat(),
        "generated_at": generated_at.isoformat(),
        "data_mode": dataset.data_mode,
        "source_manifest_summary": dataset.manifest,
        "coverage_summary": _coverage_summary(dataset),
        "headline": _headline(worked, failed, changed, journal_status),
        "executive_summary": _executive_summary(dataset, worked, failed, changed),
        "what_worked": worked,
        "what_failed": failed,
        "what_changed": changed,
        "watch_items": watch,
        "data_quality_items": data_quality,
        "risk_and_sizing_summary": _section_summary(
            metrics,
            finding_set,
            "risk_and_sizing",
            "Phase 3M/3N distributions were evaluated without changing sizing or risk controls.",
        ),
        "forecast_and_opportunity_summary": _section_summary(
            metrics,
            finding_set,
            "forecast_and_opportunity",
            "Forecasts and opportunity scores were evaluated with pending labels excluded.",
        ),
        "trade_and_execution_summary": _section_summary(
            metrics,
            finding_set,
            "trade_and_execution",
            "Trade metrics are separated by execution mode and exclude open trades.",
        ),
        "model_and_version_summary": _section_summary(
            metrics,
            finding_set,
            "model_and_version",
            "Model and feature lineage changes were checked from Phase 3O records.",
        ),
        "key_metrics": metric_payloads,
        "unresolved_outcomes": _unresolved(dataset),
        "recommended_follow_ups": finding_set.recommended_follow_ups,
        "caveats": _caveats(settings),
        "evidence_appendix": {
            "metric_record_ids": [metric.metric_record_id for metric in metrics],
            "finding_ids": [finding["finding_id"] for finding in worked + failed + changed],
            "source_references": dataset.source_references[:500],
            "excluded_rows_by_reason": dataset.excluded_after_cutoff,
            "notes": [
                "Phase 3P is read-only and cannot mutate trading, sizing, risk, or model config.",
                "Counterfactual metrics are not generated without a versioned simulator.",
            ],
        },
    }
    if supersedes_journal_id:
        payload["supersedes_journal_id"] = supersedes_journal_id
    return payload


def _journal_status(dataset: EvaluationDataset) -> str:
    if not dataset.forecast_rows and not dataset.trade_rows:
        return JOURNAL_STATUS_NO_ACTIVITY
    if dataset.pending_forecasts or dataset.open_trades:
        return JOURNAL_STATUS_PROVISIONAL
    return JOURNAL_STATUS_FINAL


def _coverage_summary(dataset: EvaluationDataset) -> dict[str, Any]:
    eligible_forecasts = len(dataset.forecast_rows)
    finalized_forecasts = len(dataset.finalized_forecasts)
    eligible_trades = len(dataset.trade_rows)
    finalized_trades = len(dataset.finalized_trades)
    market_linked = sum(1 for row in dataset.forecast_rows if row.market_memory_id)
    flags = []
    if dataset.pending_forecasts or dataset.open_trades:
        flags.append("PENDING_OUTCOMES")
    if eligible_forecasts and market_linked < eligible_forecasts:
        flags.append("MISSING_MARKET_LINKS")
    if any(dataset.excluded_after_cutoff.values()):
        flags.append("ROWS_EXCLUDED_AFTER_CUTOFF")
    reliability = "HIGH"
    if flags:
        reliability = "MEDIUM" if finalized_forecasts or finalized_trades else "LOW"
    if not eligible_forecasts and not eligible_trades:
        reliability = "INSUFFICIENT_DATA"
    return {
        "eligible_forecasts": eligible_forecasts,
        "finalized_forecasts": finalized_forecasts,
        "pending_forecasts": len(dataset.pending_forecasts),
        "eligible_trades": eligible_trades,
        "finalized_trades": finalized_trades,
        "open_trades": len(dataset.open_trades),
        "forecast_lineage_complete_rate": _ratio(
            eligible_forecasts
            - sum(1 for row in dataset.forecast_rows if not row.primary_model_version),
            eligible_forecasts,
        ),
        "market_snapshot_link_rate": _ratio(market_linked, eligible_forecasts),
        "trade_settlement_complete_rate": _ratio(finalized_trades, eligible_trades),
        "reliability_grade": reliability,
        "quality_flags": flags,
    }


def _headline(
    worked: list[dict[str, Any]],
    failed: list[dict[str, Any]],
    changed: list[dict[str, Any]],
    status: str,
) -> str:
    if status == JOURNAL_STATUS_NO_ACTIVITY:
        return "No forecast or trade activity was available for this session."
    parts = []
    if worked:
        parts.append(f"{len(worked)} supported improvement signal")
    if failed:
        parts.append(f"{len(failed)} supported failure signal")
    if changed:
        parts.append(f"{len(changed)} deterministic change")
    return "; ".join(parts) + "." if parts else "No supported worked/failed/changed verdict."


def _executive_summary(
    dataset: EvaluationDataset,
    worked: list[dict[str, Any]],
    failed: list[dict[str, Any]],
    changed: list[dict[str, Any]],
) -> str:
    return (
        "Phase 3P evaluated "
        f"{len(dataset.forecast_rows)} forecasts and {len(dataset.trade_rows)} trades from "
        "Phase 3O memory using a frozen cutoff. "
        f"Supported findings: worked={len(worked)}, failed={len(failed)}, changed={len(changed)}. "
        "Pending labels and open trades are excluded from finalized performance metrics."
    )


def _section_summary(
    metrics: list[MetricRecord],
    finding_set: FindingSet,
    section: str,
    fallback: str,
) -> dict[str, Any]:
    section_metrics = [metric for metric in metrics if metric.section == section]
    section_metric_ids = {metric.metric_record_id for metric in section_metrics}
    section_findings = [
        finding
        for finding in _all_findings(finding_set)
        if finding.primary_metric_record_id in section_metric_ids
    ]
    return {
        "summary": _summary_sentence(section, section_metrics, section_findings, fallback),
        "metric_record_ids": [metric.metric_record_id for metric in section_metrics],
        "finding_ids": [finding.finding_id for finding in section_findings],
    }


def _summary_sentence(
    section: str,
    metrics: list[MetricRecord],
    findings: list[Any],
    fallback: str,
) -> str:
    if findings:
        return f"{section.replace('_', ' ').title()} produced {len(findings)} finding(s)."
    if metrics:
        return fallback
    return f"No authoritative {section.replace('_', ' ')} records were available."


def _unresolved(dataset: EvaluationDataset) -> dict[str, Any]:
    return {
        "pending_forecasts": len(dataset.pending_forecasts),
        "open_trades": len(dataset.open_trades),
        "preliminary_settlements": len(dataset.open_trades),
        "late_or_missing_partitions": sum(dataset.excluded_after_cutoff.values()),
        "may_change_metrics": [
            "forecast.direction_accuracy",
            "forecast.brier_score",
            "trade.net_pnl.total",
        ]
        if dataset.pending_forecasts or dataset.open_trades
        else [],
        "next_revision_condition": (
            "Late/final outcomes, source corrections, or lineage repairs change the "
            "source manifest."
        ),
    }


def _caveats(settings: Settings) -> list[str]:
    return [
        (
            "No authoritative holiday or early-close session calendar was found; "
            "Phase 3P uses a full local calendar-day session boundary."
        ),
        (
            "Counterfactual metrics are unsupported until a versioned simulator/fill "
            "model is available."
        ),
        (
            "Operational telemetry and publication alerting are summarized only where "
            "existing source rows expose the data."
        ),
        (
            f"Policy {settings.phase_3p_evaluation_policy_id} "
            f"v{settings.phase_3p_evaluation_policy_version} is scaffolding and "
            "should be reviewed before production publication."
        ),
    ]


def _revision_reason_codes(latest: Any | None, dataset: EvaluationDataset) -> list[str]:
    if latest is None:
        return ["INITIAL_JOURNAL"]
    previous = load_journal_payload(latest) or {}
    previous_manifest = previous.get("source_manifest_summary") or {}
    if previous_manifest.get("manifest_hash") != dataset.manifest.get("manifest_hash"):
        return ["SOURCE_MANIFEST_CHANGED"]
    return ["MANUAL_REPLAY"]


def _all_findings(finding_set: FindingSet) -> list[Any]:
    return (
        finding_set.what_worked
        + finding_set.what_failed
        + finding_set.what_changed
        + finding_set.watch_items
        + finding_set.data_quality_items
    )


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def _write_text(path: str | Path | None, text: str) -> Path | None:
    if path is None:
        return None
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    return output


def _json_text(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, indent=2, sort_keys=True, default=str)
