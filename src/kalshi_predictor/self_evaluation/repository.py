from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json, encode_json
from kalshi_predictor.data.schema import (
    SelfEvaluationFinding,
    SelfEvaluationJournal,
    SelfEvaluationMetric,
    SelfEvaluationRun,
)
from kalshi_predictor.self_evaluation.contracts import FindingRecord, MetricRecord
from kalshi_predictor.utils.time import utc_now


def existing_evaluation_run(
    session: Session,
    *,
    trading_session_id: str,
    evaluation_as_of: datetime,
    policy_id: str,
    policy_version: str,
    data_mode: str,
    source_manifest_hash: str,
) -> SelfEvaluationRun | None:
    return session.scalar(
        select(SelfEvaluationRun)
        .where(SelfEvaluationRun.trading_session_id == trading_session_id)
        .where(SelfEvaluationRun.policy_id == policy_id)
        .where(SelfEvaluationRun.policy_version == policy_version)
        .where(SelfEvaluationRun.data_mode == data_mode)
        .where(SelfEvaluationRun.source_manifest_hash == source_manifest_hash)
        .limit(1)
    )


def latest_journal_for_session(
    session: Session,
    *,
    trading_session_id: str,
    policy_id: str,
    policy_version: str,
) -> SelfEvaluationJournal | None:
    return session.scalar(
        select(SelfEvaluationJournal)
        .where(SelfEvaluationJournal.trading_session_id == trading_session_id)
        .where(SelfEvaluationJournal.policy_id == policy_id)
        .where(SelfEvaluationJournal.policy_version == policy_version)
        .order_by(
            desc(SelfEvaluationJournal.journal_revision),
            desc(SelfEvaluationJournal.created_at),
        )
        .limit(1)
    )


def persist_evaluation_run(
    session: Session,
    *,
    evaluation_run_id: str,
    trading_session: dict[str, Any],
    evaluation_as_of: datetime,
    generated_at: datetime,
    completed_at: datetime,
    run_type: str,
    status: str,
    policy_id: str,
    policy_version: str,
    data_mode: str,
    manifest: dict[str, Any],
    journal_id: str,
    journal_revision: int,
    summary: dict[str, Any],
) -> SelfEvaluationRun:
    row = SelfEvaluationRun(
        evaluation_run_id=evaluation_run_id,
        trading_session_id=trading_session["trading_session_id"],
        session_label=trading_session["session_label"],
        session_timezone=trading_session["session_timezone"],
        session_open_at=trading_session["session_open_at"],
        session_close_at=trading_session["session_close_at"],
        evaluation_as_of=evaluation_as_of,
        generated_at=generated_at,
        completed_at=completed_at,
        run_type=run_type,
        status=status,
        policy_id=policy_id,
        policy_version=policy_version,
        data_mode=data_mode,
        source_manifest_hash=str(manifest["manifest_hash"]),
        input_checksum=str(manifest["input_checksum"]),
        journal_id=journal_id,
        journal_revision=journal_revision,
        summary_json=encode_json(summary),
        source_manifest_json=encode_json(manifest),
        raw_json=encode_json(
            {
                "trading_session": trading_session,
                "summary": summary,
                "source_manifest": manifest,
            }
        ),
    )
    session.add(row)
    session.flush()
    return row


def persist_metric_records(
    session: Session,
    *,
    evaluation_run_id: str,
    trading_session_id: str,
    metrics: list[MetricRecord],
) -> list[SelfEvaluationMetric]:
    created_at = utc_now()
    rows = []
    for metric in metrics:
        payload = metric.as_payload()
        row = SelfEvaluationMetric(
            metric_record_id=metric.metric_record_id,
            evaluation_run_id=evaluation_run_id,
            trading_session_id=trading_session_id,
            metric_name=metric.metric_name,
            metric_version=metric.metric_version,
            metric_type=metric.metric_type,
            section=metric.section,
            cohort_json=encode_json(metric.cohort),
            value=metric.value,
            unit=metric.unit,
            sample_size=metric.sample_size,
            finalized_count=metric.finalized_count,
            pending_count=metric.pending_count,
            baseline_json=encode_json(metric.baseline.as_payload()),
            reliability_grade=metric.reliability_grade,
            evidence_json=encode_json(metric.evidence_references),
            created_at=created_at,
            raw_json=encode_json(payload),
        )
        session.add(row)
        rows.append(row)
    session.flush()
    return rows


def persist_finding_records(
    session: Session,
    *,
    evaluation_run_id: str,
    trading_session_id: str,
    findings: list[FindingRecord],
) -> list[SelfEvaluationFinding]:
    created_at = utc_now()
    rows = []
    for finding in findings:
        payload = finding.as_payload()
        row = SelfEvaluationFinding(
            finding_id=finding.finding_id,
            evaluation_run_id=evaluation_run_id,
            trading_session_id=trading_session_id,
            finding_type=finding.finding_type,
            finding_subtype=finding.finding_subtype,
            severity=finding.severity,
            status=finding.status,
            title=finding.title,
            concise_statement=finding.concise_statement,
            detailed_explanation=finding.detailed_explanation,
            primary_metric_record_id=finding.primary_metric_record_id,
            sample_size=finding.sample_size,
            current_value=finding.current_value,
            baseline_value=finding.baseline.value,
            effect_size=finding.effect_size,
            reliability_grade=finding.reliability_grade,
            evidence_type=finding.evidence_type,
            attribution_level=finding.attribution_level,
            evidence_references_json=encode_json(finding.evidence_references),
            reason_codes_json=encode_json(finding.reason_codes),
            recommended_follow_up_ids_json=encode_json(finding.recommended_follow_up_ids),
            created_at=created_at,
            raw_json=encode_json(payload),
        )
        session.add(row)
        rows.append(row)
    session.flush()
    return rows


def persist_journal_record(
    session: Session,
    *,
    journal_id: str,
    evaluation_run_id: str,
    trading_session_id: str,
    journal_revision: int,
    journal_status: str,
    schema_version: str,
    policy_id: str,
    policy_version: str,
    generated_at: datetime,
    evaluation_as_of: datetime,
    payload_checksum: str,
    markdown_checksum: str,
    markdown_path: str | None,
    payload: dict[str, Any],
    supersedes_journal_id: str | None,
    revision_reason_codes: list[str],
) -> SelfEvaluationJournal:
    row = SelfEvaluationJournal(
        journal_id=journal_id,
        evaluation_run_id=evaluation_run_id,
        trading_session_id=trading_session_id,
        journal_revision=journal_revision,
        journal_status=journal_status,
        schema_version=schema_version,
        policy_id=policy_id,
        policy_version=policy_version,
        generated_at=generated_at,
        evaluation_as_of=evaluation_as_of,
        payload_checksum=payload_checksum,
        markdown_checksum=markdown_checksum,
        markdown_path=markdown_path,
        payload_json=encode_json(payload),
        supersedes_journal_id=supersedes_journal_id,
        revision_reason_codes_json=encode_json(revision_reason_codes),
        created_at=utc_now(),
    )
    session.add(row)
    session.flush()
    return row


def load_journal_payload(row: SelfEvaluationJournal | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return decode_json(row.payload_json)
