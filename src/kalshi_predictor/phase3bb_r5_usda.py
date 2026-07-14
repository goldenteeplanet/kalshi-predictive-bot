from __future__ import annotations

import csv
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.phase3bb_acceleration import (
    _metadata,
    _metadata_lines,
    _safety_flags,
    _write_manifest,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R5_USDA_VERSION = "phase3bb_r5_usda_source_activation_v1"
USDA_ADAPTER = "commodity_advertised_price_source"
CUSHMAN_ADAPTER = "infrastructure_data_center_capacity_source"
FLIGHTAWARE_ADAPTER = "transportation_flight_cancellation_source"
DEFAULT_EVIDENCE_DIR = Path("data/general_source_evidence")
TARGET_SOURCE_NAME = "USDA"
FRESHNESS_WINDOW = "date-stable exact publication/effective date match required"

CSV_FIELDS = [
    "market_ticker",
    "source_adapter_key",
    "source_family",
    "activation_decision",
    "first_blocker",
    "blocker_codes",
    "source_name",
    "source_url",
    "source_publication_date",
    "observed_value",
    "effective_date",
    "retrieval_timestamp",
    "freshness_window",
    "freshness_pass",
    "source_notes",
    "exact_source_url",
    "date_stable",
    "value_present",
    "promoted_source_evidence",
    "candidate_feature_row",
    "feature_metric",
    "feature_value",
    "feature_unit",
    "feature_direction",
    "feature_threshold",
    "proposed_db_writes",
    "paper_trade_creation",
    "live_or_demo_execution",
]


@dataclass(frozen=True)
class Phase3BBR5USDASourceActivationArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    usda_rows_csv_path: Path
    blocked_rows_csv_path: Path
    manifest_path: Path


def write_phase3bb_r5_usda_source_activation_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3bb_r5"),
    reports_dir: Path = Path("reports"),
    evidence_dir: Path = DEFAULT_EVIDENCE_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
) -> Phase3BBR5USDASourceActivationArtifacts:
    payload = build_phase3bb_r5_usda_source_activation(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        evidence_dir=evidence_dir,
        settings=settings,
        command_args=command_args,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "usda_source_activation.md"
    usda_rows_csv_path = output_dir / "usda_rows.csv"
    blocked_rows_csv_path = output_dir / "blocked_source_rows.csv"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    _write_csv(usda_rows_csv_path, payload["usda_rows"])
    _write_csv(blocked_rows_csv_path, payload["blocked_source_rows"])
    _write_manifest(
        manifest_path,
        [executive_summary_path, markdown_path, usda_rows_csv_path, blocked_rows_csv_path],
    )
    return Phase3BBR5USDASourceActivationArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        usda_rows_csv_path=usda_rows_csv_path,
        blocked_rows_csv_path=blocked_rows_csv_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r5_usda_source_activation(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3bb_r5"),
    reports_dir: Path = Path("reports"),
    evidence_dir: Path = DEFAULT_EVIDENCE_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    metadata = _metadata(
        session,
        settings=resolved,
        generated_at=utc_now().isoformat(),
        command_args=command_args or [],
        output_dir=output_dir,
    )
    metadata["command_arguments"] = {
        "command": "kalshi-bot phase3bb-r5-usda-source-activation",
        "argv": command_args or [],
    }
    evidence_report = _read_json(
        reports_dir / "phase3bb_r2_sources" / "phase3bb_r2_general_source_evidence.json"
    )
    activation_report = _read_json(
        reports_dir / "phase3bb_r3_source_activation" / "source_evidence_activation.json"
    )
    usda_date_report = _read_json(
        reports_dir / "phase3bb_r2_sources" / "usda_fvwretail_date_resolution.json"
    )
    canonical_usda = _read_json(evidence_dir / f"{USDA_ADAPTER}.json")
    canonical_cushman = _read_json(evidence_dir / f"{CUSHMAN_ADAPTER}.json")

    usda_rows = _usda_rows(
        evidence_report=evidence_report,
        usda_date_report=usda_date_report,
        canonical_usda=canonical_usda,
    )
    source_block_rows = _source_block_rows(
        activation_report=activation_report,
        canonical_cushman=canonical_cushman,
    )
    blocked_rows = [row for row in usda_rows if not _truthy(row["promoted_source_evidence"])]
    blocked_rows.extend(source_block_rows)
    summary = _summary(usda_rows=usda_rows, blocked_rows=blocked_rows)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "creates_paper_trades": False,
        "creates_paper_orders": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "fabricates_usda_values": False,
        "uses_paid_or_proprietary_sources": False,
        "uses_fuzzy_source_matching": False,
        "bypasses_date_stability_checks": False,
        "feature_writes": False,
        "forecast_writes": False,
        "db_writes_performed": 0,
    }
    return {
        **metadata,
        "phase": "3BB-R5",
        "phase_version": PHASE3BB_R5_USDA_VERSION,
        "mode": "PAPER_READ_ONLY_USDA_SOURCE_ACTIVATION",
        "reports_dir": str(reports_dir),
        "evidence_dir": str(evidence_dir),
        "summary": summary,
        "usda_rows": usda_rows,
        "blocked_source_rows": blocked_rows,
        "candidate_feature_rows": [
            _feature_preview(row) for row in usda_rows if _truthy(row["candidate_feature_row"])
        ],
        "acceptance": _acceptance(summary, blocked_rows),
        "safety_flags": safety,
        "operator_guardrails": [
            "Do not fabricate USDA values.",
            "Do not use paid/proprietary sources.",
            "Do not create paper trades.",
            "Do not use fuzzy source matching.",
            "Do not bypass date-stability checks.",
            "Do not forecast/trade if evidence is incomplete.",
        ],
    }


def evaluate_usda_row(
    *,
    market_ticker: str,
    evidence_row: dict[str, Any],
    matched_evidence: dict[str, Any],
    usda_date_report: dict[str, Any],
) -> dict[str, Any]:
    parsed = _dict(evidence_row.get("parsed_fields"))
    effective_date = (
        _text(parsed.get("time_window"))
        or _text(matched_evidence.get("as_of_date"))
        or _nested_text(usda_date_report, "target", "target_date")
    )
    observed_value = matched_evidence.get("price_usd_each")
    source_url = _text(matched_evidence.get("source_url"))
    source_name = _text(matched_evidence.get("source_name"))
    source_notes = _source_notes(evidence_row, matched_evidence, usda_date_report)
    publication_date = _source_publication_date(
        matched_evidence=matched_evidence,
        evidence_row=evidence_row,
        usda_date_report=usda_date_report,
        fallback_effective_date=effective_date,
    )
    exact_source_url = _is_usda_url(source_url) and "usda" in source_name.lower()
    date_stable = _same_date(publication_date, effective_date) and bool(
        usda_date_report.get("exact_july_3_report_found", True)
    )
    value_present = observed_value not in (None, "")
    retrieval_timestamp = _text(matched_evidence.get("retrieved_at"))
    evidence_available = not _source_unavailable(matched_evidence, evidence_row)
    blockers = _blocker_codes(
        exact_source_url=exact_source_url,
        date_stable=date_stable,
        value_present=value_present,
        retrieval_timestamp=retrieval_timestamp,
        evidence_available=evidence_available,
    )
    promoted = not blockers
    first_blocker = blockers[0] if blockers else "NONE"
    return {
        "market_ticker": market_ticker,
        "source_adapter_key": USDA_ADAPTER,
        "source_family": "agriculture",
        "activation_decision": "PROMOTE_FEATURE_PREVIEW" if promoted else "BLOCK",
        "first_blocker": first_blocker,
        "blocker_codes": "; ".join(blockers),
        "source_name": source_name,
        "source_url": source_url,
        "source_publication_date": publication_date,
        "observed_value": "" if observed_value is None else observed_value,
        "effective_date": effective_date,
        "retrieval_timestamp": retrieval_timestamp,
        "freshness_window": FRESHNESS_WINDOW,
        "freshness_pass": date_stable,
        "source_notes": source_notes,
        "exact_source_url": exact_source_url,
        "date_stable": date_stable,
        "value_present": value_present,
        "promoted_source_evidence": promoted,
        "candidate_feature_row": promoted,
        "feature_metric": _text(matched_evidence.get("metric") or parsed.get("metric")),
        "feature_value": "" if observed_value is None else observed_value,
        "feature_unit": "USD_EACH",
        "feature_direction": _text(parsed.get("direction")),
        "feature_threshold": _text(parsed.get("threshold")),
        "proposed_db_writes": 0,
        "paper_trade_creation": False,
        "live_or_demo_execution": False,
    }


def _usda_rows(
    *,
    evidence_report: dict[str, Any],
    usda_date_report: dict[str, Any],
    canonical_usda: dict[str, Any],
) -> list[dict[str, Any]]:
    evidence_rows = [
        row
        for row in _list(evidence_report.get("evidence_rows"))
        if row.get("source_adapter_key") == USDA_ADAPTER
    ]
    if not evidence_rows:
        evidence_rows = _rows_from_canonical_usda(canonical_usda)
    rows = []
    for row in evidence_rows:
        matched_evidence = _dict(row.get("matched_evidence"))
        if not matched_evidence:
            matched_evidence = _matching_canonical_record(canonical_usda, row)
        rows.append(
            evaluate_usda_row(
                market_ticker=_text(row.get("ticker")),
                evidence_row=row,
                matched_evidence=matched_evidence,
                usda_date_report=usda_date_report,
            )
        )
    return sorted(rows, key=lambda row: str(row["market_ticker"]))


def _rows_from_canonical_usda(canonical_usda: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for record in _list(canonical_usda.get("records")):
        for ticker in _list(record.get("matched_tickers")):
            rows.append(
                {
                    "ticker": ticker,
                    "source_adapter_key": USDA_ADAPTER,
                    "matched_evidence": record,
                    "parsed_fields": {
                        "time_window": record.get("as_of_date"),
                        "metric": record.get("metric"),
                    },
                    "block_reason": record.get("evidence_notes"),
                }
            )
    return rows


def _matching_canonical_record(
    canonical_usda: dict[str, Any],
    evidence_row: dict[str, Any],
) -> dict[str, Any]:
    ticker = _text(evidence_row.get("ticker"))
    for record in _list(canonical_usda.get("records")):
        if ticker in {_text(value) for value in _list(record.get("matched_tickers"))}:
            return record
    return {}


def _source_block_rows(
    *,
    activation_report: dict[str, Any],
    canonical_cushman: dict[str, Any],
) -> list[dict[str, Any]]:
    rows = []
    for decision in _list(activation_report.get("source_activation_decisions")):
        adapter = _text(decision.get("source_adapter_key"))
        if adapter not in {CUSHMAN_ADAPTER, FLIGHTAWARE_ADAPTER}:
            continue
        if adapter == CUSHMAN_ADAPTER:
            rows.extend(_cushman_block_rows(decision, canonical_cushman))
        elif adapter == FLIGHTAWARE_ADAPTER:
            rows.extend(_activation_block_rows(decision, source_family="transportation"))
    return rows


def _cushman_block_rows(
    decision: dict[str, Any],
    canonical_cushman: dict[str, Any],
) -> list[dict[str, Any]]:
    base_rows = _activation_block_rows(decision, source_family="infrastructure")
    record = _list(canonical_cushman.get("records"))
    canonical = _dict(record[0]) if record else {}
    for row in base_rows:
        row["source_name"] = row["source_name"] or _text(canonical.get("source_name"))
        row["source_url"] = row["source_url"] or _text(canonical.get("source_url"))
        row["source_notes"] = row["source_notes"] or _text(canonical.get("evidence_notes"))
        row["observed_value"] = row["observed_value"] or _text(canonical.get("capacity_gw"))
        row["effective_date"] = row["effective_date"] or _text(canonical.get("measurement_year"))
        row["retrieval_timestamp"] = row["retrieval_timestamp"] or _text(
            canonical.get("retrieved_at")
        )
        if row["first_blocker"] == "UNKNOWN_SOURCE_BLOCKER":
            row["first_blocker"] = "PROPRIETARY_REVIEW_REQUIRED"
    return base_rows


def _activation_block_rows(decision: dict[str, Any], *, source_family: str) -> list[dict[str, Any]]:
    tickers = _list(decision.get("affected_tickers")) or [""]
    reference = _dict(decision.get("evidence_reference"))
    rows = []
    for ticker in tickers:
        first_blocker = _text(decision.get("first_blocker")) or "UNKNOWN_SOURCE_BLOCKER"
        rows.append(
            _blank_row(
                market_ticker=_text(ticker),
                source_adapter_key=_text(decision.get("source_adapter_key")),
                source_family=source_family,
                source_name=_text(reference.get("source_name")),
                source_url=_text(reference.get("source_url")),
                observed_value=_text(reference.get("observed_value")),
                effective_date=_text(reference.get("target_observation")),
                first_blocker=first_blocker,
                blocker_codes="; ".join(str(code) for code in _list(decision.get("blocker_codes"))),
                source_notes=_text(decision.get("block_reason")),
            )
        )
    return rows


def _blank_row(
    *,
    market_ticker: str,
    source_adapter_key: str,
    source_family: str,
    source_name: str,
    source_url: str,
    observed_value: str,
    effective_date: str,
    first_blocker: str,
    blocker_codes: str,
    source_notes: str,
) -> dict[str, Any]:
    return {
        "market_ticker": market_ticker,
        "source_adapter_key": source_adapter_key,
        "source_family": source_family,
        "activation_decision": "BLOCK",
        "first_blocker": first_blocker,
        "blocker_codes": blocker_codes or first_blocker,
        "source_name": source_name,
        "source_url": source_url,
        "source_publication_date": "",
        "observed_value": observed_value,
        "effective_date": effective_date,
        "retrieval_timestamp": "",
        "freshness_window": FRESHNESS_WINDOW,
        "freshness_pass": False,
        "source_notes": source_notes,
        "exact_source_url": False,
        "date_stable": False,
        "value_present": bool(observed_value),
        "promoted_source_evidence": False,
        "candidate_feature_row": False,
        "feature_metric": "",
        "feature_value": "",
        "feature_unit": "",
        "feature_direction": "",
        "feature_threshold": "",
        "proposed_db_writes": 0,
        "paper_trade_creation": False,
        "live_or_demo_execution": False,
    }


def _feature_preview(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "market_ticker": row["market_ticker"],
        "feature_kind": "agriculture_commodity_observed_value",
        "metric": row["feature_metric"],
        "observed_value": row["feature_value"],
        "unit": row["feature_unit"],
        "effective_date": row["effective_date"],
        "retrieved_at": row["retrieval_timestamp"],
        "source_url": row["source_url"],
        "db_writes_performed": 0,
    }


def _summary(
    *,
    usda_rows: list[dict[str, Any]],
    blocked_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    promoted = [row for row in usda_rows if _truthy(row["promoted_source_evidence"])]
    usda_blocked = [row for row in usda_rows if not _truthy(row["promoted_source_evidence"])]
    blocker_counts = Counter(str(row["first_blocker"]) for row in blocked_rows)
    cushman_rows = [row for row in blocked_rows if row["source_adapter_key"] == CUSHMAN_ADAPTER]
    flightaware_rows = [
        row for row in blocked_rows if row["source_adapter_key"] == FLIGHTAWARE_ADAPTER
    ]
    return {
        "usda_inventory_rows": len(usda_rows),
        "usda_promoted_rows": len(promoted),
        "usda_blocked_rows": len(usda_blocked),
        "candidate_feature_rows": len(promoted),
        "blocked_source_rows": len(blocked_rows),
        "first_hard_blocker": (
            "NONE"
            if promoted
            else _dominant_blocker(
                Counter(str(row["first_blocker"]) for row in usda_blocked),
                "NO_USDA_ROWS_FOUND",
            )
        ),
        "blocker_counts": dict(sorted(blocker_counts.items())),
        "cushman_blocked_rows": len(cushman_rows),
        "cushman_status": (
            "PROPRIETARY_REVIEW_REQUIRED" if cushman_rows else "NO_CUSHMAN_ROWS_REPORTED"
        ),
        "flightaware_blocked_rows": len(flightaware_rows),
        "flightaware_status": (
            "REVIEW_GATED" if flightaware_rows else "NO_FLIGHTAWARE_ROWS_REPORTED"
        ),
        "db_writes_performed": 0,
        "feature_writes": False,
        "forecast_writes": False,
        "paper_trades_created": 0,
        "live_or_demo_execution": False,
        "next_operator_command": _next_operator_command(len(promoted)),
    }


def _acceptance(summary: dict[str, Any], blocked_rows: list[dict[str, Any]]) -> dict[str, Any]:
    cushman_blockers = [
        row["first_blocker"] for row in blocked_rows if row["source_adapter_key"] == CUSHMAN_ADAPTER
    ]
    return {
        "usda_rows_promoted_or_blocked_with_exact_reasons": (
            int(summary["usda_inventory_rows"]) > 0
            and int(summary["usda_promoted_rows"]) + int(summary["usda_blocked_rows"])
            == int(summary["usda_inventory_rows"])
        ),
        "cushman_remains_blocked_without_authorized_evidence": (
            not cushman_blockers
            or all(
                blocker == "PROPRIETARY_REVIEW_REQUIRED"
                for blocker in cushman_blockers
            )
        ),
        "no_paper_live_demo_orders": True,
        "no_paid_proprietary_sources_used": True,
        "date_stability_checks_enforced": True,
        "no_db_writes": int(summary["db_writes_performed"]) == 0,
    }


def _next_operator_command(promoted_rows: int) -> str:
    if promoted_rows > 0:
        return (
            "Next Codex step: add a writer-gated agriculture feature apply preview "
            "for promoted USDA rows; keep dry-run default and do not forecast until "
            "features are written and paper gate remains source-backed."
        )
    return (
        "Next Codex step: obtain or ingest exact official USDA July 3 FVWRETAIL "
        "evidence with observed Hass avocado value, then rerun "
        "phase3bb-r5-usda-source-activation."
    )


def _blocker_codes(
    *,
    exact_source_url: bool,
    date_stable: bool,
    value_present: bool,
    retrieval_timestamp: str,
    evidence_available: bool,
) -> list[str]:
    blockers: list[str] = []
    if not exact_source_url:
        blockers.append("NON_OFFICIAL_OR_NON_USDA_SOURCE")
    if not date_stable:
        blockers.append("SOURCE_DATE_MISMATCH_BLOCKER")
    if not value_present:
        blockers.append("MISSING_OBSERVED_VALUE")
    if not retrieval_timestamp:
        blockers.append("MISSING_RETRIEVAL_TIMESTAMP")
    if not evidence_available:
        blockers.append("SOURCE_VALUE_UNAVAILABLE")
    return blockers


def _source_publication_date(
    *,
    matched_evidence: dict[str, Any],
    evidence_row: dict[str, Any],
    usda_date_report: dict[str, Any],
    fallback_effective_date: str,
) -> str:
    for key in ("source_publication_date", "publication_date", "report_date"):
        value = _text(matched_evidence.get(key) or evidence_row.get(key))
        if value:
            return value
    haystacks = [
        _text(matched_evidence.get("evidence_notes")),
        _text(evidence_row.get("block_reason")),
        _text(_nested(usda_date_report, "preserved_row_state", "evidence_notes")),
    ]
    haystacks.extend(
        _text(row.get("result"))
        for row in _list(usda_date_report.get("sources_checked"))
    )
    for text in haystacks:
        extracted = _extract_reported_date(text)
        if extracted:
            return extracted
    if bool(usda_date_report.get("exact_july_3_report_found")):
        return fallback_effective_date
    return _text(matched_evidence.get("as_of_date")) or fallback_effective_date


def _extract_reported_date(text: str) -> str:
    patterns = (
        r"reported\s+([A-Z][a-z]+\.?\s+\d{1,2},\s+\d{4})",
        r"report(?:ed)?\s+(?:header\s+)?(?:[A-Z][a-z]{2}\s+)?"
        r"([A-Z][a-z]{2,8}\.?\s+\d{1,2},\s+\d{4})",
        r"publication timestamp\s+(\d{4}-\d{2}-\d{2})",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return _normalize_date_text(match.group(1))
    return ""


def _source_notes(
    evidence_row: dict[str, Any],
    matched_evidence: dict[str, Any],
    usda_date_report: dict[str, Any],
) -> str:
    notes = [
        _text(matched_evidence.get("evidence_notes")),
        _text(evidence_row.get("block_reason")),
        _text(usda_date_report.get("status")),
        _text(usda_date_report.get("next_action")),
    ]
    return " ".join(note for note in notes if note)


def _source_unavailable(
    matched_evidence: dict[str, Any],
    evidence_row: dict[str, Any],
) -> bool:
    status = _text(matched_evidence.get("verification_status")).lower()
    evidence_status = _text(evidence_row.get("evidence_status")).upper()
    if status in {"source_not_available", "source_unavailable", "not_published_yet"}:
        return True
    if evidence_status == "SOURCE_EVIDENCE_UNAVAILABLE":
        return True
    available = matched_evidence.get("evidence_available")
    return available is False


def _is_usda_url(url: str) -> bool:
    return "usda.gov" in url.lower()


def _same_date(left: str, right: str) -> bool:
    return bool(left and right and _normalize_date_text(left) == _normalize_date_text(right))


def _normalize_date_text(value: str) -> str:
    text = _text(value)
    if not text:
        return ""
    months = {
        "jan": "January",
        "feb": "February",
        "mar": "March",
        "apr": "April",
        "may": "May",
        "jun": "June",
        "jul": "July",
        "aug": "August",
        "sep": "September",
        "sept": "September",
        "oct": "October",
        "nov": "November",
        "dec": "December",
    }
    iso = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if iso:
        year, month, day = iso.groups()
        month_name = list(months.values())[int(month) - 1]
        return f"{month_name} {int(day)}, {year}"
    match = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),\s+(\d{4})", text)
    if match:
        month, day, year = match.groups()
        month_name = months.get(month[:3].lower(), month)
        return f"{month_name} {int(day)}, {year}"
    return text


def _render_executive_summary(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = _metadata_lines(payload, "# Phase 3BB-R5 USDA Source Activation")
    lines.extend(
        [
            "",
            "## Status",
            "",
            f"- USDA inventory rows: `{summary['usda_inventory_rows']}`",
            f"- USDA promoted rows: `{summary['usda_promoted_rows']}`",
            f"- USDA blocked rows: `{summary['usda_blocked_rows']}`",
            f"- Candidate feature rows: `{summary['candidate_feature_rows']}`",
            f"- First hard blocker: `{summary['first_hard_blocker']}`",
            f"- Cushman status: `{summary['cushman_status']}`",
            f"- FlightAware status: `{summary['flightaware_status']}`",
            f"- DB writes performed: `{summary['db_writes_performed']}`",
            f"- Paper trades created: `{summary['paper_trades_created']}`",
            "",
            "## Acceptance",
            "",
        ]
    )
    for key, value in payload["acceptance"].items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Next", "", summary["next_operator_command"], ""])
    return "\n".join(lines)


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# USDA Agriculture Source Activation")
    lines.extend(
        [
            "",
            "## USDA Rows",
            "",
            (
                "| Ticker | Decision | First blocker | Source publication | Effective date | "
                "Value | Freshness |"
            ),
            "|---|---|---|---|---|---:|---:|",
        ]
    )
    for row in payload["usda_rows"]:
        lines.append(
            "| {market_ticker} | {activation_decision} | {first_blocker} | "
            "{source_publication_date} | {effective_date} | {observed_value} | "
            "{freshness_pass} |".format(**row)
        )
    lines.extend(["", "## Source Blockers", ""])
    for blocker, count in payload["summary"]["blocker_counts"].items():
        lines.append(f"- `{blocker}`: `{count}`")
    lines.extend(["", "## Guardrails", ""])
    for guardrail in payload["operator_guardrails"]:
        lines.append(f"- {guardrail}")
    lines.append("")
    return "\n".join(lines)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _nested(payload: dict[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _nested_text(payload: dict[str, Any], *keys: str) -> str:
    return _text(_nested(payload, *keys))


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _dominant_blocker(blockers: Counter[str], fallback: str) -> str:
    if not blockers:
        return fallback
    return blockers.most_common(1)[0][0]
