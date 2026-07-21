from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import delete, desc, distinct, func, or_, select, text
from sqlalchemy.orm import Session

import kalshi_predictor
from kalshi_predictor.data.backend import detect_backend, redact_database_url
from kalshi_predictor.data.db import describe_db_location
from kalshi_predictor.data.repositories import decode_json, encode_json
from kalshi_predictor.data.schema import (
    CryptoMarketLink,
    EconomicEvent,
    EconomicFeature,
    EconomicMarketLink,
    Forecast,
    Market,
    MarketLeg,
    MarketOpportunity,
    MarketSnapshot,
    NewsFeature,
    NewsItem,
    NewsMarketLink,
    PaperOrder,
    Settlement,
    SportsMarketLink,
    WeatherMarketLink,
)
from kalshi_predictor.market_legs import link_coverage_dashboard, parse_market_legs
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.sports.repository import insert_sports_market_link
from kalshi_predictor.utils.time import utc_now

PHASE_3BB_VERSION = "phase3bb_domain_readiness_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb")
DOMAIN_ECONOMIC = "economic"
DOMAIN_NEWS = "news"
DOMAIN_GENERAL = "general"
OPEN_STATUSES = {"active", "open", "initialized"}
GENERAL_TAXONOMY_EXAMPLE_LIMIT = 5
GENERAL_SOURCE_TAXONOMY_VERSION = "phase3bb_r2_general_taxonomy_v2"
GENERAL_SOURCE_READINESS_SCHEMA_VERSION = "phase3bb_r2_source_readiness_schema_v1"
GENERAL_SOURCE_SAFETY_MODE = "REPORT_ONLY_NO_WRITES"
GENERAL_SOURCE_ALLOWED_STATES = {
    "NOT_CONFIGURED",
    "CONFIGURED_NO_VALUES",
    "VALUES_AVAILABLE",
    "VALUES_STALE",
    "PARSER_FAILED",
    "READY_FOR_REVIEW",
    "LINK_SAFE",
    "FORECAST_SAFE",
    "DISABLED",
    "UNSUPPORTED_SOURCE",
    "PROPRIETARY_SOURCE_REVIEW_REQUIRED",
}
ECONOMIC_TERMS = (
    "cpi",
    "inflation",
    "fed",
    "fomc",
    "interest rate",
    "unemployment",
    "payroll",
    "jobs",
    "gdp",
)
POLITICS_TERMS = ("election", "president", "senate", "congress", "house", "governor")
COMPANY_TERMS = ("stock", "shares", "earnings", "revenue", "ipo", "sec")
GEOPOLITICAL_TERMS = ("oil", "gas", "tariff", "war", "sanction", "ceasefire")
COMMODITY_PRICE_TERMS = (
    "advertised price",
    "average price",
    "avocado",
    "avocados",
    "hass",
)
TRANSPORTATION_OPERATION_TERMS = (
    "cancellation",
    "cancellations",
    "flight",
    "flights",
    "airport",
)
INFRASTRUCTURE_CAPACITY_TERMS = (
    "data center",
    "datacenter",
    "operational data center capacity",
    "capacity",
    "gw",
)
GENERAL_SIGNAL_BUCKETS = {
    "COMMODITY_PRICE_CANDIDATE",
    "TRANSPORTATION_OPERATION_CANDIDATE",
    "INFRASTRUCTURE_CAPACITY_CANDIDATE",
}
GENERAL_SOURCE_ADAPTER_KEYS = (
    "commodity_advertised_price_source",
    "transportation_flight_cancellation_source",
    "infrastructure_data_center_capacity_source",
)
DEFAULT_GENERAL_SOURCE_EVIDENCE_DIR = Path("data/general_source_evidence")
SPORTS_TERMS = (
    "advances",
    "wins by",
    "goals scored",
    "both teams to score",
    "corners",
    "1h goals",
    "reg time",
    " mlb",
    " nba",
    " nfl",
    " nhl",
    "cs2",
    "cod",
    "esports",
    "t20",
)
SPORTS_MARKET_PREFIXES = (
    "kxmv",
    "kxcs2",
    "kxcodgame",
    "kxvalorant",
    "kxt20match",
    "kxwt20match",
    "kxwodimatch",
    "kxmlb",
    "kxnba",
    "kxwnba",
    "kxnhl",
    "kxnfl",
)
R3_EXACT_SPORTS_LINK_PREFIXES = (
    "KXCS2GAME",
    "KXCS2MAP",
    "KXCS2TOTALMAPS",
    "KXVALORANTGAME",
    "KXVALORANTMAP",
    "KXT20MATCH",
    "KXWT20MATCH",
    "KXWODIMATCH",
)
R3_KXMVE_COMPOSITE_PREFIXES = (
    "KXMVESPORTSMULTIGAME",
    "KXMVECROSSCATEGORY",
)
R3_COMPOSITE_CATEGORIES = (
    "crypto",
    "weather",
    "economic",
    "sports",
    "news",
    "cross_category",
    "general",
    "unknown",
)
R3_COMPONENT_SUPPORTED_SIDES = {"yes", "no"}


@dataclass(frozen=True)
class Phase3BBDomainReadinessArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    rows_path: Path


@dataclass(frozen=True)
class Phase3BBR2GeneralRoutingArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    rows_path: Path
    diagnostics_path: Path


@dataclass(frozen=True)
class Phase3BBR3GeneralReclassificationArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    candidates_path: Path
    manual_review_path: Path


@dataclass(frozen=True)
class Phase3BBR3SafeParserReparseArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    rows_safe_to_reparse: int
    rows_reparsed: int
    rows_deleted: int
    rows_inserted: int


@dataclass(frozen=True)
class Phase3BBR3ExactSportsLinkArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    rows_safe_to_link: int
    links_created: int
    apply: bool


@dataclass(frozen=True)
class Phase3BBR3CompositePreviewGateArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    rows_path: Path
    rows_reviewed: int
    verified_component_evidence_rows: int
    true_composite_rows: int


@dataclass(frozen=True)
class Phase3BBR3CompositeOperatorPreflightArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    rows_path: Path
    paper_composite_review_ready_rows: int
    blocked_rows: int


@dataclass(frozen=True)
class Phase3BBR2GeneralSourceEvidenceArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    evidence_rows_path: Path
    templates_path: Path


@dataclass(frozen=True)
class Phase3BBR2GeneralSourceAvailabilityArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    availability_rows_path: Path


@dataclass(frozen=True)
class Phase3BBR2GeneralSourceIntakeArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    template_json_path: Path
    template_csv_path: Path
    canonical_json_path: Path
    canonical_markdown_path: Path
    taxonomy_review_path: Path
    source_evidence_requirements_path: Path
    source_readiness_matrix_path: Path
    candidate_market_samples_path: Path
    next_actions_path: Path
    manifest_path: Path


@dataclass(frozen=True)
class Phase3BBR2GroupSourceReviewArtifactSet:
    output_path: Path
    row_count: int
    group_count: int


@dataclass(frozen=True)
class Phase3BBR2AppliedGroupSourceReviewArtifactSet:
    output_path: Path
    template_rows: int
    rows_updated: int


def build_phase3bb_domain_readiness(session: Session) -> dict[str, Any]:
    """Report economic/news/general readiness without creating links or features."""

    coverage = link_coverage_dashboard(session)
    coverage_by_category = {
        str(row.get("category") or ""): row for row in coverage.get("category_rows", [])
    }
    rows = [
        _economic_row(session, coverage_by_category.get(DOMAIN_ECONOMIC, {})),
        _news_row(session, coverage_by_category.get(DOMAIN_NEWS, {})),
        _general_row(session, coverage_by_category.get(DOMAIN_GENERAL, {})),
    ]
    status_counts: dict[str, int] = {}
    for row in rows:
        status = str(row["status"])
        status_counts[status] = status_counts.get(status, 0) + 1

    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3BB",
        "phase_version": PHASE_3BB_VERSION,
        "mode": "PAPER_ONLY_DOMAIN_READINESS",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "summary": {
            "domains_reviewed": len(rows),
            "actionable_domains": sum(1 for row in rows if row["actionable_now"]),
            "blocked_domains": sum(1 for row in rows if not row["actionable_now"]),
            "status_counts": status_counts,
        },
        "domain_rows": rows,
        "recommended_next_action": _recommended_next_action(rows),
        "next_commands": _next_commands(rows),
    }


def write_phase3bb_domain_readiness_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> Phase3BBDomainReadinessArtifactSet:
    payload = build_phase3bb_domain_readiness(session)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3bb_domain_readiness.json"
    markdown_path = output_dir / "phase3bb_domain_readiness.md"
    rows_path = output_dir / "phase3bb_domain_rows.json"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    rows_path.write_text(
        json.dumps(payload["domain_rows"], indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return Phase3BBDomainReadinessArtifactSet(output_dir, json_path, markdown_path, rows_path)


def build_phase3bb_general_candidate_routing(
    session: Session,
    *,
    limit_per_bucket: int = 50,
) -> dict[str, Any]:
    """Bucket general markets into review queues without creating or upgrading links."""

    grouped = _group_general_markets(session)
    bucket_counts: dict[str, int] = {}
    bucket_examples: dict[str, list[dict[str, Any]]] = {}
    route_domain_counts: dict[str, int] = {}
    family_counts_by_bucket: dict[str, dict[str, int]] = {}
    general_signal_diagnostic_rows: list[dict[str, Any]] = []
    for item in grouped.values():
        bucket = _general_taxonomy_bucket(item)
        route_domain, _, _ = _general_route_metadata(bucket)
        family_key = _family_key(item)
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
        route_domain_counts[route_domain] = route_domain_counts.get(route_domain, 0) + 1
        family_counts = family_counts_by_bucket.setdefault(bucket, {})
        family_counts[family_key] = family_counts.get(family_key, 0) + 1
        bucket_examples.setdefault(bucket, [])
        if len(bucket_examples[bucket]) < limit_per_bucket:
            bucket_examples[bucket].append(_general_route_row(item, bucket))
        if bucket in GENERAL_SIGNAL_BUCKETS:
            general_signal_diagnostic_rows.append(
                _general_signal_diagnostic_row(item, bucket)
            )

    route_rows = [
        row
        for bucket in sorted(bucket_examples)
        for row in bucket_examples[bucket]
    ]
    candidate_buckets = {
        "economic": bucket_counts.get("ECONOMIC_CANDIDATE", 0),
        "news": (
            bucket_counts.get("POLITICS_NEWS_CANDIDATE", 0)
            + bucket_counts.get("COMPANY_NEWS_CANDIDATE", 0)
            + bucket_counts.get("GEOPOLITICAL_NEWS_CANDIDATE", 0)
        ),
        "operational_or_commodity": (
            bucket_counts.get("COMMODITY_PRICE_CANDIDATE", 0)
            + bucket_counts.get("TRANSPORTATION_OPERATION_CANDIDATE", 0)
            + bucket_counts.get("INFRASTRUCTURE_CAPACITY_CANDIDATE", 0)
        ),
        "sports_or_cross_category_leakage": bucket_counts.get(
            "SPORTS_OR_CROSS_CATEGORY_LEAKAGE",
            0,
        ),
        "unsupported_or_unclassified": (
            bucket_counts.get("UNSUPPORTED_MULTI_LEG_GENERAL", 0)
            + bucket_counts.get("GENERAL_UNCLASSIFIED", 0)
        ),
    }
    general_signal_diagnostics = _general_signal_diagnostics_summary(
        general_signal_diagnostic_rows
    )
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3BB-R2",
        "phase_version": "phase3bb_r2_general_candidate_routing_v2",
        "mode": "PAPER_ONLY_GENERAL_CANDIDATE_ROUTING",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "summary": {
            "general_markets_reviewed": len(grouped),
            "sample_limit_per_bucket": limit_per_bucket,
            "bucket_counts": dict(sorted(bucket_counts.items())),
            "route_domain_counts": dict(sorted(route_domain_counts.items())),
            "top_families_by_bucket": {
                bucket: _top_family_rows(counts)
                for bucket, counts in sorted(family_counts_by_bucket.items())
            },
            "candidate_buckets": candidate_buckets,
            "safe_link_upgrade_candidates": 0,
            "general_signal_diagnostics": general_signal_diagnostics,
        },
        "route_rows": route_rows,
        "general_signal_diagnostic_rows": general_signal_diagnostic_rows,
        "recommended_next_action": _r2_recommended_next_action(candidate_buckets),
        "next_commands": [
            "kalshi-bot phase3bb-r2-general-candidate-routing --output-dir reports/phase3bb_r2",
            "kalshi-bot phase3bb-domain-readiness --output-dir reports/phase3bb",
            "kalshi-bot phase3az-gap-analysis --output-dir reports/phase3az --reports-dir reports",
        ],
    }


def write_phase3bb_general_candidate_routing_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3bb_r2"),
    limit_per_bucket: int = 50,
) -> Phase3BBR2GeneralRoutingArtifactSet:
    payload = build_phase3bb_general_candidate_routing(
        session,
        limit_per_bucket=limit_per_bucket,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3bb_r2_general_candidate_routing.json"
    markdown_path = output_dir / "phase3bb_r2_general_candidate_routing.md"
    rows_path = output_dir / "phase3bb_r2_general_candidate_rows.json"
    diagnostics_path = output_dir / "phase3bb_r2_general_signal_diagnostics.json"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    rows_path.write_text(
        json.dumps(payload["route_rows"], indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    diagnostics_path.write_text(
        json.dumps(
            payload["general_signal_diagnostic_rows"],
            indent=2,
            sort_keys=True,
            default=str,
        ),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_r2_markdown(payload), encoding="utf-8")
    return Phase3BBR2GeneralRoutingArtifactSet(
        output_dir,
        json_path,
        markdown_path,
        rows_path,
        diagnostics_path,
    )


def build_phase3bb_general_source_evidence(
    session: Session,
    *,
    evidence_dir: Path = DEFAULT_GENERAL_SOURCE_EVIDENCE_DIR,
    limit_per_bucket: int = 50,
) -> dict[str, Any]:
    """Validate report-only source evidence for R2 general-signal diagnostics."""

    routing_payload = build_phase3bb_general_candidate_routing(
        session,
        limit_per_bucket=limit_per_bucket,
    )
    diagnostic_rows = routing_payload["general_signal_diagnostic_rows"]
    source_inputs = {
        adapter: _load_general_source_records(evidence_dir, adapter)
        for adapter in GENERAL_SOURCE_ADAPTER_KEYS
    }
    evidence_rows = [
        _general_source_evidence_row(row, source_inputs)
        for row in diagnostic_rows
        if row.get("source_adapter_key") in source_inputs
    ]
    status_counts: dict[str, int] = {}
    adapter_counts: dict[str, int] = {}
    for row in evidence_rows:
        status = str(row["evidence_status"])
        adapter = str(row["source_adapter_key"])
        status_counts[status] = status_counts.get(status, 0) + 1
        adapter_counts[adapter] = adapter_counts.get(adapter, 0) + 1

    exact_ready_rows = sum(
        1
        for row in evidence_rows
        if row["evidence_status"] == "EXACT_EVIDENCE_READY_FOR_REVIEW"
    )
    unavailable_rows = sum(
        1
        for row in evidence_rows
        if row["evidence_status"] == "SOURCE_EVIDENCE_UNAVAILABLE"
    )
    missing_files = [
        str(payload["path"])
        for payload in source_inputs.values()
        if not payload["file_exists"]
    ]
    invalid_files = [
        str(payload["path"])
        for payload in source_inputs.values()
        if payload["file_exists"] and payload.get("load_error")
    ]
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3BB-R2",
        "phase_version": "phase3bb_r2_general_source_evidence_v1",
        "mode": "PAPER_ONLY_GENERAL_SOURCE_EVIDENCE",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "evidence_dir": str(evidence_dir),
        "summary": {
            "diagnostic_rows": len(diagnostic_rows),
            "evidence_rows": len(evidence_rows),
            "source_adapter_counts": dict(sorted(adapter_counts.items())),
            "evidence_status_counts": dict(sorted(status_counts.items())),
            "evidence_files_seen": {
                adapter: str(payload["path"])
                for adapter, payload in sorted(source_inputs.items())
                if payload["file_exists"]
            },
            "missing_evidence_files": missing_files,
            "invalid_evidence_files": invalid_files,
            "exact_evidence_ready_rows": exact_ready_rows,
            "source_evidence_unavailable_rows": unavailable_rows,
            "safe_to_link_rows": 0,
            "safe_to_forecast_rows": 0,
            "proposed_db_writes": 0,
            "link_writes": False,
            "feature_writes": False,
            "forecast_writes": False,
            "live_or_demo_execution": False,
        },
        "safety_gate": {
            "writes_links": False,
            "writes_features": False,
            "writes_forecasts": False,
            "places_paper_orders": False,
            "places_demo_orders": False,
            "places_live_orders": False,
            "safe_to_link": False,
            "safe_to_forecast": False,
            "reason": (
                "This pass only checks whether exact external evidence records exist. "
                "A reviewed adapter/linker implementation is required before any "
                "link, feature, forecast, or trade output."
            ),
        },
        "source_adapter_templates": _general_source_evidence_templates(),
        "evidence_rows": evidence_rows,
        "recommended_next_action": _source_evidence_recommended_next_action(
            missing_files=missing_files,
            invalid_files=invalid_files,
            exact_ready_rows=exact_ready_rows,
            unavailable_rows=unavailable_rows,
            evidence_rows=len(evidence_rows),
        ),
        "next_commands": [
            (
                "kalshi-bot phase3bb-r2-general-source-intake "
                "--output-dir reports/phase3bb_r2_sources"
            ),
            (
                "kalshi-bot phase3bb-r2-general-source-evidence "
                "--output-dir reports/phase3bb_r2_sources"
            ),
            (
                "kalshi-bot phase3bb-r2-general-source-availability "
                "--output-dir reports/phase3bb_r2_sources"
            ),
            "kalshi-bot phase3bb-r2-general-candidate-routing --output-dir reports/phase3bb_r2",
            "kalshi-bot phase3az-gap-analysis --output-dir reports/phase3az --reports-dir reports",
        ],
    }


def write_phase3bb_general_source_evidence_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3bb_r2_sources"),
    evidence_dir: Path = DEFAULT_GENERAL_SOURCE_EVIDENCE_DIR,
    limit_per_bucket: int = 50,
) -> Phase3BBR2GeneralSourceEvidenceArtifactSet:
    payload = build_phase3bb_general_source_evidence(
        session,
        evidence_dir=evidence_dir,
        limit_per_bucket=limit_per_bucket,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3bb_r2_general_source_evidence.json"
    markdown_path = output_dir / "phase3bb_r2_general_source_evidence.md"
    evidence_rows_path = output_dir / "phase3bb_r2_general_source_evidence_rows.json"
    templates_path = output_dir / "phase3bb_r2_general_source_evidence_templates.json"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    evidence_rows_path.write_text(
        json.dumps(payload["evidence_rows"], indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    templates_path.write_text(
        json.dumps(
            payload["source_adapter_templates"],
            indent=2,
            sort_keys=True,
            default=str,
        ),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_source_evidence_markdown(payload), encoding="utf-8")
    return Phase3BBR2GeneralSourceEvidenceArtifactSet(
        output_dir,
        json_path,
        markdown_path,
        evidence_rows_path,
        templates_path,
    )


def build_phase3bb_general_source_availability(
    session: Session,
    *,
    evidence_dir: Path = DEFAULT_GENERAL_SOURCE_EVIDENCE_DIR,
    limit_per_bucket: int = 50,
    check_source_urls: bool = False,
    url_timeout_seconds: float = 8.0,
) -> dict[str, Any]:
    """Watch exact source publication availability without creating downstream writes."""

    source_inputs = {
        adapter: _load_general_source_records(evidence_dir, adapter)
        for adapter in GENERAL_SOURCE_ADAPTER_KEYS
    }
    evidence_payload = build_phase3bb_general_source_evidence(
        session,
        evidence_dir=evidence_dir,
        limit_per_bucket=limit_per_bucket,
    )
    evidence_rows_by_adapter: dict[str, list[dict[str, Any]]] = {
        adapter: [] for adapter in GENERAL_SOURCE_ADAPTER_KEYS
    }
    for row in evidence_payload["evidence_rows"]:
        adapter = str(row.get("source_adapter_key") or "")
        if adapter in evidence_rows_by_adapter:
            evidence_rows_by_adapter[adapter].append(row)

    availability_rows = [
        row
        for adapter in GENERAL_SOURCE_ADAPTER_KEYS
        for row in _general_source_availability_rows(
            adapter,
            source_inputs[adapter],
            evidence_rows_by_adapter.get(adapter, []),
            check_source_urls=check_source_urls,
            url_timeout_seconds=url_timeout_seconds,
        )
    ]
    status_counts: dict[str, int] = {}
    adapter_counts: dict[str, int] = {}
    for row in availability_rows:
        status = str(row["availability_status"])
        adapter = str(row["source_adapter_key"])
        status_counts[status] = status_counts.get(status, 0) + 1
        adapter_counts[adapter] = adapter_counts.get(adapter, 0) + 1

    value_ready_rows = sum(
        1
        for row in availability_rows
        if row["availability_status"] == "SOURCE_VALUE_AVAILABLE_FOR_REVIEW"
    )
    pending_rows = sum(
        1
        for row in availability_rows
        if row["availability_status"] == "PENDING_SOURCE_PUBLICATION"
    )
    incomplete_rows = sum(
        1
        for row in availability_rows
        if row["availability_status"]
        in {
            "NO_SOURCE_RECORD",
            "SOURCE_FILE_MISSING",
            "SOURCE_FILE_INVALID",
            "SOURCE_RECORD_INCOMPLETE",
            "SOURCE_URL_MISSING",
        }
    )
    remote_checked_rows = sum(
        1 for row in availability_rows if row["remote_check"]["requested"]
    )
    remote_ok_rows = sum(
        1
        for row in availability_rows
        if row["remote_check"]["status"] == "FETCH_OK"
    )
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3BB-R2",
        "phase_version": "phase3bb_r2_general_source_availability_v1",
        "mode": "PAPER_ONLY_GENERAL_SOURCE_AVAILABILITY",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "evidence_dir": str(evidence_dir),
        "summary": {
            "source_files_reviewed": len(source_inputs),
            "availability_rows": len(availability_rows),
            "availability_status_counts": dict(sorted(status_counts.items())),
            "source_adapter_counts": dict(sorted(adapter_counts.items())),
            "source_value_available_rows": value_ready_rows,
            "pending_source_publication_rows": pending_rows,
            "source_record_incomplete_rows": incomplete_rows,
            "remote_checks_requested": check_source_urls,
            "remote_checked_rows": remote_checked_rows,
            "remote_fetch_ok_rows": remote_ok_rows,
            "diagnostic_rows": evidence_payload["summary"]["diagnostic_rows"],
            "evidence_rows": evidence_payload["summary"]["evidence_rows"],
            "safe_to_link_rows": 0,
            "safe_to_forecast_rows": 0,
            "proposed_db_writes": 0,
            "link_writes": False,
            "feature_writes": False,
            "forecast_writes": False,
            "live_or_demo_execution": False,
        },
        "safety_gate": {
            "writes_database": False,
            "writes_links": False,
            "writes_features": False,
            "writes_forecasts": False,
            "places_paper_orders": False,
            "places_demo_orders": False,
            "places_live_orders": False,
            "safe_to_link": False,
            "safe_to_forecast": False,
            "reason": (
                "This pass only watches whether exact source publications and "
                "observed values are available. It never writes links, features, "
                "forecasts, orders, or settlement rows."
            ),
        },
        "availability_rows": availability_rows,
        "recommended_next_action": _source_availability_recommended_next_action(
            pending_rows=pending_rows,
            value_ready_rows=value_ready_rows,
            incomplete_rows=incomplete_rows,
        ),
        "next_commands": [
            (
                "kalshi-bot phase3bb-r2-general-source-availability "
                "--check-source-urls --output-dir reports/phase3bb_r2_sources"
            ),
            (
                "kalshi-bot phase3bb-r2-general-source-evidence "
                "--output-dir reports/phase3bb_r2_sources"
            ),
            "kalshi-bot phase3bb-r2-general-candidate-routing --output-dir reports/phase3bb_r2",
            "kalshi-bot phase3az-gap-analysis --output-dir reports/phase3az --reports-dir reports",
        ],
    }


def write_phase3bb_general_source_availability_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3bb_r2_sources"),
    evidence_dir: Path = DEFAULT_GENERAL_SOURCE_EVIDENCE_DIR,
    limit_per_bucket: int = 50,
    check_source_urls: bool = False,
    url_timeout_seconds: float = 8.0,
) -> Phase3BBR2GeneralSourceAvailabilityArtifactSet:
    payload = build_phase3bb_general_source_availability(
        session,
        evidence_dir=evidence_dir,
        limit_per_bucket=limit_per_bucket,
        check_source_urls=check_source_urls,
        url_timeout_seconds=url_timeout_seconds,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3bb_r2_general_source_availability.json"
    markdown_path = output_dir / "phase3bb_r2_general_source_availability.md"
    availability_rows_path = (
        output_dir / "phase3bb_r2_general_source_availability_rows.json"
    )
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    availability_rows_path.write_text(
        json.dumps(
            payload["availability_rows"],
            indent=2,
            sort_keys=True,
            default=str,
        ),
        encoding="utf-8",
    )
    markdown_path.write_text(
        _render_source_availability_markdown(payload),
        encoding="utf-8",
    )
    return Phase3BBR2GeneralSourceAvailabilityArtifactSet(
        output_dir,
        json_path,
        markdown_path,
        availability_rows_path,
    )


def build_phase3bb_general_source_intake(
    session: Session,
    *,
    output_dir: Path | None = None,
    input_file: Path | None = None,
    evidence_dir: Path = DEFAULT_GENERAL_SOURCE_EVIDENCE_DIR,
    limit_per_bucket: int = 50,
    write_evidence_files: bool = False,
) -> dict[str, Any]:
    """Prepare or ingest audited source evidence files for R2 diagnostics."""

    command_args = _source_intake_command_args(
        output_dir=output_dir,
        input_file=input_file,
        evidence_dir=evidence_dir,
        limit_per_bucket=limit_per_bucket,
        write_evidence_files=write_evidence_files,
    )
    metadata = _phase3bb_r2_report_metadata(session, command_args=command_args)
    grouped = _group_general_markets(session)
    routing_payload = build_phase3bb_general_candidate_routing(
        session,
        limit_per_bucket=limit_per_bucket,
    )
    taxonomy_rows = [
        _source_intake_taxonomy_review_row(item)
        for item in sorted(grouped.values(), key=lambda market: str(market["ticker"]))
    ]
    source_evidence_requirements = [
        _source_evidence_requirement_row(row) for row in taxonomy_rows
    ]
    source_readiness_matrix = _source_readiness_matrix()
    candidate_market_samples = _candidate_market_samples(
        taxonomy_rows,
        limit_per_bucket=limit_per_bucket,
    )
    next_actions = _source_intake_next_actions(
        taxonomy_rows=taxonomy_rows,
        source_readiness_matrix=source_readiness_matrix,
    )
    diagnostic_rows = [
        row
        for row in routing_payload["general_signal_diagnostic_rows"]
        if row.get("source_adapter_key") in GENERAL_SOURCE_ADAPTER_KEYS
    ]
    template_rows = _general_source_intake_template_rows(diagnostic_rows)
    source_rows, source_error = _load_general_source_input_rows(input_file)
    normalized_rows = [
        _general_source_input_row(row, diagnostic_rows) for row in source_rows
    ]
    valid_rows = [row for row in normalized_rows if row["status"] == "READY_TO_WRITE"]
    records_by_adapter = _general_source_records_by_adapter(valid_rows)
    files_written: list[str] = []
    if write_evidence_files and records_by_adapter:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        for adapter_key, records in sorted(records_by_adapter.items()):
            path = evidence_dir / f"{adapter_key}.json"
            path.write_text(
                json.dumps(
                    {
                        "generated_at": utc_now().isoformat(),
                        "source_adapter_key": adapter_key,
                        "records": records,
                    },
                    indent=2,
                    sort_keys=True,
                    default=str,
                ),
                encoding="utf-8",
            )
            files_written.append(str(path))

    status_counts: dict[str, int] = {}
    adapter_counts: dict[str, int] = {}
    for row in normalized_rows:
        status = str(row["status"])
        adapter = str(row.get("source_adapter_key") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        adapter_counts[adapter] = adapter_counts.get(adapter, 0) + 1
    source_state = _general_source_intake_state(
        input_file=input_file,
        source_error=source_error,
        write_evidence_files=write_evidence_files,
    )
    taxonomy_counts = _count_values(taxonomy_rows, "taxonomy_label")
    source_state_counts = _count_values(source_readiness_matrix, "readiness_state")
    summary = {
        "general_markets_reviewed": len(taxonomy_rows),
        "active_general_markets_reviewed": sum(
            1
            for row in taxonomy_rows
            if str(row.get("market_status") or "").lower() in OPEN_STATUSES
        ),
        "taxonomy_counts": taxonomy_counts,
        "commodity_candidates": taxonomy_counts.get("COMMODITY_PRICE_CANDIDATE", 0),
        "transportation_candidates": taxonomy_counts.get(
            "TRANSPORTATION_OPERATION_CANDIDATE",
            0,
        ),
        "infrastructure_candidates": taxonomy_counts.get(
            "INFRASTRUCTURE_CAPACITY_CANDIDATE",
            0,
        ),
        "sports_or_cross_category_leakage": taxonomy_counts.get(
            "SPORTS_OR_CROSS_CATEGORY_LEAKAGE",
            0,
        ),
        "general_unclassified": taxonomy_counts.get("GENERAL_UNCLASSIFIED", 0),
        "source_readiness_counts": source_state_counts,
        "diagnostic_rows": len(diagnostic_rows),
        "template_rows": len(template_rows),
        "input_rows": len(source_rows),
        "valid_input_rows": len(valid_rows),
        "invalid_input_rows": len(normalized_rows) - len(valid_rows),
        "input_status_counts": dict(sorted(status_counts.items())),
        "input_adapter_counts": dict(sorted(adapter_counts.items())),
        "evidence_records_ready": sum(len(records) for records in records_by_adapter.values()),
        "evidence_files_written": len(files_written),
        "writes_requested": write_evidence_files,
        "db_writes": False,
        "link_writes": False,
        "feature_writes": False,
        "forecast_writes": False,
        "opportunity_writes": False,
        "paper_trade_writes": False,
        "settlement_writes": False,
        "live_or_demo_execution": False,
    }
    return {
        **_top_level_report_fields(metadata),
        "generated_at": metadata["generated_at"],
        "phase": "3BB-R2",
        "phase_version": "phase3bb_r2_general_source_intake_v1",
        "mode": GENERAL_SOURCE_SAFETY_MODE,
        "safety_mode": GENERAL_SOURCE_SAFETY_MODE,
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "report_metadata": metadata,
        "source_state": source_state,
        "evidence_dir": str(evidence_dir),
        "summary": summary,
        "safety_gate": {
            "writes_evidence_files": bool(write_evidence_files),
            "writes_database": False,
            "writes_links": False,
            "writes_features": False,
            "writes_forecasts": False,
            "writes_opportunities": False,
            "places_paper_orders": False,
            "settles_trades": False,
            "places_demo_orders": False,
            "places_live_orders": False,
            "reason": (
                "The default command emits report artifacts only. Optional local "
                "evidence JSON files may be written only when explicitly requested, "
                "but links, features, forecasts, opportunities, paper trades, "
                "settlements, and exchange orders stay blocked."
            ),
        },
        "taxonomy_review_rows": taxonomy_rows,
        "source_evidence_requirements": source_evidence_requirements,
        "source_readiness_matrix": source_readiness_matrix,
        "candidate_market_samples": candidate_market_samples,
        "next_actions": next_actions,
        "template_rows": template_rows,
        "input_rows": normalized_rows,
        "evidence_files_written": files_written,
        "recommended_next_action": _source_intake_recommended_next_action(
            source_state=source_state,
            summary=summary,
        ),
        "next_commands": [
            (
                "kalshi-bot phase3bb-r2-general-source-intake "
                "--output-dir reports/phase3bb_r2_sources"
            ),
            (
                "kalshi-bot phase3bb-r2-general-source-evidence "
                "--output-dir reports/phase3bb_r2_sources"
            ),
            (
                "kalshi-bot phase3bb-r2-general-source-availability "
                "--output-dir reports/phase3bb_r2_sources"
            ),
            "kalshi-bot phase3az-gap-analysis --output-dir reports/phase3az --reports-dir reports",
        ],
    }


def write_phase3bb_general_source_intake_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3bb_r2_sources"),
    input_file: Path | None = None,
    evidence_dir: Path = DEFAULT_GENERAL_SOURCE_EVIDENCE_DIR,
    limit_per_bucket: int = 50,
    write_evidence_files: bool = False,
) -> Phase3BBR2GeneralSourceIntakeArtifactSet:
    payload = build_phase3bb_general_source_intake(
        session,
        output_dir=output_dir,
        input_file=input_file,
        evidence_dir=evidence_dir,
        limit_per_bucket=limit_per_bucket,
        write_evidence_files=write_evidence_files,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3bb_r2_general_source_intake.json"
    markdown_path = output_dir / "phase3bb_r2_general_source_intake.md"
    template_json_path = output_dir / "phase3bb_r2_general_source_input_template.json"
    template_csv_path = output_dir / "phase3bb_r2_general_source_input_template.csv"
    canonical_json_path = output_dir / "general_source_intake.json"
    canonical_markdown_path = output_dir / "general_source_intake.md"
    taxonomy_review_path = output_dir / "taxonomy_review.json"
    source_evidence_requirements_path = output_dir / "source_evidence_requirements.json"
    source_readiness_matrix_path = output_dir / "source_readiness_matrix.json"
    candidate_market_samples_path = output_dir / "candidate_market_samples.json"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    canonical_json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    taxonomy_review_path.write_text(
        json.dumps(
            _section_report(payload, "taxonomy_review", "taxonomy_review_rows"),
            indent=2,
            sort_keys=True,
            default=str,
        ),
        encoding="utf-8",
    )
    source_evidence_requirements_path.write_text(
        json.dumps(
            _section_report(
                payload,
                "source_evidence_requirements",
                "source_evidence_requirements",
            ),
            indent=2,
            sort_keys=True,
            default=str,
        ),
        encoding="utf-8",
    )
    source_readiness_matrix_path.write_text(
        json.dumps(
            _section_report(
                payload,
                "source_readiness_matrix",
                "source_readiness_matrix",
            ),
            indent=2,
            sort_keys=True,
            default=str,
        ),
        encoding="utf-8",
    )
    candidate_market_samples_path.write_text(
        json.dumps(
            _section_report(
                payload,
                "candidate_market_samples",
                "candidate_market_samples",
            ),
            indent=2,
            sort_keys=True,
            default=str,
        ),
        encoding="utf-8",
    )
    template_json_path.write_text(
        json.dumps(payload["template_rows"], indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    _write_general_source_template_csv(template_csv_path, payload["template_rows"])
    markdown = _render_source_intake_markdown(payload)
    markdown_path.write_text(markdown, encoding="utf-8")
    canonical_markdown_path.write_text(markdown, encoding="utf-8")
    next_actions_path.write_text(_render_source_next_actions_markdown(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [
            json_path,
            markdown_path,
            template_json_path,
            template_csv_path,
            canonical_json_path,
            canonical_markdown_path,
            taxonomy_review_path,
            source_evidence_requirements_path,
            source_readiness_matrix_path,
            candidate_market_samples_path,
            next_actions_path,
        ],
    )
    return Phase3BBR2GeneralSourceIntakeArtifactSet(
        output_dir,
        json_path,
        markdown_path,
        template_json_path,
        template_csv_path,
        canonical_json_path,
        canonical_markdown_path,
        taxonomy_review_path,
        source_evidence_requirements_path,
        source_readiness_matrix_path,
        candidate_market_samples_path,
        next_actions_path,
        manifest_path,
    )


def write_phase3bb_group_source_review(
    *,
    input_path: Path,
    output_path: Path,
) -> Phase3BBR2GroupSourceReviewArtifactSet:
    rows = _read_csv_rows(input_path)
    groups: dict[tuple[str, str, str, str, str], list[dict[str, str]]] = {}
    for row in rows:
        groups.setdefault(_group_source_key(row), []).append(row)

    output_rows = [
        _group_source_review_row(group_rows)
        for _, group_rows in sorted(groups.items(), key=lambda item: item[0])
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_csv(output_path, _group_source_review_headers(), output_rows)
    return Phase3BBR2GroupSourceReviewArtifactSet(
        output_path=output_path,
        row_count=len(rows),
        group_count=len(output_rows),
    )


def write_phase3bb_apply_group_source_review(
    *,
    group_review_path: Path,
    template_path: Path,
    output_path: Path,
) -> Phase3BBR2AppliedGroupSourceReviewArtifactSet:
    group_rows = _read_csv_rows(group_review_path)
    template_rows = _read_csv_rows(template_path)
    groups = {_group_source_key(row): row for row in group_rows}
    updated = 0
    filled_rows: list[dict[str, str]] = []
    for row in template_rows:
        filled = dict(row)
        group = groups.get(_group_source_key(row))
        if group is not None and _apply_group_values(filled, group):
            updated += 1
        filled_rows.append(filled)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    headers = _csv_headers(filled_rows) or _csv_headers(template_rows)
    _write_csv(output_path, headers, filled_rows)
    return Phase3BBR2AppliedGroupSourceReviewArtifactSet(
        output_path=output_path,
        template_rows=len(template_rows),
        rows_updated=updated,
    )


def build_phase3bb_general_reclassification(
    session: Session,
    *,
    sample_limit: int = 200,
) -> dict[str, Any]:
    """Build a report-only repair queue for general sports/cross-category leakage."""

    grouped = _group_general_markets(session)
    sports_rows: list[dict[str, Any]] = []
    manual_rows: list[dict[str, Any]] = []
    reason_counts: dict[str, int] = {}
    family_counts: dict[str, int] = {}
    target_counts: dict[str, int] = {}
    rows_safe_to_reparse = 0

    for item in grouped.values():
        bucket = _general_taxonomy_bucket(item)
        if bucket == "SPORTS_OR_CROSS_CATEGORY_LEAKAGE":
            candidate = _sports_reclassification_candidate(item)
            preview = _r3_parser_preview(
                session,
                item,
                session.get(Market, str(item["ticker"])),
                proposed_category=str(candidate["proposed_category"]),
            )
            candidate["parser_preview"] = preview
            candidate["safe_to_reparse"] = preview["safe_to_reparse"]
            if preview["safe_to_reparse"]:
                rows_safe_to_reparse += 1
            sports_rows.append(candidate)
            target = str(candidate["proposed_category"])
            target_counts[target] = target_counts.get(target, 0) + 1
            family = str(candidate["family_key"])
            family_counts[family] = family_counts.get(family, 0) + 1
            for reason in candidate["leakage_reasons"]:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
        elif bucket == "GENERAL_UNCLASSIFIED":
            manual_rows.append(_manual_general_review_row(item))

    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3BB-R3",
        "phase_version": "phase3bb_r3_general_reclassification_v1",
        "mode": "PAPER_ONLY_RECLASSIFICATION_DIAGNOSTIC",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "summary": {
            "general_markets_reviewed": len(grouped),
            "sports_cross_category_reclassification_candidates": len(sports_rows),
            "manual_review_rows": len(manual_rows),
            "safe_to_apply_rows": 0,
            "rows_safe_to_reparse": rows_safe_to_reparse,
            "proposed_db_writes": 0,
            "proposed_category_counts": dict(sorted(target_counts.items())),
            "leakage_reason_counts": dict(sorted(reason_counts.items())),
            "top_reclassification_families": _top_family_rows(family_counts),
            "candidate_sample_rows": min(len(sports_rows), sample_limit),
        },
        "safety_gate": {
            "writes_market_legs": False,
            "writes_links": False,
            "runs_phase3ae": False,
            "auto_reclassification_enabled": False,
            "safe_to_apply": False,
            "safe_to_reparse": rows_safe_to_reparse > 0,
            "reason": (
                "This report identifies parser/reclassification candidates only. "
                "Only parser-preview-safe rows may be reparsed by the controlled "
                "Phase 3BB-R3 safe parser reparse command."
            ),
        },
        "reclassification_candidates": sports_rows[:sample_limit],
        "manual_review_rows": manual_rows,
        "recommended_next_action": _r3_recommended_next_action(
            sports_rows=sports_rows,
            manual_rows=manual_rows,
            rows_safe_to_reparse=rows_safe_to_reparse,
        ),
        "next_commands": [
            "kalshi-bot phase3bb-r3-general-reclassification --output-dir reports/phase3bb_r3",
            (
                "kalshi-bot phase3bb-r3-safe-parser-reparse --output-dir reports/phase3bb_r3"
                if rows_safe_to_reparse > 0
                else "# skip phase3bb-r3-safe-parser-reparse until rows_safe_to_reparse > 0"
            ),
            "kalshi-bot phase3bb-r2-general-candidate-routing --output-dir reports/phase3bb_r2",
            "kalshi-bot market-coverage-doctor --output-dir reports/market_coverage",
            "kalshi-bot phase3az-gap-analysis --output-dir reports/phase3az --reports-dir reports",
        ],
    }


def write_phase3bb_general_reclassification_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3bb_r3"),
    sample_limit: int = 200,
) -> Phase3BBR3GeneralReclassificationArtifactSet:
    payload = build_phase3bb_general_reclassification(session, sample_limit=sample_limit)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3bb_r3_general_reclassification.json"
    markdown_path = output_dir / "phase3bb_r3_general_reclassification.md"
    candidates_path = output_dir / "phase3bb_r3_reclassification_candidates.json"
    manual_review_path = output_dir / "phase3bb_r3_manual_review_rows.json"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    candidates_path.write_text(
        json.dumps(
            payload["reclassification_candidates"],
            indent=2,
            sort_keys=True,
            default=str,
        ),
        encoding="utf-8",
    )
    manual_review_path.write_text(
        json.dumps(payload["manual_review_rows"], indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_r3_markdown(payload), encoding="utf-8")
    return Phase3BBR3GeneralReclassificationArtifactSet(
        output_dir,
        json_path,
        markdown_path,
        candidates_path,
        manual_review_path,
    )


def write_phase3bb_r3_safe_parser_reparse_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3bb_r3"),
    sample_limit: int = 1000,
) -> Phase3BBR3SafeParserReparseArtifactSet:
    preflight = build_phase3bb_general_reclassification(session, sample_limit=sample_limit)
    safe_rows = [
        row
        for row in preflight["reclassification_candidates"]
        if row.get("safe_to_reparse")
        and (row.get("parser_preview") or {}).get("safe_to_reparse")
    ]
    tickers = sorted({str(row["ticker"]) for row in safe_rows})
    payload: dict[str, Any] = {
        "generated_at": utc_now().isoformat(),
        "phase": "3BB-R3",
        "phase_version": "phase3bb_r3_safe_parser_reparse_v1",
        "mode": "PAPER_ONLY_CONTROLLED_MARKET_LEG_REFRESH",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "status": "NO_SAFE_ROWS",
        "safety_gate": {
            "preview_rebuilt_before_write": True,
            "writes_market_legs": False,
            "writes_links": False,
            "runs_phase3ae": False,
            "live_or_demo_execution": False,
            "scope": "exact parser-preview-safe tickers only",
        },
        "preflight_summary": preflight["summary"],
        "summary": {
            "rows_safe_to_reparse": len(safe_rows),
            "rows_reparsed": 0,
            "rows_deleted": 0,
            "rows_inserted": 0,
            "missing_markets": 0,
        },
        "safe_rows": safe_rows,
        "tickers_to_reparse": tickers,
        "missing_tickers": [],
        "next_commands": [
            "kalshi-bot phase3bb-r3-general-reclassification --output-dir reports/phase3bb_r3",
            "kalshi-bot market-coverage-doctor --output-dir reports/market_coverage",
            "kalshi-bot link-coverage --output reports/link_coverage_report.md",
        ],
    }

    if tickers:
        markets = {
            market.ticker: market
            for market in session.scalars(
                select(Market).where(Market.ticker.in_(tickers)).order_by(Market.ticker)
            )
        }
        missing_tickers = sorted(set(tickers) - set(markets))
        payload["missing_tickers"] = missing_tickers
        payload["summary"]["missing_markets"] = len(missing_tickers)
        if missing_tickers:
            payload["status"] = "BLOCKED_MISSING_MARKETS"
        else:
            delete_result = session.execute(delete(MarketLeg).where(MarketLeg.ticker.in_(tickers)))
            rows_deleted = int(delete_result.rowcount or 0)
            rows_inserted = 0
            parsed_at = utc_now()
            for ticker in tickers:
                for parsed in parse_market_legs(markets[ticker]):
                    session.add(
                        MarketLeg(
                            ticker=ticker,
                            leg_index=parsed.leg_index,
                            parsed_at=parsed_at,
                            side=parsed.side,
                            category=parsed.category,
                            market_type=parsed.market_type,
                            entity_name=parsed.entity_name,
                            operator=parsed.operator,
                            threshold_value=parsed.threshold_value,
                            unit=parsed.unit,
                            confidence=parsed.confidence,
                            raw_text=parsed.raw_text,
                            reason=parsed.reason,
                            raw_json=encode_json(parsed.raw_json),
                        )
                    )
                    rows_inserted += 1
            session.flush()
            payload["status"] = "APPLIED"
            payload["safety_gate"]["writes_market_legs"] = True
            payload["summary"].update(
                {
                    "rows_reparsed": len(tickers),
                    "rows_deleted": rows_deleted,
                    "rows_inserted": rows_inserted,
                }
            )

    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3bb_r3_safe_parser_reparse.json"
    markdown_path = output_dir / "phase3bb_r3_safe_parser_reparse.md"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_r3_safe_parser_reparse_markdown(payload), encoding="utf-8")
    summary = payload["summary"]
    return Phase3BBR3SafeParserReparseArtifactSet(
        output_dir=output_dir,
        json_path=json_path,
        markdown_path=markdown_path,
        rows_safe_to_reparse=int(summary["rows_safe_to_reparse"]),
        rows_reparsed=int(summary["rows_reparsed"]),
        rows_deleted=int(summary["rows_deleted"]),
        rows_inserted=int(summary["rows_inserted"]),
    )


def write_phase3bb_r3_exact_sports_link_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3bb_r3"),
    apply: bool = False,
    sample_limit: int = 1000,
) -> Phase3BBR3ExactSportsLinkArtifactSet:
    preview_rows = _r3_exact_sports_link_preview_rows(session, sample_limit=sample_limit)
    safe_rows = [row for row in preview_rows if row["safe_to_link"]]
    links_created = 0
    if apply and safe_rows:
        for row in safe_rows:
            _, was_created = insert_sports_market_link(
                session,
                ticker=str(row["ticker"]),
                league=str(row["league"]),
                game_key=str(row["game_key"]),
                market_type=str(row["market_type"]),
                link_confidence=Decimal(str(row["link_confidence"])),
                link_reason=str(row["link_reason"]),
                matched_terms=list(row["matched_terms"]),
                raw_json=dict(row["raw_json"]),
            )
            links_created += int(was_created)
        session.flush()

    payload = {
        "generated_at": utc_now().isoformat(),
        "phase": "3BB-R3",
        "phase_version": "phase3bb_r3_exact_sports_link_v1",
        "mode": (
            "PAPER_ONLY_EXACT_SPORTS_LINK_APPLY"
            if apply
            else "PAPER_ONLY_EXACT_SPORTS_LINK_PREVIEW"
        ),
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "status": "APPLIED" if apply and links_created else "PREVIEW_ONLY",
        "safety_gate": {
            "writes_links": bool(apply and links_created),
            "writes_market_legs": False,
            "runs_phase3ae": False,
            "live_or_demo_execution": False,
            "scope": "current unlinked CS2/Valorant/cricket sports tickers only",
            "provenance": "kalshi_event_derived",
        },
        "summary": {
            "candidate_rows": len(preview_rows),
            "rows_safe_to_link": len(safe_rows),
            "blocked_rows": len(preview_rows) - len(safe_rows),
            "links_created": links_created,
            "apply": apply,
        },
        "preview_rows": preview_rows,
        "safe_tickers": [row["ticker"] for row in safe_rows],
        "blocked_rows": [row for row in preview_rows if not row["safe_to_link"]],
        "next_commands": [
            "kalshi-bot phase3bb-r3-exact-sports-link --apply --output-dir reports/phase3bb_r3"
            if not apply and safe_rows
            else "# exact sports link apply already complete or no safe rows",
            "kalshi-bot market-coverage-doctor --output-dir reports/market_coverage",
            "kalshi-bot link-coverage --output reports/link_coverage_report.md",
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3bb_r3_exact_sports_link.json"
    markdown_path = output_dir / "phase3bb_r3_exact_sports_link.md"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_r3_exact_sports_link_markdown(payload), encoding="utf-8")
    return Phase3BBR3ExactSportsLinkArtifactSet(
        output_dir=output_dir,
        json_path=json_path,
        markdown_path=markdown_path,
        rows_safe_to_link=len(safe_rows),
        links_created=links_created,
        apply=apply,
    )


def build_phase3bb_r3_composite_preview_gate(
    session: Session,
    *,
    sample_limit: int = 50000,
) -> dict[str, Any]:
    """Classify unsupported KXMVE composites without link-remediation writes."""

    category_counts = _r3_composite_category_counts(session)
    rows = _r3_composite_preview_rows(session, sample_limit=sample_limit)
    verified_rows = [
        row for row in rows if row["classification"] == "VERIFIED_COMPONENT_EVIDENCE"
    ]
    mapped_rows = [row for row in rows if row["component_mapping_status"] == "MAPPED"]
    true_rows = [
        row
        for row in rows
        if row["classification"]
        in {"TRUE_COMPOSITE_NO_COMPONENT_MAPPING", "COMPONENT_MAPPING_INVALID"}
    ]
    total_unsupported = sum(row["markets"] for row in category_counts.values())
    classification_counts = Counter(str(row["classification"]) for row in rows)
    category_sample_counts: Counter[str] = Counter()
    for row in rows:
        category_sample_counts.update(str(category) for category in row["unsupported_categories"])

    status = (
        "VERIFIED_COMPONENT_REVIEW_READY"
        if verified_rows
        else "TRUE_COMPOSITE_BACKLOG"
        if true_rows
        else "NO_UNSUPPORTED_COMPOSITES_IN_PREVIEW"
    )
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3BB-R3",
        "phase_version": "phase3bb_r3_composite_preview_gate_v1",
        "mode": "PAPER_ONLY_COMPOSITE_PREVIEW_GATE",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "status": status,
        "safety_gate": {
            "writes_market_legs": False,
            "writes_links": False,
            "writes_settlements": False,
            "runs_phase3ae": False,
            "runs_single_market_remediation": False,
            "live_or_demo_execution": False,
            "safe_to_apply_rows": 0,
            "reason": (
                "This gate only classifies KXMVE composite rows. It does not create "
                "single-market links, refresh market legs, or promote composites."
            ),
        },
        "summary": {
            "unsupported_composite_markets_from_category_counts": total_unsupported,
            "rows_reviewed": len(rows),
            "sample_limit": sample_limit,
            "sample_truncated": len(rows) >= sample_limit if sample_limit > 0 else False,
            "true_composite_rows": len(true_rows),
            "component_mapped_rows": len(mapped_rows),
            "component_mapped_unverified_rows": sum(
                1
                for row in mapped_rows
                if row["classification"] != "VERIFIED_COMPONENT_EVIDENCE"
            ),
            "verified_component_evidence_rows": len(verified_rows),
            "safe_to_apply_rows": 0,
            "classification_counts": dict(sorted(classification_counts.items())),
            "category_counts": category_counts,
            "preview_category_counts": dict(sorted(category_sample_counts.items())),
        },
        "rows": rows,
        "verified_component_evidence_rows": verified_rows,
        "true_composite_rows": true_rows,
        "recommended_next_action": _r3_composite_recommended_next_action(
            verified_rows=verified_rows,
            true_rows=true_rows,
        ),
        "next_commands": _r3_composite_next_commands(verified_rows=verified_rows),
    }


def write_phase3bb_r3_composite_preview_gate_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3bb_r3_composites"),
    sample_limit: int = 50000,
) -> Phase3BBR3CompositePreviewGateArtifactSet:
    payload = build_phase3bb_r3_composite_preview_gate(
        session,
        sample_limit=sample_limit,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3bb_r3_composite_preview_gate.json"
    markdown_path = output_dir / "phase3bb_r3_composite_preview_gate.md"
    rows_path = output_dir / "phase3bb_r3_composite_preview_rows.json"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    rows_path.write_text(
        json.dumps(payload["rows"], indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_r3_composite_preview_gate_markdown(payload), encoding="utf-8")
    summary = payload["summary"]
    return Phase3BBR3CompositePreviewGateArtifactSet(
        output_dir=output_dir,
        json_path=json_path,
        markdown_path=markdown_path,
        rows_path=rows_path,
        rows_reviewed=int(summary["rows_reviewed"]),
        verified_component_evidence_rows=int(summary["verified_component_evidence_rows"]),
        true_composite_rows=int(summary["true_composite_rows"]),
    )


def build_phase3bb_r3_composite_operator_preflight(
    session: Session,
    *,
    preview_path: Path = Path(
        "reports/phase3bb_r3_composites/phase3bb_r3_composite_preview_gate.json"
    ),
    max_quote_age_minutes: int = 30,
    min_liquidity_dollars: Decimal = Decimal("1"),
    sample_limit: int = 1000,
) -> dict[str, Any]:
    """Preflight verified-component composites without creating paper trades."""

    if not preview_path.exists():
        return _r3_composite_operator_preflight_missing_payload(
            preview_path=preview_path,
            max_quote_age_minutes=max_quote_age_minutes,
            min_liquidity_dollars=min_liquidity_dollars,
        )
    preview = json.loads(preview_path.read_text(encoding="utf-8"))
    verified_rows = list(preview.get("verified_component_evidence_rows") or [])
    rows = [
        _r3_composite_operator_preflight_row(
            session,
            row,
            max_quote_age_minutes=max_quote_age_minutes,
            min_liquidity_dollars=min_liquidity_dollars,
        )
        for row in verified_rows[:sample_limit]
    ]
    ready_rows = [row for row in rows if row["paper_composite_review_ready"]]
    blocked_rows = [row for row in rows if not row["paper_composite_review_ready"]]
    blocker_counts = Counter(
        reason for row in blocked_rows for reason in row.get("block_reasons", [])
    )
    status = "PAPER_COMPOSITE_REVIEW_READY" if ready_rows else "PAPER_COMPOSITE_REVIEW_BLOCKED"
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3BB-R3",
        "phase_version": "phase3bb_r3_composite_operator_preflight_v1",
        "mode": "PAPER_ONLY_COMPOSITE_OPERATOR_PREFLIGHT",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "status": status,
        "preview_source_path": str(preview_path),
        "preview_status": preview.get("status"),
        "safety_gate": {
            "writes_market_legs": False,
            "writes_links": False,
            "writes_settlements": False,
            "creates_paper_trades": False,
            "runs_single_market_remediation": False,
            "live_or_demo_execution": False,
            "safe_to_apply_rows": 0,
            "reason": (
                "This preflight only checks whether verified-component composites are "
                "ready for paper-only operator review. It does not place or create trades."
            ),
        },
        "settings": {
            "max_quote_age_minutes": max_quote_age_minutes,
            "min_liquidity_dollars": str(min_liquidity_dollars),
            "sample_limit": sample_limit,
        },
        "summary": {
            "verified_component_rows_from_preview": len(verified_rows),
            "rows_reviewed": len(rows),
            "sample_truncated": len(verified_rows) > len(rows),
            "paper_composite_review_ready_rows": len(ready_rows),
            "blocked_rows": len(blocked_rows),
            "safe_to_apply_rows": 0,
            "blocker_counts": dict(sorted(blocker_counts.items())),
        },
        "rows": rows,
        "paper_composite_review_ready_rows": ready_rows,
        "blocked_rows": blocked_rows,
        "recommended_next_action": _r3_composite_operator_preflight_next_action(ready_rows),
        "next_commands": _r3_composite_operator_preflight_next_commands(ready_rows),
    }


def write_phase3bb_r3_composite_operator_preflight_report(
    session: Session,
    *,
    preview_path: Path = Path(
        "reports/phase3bb_r3_composites/phase3bb_r3_composite_preview_gate.json"
    ),
    output_dir: Path = Path("reports/phase3bb_r3_composites"),
    max_quote_age_minutes: int = 30,
    min_liquidity_dollars: Decimal = Decimal("1"),
    sample_limit: int = 1000,
) -> Phase3BBR3CompositeOperatorPreflightArtifactSet:
    payload = build_phase3bb_r3_composite_operator_preflight(
        session,
        preview_path=preview_path,
        max_quote_age_minutes=max_quote_age_minutes,
        min_liquidity_dollars=min_liquidity_dollars,
        sample_limit=sample_limit,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3bb_r3_composite_operator_preflight.json"
    markdown_path = output_dir / "phase3bb_r3_composite_operator_preflight.md"
    rows_path = output_dir / "phase3bb_r3_composite_operator_preflight_rows.json"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    rows_path.write_text(
        json.dumps(payload["rows"], indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(
        _render_r3_composite_operator_preflight_markdown(payload),
        encoding="utf-8",
    )
    summary = payload["summary"]
    return Phase3BBR3CompositeOperatorPreflightArtifactSet(
        output_dir=output_dir,
        json_path=json_path,
        markdown_path=markdown_path,
        rows_path=rows_path,
        paper_composite_review_ready_rows=int(
            summary["paper_composite_review_ready_rows"]
        ),
        blocked_rows=int(summary["blocked_rows"]),
    )


def _source_intake_command_args(
    *,
    output_dir: Path | None,
    input_file: Path | None,
    evidence_dir: Path,
    limit_per_bucket: int,
    write_evidence_files: bool,
) -> dict[str, Any]:
    return {
        "command": "kalshi-bot phase3bb-r2-general-source-intake",
        "output_dir": str(output_dir) if output_dir is not None else None,
        "input_file": str(input_file) if input_file is not None else None,
        "evidence_dir": str(evidence_dir),
        "limit_per_bucket": limit_per_bucket,
        "write_evidence_files": write_evidence_files,
    }


def _phase3bb_r2_report_metadata(
    session: Session,
    *,
    command_args: dict[str, Any],
) -> dict[str, Any]:
    bind = session.get_bind()
    if bind is None or getattr(bind, "url", None) is None:
        raise RuntimeError(
            "Phase 3BB-R2 source intake failed closed: database identity is ambiguous"
        )
    db_url = str(bind.url)
    if not db_url:
        raise RuntimeError("Phase 3BB-R2 source intake failed closed: DATABASE_URL is empty")
    redacted_db_url = redact_database_url(db_url)
    repo_root = _repo_root()
    package_path = Path(kalshi_predictor.__file__).resolve()
    db_location = describe_db_location(db_url)
    fingerprint = _database_fingerprint(redacted_db_url=redacted_db_url, location=db_location)
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3BB-R2",
        "phase_version": "phase3bb_r2_general_source_intake_v2",
        "repository_root": str(repo_root),
        "git_branch": _git_value(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
        or "UNKNOWN_GIT_BRANCH",
        "git_commit": _git_value(repo_root, "rev-parse", "HEAD") or "UNKNOWN_GIT_COMMIT",
        "python_executable": str(Path(sys.executable).resolve()),
        "installed_package_path": str(package_path),
        "resolved_database_url": redacted_db_url,
        "database_location": db_location,
        "database_backend": detect_backend(None, db_url=db_url),
        "database_fingerprint": fingerprint,
        "migration_revision": _migration_revision(session),
        "cli_database_identity": {
            "database_url": redacted_db_url,
            "database_location": db_location,
            "database_fingerprint": fingerprint,
        },
        "ui_database_identity": _ui_database_identity(),
        "timezone": time.tzname[0] if time.tzname else "unknown",
        "paper_report_only_safety_state": GENERAL_SOURCE_SAFETY_MODE,
        "safety_mode": GENERAL_SOURCE_SAFETY_MODE,
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "command_arguments": command_args,
        "data_watermark": _data_watermark(session),
        "taxonomy_version": GENERAL_SOURCE_TAXONOMY_VERSION,
        "source_readiness_schema_version": GENERAL_SOURCE_READINESS_SCHEMA_VERSION,
        "downstream_table_counts": _downstream_table_counts(session),
    }


def _top_level_report_fields(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "git_commit": metadata["git_commit"],
        "database_fingerprint": metadata["database_fingerprint"],
        "command_arguments": metadata["command_arguments"],
        "data_watermark": metadata["data_watermark"],
        "taxonomy_version": metadata["taxonomy_version"],
        "source_readiness_schema_version": metadata["source_readiness_schema_version"],
    }


def _section_report(
    payload: dict[str, Any],
    report_type: str,
    section_key: str,
) -> dict[str, Any]:
    metadata = payload["report_metadata"]
    return {
        **_top_level_report_fields(metadata),
        "generated_at": metadata["generated_at"],
        "phase": payload["phase"],
        "phase_version": payload["phase_version"],
        "report_type": report_type,
        "safety_mode": GENERAL_SOURCE_SAFETY_MODE,
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "report_metadata": metadata,
        "summary": payload["summary"],
        "data": payload[section_key],
    }


def _repo_root() -> Path:
    path = Path(__file__).resolve()
    for parent in path.parents:
        if (parent / ".git").exists():
            return parent
    return path.parents[2]


def _git_value(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return _git_value_from_files(root, *args)
    if result.returncode != 0:
        return _git_value_from_files(root, *args)
    return result.stdout.strip() or None


def _git_value_from_files(root: Path, *args: str) -> str | None:
    if args == ("rev-parse", "HEAD"):
        return _git_head_commit(root)
    if args == ("rev-parse", "--abbrev-ref", "HEAD"):
        head = _git_head_text(root)
        if head and head.startswith("ref: refs/heads/"):
            return head.removeprefix("ref: refs/heads/")
    return None


def _git_head_text(root: Path) -> str | None:
    try:
        return (root / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _git_head_commit(root: Path) -> str | None:
    head = _git_head_text(root)
    if not head:
        return None
    if not head.startswith("ref: "):
        return head
    ref = head.removeprefix("ref: ").strip()
    try:
        return (root / ".git" / ref).read_text(encoding="utf-8").strip()
    except OSError:
        packed = root / ".git" / "packed-refs"
        try:
            lines = packed.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        for line in lines:
            if line.startswith("#") or not line.strip():
                continue
            commit, _, packed_ref = line.partition(" ")
            if packed_ref.strip() == ref:
                return commit
    return None


def _database_fingerprint(*, redacted_db_url: str, location: str) -> str:
    payload = json.dumps(
        {"database_url": redacted_db_url, "location": location},
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _migration_revision(session: Session) -> str | None:
    try:
        exists = session.execute(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='alembic_version'"
            )
        ).first()
        if exists is None:
            return None
        revision = session.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:
        return None
    return str(revision) if revision is not None else None


def _ui_database_identity() -> dict[str, Any]:
    workspace_guard = Path("reports/phase3bb/phase3bb_workspace_guard.json")
    if workspace_guard.exists():
        try:
            payload = json.loads(workspace_guard.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"status": "UNAVAILABLE", "reason": "workspace guard JSON could not be read"}
        database = payload.get("guard", {}).get("database") if isinstance(payload, dict) else None
        if database:
            return {"status": "AVAILABLE_FROM_WORKSPACE_GUARD", "database": database}
    return {
        "status": "NOT_AVAILABLE",
        "reason": "No UI database identity artifact was available to this CLI report.",
    }


def _data_watermark(session: Session) -> dict[str, Any]:
    return {
        "market_rows": _count(session, Market.ticker),
        "general_market_leg_rows": int(
            session.scalar(
                select(func.count(MarketLeg.id)).where(MarketLeg.category == DOMAIN_GENERAL)
            )
            or 0
        ),
        "latest_market_last_seen_at": _iso_or_none(_max_value(session, Market.last_seen_at)),
        "latest_general_leg_parsed_at": _iso_or_none(_latest_leg_time(session, DOMAIN_GENERAL)),
    }


def _downstream_table_counts(session: Session) -> dict[str, int]:
    return {
        "links": _count(session, EconomicMarketLink.id) + _count(session, NewsMarketLink.id),
        "features": _count(session, EconomicFeature.id) + _count(session, NewsFeature.id),
        "forecasts": _count(session, Forecast.id),
        "opportunities": _count(session, MarketOpportunity.id),
        "paper_orders": _count(session, PaperOrder.id),
    }


def _source_intake_taxonomy_review_row(item: dict[str, Any]) -> dict[str, Any]:
    bucket = _general_taxonomy_bucket(item)
    label = _source_intake_taxonomy_label(bucket)
    confidence = _taxonomy_confidence(item, label)
    parsed_proposition = "; ".join(str(leg) for leg in item.get("legs") or [])
    row = {
        "market_ticker": item["ticker"],
        "event_ticker": item.get("event_ticker"),
        "series_ticker": item.get("series_ticker"),
        "title": item.get("title"),
        "subtitle": item.get("subtitle"),
        "market_status": item.get("status"),
        "current_category": DOMAIN_GENERAL,
        "parsed_proposition": parsed_proposition,
        "taxonomy_label": label,
        "taxonomy_confidence": confidence,
        "taxonomy_reason": _taxonomy_reason(item, label, bucket),
        "source_evidence_required": _source_evidence_required(label),
        "known_blockers": _source_intake_blockers(item, label),
        "suggested_next_action": _source_intake_market_next_action(label),
        "sample_legs": list(item.get("legs") or [])[:5],
        "market_types": list(item.get("market_types") or []),
        "parser_reason_samples": list(item.get("parser_reasons") or [])[:3],
        "safe_to_link": False,
        "safe_to_feature": False,
        "safe_to_forecast": False,
        "safe_to_trade": False,
        "proposed_db_writes": 0,
    }
    row.update(_source_family_diagnostics(row, item, label))
    return row


def _source_intake_taxonomy_label(bucket: str) -> str:
    if bucket in {
        "COMMODITY_PRICE_CANDIDATE",
        "TRANSPORTATION_OPERATION_CANDIDATE",
        "INFRASTRUCTURE_CAPACITY_CANDIDATE",
        "SPORTS_OR_CROSS_CATEGORY_LEAKAGE",
        "GENERAL_UNCLASSIFIED",
    }:
        return bucket
    if bucket == "UNSUPPORTED_MULTI_LEG_GENERAL":
        return "UNSUPPORTED"
    if bucket in {
        "ECONOMIC_CANDIDATE",
        "POLITICS_NEWS_CANDIDATE",
        "COMPANY_NEWS_CANDIDATE",
        "GEOPOLITICAL_NEWS_CANDIDATE",
    }:
        return "NEEDS_HUMAN_REVIEW"
    return "AMBIGUOUS"


def _taxonomy_confidence(item: dict[str, Any], label: str) -> str:
    numeric: list[float] = []
    for value in item.get("confidences") or []:
        try:
            numeric.append(float(value))
        except (TypeError, ValueError):
            continue
    if numeric:
        return f"{max(numeric):.2f}"
    defaults = {
        "COMMODITY_PRICE_CANDIDATE": "0.78",
        "TRANSPORTATION_OPERATION_CANDIDATE": "0.78",
        "INFRASTRUCTURE_CAPACITY_CANDIDATE": "0.78",
        "SPORTS_OR_CROSS_CATEGORY_LEAKAGE": "0.82",
        "GENERAL_UNCLASSIFIED": "0.45",
        "UNSUPPORTED": "0.60",
        "NEEDS_HUMAN_REVIEW": "0.35",
        "AMBIGUOUS": "0.30",
    }
    return defaults.get(label, "0.30")


def _taxonomy_reason(item: dict[str, Any], label: str, bucket: str) -> str:
    text_value = _taxonomy_text(item)
    terms = _matched_terms(text_value, bucket)
    if terms:
        return f"Matched metadata terms: {', '.join(terms)}."
    if label == "UNSUPPORTED":
        return "Multiple general legs require a structured component parser before routing."
    if label == "GENERAL_UNCLASSIFIED":
        return "No supported source-family, sports leakage, or multi-leg rule matched."
    if label == "NEEDS_HUMAN_REVIEW":
        return "Metadata resembles another domain, but this R2 report cannot safely reroute it."
    return "Low-confidence metadata requires manual review."


def _source_evidence_required(label: str) -> list[str]:
    requirements = {
        "COMMODITY_PRICE_CANDIDATE": [
            "named commodity or entity",
            "price or index metric",
            "comparator and threshold",
            "observation date or settlement window",
            "official source URL and publication timestamp",
            "point-in-time availability proof",
        ],
        "TRANSPORTATION_OPERATION_CANDIDATE": [
            "transportation entity",
            "operation metric",
            "location, airport, route, or region",
            "time window",
            "source timestamp and freshness rule",
            "point-in-time no-leakage proof",
        ],
        "INFRASTRUCTURE_CAPACITY_CANDIDATE": [
            "infrastructure entity",
            "capacity or utilization metric",
            "geography",
            "observation date or window",
            "source availability and licensing review",
            "point-in-time availability proof",
        ],
        "SPORTS_OR_CROSS_CATEGORY_LEAKAGE": [
            "sports placeholder and schedule evidence",
            "Phase 3AH evidence path",
            "explicit non-general routing review",
        ],
        "GENERAL_UNCLASSIFIED": [
            "complete title and rule metadata",
            "settlement source identification",
            "entity and metric disambiguation",
            "human source-family review",
        ],
    }
    return requirements.get(
        label,
        ["human review", "source-family design", "point-in-time safety assessment"],
    )


def _source_intake_blockers(item: dict[str, Any], label: str) -> list[str]:
    blockers = {
        "COMMODITY_PRICE_CANDIDATE": [
            "USDA values are currently unavailable",
            "commodity source values are not configured in this phase",
            "no provenance-backed observed value",
            "point-in-time source safety not proven",
        ],
        "TRANSPORTATION_OPERATION_CANDIDATE": [
            "FlightAware or transportation source remains review-gated",
            "entity/time-window ambiguity tests have not passed",
            "point-in-time source safety not proven",
        ],
        "INFRASTRUCTURE_CAPACITY_CANDIDATE": [
            "Cushman or infrastructure source values are unavailable",
            "proprietary/licensing review is required",
            "point-in-time source safety not proven",
        ],
        "SPORTS_OR_CROSS_CATEGORY_LEAKAGE": [
            "not safe for general source diagnostics",
            "sports links must not be created by this command",
            "Phase 3AH evidence remains required",
        ],
        "GENERAL_UNCLASSIFIED": _unclassified_reason_codes(item),
        "UNSUPPORTED": ["unsupported market shape", "manual parser design required"],
        "AMBIGUOUS": ["taxonomy confidence is low", "manual review required"],
        "NEEDS_HUMAN_REVIEW": ["metadata resembles another domain", "manual review required"],
    }
    return blockers.get(label, ["manual review required"])


def _source_intake_market_next_action(label: str) -> str:
    actions = {
        "COMMODITY_PRICE_CANDIDATE": (
            "Collect official commodity source evidence; keep link and forecast gates blocked."
        ),
        "TRANSPORTATION_OPERATION_CANDIDATE": (
            "Run transportation source review and ambiguity tests before any adapter work."
        ),
        "INFRASTRUCTURE_CAPACITY_CANDIDATE": (
            "Resolve infrastructure source availability and licensing before any adapter work."
        ),
        "SPORTS_OR_CROSS_CATEGORY_LEAKAGE": (
            "Route to sports or cross-category diagnostics; do not create links here."
        ),
        "GENERAL_UNCLASSIFIED": (
            "Keep unclassified and request human review for source-family design."
        ),
        "UNSUPPORTED": "Keep blocked until a specific report-only parser design exists.",
    }
    return actions.get(label, "Keep blocked for manual review.")


def _source_family_diagnostics(
    row: dict[str, Any],
    item: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    if label == "COMMODITY_PRICE_CANDIDATE":
        return {"commodity_diagnostic": _commodity_candidate_diagnostic(row, item)}
    if label == "TRANSPORTATION_OPERATION_CANDIDATE":
        return {"transportation_diagnostic": _transportation_candidate_diagnostic(row, item)}
    if label == "INFRASTRUCTURE_CAPACITY_CANDIDATE":
        return {"infrastructure_diagnostic": _infrastructure_candidate_diagnostic(row, item)}
    if label == "SPORTS_OR_CROSS_CATEGORY_LEAKAGE":
        return {"leakage_diagnostic": _sports_or_cross_category_diagnostic(item)}
    if label == "GENERAL_UNCLASSIFIED":
        return {"unclassified_diagnostic": _general_unclassified_diagnostic(item)}
    return {}


def _commodity_candidate_diagnostic(
    row: dict[str, Any],
    item: dict[str, Any],
) -> dict[str, Any]:
    text_value = _taxonomy_text(item)
    return {
        "commodity_or_entity": "Avocados, Hass" if "avocado" in text_value else "unknown",
        "price_or_index_metric": "weighted_average_advertised_price",
        "comparator": _direction_from_text(str(row.get("title") or "")),
        "threshold_or_strike": _threshold_from_ticker_or_text(
            str(row.get("market_ticker") or ""),
            str(row.get("title") or ""),
        ),
        "observation_date_time": _general_signal_time_window(
            "COMMODITY_PRICE_CANDIDATE",
            str(row.get("title") or ""),
        ),
        "settlement_source_named": _named_source(item, fallback="USDA_OR_COMMODITY_SOURCE"),
        "appears_to_require": _required_source_flags("commodity", item),
        "source_readiness_state": "CONFIGURED_NO_VALUES",
        "source_readiness_blocker": "USDA values are currently unavailable.",
        "point_in_time_risk": "HIGH_UNTIL_PUBLICATION_TIMESTAMP_AND_CAPTURE_ARE_PROVEN",
        "link_safety_state": "BLOCKED_SOURCE_VALUES_UNAVAILABLE",
        "forecast_safety_state": "BLOCKED_SOURCE_VALUES_UNAVAILABLE",
    }


def _transportation_candidate_diagnostic(
    row: dict[str, Any],
    item: dict[str, Any],
) -> dict[str, Any]:
    title = str(row.get("title") or "")
    return {
        "transportation_entity": "United States flights"
        if "united states" in title.lower()
        else "unknown",
        "metric": "total_flight_cancellations",
        "location_airport_route": _general_signal_region(
            "TRANSPORTATION_OPERATION_CANDIDATE",
            title,
        ),
        "time_window": _general_signal_time_window(
            "TRANSPORTATION_OPERATION_CANDIDATE",
            title,
        ),
        "settlement_source": _named_source(item, fallback="FLIGHTAWARE_OR_TRANSPORTATION_SOURCE"),
        "ambiguity_risk": "HIGH_UNTIL_ENTITY_WINDOW_AND_SOURCE_TIMESTAMP_TESTS_PASS",
        "appears_to_require": _required_source_flags("transportation", item),
        "required_source": "FlightAware or equivalent transportation operations source",
        "readiness_state": "READY_FOR_REVIEW",
        "blocker": (
            "FlightAware may be reviewed, but link and forecast gates require mapping, "
            "freshness, point-in-time, and explicit approval tests."
        ),
        "next_action": "Run report-only entity, window, freshness, and no-leakage tests.",
    }


def _infrastructure_candidate_diagnostic(
    row: dict[str, Any],
    item: dict[str, Any],
) -> dict[str, Any]:
    title = str(row.get("title") or "")
    return {
        "infrastructure_entity": "operational data center capacity",
        "capacity_or_utilization_metric": "operational_data_center_capacity",
        "geography": _general_signal_region("INFRASTRUCTURE_CAPACITY_CANDIDATE", title),
        "observation_date_window": _general_signal_time_window(
            "INFRASTRUCTURE_CAPACITY_CANDIDATE",
            title,
        ),
        "source_named_in_market": _named_source(item, fallback="CUSHMAN_OR_INFRASTRUCTURE_SOURCE"),
        "appears_to_require": _required_source_flags("infrastructure", item),
        "source_availability": "VALUES_UNAVAILABLE",
        "proprietary_licensing_concern": True,
        "readiness_state": "PROPRIETARY_SOURCE_REVIEW_REQUIRED",
        "blocker": "Cushman-backed or proprietary capacity values are unavailable.",
        "next_action": (
            "Resolve licensing and point-in-time source availability before adapter work."
        ),
    }


def _sports_or_cross_category_diagnostic(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "reason_codes": _sports_leakage_reason_codes(item),
        "recommended_route": _sports_leakage_route(item),
        "blocker": "This command must not create sports links or treat placeholders as teams.",
    }


def _general_unclassified_diagnostic(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "reason_codes": _unclassified_reason_codes(item),
        "recommended_route": "manual_general_source_family_review",
        "blocker": (
            "Insufficient metadata to select a source family without forcing classification."
        ),
    }


def _source_evidence_requirement_row(row: dict[str, Any]) -> dict[str, Any]:
    label = str(row["taxonomy_label"])
    requirement = {
        "market_ticker": row["market_ticker"],
        "taxonomy_label": label,
        "title": row.get("title"),
        "source_evidence_required": row["source_evidence_required"],
        "known_blockers": row["known_blockers"],
        "source_state": _market_source_state(label),
        "link_safe": False,
        "feature_safe": False,
        "forecast_safe": False,
        "trade_safe": False,
        "proposed_db_writes": 0,
    }
    for key in (
        "commodity_diagnostic",
        "transportation_diagnostic",
        "infrastructure_diagnostic",
        "leakage_diagnostic",
        "unclassified_diagnostic",
    ):
        if key in row:
            requirement[key] = row[key]
    return requirement


def _market_source_state(label: str) -> str:
    states = {
        "COMMODITY_PRICE_CANDIDATE": "CONFIGURED_NO_VALUES",
        "TRANSPORTATION_OPERATION_CANDIDATE": "READY_FOR_REVIEW",
        "INFRASTRUCTURE_CAPACITY_CANDIDATE": "PROPRIETARY_SOURCE_REVIEW_REQUIRED",
        "SPORTS_OR_CROSS_CATEGORY_LEAKAGE": "DISABLED",
        "GENERAL_UNCLASSIFIED": "NOT_CONFIGURED",
        "UNSUPPORTED": "UNSUPPORTED_SOURCE",
        "AMBIGUOUS": "NOT_CONFIGURED",
        "NEEDS_HUMAN_REVIEW": "NOT_CONFIGURED",
    }
    return states.get(label, "NOT_CONFIGURED")


def _source_readiness_matrix() -> list[dict[str, Any]]:
    rows = [
        _source_readiness_row(
            source_name="USDA",
            source_family="commodity",
            configured=True,
            values_available=False,
            parser_valid=False,
            provenance_available=False,
            freshness_rule_defined=True,
            point_in_time_safe=False,
            review_approved=False,
            readiness_state="CONFIGURED_NO_VALUES",
            current_blocker="USDA values are currently unavailable.",
            next_action="Add official point-in-time USDA value capture before review.",
        ),
        _source_readiness_row(
            source_name="Cushman",
            source_family="infrastructure",
            configured=True,
            values_available=False,
            parser_valid=False,
            provenance_available=False,
            freshness_rule_defined=False,
            point_in_time_safe=False,
            review_approved=False,
            readiness_state="PROPRIETARY_SOURCE_REVIEW_REQUIRED",
            current_blocker="Cushman values are unavailable and licensing review is required.",
            next_action="Confirm permissible source access and redacted reporting rules.",
        ),
        _source_readiness_row(
            source_name="FlightAware",
            source_family="transportation",
            configured=True,
            values_available=False,
            parser_valid=False,
            provenance_available=False,
            freshness_rule_defined=False,
            point_in_time_safe=False,
            review_approved=False,
            readiness_state="READY_FOR_REVIEW",
            current_blocker=(
                "Entity, airport/route/time-window, freshness, no-leakage, and review "
                "approval tests have not passed."
            ),
            next_action="Run report-only FlightAware ambiguity and freshness tests.",
        ),
        _source_readiness_row(
            source_name="commodity price source TBD",
            source_family="commodity",
            configured=False,
            values_available=False,
            parser_valid=False,
            provenance_available=False,
            freshness_rule_defined=False,
            point_in_time_safe=False,
            review_approved=False,
            readiness_state="NOT_CONFIGURED",
            current_blocker="No approved commodity price source adapter is configured.",
            next_action="Choose a public or licensed source and define freshness rules.",
        ),
        _source_readiness_row(
            source_name="transportation operations source TBD",
            source_family="transportation",
            configured=False,
            values_available=False,
            parser_valid=False,
            provenance_available=False,
            freshness_rule_defined=False,
            point_in_time_safe=False,
            review_approved=False,
            readiness_state="NOT_CONFIGURED",
            current_blocker="No approved transportation operations source adapter is configured.",
            next_action="Select source and define entity/time-window mapping tests.",
        ),
        _source_readiness_row(
            source_name="infrastructure capacity source TBD",
            source_family="infrastructure",
            configured=False,
            values_available=False,
            parser_valid=False,
            provenance_available=False,
            freshness_rule_defined=False,
            point_in_time_safe=False,
            review_approved=False,
            readiness_state="NOT_CONFIGURED",
            current_blocker="No approved infrastructure capacity source adapter is configured.",
            next_action="Select source and define licensing plus point-in-time controls.",
        ),
    ]
    return rows


def _source_readiness_row(
    *,
    source_name: str,
    source_family: str,
    configured: bool,
    values_available: bool,
    parser_valid: bool,
    provenance_available: bool,
    freshness_rule_defined: bool,
    point_in_time_safe: bool,
    review_approved: bool,
    readiness_state: str,
    current_blocker: str,
    next_action: str,
) -> dict[str, Any]:
    link_safe = all(
        [
            configured,
            values_available,
            parser_valid,
            provenance_available,
            freshness_rule_defined,
            point_in_time_safe,
            review_approved,
        ]
    )
    forecast_safe = link_safe and readiness_state == "FORECAST_SAFE"
    if not values_available:
        link_safe = False
        forecast_safe = False
    if readiness_state == "READY_FOR_REVIEW":
        link_safe = False
        forecast_safe = False
    if not point_in_time_safe:
        forecast_safe = False
    if readiness_state not in GENERAL_SOURCE_ALLOWED_STATES:
        readiness_state = "PARSER_FAILED"
        link_safe = False
        forecast_safe = False
    return {
        "source_name": source_name,
        "source_family": source_family,
        "readiness_state": readiness_state,
        "configured": configured,
        "values_available": values_available,
        "parser_valid": parser_valid,
        "provenance_available": provenance_available,
        "freshness_rule_defined": freshness_rule_defined,
        "point_in_time_safe": point_in_time_safe,
        "review_approved": review_approved,
        "link_safe": link_safe,
        "forecast_safe": forecast_safe,
        "current_blocker": current_blocker,
        "next_action": next_action,
    }


def _candidate_market_samples(
    taxonomy_rows: list[dict[str, Any]],
    *,
    limit_per_bucket: int,
) -> dict[str, list[dict[str, Any]]]:
    samples: dict[str, list[dict[str, Any]]] = {}
    for row in taxonomy_rows:
        label = str(row["taxonomy_label"])
        samples.setdefault(label, [])
        if len(samples[label]) >= limit_per_bucket:
            continue
        samples[label].append(
            {
                "market_ticker": row["market_ticker"],
                "event_ticker": row.get("event_ticker"),
                "series_ticker": row.get("series_ticker"),
                "title": row.get("title"),
                "taxonomy_label": label,
                "taxonomy_reason": row.get("taxonomy_reason"),
                "known_blockers": row.get("known_blockers"),
                "suggested_next_action": row.get("suggested_next_action"),
            }
        )
    return dict(sorted(samples.items()))


def _source_intake_next_actions(
    *,
    taxonomy_rows: list[dict[str, Any]],
    source_readiness_matrix: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    counts = _count_values(taxonomy_rows, "taxonomy_label")
    blocked_sources = [
        row["source_name"] for row in source_readiness_matrix if not row["forecast_safe"]
    ]
    return [
        {
            "priority": 1,
            "action_type": "code",
            "reason": "Keep the general-domain evidence bundle current for Phase 3AZ/3W.",
            "exact_command": (
                "kalshi-bot phase3bb-r2-general-source-intake "
                "--output-dir reports/phase3bb_r2_sources"
            ),
            "expected_output": (
                "canonical intake, taxonomy, readiness, sample, and next-action reports"
            ),
            "success_criteria": (
                "All reports regenerate with zero link/feature/forecast/trade writes."
            ),
            "stop_condition": "Stop if database identity is ambiguous or the command fails.",
            "safe_while_another_db_writer_is_active": True,
        },
        {
            "priority": 2,
            "action_type": "data-provider",
            "reason": f"Blocked source families require evidence: {', '.join(blocked_sources)}.",
            "exact_command": None,
            "expected_output": "provider source URLs, publication timestamps, and observed values",
            "success_criteria": "Values are provenance-backed and point-in-time safe.",
            "stop_condition": "Stop if source values are unavailable, proprietary, or ambiguous.",
            "safe_while_another_db_writer_is_active": True,
        },
        {
            "priority": 3,
            "action_type": "review",
            "reason": (
                f"{counts.get('SPORTS_OR_CROSS_CATEGORY_LEAKAGE', 0)} leakage row(s) "
                "must stay out of general-source processing."
            ),
            "exact_command": (
                "kalshi-bot phase3ah-sports-placeholder-watch "
                "--output-dir reports/phase3ah_sports"
            ),
            "expected_output": "sports placeholder and schedule evidence report",
            "success_criteria": (
                "Leakage rows remain routed away from general without unsafe links."
            ),
            "stop_condition": "Stop if placeholders or teams are ambiguous.",
            "safe_while_another_db_writer_is_active": True,
        },
        {
            "priority": 4,
            "action_type": "operator",
            "reason": (
                f"{counts.get('GENERAL_UNCLASSIFIED', 0)} unclassified row(s) should not "
                "be forced into a source family."
            ),
            "exact_command": None,
            "expected_output": "manual source-family notes or explicit unsupported classification",
            "success_criteria": "Each reviewed row has an evidence-backed source-family decision.",
            "stop_condition": "Stop if title/rule metadata is insufficient.",
            "safe_while_another_db_writer_is_active": True,
        },
        {
            "priority": 5,
            "action_type": "safety",
            "reason": "This phase must remain report-only and preserve all downstream gates.",
            "exact_command": (
                "kalshi-bot phase3az-gap-analysis --output-dir reports/phase3az "
                "--reports-dir reports"
            ),
            "expected_output": "Phase 3AZ recognizes the updated evidence bundle.",
            "success_criteria": (
                "No production links, features, forecasts, opportunities, trades, or settlements."
            ),
            "stop_condition": "Stop if any downstream write is proposed.",
            "safe_while_another_db_writer_is_active": True,
        },
    ]


def _count_values(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "UNKNOWN")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _named_source(item: dict[str, Any], *, fallback: str) -> str:
    text_value = " ".join(
        str(part or "")
        for part in (
            item.get("title"),
            item.get("subtitle"),
            item.get("rules_primary"),
            item.get("rules_secondary"),
        )
    ).lower()
    source_terms = {
        "USDA": ("usda", "ams"),
        "FlightAware": ("flightaware",),
        "Cushman": ("cushman", "wakefield"),
    }
    for name, terms in source_terms.items():
        if any(term in text_value for term in terms):
            return name
    return fallback


def _required_source_flags(source_family: str, item: dict[str, Any]) -> dict[str, bool]:
    text_value = _taxonomy_text(item)
    if source_family == "commodity":
        return {
            "usda_data": "avocado" in text_value or "advertised price" in text_value,
            "commodity_spot_price": "spot" in text_value,
            "futures_settlement_price": "future" in text_value,
            "official_index_value": "index" in text_value,
            "exchange_settlement_value": "settlement" in text_value,
            "government_release": "usda" in text_value,
            "proprietary_licensed_data": False,
            "unsupported_source": False,
        }
    if source_family == "transportation":
        return {
            "flightaware": "flight" in text_value or "cancellation" in text_value,
            "airport_operations_data": "airport" in text_value,
            "flight_cancellation_data": "cancellation" in text_value,
            "delay_data": "delay" in text_value,
            "government_transportation_data": "government" in text_value,
            "port_rail_trucking_data": any(
                term in text_value for term in ("port", "rail", "truck")
            ),
            "proprietary_operational_source": False,
            "unsupported_source": False,
        }
    return {
        "cushman": "data center" in text_value or "capacity" in text_value,
        "commercial_real_estate_capacity_data": "data center" in text_value,
        "energy_grid_data": "grid" in text_value,
        "infrastructure_utilization_data": "utilization" in text_value,
        "public_agency_data": "public agency" in text_value,
        "private_proprietary_reports": True,
        "unsupported_source": False,
    }


def _sports_leakage_reason_codes(item: dict[str, Any]) -> list[str]:
    text_value = _taxonomy_text(item)
    reasons: list[str] = []
    if _sports_market_prefix_match(text_value) or _matched_terms(
        text_value,
        "SPORTS_OR_CROSS_CATEGORY_LEAKAGE",
    ):
        reasons.append("SPORTS_TEAM_PATTERN")
    if "round" in text_value or "placeholder" in text_value:
        reasons.append("ROUND_PLACEHOLDER")
    if len(item.get("legs") or []) > 1 or "multigame" in text_value:
        reasons.append("MULTI_OPTION_SPORTS_MARKET")
    if "crosscategory" in text_value or "cross category" in text_value:
        reasons.append("CROSS_CATEGORY_COMPOSITE")
    if len(item.get("legs") or []) > 1:
        reasons.append("UNSUPPORTED_MULTI_LEG")
    if str(item.get("series_ticker") or "").lower().startswith(SPORTS_MARKET_PREFIXES):
        reasons.append("CATEGORY_MAPPER_MISMATCH")
    if not reasons:
        reasons.append("NEEDS_PHASE_3AH_EVIDENCE")
    if "NEEDS_PHASE_3AH_EVIDENCE" not in reasons:
        reasons.append("NEEDS_PHASE_3AH_EVIDENCE")
    return sorted(set(reasons))


def _sports_leakage_route(item: dict[str, Any]) -> str:
    reasons = _sports_leakage_reason_codes(item)
    if "CROSS_CATEGORY_COMPOSITE" in reasons or "UNSUPPORTED_MULTI_LEG" in reasons:
        return "cross_category_diagnostics"
    if "SPORTS_TEAM_PATTERN" in reasons or "ROUND_PLACEHOLDER" in reasons:
        return "sports_diagnostics"
    return "unsupported_diagnostics"


def _unclassified_reason_codes(item: dict[str, Any]) -> list[str]:
    text_value = _taxonomy_text(item)
    reasons: list[str] = []
    if not item.get("title") or not item.get("rules_primary"):
        reasons.append("insufficient title/rule metadata")
    if len(item.get("legs") or []) > 1:
        reasons.append("unsupported market type")
    if not _named_source(item, fallback=""):
        reasons.append("missing settlement source")
    if not re.search(r"\b[A-Z][A-Za-z0-9&.\- ]{2,}\b", str(item.get("title") or "")):
        reasons.append("ambiguous entity")
    if not _matched_terms(text_value, _general_taxonomy_bucket(item)):
        reasons.append("ambiguous metric")
    if not any(
        term in text_value
        for term in (
            "price",
            "capacity",
            "flight",
            "cancellation",
            "index",
            "data center",
        )
    ):
        reasons.append("no known source family")
    if not reasons:
        reasons.append("needs human review")
    if "needs human review" not in reasons:
        reasons.append("needs human review")
    return reasons


def _economic_row(session: Session, coverage: dict[str, Any]) -> dict[str, Any]:
    event_count = _count(session, EconomicEvent.id)
    feature_count = _count(session, EconomicFeature.id)
    link_count = _count(session, EconomicMarketLink.id)
    parsed_markets = int(coverage.get("parsed_markets") or 0)
    active_parsed_markets = _active_parsed_markets(session, DOMAIN_ECONOMIC)
    latest_event_time = _max_value(session, EconomicEvent.event_time)
    latest_feature_time = _max_value(session, EconomicFeature.generated_at)
    latest_link_time = _max_value(session, EconomicMarketLink.detected_at)

    if event_count == 0:
        status = "NEEDS_ECONOMIC_SOURCE_DATA"
        blocker = "No economic event rows exist."
        actionable = True
        next_action = "Ingest economic calendar/event data, then build features."
    elif feature_count == 0:
        status = "NEEDS_ECONOMIC_FEATURES"
        blocker = "Economic events exist but no economic feature rows exist."
        actionable = True
        next_action = "Run build-economic-features."
    elif parsed_markets == 0:
        status = "WAITING_FOR_COMPATIBLE_MARKETS"
        blocker = "Economic evidence exists, but no current market is parsed as economic."
        actionable = False
        next_action = (
            "Keep market refreshes running; link when compatible CPI/Fed/jobs/GDP "
            "markets appear."
        )
    elif link_count == 0:
        status = "READY_TO_LINK"
        blocker = "Economic parsed markets exist but no economic link rows exist."
        actionable = True
        next_action = "Run link-economic-markets, then rebuild economic features if needed."
    else:
        status = "READY_FOR_FORECASTS"
        blocker = "none"
        actionable = True
        next_action = "Run economic forecasts/opportunity diagnostics."

    return {
        "domain": DOMAIN_ECONOMIC,
        "status": status,
        "actionable_now": actionable,
        "primary_blocker": blocker,
        "next_action": next_action,
        "counts": {
            "events": event_count,
            "features": feature_count,
            "links": link_count,
            "parsed_markets": parsed_markets,
            "active_parsed_markets": active_parsed_markets,
        },
        "latest": {
            "event_time": _iso_or_none(latest_event_time),
            "feature_generated_at": _iso_or_none(latest_feature_time),
            "link_detected_at": _iso_or_none(latest_link_time),
        },
        "examples": _market_examples(session, DOMAIN_ECONOMIC),
        "safe_commands": [
            "kalshi-bot ingest-economic --input-file examples/economic_sample.json",
            "kalshi-bot build-economic-features",
            "kalshi-bot link-economic-markets",
        ],
    }


def _news_row(session: Session, coverage: dict[str, Any]) -> dict[str, Any]:
    item_count = _count(session, NewsItem.id)
    feature_count = _count(session, NewsFeature.id)
    link_count = _count(session, NewsMarketLink.id)
    parsed_markets = int(coverage.get("parsed_markets") or 0)
    active_parsed_markets = _active_parsed_markets(session, DOMAIN_NEWS)
    latest_item_time = _max_value(session, NewsItem.ingested_at)
    latest_feature_time = _max_value(session, NewsFeature.created_at)
    latest_link_time = _max_value(session, NewsMarketLink.created_at)

    if item_count == 0:
        status = "NEEDS_NEWS_INGESTION"
        blocker = "No news item rows exist."
        actionable = True
        next_action = "Ingest RSS or file-based news, then link markets."
    elif link_count == 0:
        status = "READY_TO_LINK_NEWS"
        blocker = "News items exist but no news market links exist."
        actionable = True
        next_action = "Run link-news-markets."
    elif feature_count == 0:
        status = "NEEDS_NEWS_FEATURES"
        blocker = "News links exist but no news feature rows exist."
        actionable = True
        next_action = "Run build-news-features."
    elif parsed_markets == 0:
        status = "CONTEXT_READY_NO_ACTIVE_NEWS_MARKETS"
        blocker = "News context exists, but no current market is parsed as news."
        actionable = False
        next_action = "Keep news ingestion fresh; use news context only where linked markets exist."
    else:
        status = "READY_FOR_NEWS_FORECASTS"
        blocker = "none"
        actionable = True
        next_action = "Run news-report and news-opportunities."

    return {
        "domain": DOMAIN_NEWS,
        "status": status,
        "actionable_now": actionable,
        "primary_blocker": blocker,
        "next_action": next_action,
        "counts": {
            "items": item_count,
            "features": feature_count,
            "links": link_count,
            "parsed_markets": parsed_markets,
            "active_parsed_markets": active_parsed_markets,
        },
        "latest": {
            "item_ingested_at": _iso_or_none(latest_item_time),
            "feature_created_at": _iso_or_none(latest_feature_time),
            "link_created_at": _iso_or_none(latest_link_time),
        },
        "examples": _market_examples(session, DOMAIN_NEWS),
        "safe_commands": [
            "kalshi-bot ingest-news --source rss",
            "kalshi-bot link-news-markets",
            "kalshi-bot build-news-features",
            "kalshi-bot news-report --output reports/news_report.md",
            "kalshi-bot news-opportunities --output reports/news_opportunities.md",
        ],
    }


def _general_row(session: Session, coverage: dict[str, Any]) -> dict[str, Any]:
    parsed_markets = int(coverage.get("parsed_markets") or 0)
    parsed_legs = int(coverage.get("parsed_legs") or 0)
    active_parsed_markets = _active_parsed_markets(session, DOMAIN_GENERAL)
    examples = _market_examples(session, DOMAIN_GENERAL, limit=10)
    taxonomy = _general_taxonomy(session)
    if parsed_markets == 0:
        status = "NO_GENERAL_MARKETS"
        blocker = "No markets are currently parsed as general."
        actionable = False
        next_action = "No general-market work is available until market refreshes find rows."
    else:
        status = "OBSERVED_ONLY_NO_SPECIALIZED_LINKER"
        blocker = "General markets are parsed, but no specialized external-evidence linker exists."
        actionable = True
        next_action = "Build a general-market taxonomy/report before any model-specific upgrade."

    return {
        "domain": DOMAIN_GENERAL,
        "status": status,
        "actionable_now": actionable,
        "primary_blocker": blocker,
        "next_action": next_action,
        "counts": {
            "parsed_markets": parsed_markets,
            "parsed_legs": parsed_legs,
            "active_parsed_markets": active_parsed_markets,
            "specialized_links": 0,
        },
        "taxonomy_counts": taxonomy["counts"],
        "taxonomy_examples": taxonomy["examples"],
        "latest": {
            "latest_parsed_at": _iso_or_none(_latest_leg_time(session, DOMAIN_GENERAL)),
        },
        "examples": examples,
        "safe_commands": [
            "kalshi-bot market-legs-parse --refresh",
            "kalshi-bot phase3bb-domain-readiness --output-dir reports/phase3bb",
        ],
    }


def _general_taxonomy(session: Session) -> dict[str, Any]:
    grouped = _group_general_markets(session)
    counts: dict[str, int] = {}
    examples: dict[str, list[dict[str, Any]]] = {}
    for item in grouped.values():
        bucket = _general_taxonomy_bucket(item)
        counts[bucket] = counts.get(bucket, 0) + 1
        examples.setdefault(bucket, [])
        if len(examples[bucket]) < GENERAL_TAXONOMY_EXAMPLE_LIMIT:
            examples[bucket].append(
                {
                    "ticker": item["ticker"],
                    "title": item["title"],
                    "status": item["status"],
                    "leg_count": len(item["legs"]),
                    "sample_legs": item["legs"][:5],
                }
            )
    return {"counts": dict(sorted(counts.items())), "examples": examples}


def _group_general_markets(session: Session) -> dict[str, dict[str, Any]]:
    rows = session.execute(
        select(
            Market.ticker,
            Market.title,
            Market.subtitle,
            Market.status,
            Market.series_ticker,
            Market.event_ticker,
            Market.rules_primary,
            Market.rules_secondary,
            Market.last_seen_at,
            MarketLeg.raw_text,
            MarketLeg.market_type,
            MarketLeg.confidence,
            MarketLeg.reason,
        )
        .join(MarketLeg, MarketLeg.ticker == Market.ticker)
        .where(MarketLeg.category == DOMAIN_GENERAL)
        .order_by(Market.ticker, MarketLeg.leg_index)
    )
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        ticker = str(row.ticker)
        item = grouped.setdefault(
            ticker,
            {
                "ticker": ticker,
                "title": row.title,
                "subtitle": row.subtitle,
                "status": row.status,
                "series_ticker": row.series_ticker,
                "event_ticker": row.event_ticker,
                "rules_primary": row.rules_primary,
                "rules_secondary": row.rules_secondary,
                "last_seen_at": row.last_seen_at,
                "market_types": set(),
                "confidences": [],
                "legs": [],
                "parser_reasons": [],
            },
        )
        item["legs"].append(str(row.raw_text or ""))
        if row.reason:
            item["parser_reasons"].append(str(row.reason))
        if row.market_type:
            item["market_types"].add(str(row.market_type))
        if row.confidence is not None:
            item["confidences"].append(str(row.confidence))
    for item in grouped.values():
        item["market_types"] = sorted(item["market_types"])
    return grouped


def _general_taxonomy_bucket(item: dict[str, Any]) -> str:
    text = _taxonomy_text(item)
    leg_count = len(item.get("legs") or [])
    if _sports_market_prefix_match(text) or _contains_any(text, SPORTS_TERMS):
        return "SPORTS_OR_CROSS_CATEGORY_LEAKAGE"
    if _contains_any(text, ECONOMIC_TERMS):
        return "ECONOMIC_CANDIDATE"
    if _contains_any(text, POLITICS_TERMS):
        return "POLITICS_NEWS_CANDIDATE"
    if _contains_any(text, COMPANY_TERMS):
        return "COMPANY_NEWS_CANDIDATE"
    if _contains_any(text, GEOPOLITICAL_TERMS):
        return "GEOPOLITICAL_NEWS_CANDIDATE"
    if _contains_any(text, COMMODITY_PRICE_TERMS):
        return "COMMODITY_PRICE_CANDIDATE"
    if _contains_any(text, TRANSPORTATION_OPERATION_TERMS):
        return "TRANSPORTATION_OPERATION_CANDIDATE"
    if _contains_any(text, INFRASTRUCTURE_CAPACITY_TERMS):
        return "INFRASTRUCTURE_CAPACITY_CANDIDATE"
    if leg_count > 1:
        return "UNSUPPORTED_MULTI_LEG_GENERAL"
    return "GENERAL_UNCLASSIFIED"


def _general_route_row(item: dict[str, Any], bucket: str) -> dict[str, Any]:
    route_domain, route_action, block_reason = _general_route_metadata(bucket)
    taxonomy_text = _taxonomy_text(item)
    return {
        "ticker": item["ticker"],
        "title": item.get("title"),
        "status": item.get("status"),
        "series_ticker": item.get("series_ticker"),
        "event_ticker": item.get("event_ticker"),
        "family_key": _family_key(item),
        "taxonomy_bucket": bucket,
        "route_domain": route_domain,
        "route_action": route_action,
        "candidate_priority": _route_priority(item, bucket),
        "matched_terms": _matched_terms(taxonomy_text, bucket),
        "parser_recommendation": _parser_recommendation(bucket),
        "leg_count": len(item.get("legs") or []),
        "sample_legs": list(item.get("legs") or [])[:5],
        "market_types": list(item.get("market_types") or []),
        "confidence_samples": list(item.get("confidences") or [])[:5],
        "safe_to_apply": False,
        "block_reason": block_reason,
    }


def _general_route_metadata(bucket: str) -> tuple[str, str, str]:
    if bucket == "ECONOMIC_CANDIDATE":
        return (
            "economic",
            "candidate_for_economic_parser_review",
            "Keyword evidence only; requires explicit economic market parsing/link evidence.",
        )
    if bucket in {
        "POLITICS_NEWS_CANDIDATE",
        "COMPANY_NEWS_CANDIDATE",
        "GEOPOLITICAL_NEWS_CANDIDATE",
    }:
        return (
            "news",
            "candidate_for_news_or_event_parser_review",
            "Keyword evidence only; requires explicit news/entity market link evidence.",
        )
    if bucket in {
        "COMMODITY_PRICE_CANDIDATE",
        "TRANSPORTATION_OPERATION_CANDIDATE",
        "INFRASTRUCTURE_CAPACITY_CANDIDATE",
    }:
        return (
            "general",
            "candidate_for_general_signal_parser_review",
            "Structured general-market signal only; requires external source/evidence design.",
        )
    if bucket == "SPORTS_OR_CROSS_CATEGORY_LEAKAGE":
        return (
            "sports",
            "keep_on_sports_placeholder_provenance_path",
            "Looks sports/cross-category; do not route through general/economic/news.",
        )
    if bucket == "UNSUPPORTED_MULTI_LEG_GENERAL":
        return (
            "unsupported",
            "manual_parser_design_required",
            "Multi-leg general market needs structured component verification first.",
        )
    return (
        "general",
        "manual_review",
        "No safe specialized domain route exists yet.",
    )


def _general_signal_diagnostic_row(
    item: dict[str, Any],
    bucket: str,
) -> dict[str, Any]:
    parser = _general_signal_parser_spec(bucket)
    taxonomy_text = _taxonomy_text(item)
    title = str(item.get("title") or "")
    ticker = str(item.get("ticker") or "")
    threshold = _threshold_from_ticker_or_text(ticker, title)
    direction = _direction_from_text(title or taxonomy_text)
    parsed_fields = {
        "source_subject": parser["source_subject"],
        "metric": parser["metric"],
        "threshold": threshold,
        "threshold_unit": parser["threshold_unit"],
        "direction": direction,
        "region": _general_signal_region(bucket, title or taxonomy_text),
        "time_window": _general_signal_time_window(bucket, title),
    }
    required_fields = set(parser["expected_source_fields"]) | {
        "source_subject",
        "metric",
        "threshold",
        "threshold_unit",
        "direction",
        "time_window",
    }
    if bucket != "COMMODITY_PRICE_CANDIDATE":
        required_fields.add("region")
    missing_fields = [
        key
        for key, value in parsed_fields.items()
        if key in required_fields and value in {None, "", "unknown"}
    ]
    evidence_gaps = [
        "source_adapter_missing",
        "source_observation_not_ingested",
        "candidate_not_linked_to_external_source",
        "paper_forecast_blocked_until_source_evidence_exists",
    ]
    if missing_fields:
        evidence_gaps.append("parser_field_incomplete")
    return {
        "ticker": ticker,
        "title": item.get("title"),
        "status": item.get("status"),
        "series_ticker": item.get("series_ticker"),
        "event_ticker": item.get("event_ticker"),
        "family_key": _family_key(item),
        "taxonomy_bucket": bucket,
        "diagnostic_name": parser["diagnostic_name"],
        "source_adapter_key": parser["source_adapter_key"],
        "parser_recommendation": _parser_recommendation(bucket),
        "matched_terms": _matched_terms(taxonomy_text, bucket),
        "parsed_fields": parsed_fields,
        "missing_fields": missing_fields,
        "expected_source_fields": parser["expected_source_fields"],
        "evidence_gaps": evidence_gaps,
        "readiness": "SOURCE_DESIGN_REQUIRED",
        "next_parser_step": parser["next_parser_step"],
        "safe_to_apply": False,
        "safe_to_forecast": False,
        "proposed_db_writes": 0,
        "link_writes": False,
        "feature_writes": False,
        "live_or_demo_execution": False,
        "sample_legs": list(item.get("legs") or [])[:5],
        "block_reason": (
            "Diagnostic only. A reviewed external source adapter and exact evidence "
            "mapping are required before any link, feature, forecast, or trade output."
        ),
    }


def _general_signal_diagnostics_summary(
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    bucket_counts: dict[str, int] = {}
    adapter_counts: dict[str, int] = {}
    readiness_counts: dict[str, int] = {}
    missing_field_counts: dict[str, int] = {}
    for row in rows:
        bucket = str(row["taxonomy_bucket"])
        adapter = str(row["source_adapter_key"])
        readiness = str(row["readiness"])
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
        adapter_counts[adapter] = adapter_counts.get(adapter, 0) + 1
        readiness_counts[readiness] = readiness_counts.get(readiness, 0) + 1
        for field in row.get("missing_fields") or []:
            key = str(field)
            missing_field_counts[key] = missing_field_counts.get(key, 0) + 1
    return {
        "diagnostic_rows": len(rows),
        "bucket_counts": dict(sorted(bucket_counts.items())),
        "source_adapter_counts": dict(sorted(adapter_counts.items())),
        "readiness_counts": dict(sorted(readiness_counts.items())),
        "missing_field_counts": dict(sorted(missing_field_counts.items())),
        "safe_to_apply_rows": sum(1 for row in rows if row.get("safe_to_apply")),
        "safe_to_forecast_rows": sum(1 for row in rows if row.get("safe_to_forecast")),
        "proposed_db_writes": sum(int(row.get("proposed_db_writes") or 0) for row in rows),
        "link_writes": False,
        "feature_writes": False,
        "live_or_demo_execution": False,
    }


def _load_general_source_records(
    evidence_dir: Path,
    adapter_key: str,
) -> dict[str, Any]:
    path = evidence_dir / f"{adapter_key}.json"
    if not path.exists():
        return {
            "path": path,
            "file_exists": False,
            "records": [],
            "load_error": None,
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "path": path,
            "file_exists": True,
            "records": [],
            "load_error": str(exc),
        }
    records = data.get("records") if isinstance(data, dict) else data
    if not isinstance(records, list):
        return {
            "path": path,
            "file_exists": True,
            "records": [],
            "load_error": "expected a JSON list or an object with a records list",
        }
    return {
        "path": path,
        "file_exists": True,
        "records": [record for record in records if isinstance(record, dict)],
        "load_error": None,
    }


def _load_general_source_input_rows(
    input_file: Path | None,
) -> tuple[list[dict[str, Any]], str | None]:
    if input_file is None:
        return [], None
    if not input_file.exists():
        return [], f"input file does not exist: {input_file}"
    try:
        text = input_file.read_text(encoding="utf-8")
        if input_file.suffix.lower() == ".csv":
            return [dict(row) for row in csv.DictReader(io.StringIO(text))], None
        payload = json.loads(text)
    except (OSError, json.JSONDecodeError, csv.Error) as exc:
        return [], str(exc)
    rows = payload.get("records") if isinstance(payload, dict) else payload
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return [], "input file must contain a JSON list or an object with a records list"
    return [row for row in rows if isinstance(row, dict)], None


def _general_source_input_row(
    row: dict[str, Any],
    diagnostics: list[dict[str, Any]],
) -> dict[str, Any]:
    adapter_key = str(row.get("source_adapter_key") or "").strip()
    if adapter_key not in GENERAL_SOURCE_ADAPTER_KEYS:
        return _source_input_status_row(
            row,
            adapter_key=adapter_key or "unknown",
            status="UNSUPPORTED_SOURCE_ADAPTER",
            missing_fields=[],
            matched_tickers=[],
        )
    record = _canonical_general_source_record(adapter_key, row)
    missing_fields = [
        field
        for field in _general_source_evidence_spec(adapter_key)["required_record_fields"]
        if _is_blank(record.get(field))
    ]
    if not _valid_source_url(record.get("source_url")):
        missing_fields.append("valid_source_url")
    matched_tickers = [
        str(diagnostic["ticker"])
        for diagnostic in diagnostics
        if diagnostic.get("source_adapter_key") == adapter_key
        and _general_source_record_matches(diagnostic, record)
    ]
    if missing_fields:
        status = "REQUIRED_FIELDS_MISSING"
    elif not matched_tickers:
        status = "NO_MATCHING_R2_DIAGNOSTIC"
    else:
        status = "READY_TO_WRITE"
    return _source_input_status_row(
        row,
        adapter_key=adapter_key,
        status=status,
        missing_fields=missing_fields,
        matched_tickers=matched_tickers,
        canonical_record=record,
    )


def _source_input_status_row(
    raw_row: dict[str, Any],
    *,
    adapter_key: str,
    status: str,
    missing_fields: list[str],
    matched_tickers: list[str],
    canonical_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "source_adapter_key": adapter_key,
        "status": status,
        "missing_fields": sorted(set(missing_fields)),
        "matched_tickers": matched_tickers,
        "canonical_record": canonical_record,
        "raw_row": _redact_restricted_payload(raw_row),
        "safe_to_link": False,
        "safe_to_forecast": False,
        "proposed_db_writes": 0,
        "block_reason": _source_input_block_reason(status),
    }


def _redact_restricted_payload(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key).lower()
            if key_text in {
                "payload",
                "raw_payload",
                "restricted_payload",
                "proprietary_payload",
                "licensed_payload",
                "raw_response",
                "html",
                "body",
                "content",
            }:
                redacted[key] = "[REDACTED_RESTRICTED_PAYLOAD]"
            else:
                redacted[key] = _redact_restricted_payload(child)
        return redacted
    if isinstance(value, list):
        return [_redact_restricted_payload(item) for item in value]
    return value


def _source_input_block_reason(status: str) -> str:
    if status == "READY_TO_WRITE":
        return (
            "Verified source row exactly matches at least one R2 diagnostic. It may be "
            "written as a local evidence file, but links and forecasts remain blocked."
        )
    if status == "NO_MATCHING_R2_DIAGNOSTIC":
        return "Source row is complete but does not exactly match current R2 parsed fields."
    if status == "UNSUPPORTED_SOURCE_ADAPTER":
        return "Source adapter key is not one of the supported R2 source adapters."
    return "Required source evidence fields are missing or source_url is not http(s)."


def _canonical_general_source_record(
    adapter_key: str,
    row: dict[str, Any],
) -> dict[str, Any]:
    common = {
        "source_adapter_key": adapter_key,
        "metric": _text(row.get("metric")),
        "source_name": _text(row.get("source_name") or row.get("source")),
        "source_url": _text(row.get("source_url") or row.get("url")),
        "verification_status": _text(row.get("verification_status")),
        "evidence_available": row.get("evidence_available"),
        "retrieved_at": _text(row.get("retrieved_at") or row.get("observed_at")),
        "evidence_notes": _text(row.get("evidence_notes") or row.get("notes")),
    }
    if adapter_key == "commodity_advertised_price_source":
        commodity = _text(row.get("commodity"))
        variety = _text(row.get("variety"))
        if (not commodity or not variety) and row.get("source_subject"):
            parts = [part.strip() for part in str(row["source_subject"]).split(",", 1)]
            commodity = commodity or (parts[0] if parts else "")
            variety = variety or (parts[1] if len(parts) > 1 else "")
        return {
            **common,
            "commodity": commodity,
            "variety": variety,
            "source_subject": _text(row.get("source_subject") or f"{commodity}, {variety}"),
            "price_usd_each": _text(row.get("price_usd_each") or row.get("value")),
            "as_of_date": _text(row.get("as_of_date") or row.get("time_window")),
        }
    if adapter_key == "transportation_flight_cancellation_source":
        return {
            **common,
            "region": _text(row.get("region")),
            "period_start": _text(row.get("period_start")),
            "period_end": _text(row.get("period_end") or row.get("time_window")),
            "cancellation_count": _text(row.get("cancellation_count") or row.get("value")),
        }
    if adapter_key == "infrastructure_data_center_capacity_source":
        return {
            **common,
            "region": _text(row.get("region")),
            "measurement_year": _text(row.get("measurement_year") or row.get("time_window")),
            "capacity_gw": _text(row.get("capacity_gw") or row.get("value")),
        }
    return common


def _general_source_records_by_adapter(
    rows: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    by_adapter: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, str]] = set()
    for row in rows:
        adapter = str(row["source_adapter_key"])
        record = dict(row.get("canonical_record") or {})
        record["matched_tickers"] = list(row.get("matched_tickers") or [])
        identity = (adapter, json.dumps(record, sort_keys=True, default=str))
        if identity in seen:
            continue
        seen.add(identity)
        by_adapter.setdefault(adapter, []).append(record)
    return by_adapter


def _general_source_intake_template_rows(
    diagnostics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for diagnostic in diagnostics:
        adapter_key = str(diagnostic["source_adapter_key"])
        parsed = diagnostic.get("parsed_fields") or {}
        row = {
            "ticker": diagnostic["ticker"],
            "source_adapter_key": adapter_key,
            "metric": parsed.get("metric"),
            "threshold": parsed.get("threshold"),
            "threshold_unit": parsed.get("threshold_unit"),
            "direction": parsed.get("direction"),
            "region": parsed.get("region"),
            "time_window": parsed.get("time_window"),
            "source_name": "",
            "source_url": "",
            "verification_status": "",
            "retrieved_at": "",
            "evidence_notes": "",
        }
        if adapter_key == "commodity_advertised_price_source":
            row.update(
                {
                    "source_subject": parsed.get("source_subject"),
                    "commodity": "Avocados",
                    "variety": "Hass",
                    "price_usd_each": "",
                    "as_of_date": parsed.get("time_window"),
                }
            )
        elif adapter_key == "transportation_flight_cancellation_source":
            row.update(
                {
                    "period_start": "",
                    "period_end": parsed.get("time_window"),
                    "cancellation_count": "",
                }
            )
        elif adapter_key == "infrastructure_data_center_capacity_source":
            row.update(
                {
                    "measurement_year": parsed.get("time_window"),
                    "capacity_gw": "",
                }
            )
        rows.append(row)
    return rows


def _general_source_evidence_row(
    diagnostic: dict[str, Any],
    source_inputs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    adapter_key = str(diagnostic["source_adapter_key"])
    source_payload = source_inputs[adapter_key]
    source_file = str(source_payload["path"])
    spec = _general_source_evidence_spec(adapter_key)
    required_fields = spec["required_record_fields"]
    evidence_status = "MISSING_SOURCE_EVIDENCE_FILE"
    matched_evidence = None
    missing_evidence_fields = list(required_fields)
    block_reason = (
        "No local source evidence file exists for this adapter. Create the exact "
        "paper-only evidence file from the emitted template before review."
    )

    if source_payload["file_exists"] and source_payload.get("load_error"):
        evidence_status = "SOURCE_EVIDENCE_FILE_INVALID"
        missing_evidence_fields = list(required_fields)
        block_reason = f"Evidence file could not be loaded: {source_payload['load_error']}"
    elif source_payload["file_exists"]:
        matched_evidence = next(
            (
                record
                for record in source_payload["records"]
                if _general_source_record_matches(diagnostic, record)
            ),
            None,
        )
        if matched_evidence is None:
            evidence_status = "NO_EXACT_EVIDENCE_MATCH"
            missing_evidence_fields = []
            block_reason = (
                "Evidence file exists, but no record exactly matches the parsed "
                "subject/metric/region/time-window keys."
            )
        else:
            if _general_source_evidence_unavailable(matched_evidence):
                evidence_status = "SOURCE_EVIDENCE_UNAVAILABLE"
                missing_evidence_fields = [
                    field
                    for field in required_fields
                    if _is_blank(matched_evidence.get(field))
                ]
                block_reason = (
                    "An exact source key was audited, but the required observed "
                    "value is not available from the named source yet. Link, "
                    "feature, forecast, and trade writes remain blocked."
                )
            else:
                missing_evidence_fields = [
                    field
                    for field in required_fields
                    if _is_blank(matched_evidence.get(field))
                ]
                if missing_evidence_fields:
                    evidence_status = "EVIDENCE_FIELD_INCOMPLETE"
                    block_reason = (
                        "An exact evidence record matched, but required source fields "
                        "are missing or blank."
                    )
                else:
                    evidence_status = "EXACT_EVIDENCE_READY_FOR_REVIEW"
                    block_reason = (
                        "Exact evidence is present for human review. Link, feature, "
                        "forecast, and trade writes remain blocked in this pass."
                    )

    return {
        "ticker": diagnostic["ticker"],
        "title": diagnostic.get("title"),
        "family_key": diagnostic.get("family_key"),
        "taxonomy_bucket": diagnostic.get("taxonomy_bucket"),
        "diagnostic_name": diagnostic["diagnostic_name"],
        "source_adapter_key": adapter_key,
        "source_file": source_file,
        "parsed_fields": diagnostic.get("parsed_fields") or {},
        "required_source_fields": required_fields,
        "expected_source_fields": diagnostic.get("expected_source_fields") or [],
        "evidence_status": evidence_status,
        "matched_evidence": matched_evidence,
        "missing_evidence_fields": missing_evidence_fields,
        "safe_to_link": False,
        "safe_to_forecast": False,
        "proposed_db_writes": 0,
        "link_writes": False,
        "feature_writes": False,
        "forecast_writes": False,
        "live_or_demo_execution": False,
        "block_reason": block_reason,
    }


def _general_source_availability_rows(
    adapter_key: str,
    source_payload: dict[str, Any],
    evidence_rows: list[dict[str, Any]],
    *,
    check_source_urls: bool,
    url_timeout_seconds: float,
) -> list[dict[str, Any]]:
    spec = _general_source_availability_spec(adapter_key)
    base = _general_source_availability_base(adapter_key, source_payload, evidence_rows)
    if not source_payload["file_exists"]:
        return [
            {
                **base,
                "source_name": None,
                "source_url": None,
                "verification_status": None,
                "evidence_available": None,
                "observed_value": None,
                "availability_status": "SOURCE_FILE_MISSING",
                "remote_check": _remote_source_check(
                    None,
                    spec["watch_terms"],
                    requested=False,
                    timeout_seconds=url_timeout_seconds,
                ),
                "block_reason": _source_availability_block_reason(
                    "SOURCE_FILE_MISSING",
                    spec,
                ),
            }
        ]
    if source_payload.get("load_error"):
        return [
            {
                **base,
                "source_name": None,
                "source_url": None,
                "verification_status": None,
                "evidence_available": None,
                "observed_value": None,
                "availability_status": "SOURCE_FILE_INVALID",
                "remote_check": _remote_source_check(
                    None,
                    spec["watch_terms"],
                    requested=False,
                    timeout_seconds=url_timeout_seconds,
                ),
                "block_reason": str(source_payload["load_error"]),
            }
        ]
    if not source_payload["records"]:
        return [
            {
                **base,
                "source_name": None,
                "source_url": None,
                "verification_status": None,
                "evidence_available": None,
                "observed_value": None,
                "availability_status": "NO_SOURCE_RECORD",
                "remote_check": _remote_source_check(
                    None,
                    spec["watch_terms"],
                    requested=False,
                    timeout_seconds=url_timeout_seconds,
                ),
                "block_reason": _source_availability_block_reason(
                    "NO_SOURCE_RECORD",
                    spec,
                ),
            }
        ]

    rows: list[dict[str, Any]] = []
    for record in source_payload["records"]:
        canonical = _canonical_general_source_record(adapter_key, record)
        status = _source_availability_status(canonical, spec["required_value_field"])
        remote_check = _remote_source_check(
            canonical.get("source_url"),
            spec["watch_terms"],
            requested=check_source_urls,
            timeout_seconds=url_timeout_seconds,
        )
        rows.append(
            {
                **base,
                "source_name": canonical.get("source_name"),
                "source_url": canonical.get("source_url"),
                "verification_status": canonical.get("verification_status"),
                "evidence_available": canonical.get("evidence_available"),
                "retrieved_at": canonical.get("retrieved_at"),
                "observed_value": canonical.get(spec["required_value_field"]),
                "availability_status": status,
                "remote_check": remote_check,
                "safe_to_link": False,
                "safe_to_forecast": False,
                "proposed_db_writes": 0,
                "link_writes": False,
                "feature_writes": False,
                "forecast_writes": False,
                "live_or_demo_execution": False,
                "block_reason": _source_availability_block_reason(status, spec),
            }
        )
    return rows


def _general_source_availability_base(
    adapter_key: str,
    source_payload: dict[str, Any],
    evidence_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    spec = _general_source_availability_spec(adapter_key)
    status_counts: dict[str, int] = {}
    tickers: list[str] = []
    for row in evidence_rows:
        status = str(row.get("evidence_status") or "UNKNOWN")
        status_counts[status] = status_counts.get(status, 0) + 1
        ticker = row.get("ticker")
        if ticker:
            tickers.append(str(ticker))
    return {
        "source_adapter_key": adapter_key,
        "source_file": str(source_payload["path"]),
        "watch_target": spec["watch_target"],
        "target_publication": spec["target_publication"],
        "target_observation": spec["target_observation"],
        "required_value_field": spec["required_value_field"],
        "affected_diagnostic_rows": len(evidence_rows),
        "affected_tickers": sorted(set(tickers)),
        "evidence_status_counts": dict(sorted(status_counts.items())),
        "safe_to_link": False,
        "safe_to_forecast": False,
        "proposed_db_writes": 0,
        "link_writes": False,
        "feature_writes": False,
        "forecast_writes": False,
        "live_or_demo_execution": False,
    }


def _source_availability_status(
    record: dict[str, Any],
    required_value_field: str,
) -> str:
    if not _valid_source_url(record.get("source_url")):
        return "SOURCE_URL_MISSING"
    if _general_source_evidence_unavailable(record):
        return "PENDING_SOURCE_PUBLICATION"
    if _is_blank(record.get(required_value_field)):
        return "SOURCE_RECORD_INCOMPLETE"
    return "SOURCE_VALUE_AVAILABLE_FOR_REVIEW"


def _general_source_availability_spec(adapter_key: str) -> dict[str, Any]:
    if adapter_key == "commodity_advertised_price_source":
        return {
            "required_value_field": "price_usd_each",
            "target_observation": "July 3, 2026",
            "target_publication": "USDA July 3, 2026 FVWRETAIL",
            "watch_target": (
                "USDA AMS FVWRETAIL July 3, 2026 National Conventional "
                "Summary row for Avocados, Hass, each"
            ),
            "watch_terms": [
                "Weekly Grocery Store Specialty Crops Feature Activity",
                "Avocados",
                "Hass",
            ],
        }
    if adapter_key == "transportation_flight_cancellation_source":
        return {
            "required_value_field": "cancellation_count",
            "target_observation": "July 3, 2026",
            "target_publication": "FlightAware weekly cancellation outcome",
            "watch_target": (
                "United States total flight cancellations for week ending "
                "July 3, 2026"
            ),
            "watch_terms": ["FlightAware", "cancelled", "United States"],
        }
    if adapter_key == "infrastructure_data_center_capacity_source":
        return {
            "required_value_field": "capacity_gw",
            "target_observation": "2026",
            "target_publication": (
                "Cushman & Wakefield first H2 2026 or year-end 2026 "
                "Americas Data Center Update"
            ),
            "watch_target": "Americas operational data center capacity for 2026",
            "watch_terms": ["Americas Data Center Update", "2026", "operational"],
        }
    return {
        "required_value_field": "value",
        "target_observation": "unknown",
        "target_publication": "unknown",
        "watch_target": "unknown",
        "watch_terms": [],
    }


def _remote_source_check(
    source_url: Any,
    watch_terms: list[str],
    *,
    requested: bool,
    timeout_seconds: float,
) -> dict[str, Any]:
    url = _text(source_url)
    if not requested:
        return {
            "requested": False,
            "status": "NOT_REQUESTED",
            "source_url": url or None,
            "http_status": None,
            "content_type": None,
            "matched_watch_terms": [],
            "missing_watch_terms": list(watch_terms),
            "error": None,
        }
    if not _valid_source_url(url):
        return {
            "requested": True,
            "status": "INVALID_SOURCE_URL",
            "source_url": url or None,
            "http_status": None,
            "content_type": None,
            "matched_watch_terms": [],
            "missing_watch_terms": list(watch_terms),
            "error": "source_url is missing or not http(s)",
        }
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "kalshi-predictive-bot-source-watch/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read(250_000)
            content_type = response.headers.get("content-type")
            text = _decode_response_sample(body)
            matched = [
                term for term in watch_terms if term.lower() in text.lower()
            ]
            return {
                "requested": True,
                "status": "FETCH_OK",
                "source_url": url,
                "http_status": getattr(response, "status", None),
                "content_type": content_type,
                "matched_watch_terms": matched,
                "missing_watch_terms": [
                    term for term in watch_terms if term not in matched
                ],
                "error": None,
            }
    except urllib.error.HTTPError as exc:
        return {
            "requested": True,
            "status": "FETCH_HTTP_ERROR",
            "source_url": url,
            "http_status": exc.code,
            "content_type": None,
            "matched_watch_terms": [],
            "missing_watch_terms": list(watch_terms),
            "error": str(exc),
        }
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {
            "requested": True,
            "status": "FETCH_ERROR",
            "source_url": url,
            "http_status": None,
            "content_type": None,
            "matched_watch_terms": [],
            "missing_watch_terms": list(watch_terms),
            "error": str(exc),
        }


def _decode_response_sample(body: bytes) -> str:
    return body.decode("utf-8", errors="ignore")


def _source_availability_block_reason(status: str, spec: dict[str, Any]) -> str:
    if status == "SOURCE_VALUE_AVAILABLE_FOR_REVIEW":
        return (
            "The required source value is present in local evidence and can be "
            "reviewed, but link, feature, forecast, and trade writes remain blocked."
        )
    if status == "PENDING_SOURCE_PUBLICATION":
        return (
            f"Waiting for {spec['target_publication']} to publish the exact "
            f"{spec['required_value_field']} value."
        )
    if status == "SOURCE_URL_MISSING":
        return "A valid http(s) source_url is required before source review."
    if status == "SOURCE_RECORD_INCOMPLETE":
        return (
            f"The source record exists but {spec['required_value_field']} is blank."
        )
    if status == "NO_SOURCE_RECORD":
        return "The evidence file exists but contains no source records."
    if status == "SOURCE_FILE_MISSING":
        return "The canonical local source evidence file is missing."
    if status == "SOURCE_FILE_INVALID":
        return "The canonical local source evidence file could not be parsed."
    return "Source availability remains blocked."


def _general_source_record_matches(
    diagnostic: dict[str, Any],
    record: dict[str, Any],
) -> bool:
    adapter_key = str(diagnostic["source_adapter_key"])
    parsed = diagnostic.get("parsed_fields") or {}
    if not _canon_equal(record.get("metric"), parsed.get("metric")):
        return False
    if adapter_key == "commodity_advertised_price_source":
        subject = parsed.get("source_subject")
        record_subject = record.get("source_subject")
        commodity_subject = f"{record.get('commodity', '')}, {record.get('variety', '')}"
        subject_match = _canon_equal(record_subject, subject) or _canon_equal(
            commodity_subject,
            subject,
        )
        date_match = _canon_equal(
            record.get("as_of_date") or record.get("time_window"),
            parsed.get("time_window"),
        )
        return subject_match and date_match
    if adapter_key == "transportation_flight_cancellation_source":
        return _canon_equal(record.get("region"), parsed.get("region")) and _canon_equal(
            record.get("period_end") or record.get("time_window"),
            parsed.get("time_window"),
        )
    if adapter_key == "infrastructure_data_center_capacity_source":
        return _canon_equal(record.get("region"), parsed.get("region")) and _canon_equal(
            record.get("measurement_year") or record.get("time_window"),
            parsed.get("time_window"),
        )
    return False


def _general_source_evidence_unavailable(record: dict[str, Any]) -> bool:
    status = _canonical_source_text(record.get("verification_status"))
    if status in {
        "source_not_available",
        "source_unavailable",
        "source_pending_publication",
        "not_published_yet",
        "unavailable_not_ready",
    }:
        return True
    available = record.get("evidence_available")
    if isinstance(available, bool):
        return available is False
    return _canonical_source_text(available) in {"false", "no", "0"}


def _general_source_evidence_spec(adapter_key: str) -> dict[str, Any]:
    if adapter_key == "commodity_advertised_price_source":
        return {
            "required_record_fields": [
                "commodity",
                "variety",
                "metric",
                "price_usd_each",
                "as_of_date",
                "source_name",
                "source_url",
            ],
        }
    if adapter_key == "transportation_flight_cancellation_source":
        return {
            "required_record_fields": [
                "region",
                "metric",
                "period_start",
                "period_end",
                "cancellation_count",
                "source_name",
                "source_url",
            ],
        }
    if adapter_key == "infrastructure_data_center_capacity_source":
        return {
            "required_record_fields": [
                "region",
                "metric",
                "measurement_year",
                "capacity_gw",
                "source_name",
                "source_url",
            ],
        }
    return {"required_record_fields": []}


def _general_source_evidence_templates() -> list[dict[str, Any]]:
    return [
        {
            "source_adapter_key": "commodity_advertised_price_source",
            "filename": "commodity_advertised_price_source.json",
            "records": [
                {
                    "commodity": "Avocados",
                    "variety": "Hass",
                    "metric": "weighted_average_advertised_price",
                    "price_usd_each": None,
                    "as_of_date": "July 3, 2026",
                    "source_name": None,
                    "source_url": None,
                }
            ],
        },
        {
            "source_adapter_key": "transportation_flight_cancellation_source",
            "filename": "transportation_flight_cancellation_source.json",
            "records": [
                {
                    "region": "United States",
                    "metric": "total_flight_cancellations",
                    "period_start": None,
                    "period_end": "July 3, 2026",
                    "cancellation_count": None,
                    "source_name": None,
                    "source_url": None,
                }
            ],
        },
        {
            "source_adapter_key": "infrastructure_data_center_capacity_source",
            "filename": "infrastructure_data_center_capacity_source.json",
            "records": [
                {
                    "region": "Americas",
                    "metric": "operational_data_center_capacity",
                    "measurement_year": "2026",
                    "capacity_gw": None,
                    "source_name": None,
                    "source_url": None,
                }
            ],
        },
    ]


def _general_source_intake_state(
    *,
    input_file: Path | None,
    source_error: str | None,
    write_evidence_files: bool,
) -> dict[str, Any]:
    return {
        "input_file_requested": input_file is not None,
        "input_file": str(input_file) if input_file is not None else None,
        "input_file_exists": bool(input_file and input_file.exists()),
        "input_load_error": source_error,
        "write_evidence_files_requested": write_evidence_files,
        "source_url_required": True,
        "source_values_reported": False,
    }


def _source_intake_recommended_next_action(
    *,
    source_state: dict[str, Any],
    summary: dict[str, Any],
) -> str:
    if source_state.get("input_load_error"):
        return "Fix the verified source input file, then rerun source intake."
    if not source_state.get("input_file_requested"):
        return (
            "Fill the emitted source input template with audited source_url and observed "
            "values, then rerun with --input-file and --write-evidence-files."
        )
    if int(summary.get("valid_input_rows") or 0) == 0:
        return (
            "Input file was read, but no rows exactly matched R2 diagnostics with all "
            "required audited fields. Repair source fields and rerun."
        )
    if not source_state.get("write_evidence_files_requested"):
        return (
            "Verified input rows are ready. Rerun with --write-evidence-files to emit "
            "canonical local source evidence files."
        )
    return (
        "Canonical source evidence files were written. Rerun "
        "phase3bb-r2-general-source-evidence to verify exact readiness."
    )


def _source_availability_recommended_next_action(
    *,
    pending_rows: int,
    value_ready_rows: int,
    incomplete_rows: int,
) -> str:
    if pending_rows:
        return (
            "Keep Phase 3BB-R2 active and rerun the source availability check. "
            "At least one exact source publication has not exposed the required "
            "value yet; do not write links, forecasts, or trades."
        )
    if incomplete_rows:
        return (
            "Repair missing or invalid source evidence files before any adapter "
            "or linker work. This report intentionally proposes zero DB writes."
        )
    if value_ready_rows:
        return (
            "Required source values are present for review. Rerun source evidence, "
            "then manually review exact rows before guarded adapter/linker work."
        )
    return (
        "No source availability rows are ready. Keep R2 in report-only watch mode."
    )


def _source_evidence_recommended_next_action(
    *,
    missing_files: list[str],
    invalid_files: list[str],
    exact_ready_rows: int,
    unavailable_rows: int,
    evidence_rows: int,
) -> str:
    if invalid_files:
        return "Fix invalid local source evidence JSON files, then rerun this report."
    if missing_files:
        return (
            "Run phase3bb-r2-general-source-intake to emit an audited input template, "
            "populate it with source_url and observed values, then write canonical "
            "source evidence files."
        )
    if unavailable_rows:
        return (
            "Source keys are audited but at least one required source value is not "
            "published or directly available yet. Keep those rows blocked; review "
            "any exact-ready rows separately before adapter/linker work."
        )
    if exact_ready_rows:
        return (
            "Review exact evidence rows manually before building any guarded source "
            "adapter/linker. This command still proposes zero DB writes."
        )
    if evidence_rows:
        return (
            "Evidence files exist but do not exactly match the parsed diagnostic keys; "
            "repair source/metric/region/time-window values and rerun."
        )
    return "No R2 general-signal diagnostic rows are currently available for evidence review."


def _canon_equal(left: Any, right: Any) -> bool:
    return _canonical_source_text(left) == _canonical_source_text(right)


def _canonical_source_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _text(value: Any) -> str:
    return str(value or "").strip()


def _valid_source_url(value: Any) -> bool:
    text = _text(value).lower()
    return text.startswith(("http://", "https://"))


def _is_blank(value: Any) -> bool:
    return value is None or _canonical_source_text(value) in {"", "none", "unknown"}


def _general_signal_parser_spec(bucket: str) -> dict[str, Any]:
    if bucket == "COMMODITY_PRICE_CANDIDATE":
        return {
            "diagnostic_name": "commodity_advertised_price_parser",
            "source_adapter_key": "commodity_advertised_price_source",
            "source_subject": "Avocados, Hass",
            "metric": "weighted_average_advertised_price",
            "threshold_unit": "USD_EACH",
            "expected_source_fields": [
                "commodity",
                "variety",
                "price_usd_each",
                "as_of_date",
                "source_name",
            ],
            "next_parser_step": (
                "Design a public advertised-price observation adapter, then map exact "
                "commodity/variety/date/threshold fields before forecasts are allowed."
            ),
        }
    if bucket == "TRANSPORTATION_OPERATION_CANDIDATE":
        return {
            "diagnostic_name": "transportation_cancellation_count_parser",
            "source_adapter_key": "transportation_flight_cancellation_source",
            "source_subject": "US flight cancellations",
            "metric": "total_flight_cancellations",
            "threshold_unit": "CANCELLATIONS",
            "expected_source_fields": [
                "region",
                "period_start",
                "period_end",
                "cancellation_count",
                "source_name",
            ],
            "next_parser_step": (
                "Design a bounded flight-cancellation count source and require exact "
                "period/region/threshold evidence before ranking or paper candidates."
            ),
        }
    if bucket == "INFRASTRUCTURE_CAPACITY_CANDIDATE":
        return {
            "diagnostic_name": "infrastructure_capacity_parser",
            "source_adapter_key": "infrastructure_data_center_capacity_source",
            "source_subject": "Operational data center capacity",
            "metric": "operational_data_center_capacity",
            "threshold_unit": "GW",
            "expected_source_fields": [
                "region",
                "capacity_gw",
                "measurement_year",
                "source_name",
            ],
            "next_parser_step": (
                "Design an infrastructure capacity evidence source and require exact "
                "region/year/GW mapping before any forecast can consume the market."
            ),
        }
    return {
        "diagnostic_name": "manual_general_signal_parser",
        "source_adapter_key": "manual_general_signal_source",
        "source_subject": "unknown",
        "metric": "unknown",
        "threshold_unit": "unknown",
        "expected_source_fields": [],
        "next_parser_step": "Manual parser design required.",
    }


def _threshold_from_ticker_or_text(ticker: str, text: str) -> str | None:
    ticker_match = re.search(r"-T(?P<threshold>\d+(?:\.\d+)?)\b", ticker)
    if ticker_match:
        return ticker_match.group("threshold")
    text_match = re.search(
        r"\b(?:above|over|below|under)\s+\$?(?P<threshold>\d+(?:\.\d+)?)\b",
        text,
        flags=re.IGNORECASE,
    )
    if text_match:
        return text_match.group("threshold")
    return None


def _direction_from_text(text: str) -> str:
    lower = text.lower()
    if " above " in f" {lower} " or " over " in f" {lower} ":
        return "above"
    if " below " in f" {lower} " or " under " in f" {lower} ":
        return "below"
    return "unknown"


def _general_signal_region(bucket: str, text: str) -> str:
    lower = text.lower()
    if bucket == "COMMODITY_PRICE_CANDIDATE":
        return "not_applicable"
    if bucket == "TRANSPORTATION_OPERATION_CANDIDATE":
        if "united states" in lower or " u.s." in lower or " us " in f" {lower} ":
            return "United States"
    if bucket == "INFRASTRUCTURE_CAPACITY_CANDIDATE":
        if "americas" in lower:
            return "Americas"
    return "unknown"


def _general_signal_time_window(bucket: str, text: str) -> str:
    if bucket == "INFRASTRUCTURE_CAPACITY_CANDIDATE":
        year_match = re.search(r"\b(20\d{2})\b", text)
        return year_match.group(1) if year_match else "unknown"
    date_match = re.search(
        r"\b(?:for|ending)\s+([A-Z][a-z]+ \d{1,2}, 20\d{2})\b",
        text,
    )
    if date_match:
        return date_match.group(1)
    return "unknown"


def _r3_recommended_next_action(
    *,
    sports_rows: list[dict[str, Any]],
    manual_rows: list[dict[str, Any]],
    rows_safe_to_reparse: int = 0,
) -> str:
    if rows_safe_to_reparse > 0:
        return (
            "Parser preview found safe rows; run the controlled Phase 3BB-R3 safe "
            "parser reparse, then rerun coverage."
        )
    if sports_rows:
        return (
            "Parser preview is still blocked for the listed sports/cross-category "
            "families; inspect preview block reasons before any market-leg refresh."
        )
    if manual_rows:
        return (
            "Manually inspect the remaining unclassified general rows before adding a new "
            "category or parser rule."
        )
    return (
        "No general reclassification work remains; keep economic/news in watch mode until "
        "compatible parsed markets appear."
    )


def _r2_recommended_next_action(candidate_buckets: dict[str, int]) -> str:
    if candidate_buckets.get("economic", 0) or candidate_buckets.get("news", 0):
        return (
            "Review the economic/news/company/geopolitical candidate buckets and build "
            "narrow parser/linker diagnostics before any link writes. Sports leakage stays "
            "on the sports placeholder/provenance path."
        )
    if candidate_buckets.get("operational_or_commodity", 0):
        return (
            "Review commodity, transportation, and infrastructure candidate buckets for "
            "narrow general-signal parser diagnostics. Sports leakage stays blocked on "
            "the sports placeholder/provenance path."
        )
    if candidate_buckets.get("sports_or_cross_category_leakage", 0):
        return (
            "Economic/news candidates are empty after KXMV leakage filtering. The next "
            "general-market repair is parser hygiene/reclassification for sports and "
            "cross-category rows, plus manual review of any unclassified non-sports rows."
        )
    if candidate_buckets.get("unsupported_or_unclassified", 0):
        return "Only unclassified general rows remain; review them manually before parser work."
    return "No general candidate routing work remains."


def _route_priority(item: dict[str, Any], bucket: str) -> str:
    status = str(item.get("status") or "").lower()
    leg_count = len(item.get("legs") or [])
    if bucket in {
        "ECONOMIC_CANDIDATE",
        "POLITICS_NEWS_CANDIDATE",
        "COMPANY_NEWS_CANDIDATE",
        "GEOPOLITICAL_NEWS_CANDIDATE",
        "COMMODITY_PRICE_CANDIDATE",
        "TRANSPORTATION_OPERATION_CANDIDATE",
        "INFRASTRUCTURE_CAPACITY_CANDIDATE",
    }:
        if status in OPEN_STATUSES and leg_count == 1:
            return "REVIEW_HIGH"
        return "REVIEW_MEDIUM"
    if bucket == "SPORTS_OR_CROSS_CATEGORY_LEAKAGE":
        return "LEAKAGE_REVIEW"
    if bucket == "UNSUPPORTED_MULTI_LEG_GENERAL":
        return "BLOCKED_MULTI_LEG"
    return "LOW_MANUAL_REVIEW"


def _parser_recommendation(bucket: str) -> str:
    if bucket == "ECONOMIC_CANDIDATE":
        return "economic_indicator_or_fed_parser_diagnostic"
    if bucket == "COMPANY_NEWS_CANDIDATE":
        return "company_news_entity_parser_diagnostic"
    if bucket in {"POLITICS_NEWS_CANDIDATE", "GEOPOLITICAL_NEWS_CANDIDATE"}:
        return "news_or_geopolitical_event_parser_diagnostic"
    if bucket == "COMMODITY_PRICE_CANDIDATE":
        return "commodity_price_source_parser_diagnostic"
    if bucket == "TRANSPORTATION_OPERATION_CANDIDATE":
        return "transportation_operations_source_parser_diagnostic"
    if bucket == "INFRASTRUCTURE_CAPACITY_CANDIDATE":
        return "infrastructure_capacity_source_parser_diagnostic"
    if bucket == "SPORTS_OR_CROSS_CATEGORY_LEAKAGE":
        return "sports_parser_reclassification_or_placeholder_watch"
    if bucket == "UNSUPPORTED_MULTI_LEG_GENERAL":
        return "structured_multi_leg_component_parser_required"
    return "manual_general_market_review"


def _matched_terms(text: str, bucket: str) -> list[str]:
    terms_by_bucket = {
        "ECONOMIC_CANDIDATE": ECONOMIC_TERMS,
        "POLITICS_NEWS_CANDIDATE": POLITICS_TERMS,
        "COMPANY_NEWS_CANDIDATE": COMPANY_TERMS,
        "GEOPOLITICAL_NEWS_CANDIDATE": GEOPOLITICAL_TERMS,
        "COMMODITY_PRICE_CANDIDATE": COMMODITY_PRICE_TERMS,
        "TRANSPORTATION_OPERATION_CANDIDATE": TRANSPORTATION_OPERATION_TERMS,
        "INFRASTRUCTURE_CAPACITY_CANDIDATE": INFRASTRUCTURE_CAPACITY_TERMS,
        "SPORTS_OR_CROSS_CATEGORY_LEAKAGE": SPORTS_TERMS,
    }
    terms = terms_by_bucket.get(bucket, ())
    return sorted(
        {
            term.strip()
            for term in terms
            if term.strip() and _term_matches(text, term)
        }
    )


def _family_key(item: dict[str, Any]) -> str:
    for key in ("event_ticker", "series_ticker"):
        value = item.get(key)
        if value:
            return str(value)
    ticker = str(item.get("ticker") or "")
    return ticker.split("-", 1)[0] if ticker else "UNKNOWN_FAMILY"


def _top_family_rows(counts: dict[str, int], *, limit: int = 10) -> list[dict[str, Any]]:
    return [
        {"family_key": family, "count": count}
        for family, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[
            :limit
        ]
    ]


def _sports_reclassification_candidate(item: dict[str, Any]) -> dict[str, Any]:
    text = _taxonomy_text(item)
    return {
        "ticker": item["ticker"],
        "title": item.get("title"),
        "status": item.get("status"),
        "series_ticker": item.get("series_ticker"),
        "event_ticker": item.get("event_ticker"),
        "family_key": _family_key(item),
        "current_category": DOMAIN_GENERAL,
        "proposed_category": _proposed_sports_category(item),
        "leakage_reasons": _sports_leakage_reasons(item),
        "matched_sports_terms": _matched_terms(text, "SPORTS_OR_CROSS_CATEGORY_LEAKAGE"),
        "leg_count": len(item.get("legs") or []),
        "sample_legs": list(item.get("legs") or [])[:5],
        "market_types": list(item.get("market_types") or []),
        "safe_to_apply": False,
        "requires_manual_review": True,
        "block_reason": (
            "Parser hygiene candidate only; do not rewrite market legs until the "
            "classification rule is reviewed and tested."
        ),
    }


def _r3_parser_preview(
    session: Session,
    item: dict[str, Any],
    market: Market | None,
    *,
    proposed_category: str,
) -> dict[str, Any]:
    stored_legs = [str(leg or "") for leg in item.get("legs") or []]
    stored_categories = [
        str(category or "")
        for category in session.scalars(
            select(MarketLeg.category)
            .where(MarketLeg.ticker == str(item["ticker"]))
            .order_by(MarketLeg.leg_index)
        )
    ]
    block_reasons: list[str] = []
    parsed_legs = parse_market_legs(market) if market is not None else []
    parser_legs = [parsed.raw_text for parsed in parsed_legs]
    parser_categories = [parsed.category for parsed in parsed_legs]
    parser_market_types = [parsed.market_type for parsed in parsed_legs]

    if market is None:
        block_reasons.append("market row is missing")
    if not stored_legs:
        block_reasons.append("no stored general legs to compare")
    if not parsed_legs:
        block_reasons.append("new parser returned no legs")
    if len(stored_legs) != len(parsed_legs):
        block_reasons.append("stored leg count differs from parser leg count")
    if len(stored_categories) != len(stored_legs) or any(
        category != DOMAIN_GENERAL for category in stored_categories
    ):
        block_reasons.append("stored ticker has non-general or mixed category legs")
    if _normalized_preview_legs(stored_legs) != _normalized_preview_legs(parser_legs):
        block_reasons.append("stored leg text differs from parser leg text")
    if parser_categories and any(category != proposed_category for category in parser_categories):
        block_reasons.append("parser category differs from proposed category")

    return {
        "stored_general_leg_count": len(stored_legs),
        "stored_total_leg_count": len(stored_categories),
        "parser_leg_count": len(parsed_legs),
        "stored_general_legs": stored_legs[:5],
        "parser_legs": parser_legs[:5],
        "stored_categories": sorted(set(stored_categories)),
        "parser_categories": sorted(set(parser_categories)),
        "parser_market_types": sorted(set(parser_market_types)),
        "parser_reasons": [parsed.reason for parsed in parsed_legs[:5]],
        "safe_to_reparse": not block_reasons,
        "block_reasons": block_reasons,
    }


def _normalized_preview_legs(legs: list[str]) -> list[str]:
    return [re.sub(r"\s+", " ", str(leg or "").strip().lower()) for leg in legs]


def _r3_exact_sports_link_preview_rows(
    session: Session,
    *,
    sample_limit: int,
) -> list[dict[str, Any]]:
    link_tickers = select(SportsMarketLink.ticker).distinct()
    prefix_filter = or_(
        *(MarketLeg.ticker.like(f"{prefix}%") for prefix in R3_EXACT_SPORTS_LINK_PREFIXES)
    )
    tickers = list(
        session.scalars(
            select(MarketLeg.ticker)
            .where(
                MarketLeg.category == "sports",
                ~MarketLeg.ticker.in_(link_tickers),
                prefix_filter,
            )
            .distinct()
            .order_by(MarketLeg.ticker)
            .limit(sample_limit)
        )
    )
    rows: list[dict[str, Any]] = []
    for ticker in tickers:
        market = session.get(Market, str(ticker))
        legs = list(
            session.scalars(
                select(MarketLeg)
                .where(MarketLeg.ticker == str(ticker))
                .order_by(MarketLeg.leg_index)
            )
        )
        sports_legs = [leg for leg in legs if leg.category == "sports"]
        rows.append(_r3_exact_sports_link_preview_row(market, legs, sports_legs))
    return rows


def _r3_exact_sports_link_preview_row(
    market: Market | None,
    legs: list[MarketLeg],
    sports_legs: list[MarketLeg],
) -> dict[str, Any]:
    ticker = str(market.ticker if market is not None else (legs[0].ticker if legs else ""))
    market_type = _r3_exact_sports_market_type(sports_legs)
    league = "SPORTS"
    game_key = f"{league}:kalshi-event-derived:{_r3_exact_sports_slug(ticker)}"
    block_reasons: list[str] = []
    if market is None:
        block_reasons.append("market row is missing")
    if not _r3_exact_sports_prefix_allowed(ticker):
        block_reasons.append("ticker is outside exact CS2/Valorant/cricket families")
    if not sports_legs:
        block_reasons.append("ticker has no sports legs")
    if len(sports_legs) != len(legs):
        block_reasons.append("ticker has mixed non-sports legs")
    if market_type == "UNKNOWN":
        block_reasons.append("sports market type is unknown")

    return {
        "ticker": ticker,
        "title": market.title if market is not None else None,
        "event_ticker": market.event_ticker if market is not None else None,
        "series_ticker": market.series_ticker if market is not None else None,
        "leg_count": len(sports_legs),
        "league": league,
        "game_key": game_key,
        "market_type": market_type,
        "link_confidence": "0.55",
        "link_reason": (
            "Kalshi-event-derived sports link built from Phase 3BB-R3 exact "
            "sports link preview. Use external schedule/team ingestion later to "
            "upgrade provenance."
        ),
        "matched_terms": [league.lower(), market_type.lower(), "kalshi_event_derived"],
        "raw_json": {
            "source": "kalshi_event_derived",
            "phase": "3BB-R3",
            "market_ticker": ticker,
            "market_title": market.title if market is not None else None,
            "event_ticker": market.event_ticker if market is not None else None,
            "series_ticker": market.series_ticker if market is not None else None,
            "leg_count": len(sports_legs),
            "legs": [_r3_exact_sports_leg_payload(leg) for leg in sports_legs[:12]],
        },
        "safe_to_link": not block_reasons,
        "block_reasons": block_reasons,
    }


def _r3_exact_sports_market_type(legs: list[MarketLeg]) -> str:
    values = [
        str(leg.market_type or "").upper()
        for leg in legs
        if str(leg.market_type or "").upper() not in {"", "UNKNOWN", "MARKET", "BINARY"}
    ]
    if values:
        return Counter(values).most_common(1)[0][0]
    return "TEAM_PROP" if legs else "UNKNOWN"


def _r3_exact_sports_prefix_allowed(ticker: str) -> bool:
    normalized = str(ticker or "").upper()
    return any(normalized.startswith(prefix) for prefix in R3_EXACT_SPORTS_LINK_PREFIXES)


def _r3_exact_sports_slug(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-") or "unknown"


def _r3_exact_sports_leg_payload(leg: MarketLeg) -> dict[str, Any]:
    return {
        "leg_index": leg.leg_index,
        "side": leg.side,
        "market_type": leg.market_type,
        "entity_name": leg.entity_name,
        "operator": leg.operator,
        "threshold_value": leg.threshold_value,
        "unit": leg.unit,
        "confidence": leg.confidence,
        "raw_text": leg.raw_text,
    }


def _r3_composite_category_counts(session: Session) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for category in R3_COMPOSITE_CATEGORIES:
        filters: list[Any] = [
            MarketLeg.category == category,
            _r3_kxmve_ticker_filter(MarketLeg.ticker),
        ]
        link_table = _r3_category_link_table(category)
        if link_table is not None:
            filters.append(~MarketLeg.ticker.in_(select(link_table.ticker).distinct()))
        markets, legs = session.execute(
            select(
                func.count(func.distinct(MarketLeg.ticker)),
                func.count(MarketLeg.id),
            ).where(*filters)
        ).one()
        counts[category] = {"markets": int(markets or 0), "legs": int(legs or 0)}
    return counts


def _r3_composite_preview_rows(
    session: Session,
    *,
    sample_limit: int,
) -> list[dict[str, Any]]:
    if sample_limit <= 0:
        return []
    markets = list(
        session.scalars(
            select(Market)
            .where(_r3_kxmve_ticker_filter(Market.ticker))
            .order_by(Market.ticker)
            .limit(sample_limit)
        )
    )
    tickers = [str(market.ticker) for market in markets]
    legs_by_ticker = _r3_legs_by_ticker(session, tickers)
    own_link_tickers = _r3_link_ticker_sets(session, tickers)
    component_rows_by_market = {
        str(market.ticker): _r3_component_rows_from_market(market) for market in markets
    }
    component_tickers = sorted(
        {
            str(component["component_ticker"])
            for components in component_rows_by_market.values()
            for component in components
            if component.get("component_ticker")
        }
    )
    component_evidence = _r3_component_evidence_by_ticker(session, component_tickers)
    rows: list[dict[str, Any]] = []
    for market in markets:
        legs = legs_by_ticker.get(str(market.ticker), [])
        unsupported_categories = _r3_unsupported_categories_for_market(
            legs,
            own_link_tickers=own_link_tickers,
        )
        if not unsupported_categories:
            continue
        components = component_rows_by_market.get(str(market.ticker), [])
        rows.append(
            _r3_composite_preview_row(
                market,
                legs,
                unsupported_categories=unsupported_categories,
                components=components,
                component_evidence=component_evidence,
            )
        )
    return rows


def _r3_composite_preview_row(
    market: Market,
    legs: list[MarketLeg],
    *,
    unsupported_categories: list[str],
    components: list[dict[str, Any]],
    component_evidence: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    component_rows = []
    for component in components:
        ticker = str(component.get("component_ticker") or "")
        evidence = component_evidence.get(ticker, _r3_empty_component_evidence(ticker))
        component_rows.append({**component, **evidence})
    mapping_errors = [
        str(component["mapping_status"])
        for component in component_rows
        if component.get("mapping_status") != "OK"
    ]
    verified_components = [
        component for component in component_rows if component["verified_component_evidence_found"]
    ]
    if not component_rows:
        classification = "TRUE_COMPOSITE_NO_COMPONENT_MAPPING"
        mapping_status = "MISSING"
    elif mapping_errors:
        classification = "COMPONENT_MAPPING_INVALID"
        mapping_status = "INVALID"
    elif len(verified_components) == len(component_rows):
        classification = "VERIFIED_COMPONENT_EVIDENCE"
        mapping_status = "MAPPED"
    elif verified_components:
        classification = "PARTIAL_COMPONENT_EVIDENCE"
        mapping_status = "MAPPED"
    else:
        classification = "COMPONENT_MAPPING_ONLY"
        mapping_status = "MAPPED"

    return {
        "ticker": market.ticker,
        "title": market.title,
        "event_ticker": market.event_ticker,
        "series_ticker": market.series_ticker,
        "market_status": market.status,
        "unsupported_categories": unsupported_categories,
        "leg_count": len(legs),
        "component_count": len(component_rows),
        "component_mapping_status": mapping_status,
        "classification": classification,
        "verified_component_count": len(verified_components),
        "missing_verified_component_count": max(len(component_rows) - len(verified_components), 0),
        "safe_to_apply": False,
        "safe_for_single_market_remediation": False,
        "next_action": _r3_composite_row_next_action(classification),
        "sample_legs": [_r3_composite_leg_payload(leg) for leg in legs[:10]],
        "component_evidence": component_rows[:20],
    }


def _r3_legs_by_ticker(
    session: Session,
    tickers: list[str],
) -> dict[str, list[MarketLeg]]:
    grouped: dict[str, list[MarketLeg]] = {}
    for chunk in _r3_chunks(tickers, 500):
        for leg in session.scalars(
            select(MarketLeg)
            .where(MarketLeg.ticker.in_(chunk))
            .order_by(MarketLeg.ticker, MarketLeg.leg_index)
        ):
            grouped.setdefault(str(leg.ticker), []).append(leg)
    return grouped


def _r3_link_ticker_sets(
    session: Session,
    tickers: list[str],
) -> dict[str, set[str]]:
    sets: dict[str, set[str]] = {}
    for category in R3_COMPOSITE_CATEGORIES:
        link_table = _r3_category_link_table(category)
        if link_table is None:
            sets[category] = set()
            continue
        linked: set[str] = set()
        for chunk in _r3_chunks(tickers, 500):
            linked.update(
                str(ticker)
                for ticker in session.scalars(
                    select(link_table.ticker).where(link_table.ticker.in_(chunk)).distinct()
                )
            )
        sets[category] = linked
    return sets


def _r3_unsupported_categories_for_market(
    legs: list[MarketLeg],
    *,
    own_link_tickers: dict[str, set[str]],
) -> list[str]:
    categories = sorted({str(leg.category or "") for leg in legs if str(leg.category or "")})
    unsupported: list[str] = []
    ticker = str(legs[0].ticker) if legs else ""
    for category in categories:
        link_table = _r3_category_link_table(category)
        if link_table is not None and ticker in own_link_tickers.get(category, set()):
            continue
        unsupported.append(category)
    return unsupported


def _r3_component_rows_from_market(market: Market) -> list[dict[str, Any]]:
    raw = decode_json(market.raw_json)
    selected = raw.get("mve_selected_legs")
    if isinstance(selected, list):
        return _r3_component_rows_from_selected_legs(selected)
    custom = raw.get("custom_strike")
    if isinstance(custom, dict):
        return _r3_component_rows_from_custom_strike(custom)
    return []


def _r3_component_rows_from_selected_legs(selected: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(selected):
        if not isinstance(item, dict):
            rows.append(
                {
                    "leg_index": index,
                    "component_ticker": None,
                    "component_event_ticker": None,
                    "selected_side": None,
                    "mapping_status": "INVALID_COMPONENT_PAYLOAD",
                    "mapping_source": "mve_selected_legs",
                }
            )
            continue
        component_ticker = _r3_optional_text(
            item.get("market_ticker")
            or item.get("ticker")
            or item.get("component_ticker")
            or item.get("underlying_ticker")
        )
        selected_side = _r3_normalize_component_side(item.get("side") or item.get("selected_side"))
        rows.append(
            {
                "leg_index": index,
                "component_ticker": component_ticker,
                "component_event_ticker": _r3_optional_text(item.get("event_ticker")),
                "selected_side": selected_side,
                "mapping_status": "OK"
                if component_ticker and selected_side in R3_COMPONENT_SUPPORTED_SIDES
                else "INVALID_COMPONENT_MAPPING",
                "mapping_source": "mve_selected_legs",
            }
        )
    return rows


def _r3_component_rows_from_custom_strike(custom: dict[str, Any]) -> list[dict[str, Any]]:
    markets = _r3_csv_values(custom.get("Associated Markets"))
    events = _r3_csv_values(custom.get("Associated Events"))
    sides = _r3_csv_values(custom.get("Associated Market Sides"))
    rows: list[dict[str, Any]] = []
    for index, component_ticker in enumerate(markets):
        selected_side = _r3_normalize_component_side(sides[index] if index < len(sides) else None)
        rows.append(
            {
                "leg_index": index,
                "component_ticker": component_ticker or None,
                "component_event_ticker": events[index] if index < len(events) else None,
                "selected_side": selected_side,
                "mapping_status": "OK"
                if component_ticker and selected_side in R3_COMPONENT_SUPPORTED_SIDES
                else "INVALID_COMPONENT_MAPPING",
                "mapping_source": "custom_strike",
            }
        )
    return rows


def _r3_component_evidence_by_ticker(
    session: Session,
    component_tickers: list[str],
) -> dict[str, dict[str, Any]]:
    evidence = {
        ticker: _r3_empty_component_evidence(ticker)
        for ticker in component_tickers
        if ticker
    }
    if not evidence:
        return evidence
    for chunk in _r3_chunks(sorted(evidence), 500):
        market_tickers = set(
            session.scalars(select(Market.ticker).where(Market.ticker.in_(chunk)))
        )
        settlement_tickers = set(
            session.scalars(select(Settlement.ticker).where(Settlement.ticker.in_(chunk)))
        )
        for ticker in chunk:
            evidence[ticker]["exact_market_found"] = ticker in market_tickers
            evidence[ticker]["exact_settlement_found"] = ticker in settlement_tickers
    _r3_apply_component_link_evidence(session, evidence)
    for row in evidence.values():
        row["verified_component_evidence_found"] = bool(
            row["exact_settlement_found"] or row["verified_link_found"]
        )
    return evidence


def _r3_apply_component_link_evidence(
    session: Session,
    evidence: dict[str, dict[str, Any]],
) -> None:
    tickers = sorted(evidence)
    link_tables = {
        "crypto": CryptoMarketLink,
        "weather": WeatherMarketLink,
        "economic": EconomicMarketLink,
        "sports": SportsMarketLink,
        "news": NewsMarketLink,
    }
    for category, table in link_tables.items():
        for chunk in _r3_chunks(tickers, 500):
            rows = list(session.scalars(select(table).where(table.ticker.in_(chunk))))
            for row in rows:
                ticker = str(row.ticker)
                if ticker not in evidence:
                    continue
                evidence[ticker]["specialized_link_found"] = True
                evidence[ticker]["link_categories"].append(category)
                if category != "sports" or _r3_sports_link_is_verified(row):
                    evidence[ticker]["verified_link_found"] = True


def _r3_empty_component_evidence(ticker: str) -> dict[str, Any]:
    return {
        "component_ticker": ticker or None,
        "exact_market_found": False,
        "exact_settlement_found": False,
        "specialized_link_found": False,
        "verified_link_found": False,
        "verified_component_evidence_found": False,
        "link_categories": [],
    }


def _r3_sports_link_is_verified(link: SportsMarketLink) -> bool:
    reason = str(link.link_reason or "").lower()
    game_key = str(link.game_key or "").lower()
    raw = decode_json(link.raw_json)
    source = str(raw.get("source") or "").lower()
    if "verified schedule" in reason or source == "verified_schedule":
        return True
    if "kalshi-event-derived" in game_key or source == "kalshi_event_derived":
        return False
    if "market-derived" in game_key or "market-derived" in reason:
        return False
    return bool(link.link_confidence)


def _r3_composite_leg_payload(leg: MarketLeg) -> dict[str, Any]:
    return {
        "leg_index": leg.leg_index,
        "category": leg.category,
        "market_type": leg.market_type,
        "side": leg.side,
        "entity_name": leg.entity_name,
        "operator": leg.operator,
        "threshold_value": leg.threshold_value,
        "unit": leg.unit,
        "raw_text": leg.raw_text,
    }


def _r3_kxmve_ticker_filter(column: Any) -> Any:
    normalized = func.upper(func.coalesce(column, ""))
    return or_(*(normalized.like(f"{prefix}%") for prefix in R3_KXMVE_COMPOSITE_PREFIXES))


def _r3_category_link_table(category: str) -> Any | None:
    return {
        "crypto": CryptoMarketLink,
        "weather": WeatherMarketLink,
        "economic": EconomicMarketLink,
        "sports": SportsMarketLink,
        "news": NewsMarketLink,
    }.get(category)


def _r3_normalize_component_side(value: Any) -> str | None:
    normalized = _text(value).lower()
    if normalized in {"yes", "y", "1", "true"}:
        return "yes"
    if normalized in {"no", "n", "0", "false"}:
        return "no"
    return None


def _r3_optional_text(value: Any) -> str | None:
    text = _text(value)
    return text or None


def _r3_csv_values(value: Any) -> list[str]:
    return [_text(part) for part in str(value or "").split(",") if _text(part)]


def _r3_chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _r3_composite_row_next_action(classification: str) -> str:
    if classification == "VERIFIED_COMPONENT_EVIDENCE":
        return (
            "Keep out of single-market remediation; send to paper-only composite "
            "operator review/component model gate."
        )
    if classification == "PARTIAL_COMPONENT_EVIDENCE":
        return "Refresh or verify missing component evidence before any composite support."
    if classification == "COMPONENT_MAPPING_ONLY":
        return "Component tickers are mapped, but verified evidence is still missing."
    if classification == "COMPONENT_MAPPING_INVALID":
        return "Repair component mapping before any composite support."
    return "True composite backlog; requires composite-market support, not link remediation."


def _r3_composite_operator_preflight_row(
    session: Session,
    row: dict[str, Any],
    *,
    max_quote_age_minutes: int,
    min_liquidity_dollars: Decimal,
) -> dict[str, Any]:
    ticker = str(row.get("ticker") or "")
    market = session.get(Market, ticker)
    latest_snapshot = _r3_latest_snapshot(session, ticker)
    snapshot_payload = _r3_snapshot_payload(
        latest_snapshot,
        max_quote_age_minutes=max_quote_age_minutes,
        min_liquidity_dollars=min_liquidity_dollars,
    )
    component_rows = list(row.get("component_evidence") or [])
    block_reasons: list[str] = []
    if not ticker:
        block_reasons.append("missing_composite_ticker")
    if market is None:
        block_reasons.append("composite_market_missing")
    elif str(market.status or "").lower() not in OPEN_STATUSES:
        block_reasons.append("composite_market_not_open")
    if not component_rows:
        block_reasons.append("component_evidence_missing")
    if any(not component.get("verified_component_evidence_found") for component in component_rows):
        block_reasons.append("component_verified_evidence_incomplete")
    if latest_snapshot is None:
        block_reasons.append("composite_quote_missing")
    else:
        if not snapshot_payload["quote_fresh"]:
            block_reasons.append("composite_quote_stale")
        if not snapshot_payload["liquidity_ok"]:
            block_reasons.append("composite_liquidity_below_floor")

    component_summary = _r3_component_preflight_summary(
        session,
        component_rows,
        max_quote_age_minutes=max_quote_age_minutes,
    )
    if component_summary["component_quote_required_stale"] > 0:
        block_reasons.append("component_quote_required_stale")
    if component_summary["component_quote_required_missing"] > 0:
        block_reasons.append("component_quote_required_missing")

    ready = not block_reasons
    return {
        "ticker": ticker,
        "title": row.get("title"),
        "market_status": market.status if market is not None else None,
        "classification": row.get("classification"),
        "component_count": len(component_rows),
        "verified_component_count": sum(
            1 for component in component_rows if component.get("verified_component_evidence_found")
        ),
        "component_preflight": component_summary,
        "composite_snapshot": snapshot_payload,
        "paper_composite_review_ready": ready,
        "safe_to_apply": False,
        "creates_paper_trades": False,
        "block_reasons": block_reasons,
        "next_action": (
            "Paper-only operator review/risk packet can inspect this row."
            if ready
            else "Keep blocked until component evidence, quote freshness, and liquidity pass."
        ),
    }


def _r3_component_preflight_summary(
    session: Session,
    component_rows: list[dict[str, Any]],
    *,
    max_quote_age_minutes: int,
) -> dict[str, Any]:
    exact_settlement_rows = sum(1 for row in component_rows if row.get("exact_settlement_found"))
    quote_required_rows = [
        row for row in component_rows if row.get("verified_component_evidence_found")
        and not row.get("exact_settlement_found")
    ]
    stale = 0
    missing = 0
    for component in quote_required_rows:
        snapshot = _r3_latest_snapshot(session, str(component.get("component_ticker") or ""))
        if snapshot is None:
            missing += 1
            continue
        if not _r3_snapshot_is_fresh(snapshot, max_quote_age_minutes=max_quote_age_minutes):
            stale += 1
    return {
        "components": len(component_rows),
        "verified_component_evidence": sum(
            1 for row in component_rows if row.get("verified_component_evidence_found")
        ),
        "exact_component_settlements": exact_settlement_rows,
        "quote_required_components": len(quote_required_rows),
        "component_quote_required_missing": missing,
        "component_quote_required_stale": stale,
    }


def _r3_latest_snapshot(session: Session, ticker: str) -> MarketSnapshot | None:
    if not ticker:
        return None
    return session.scalar(
        select(MarketSnapshot)
        .where(MarketSnapshot.ticker == ticker)
        .order_by(desc(MarketSnapshot.captured_at))
        .limit(1)
    )


def _r3_snapshot_payload(
    snapshot: MarketSnapshot | None,
    *,
    max_quote_age_minutes: int,
    min_liquidity_dollars: Decimal,
) -> dict[str, Any]:
    if snapshot is None:
        return {
            "captured_at": None,
            "age_seconds": None,
            "quote_fresh": False,
            "liquidity_ok": False,
            "liquidity_dollars": None,
            "has_executable_quote": False,
        }
    liquidity = _r3_decimal(
        snapshot.volume_fp or snapshot.volume_24h_fp or snapshot.open_interest_fp
    )
    has_quote = any(
        _r3_decimal(value) is not None and _r3_decimal(value) > 0
        for value in (
            snapshot.yes_bid_dollars,
            snapshot.yes_ask_dollars,
            snapshot.no_bid_dollars,
            snapshot.no_ask_dollars,
            snapshot.best_yes_bid,
            snapshot.best_yes_ask,
            snapshot.best_no_bid,
            snapshot.best_no_ask,
        )
    )
    return {
        "captured_at": _iso_or_none(snapshot.captured_at),
        "age_seconds": _r3_snapshot_age_seconds(snapshot),
        "quote_fresh": _r3_snapshot_is_fresh(
            snapshot,
            max_quote_age_minutes=max_quote_age_minutes,
        ),
        "liquidity_ok": bool(
            has_quote and liquidity is not None and liquidity >= min_liquidity_dollars
        ),
        "liquidity_dollars": str(liquidity) if liquidity is not None else None,
        "has_executable_quote": has_quote,
    }


def _r3_snapshot_is_fresh(
    snapshot: MarketSnapshot,
    *,
    max_quote_age_minutes: int,
) -> bool:
    age = _r3_snapshot_age_seconds(snapshot)
    return age is not None and age <= max_quote_age_minutes * 60


def _r3_snapshot_age_seconds(snapshot: MarketSnapshot) -> int | None:
    captured = snapshot.captured_at
    if captured is None:
        return None
    now = utc_now()
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=now.tzinfo)
    return max(int((now - captured).total_seconds()), 0)


def _r3_decimal(value: Any) -> Decimal | None:
    try:
        text = str(value).replace(",", "").strip()
        return Decimal(text) if text else None
    except Exception:  # noqa: BLE001 - malformed market data should block the gate.
        return None


def _r3_composite_operator_preflight_missing_payload(
    *,
    preview_path: Path,
    max_quote_age_minutes: int,
    min_liquidity_dollars: Decimal,
) -> dict[str, Any]:
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3BB-R3",
        "phase_version": "phase3bb_r3_composite_operator_preflight_v1",
        "mode": "PAPER_ONLY_COMPOSITE_OPERATOR_PREFLIGHT",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "status": "PREVIEW_GATE_MISSING",
        "preview_source_path": str(preview_path),
        "preview_status": "MISSING",
        "safety_gate": {
            "writes_market_legs": False,
            "writes_links": False,
            "writes_settlements": False,
            "creates_paper_trades": False,
            "runs_single_market_remediation": False,
            "live_or_demo_execution": False,
            "safe_to_apply_rows": 0,
            "reason": "Composite preview gate artifact is missing.",
        },
        "settings": {
            "max_quote_age_minutes": max_quote_age_minutes,
            "min_liquidity_dollars": str(min_liquidity_dollars),
            "sample_limit": 0,
        },
        "summary": {
            "verified_component_rows_from_preview": 0,
            "rows_reviewed": 0,
            "sample_truncated": False,
            "paper_composite_review_ready_rows": 0,
            "blocked_rows": 0,
            "safe_to_apply_rows": 0,
            "blocker_counts": {"preview_gate_missing": 1},
        },
        "rows": [],
        "paper_composite_review_ready_rows": [],
        "blocked_rows": [],
        "recommended_next_action": (
            "Run phase3bb-r3-composite-preview-gate before composite preflight."
        ),
        "next_commands": [
            (
                "kalshi-bot phase3bb-r3-composite-preview-gate "
                "--output-dir reports/phase3bb_r3_composites"
            )
        ],
    }


def _r3_composite_recommended_next_action(
    *,
    verified_rows: list[dict[str, Any]],
    true_rows: list[dict[str, Any]],
) -> str:
    if verified_rows:
        return (
            "Review verified-component composite rows in paper-only mode and design a "
            "composite operator/risk gate. Do not run normal single-market remediation."
        )
    if true_rows:
        return (
            "Remaining KXMVE rows are true composites or invalid mappings. Keep them "
            "isolated until composite-market support or component mapping evidence exists."
        )
    return "No unsupported KXMVE composite rows were found in the current preview."


def _r3_composite_operator_preflight_next_action(
    ready_rows: list[dict[str, Any]],
) -> str:
    if ready_rows:
        return (
            "Review the paper-only composite operator/risk packet for ready rows; "
            "do not create paper trades until a separate explicit paper-trade gate passes."
        )
    return (
        "Verified-component composites are still blocked by freshness/liquidity/open-market "
        "checks. Refresh market data and rerun this preflight."
    )


def _r3_composite_next_commands(
    *,
    verified_rows: list[dict[str, Any]],
) -> list[str]:
    commands = [
        (
            "kalshi-bot phase3bb-r3-composite-preview-gate "
            "--output-dir reports/phase3bb_r3_composites"
        ),
        "kalshi-bot market-coverage-doctor --output-dir reports/market_coverage",
        "kalshi-bot link-coverage --output reports/link_coverage_report.md",
    ]
    if verified_rows:
        commands.append(
            "kalshi-bot phase3bb-r3-composite-operator-preflight "
            "--output-dir reports/phase3bb_r3_composites"
        )
    else:
        commands.append("# hold: no normal single-market link remediation for KXMVE composites")
    return commands


def _r3_composite_operator_preflight_next_commands(
    ready_rows: list[dict[str, Any]],
) -> list[str]:
    commands = [
        (
            "kalshi-bot phase3bb-r3-composite-preview-gate "
            "--output-dir reports/phase3bb_r3_composites"
        ),
        (
            "kalshi-bot phase3bb-r3-composite-operator-preflight "
            "--output-dir reports/phase3bb_r3_composites"
        ),
        "kalshi-bot market-coverage-doctor --output-dir reports/market_coverage",
        "kalshi-bot link-coverage --output reports/link_coverage_report.md",
    ]
    if ready_rows:
        commands.append("# next: design explicit paper composite trade gate after operator review")
    else:
        commands.append("# hold: refresh market data before any paper composite review")
    return commands


def _manual_general_review_row(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "ticker": item["ticker"],
        "title": item.get("title"),
        "status": item.get("status"),
        "series_ticker": item.get("series_ticker"),
        "event_ticker": item.get("event_ticker"),
        "family_key": _family_key(item),
        "current_category": DOMAIN_GENERAL,
        "proposed_category": "MANUAL_REVIEW_REQUIRED",
        "leg_count": len(item.get("legs") or []),
        "sample_legs": list(item.get("legs") or [])[:10],
        "market_types": list(item.get("market_types") or []),
        "safe_to_apply": False,
        "block_reason": "No reliable specialized category rule matched this general row.",
    }


def _sports_leakage_reasons(item: dict[str, Any]) -> list[str]:
    text = _taxonomy_text(item)
    reasons: list[str] = []
    if "kxmv" in text:
        reasons.append("KXMV_PREFIX")
    if _sports_market_prefix_match(text) and "KXMV_PREFIX" not in reasons:
        reasons.append("SPORTS_MARKET_PREFIX")
    if "multigame" in text:
        reasons.append("MULTIGAME_MARKET_FAMILY")
    if _matched_terms(text, "SPORTS_OR_CROSS_CATEGORY_LEAKAGE"):
        reasons.append("SPORTS_TERM_MATCH")
    if not reasons:
        reasons.append("SPORTS_LEAKAGE_HEURISTIC")
    return reasons


def _proposed_sports_category(item: dict[str, Any]) -> str:
    text = _taxonomy_text(item)
    if "crosscategory" in text:
        return "cross_category"
    return "sports"


def _taxonomy_text(item: dict[str, Any]) -> str:
    parts = [
        item.get("ticker"),
        item.get("title"),
        item.get("series_ticker"),
        item.get("event_ticker"),
        " ".join(str(leg) for leg in item.get("legs") or []),
    ]
    text = " ".join(str(part or "") for part in parts).lower()
    text = re.sub(r"\s+", " ", text)
    return f" {text} "


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(_term_matches(text, term) for term in terms)


def _term_matches(text: str, term: str) -> bool:
    needle = term.strip().lower()
    if not needle:
        return False
    if re.search(r"[a-z0-9]", needle):
        return re.search(
            rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])",
            text,
        ) is not None
    return needle in text


def _sports_market_prefix_match(text: str) -> bool:
    return any(
        re.search(rf"(?<![a-z0-9]){re.escape(prefix)}", text) is not None
        for prefix in SPORTS_MARKET_PREFIXES
    )


def _count(session: Session, column: Any) -> int:
    return int(session.scalar(select(func.count(column))) or 0)


def _max_value(session: Session, column: Any) -> Any:
    return session.scalar(select(func.max(column)))


def _active_parsed_markets(session: Session, category: str) -> int:
    return int(
        session.scalar(
            select(func.count(distinct(MarketLeg.ticker)))
            .join(Market, Market.ticker == MarketLeg.ticker)
            .where(
                MarketLeg.category == category,
                func.lower(func.coalesce(Market.status, "")).in_(OPEN_STATUSES),
            )
        )
        or 0
    )


def _latest_leg_time(session: Session, category: str) -> datetime | None:
    return session.scalar(
        select(func.max(MarketLeg.parsed_at)).where(MarketLeg.category == category)
    )


def _market_examples(
    session: Session,
    category: str,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    rows = session.execute(
        select(
            Market.ticker,
            Market.title,
            Market.status,
            Market.event_ticker,
            Market.series_ticker,
            MarketLeg.raw_text,
            MarketLeg.market_type,
        )
        .join(MarketLeg, MarketLeg.ticker == Market.ticker)
        .where(MarketLeg.category == category)
        .order_by(desc(Market.last_seen_at), Market.ticker, MarketLeg.leg_index)
        .limit(limit * 3)
    )
    examples: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        ticker = str(row.ticker)
        if ticker in seen:
            continue
        seen.add(ticker)
        examples.append(
            {
                "ticker": ticker,
                "title": row.title,
                "status": row.status,
                "event_ticker": row.event_ticker,
                "series_ticker": row.series_ticker,
                "example_leg": row.raw_text,
                "market_type": row.market_type,
            }
        )
        if len(examples) >= limit:
            break
    return examples


def _recommended_next_action(rows: list[dict[str, Any]]) -> str:
    general = next(row for row in rows if row["domain"] == DOMAIN_GENERAL)
    actionable = [row for row in rows if row["actionable_now"]]
    if general in actionable:
        return (
            "Economic/news are waiting on compatible markets or fresh source runs; "
            "the best code phase is a general-market taxonomy and readability pass."
        )
    if actionable:
        return f"Run the safe commands for {actionable[0]['domain']}."
    return "Keep market/news/economic refreshes running; no domain is actionable yet."


def _next_commands(rows: list[dict[str, Any]]) -> list[str]:
    commands: list[str] = []
    for row in rows:
        if row["actionable_now"]:
            commands.extend(str(command) for command in row.get("safe_commands", []))
    commands.extend(
        [
            "kalshi-bot market-coverage-doctor --output-dir reports/market_coverage",
            (
                "kalshi-bot phase-orchestrator --analyze --output reports/phase_orchestrator.md "
                "--json-output reports/phase_orchestrator.json --next-prompt prompts/next_phase.md"
            ),
        ]
    )
    deduped: list[str] = []
    for command in commands:
        if command not in deduped:
            deduped.append(command)
    return deduped


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3BB Domain Readiness",
        "",
        f"- Generated: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Recommended next action: {payload['recommended_next_action']}",
        "",
        "## Domain Rows",
        "",
        "| Domain | Status | Actionable | Primary blocker | Counts |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in payload["domain_rows"]:
        counts = ", ".join(f"{key}={value}" for key, value in row["counts"].items())
        lines.append(
            "| {domain} | {status} | {actionable} | {blocker} | {counts} |".format(
                domain=row["domain"],
                status=row["status"],
                actionable="yes" if row["actionable_now"] else "no",
                blocker=str(row["primary_blocker"]).replace("|", "/"),
                counts=counts,
            )
        )
        if row["domain"] == DOMAIN_GENERAL:
            taxonomy = ", ".join(
                f"{key}={value}" for key, value in row.get("taxonomy_counts", {}).items()
            )
            lines.append(
                "| {domain} taxonomy | {status} | {actionable} | {blocker} | {counts} |".format(
                    domain=row["domain"],
                    status="taxonomy",
                    actionable="n/a",
                    blocker="general market sub-buckets",
                    counts=taxonomy or "none",
                )
            )
    lines.extend(["", "## Next Commands", ""])
    lines.extend(f"- `{command}`" for command in payload["next_commands"])
    lines.extend(["", "## Examples", ""])
    for row in payload["domain_rows"]:
        lines.append(f"### {row['domain']}")
        examples = row.get("examples", [])
        if not examples:
            lines.append("- none")
            continue
        for example in examples:
            lines.append(
                "- `{ticker}` {title} ({status})".format(
                    ticker=example.get("ticker"),
                    title=example.get("title") or "",
                    status=example.get("status") or "unknown",
                )
            )
    lines.append("")
    return "\n".join(lines)


def _render_r2_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3BB-R2 General Candidate Routing",
        "",
        f"- Generated: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Recommended next action: {payload['recommended_next_action']}",
        "",
        "## Summary",
        "",
    ]
    summary = payload["summary"]
    for key, value in summary.items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Candidate Rows",
            "",
            (
                "| Bucket | Route | Priority | Family | Ticker | Matched terms | "
                "Legs | Safe | Block reason |"
            ),
            "| --- | --- | --- | --- | --- | --- | ---: | --- | --- |",
        ]
    )
    for row in payload["route_rows"]:
        lines.append(
            (
                "| {bucket} | {route} | {priority} | {family} | `{ticker}` | "
                "{terms} | {legs} | {safe} | {block} |"
            ).format(
                bucket=row["taxonomy_bucket"],
                route=row["route_domain"],
                priority=row["candidate_priority"],
                family=str(row["family_key"]).replace("|", "/"),
                ticker=row["ticker"],
                terms=", ".join(row.get("matched_terms") or []),
                legs=row["leg_count"],
                safe="yes" if row["safe_to_apply"] else "no",
                block=str(row["block_reason"]).replace("|", "/"),
            )
        )
    if not payload["route_rows"]:
        lines.append("| none | none |  | 0 | no | No general candidates found. |")
    diagnostics = payload.get("general_signal_diagnostic_rows") or []
    diagnostic_summary = summary.get("general_signal_diagnostics") or {}
    lines.extend(
        [
            "",
            "## General Signal Parser Diagnostics",
            "",
        ]
    )
    if diagnostic_summary:
        for key, value in diagnostic_summary.items():
            lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "| Diagnostic | Source adapter | Family | Ticker | Parsed fields | Gaps | Safe |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in diagnostics:
        parsed_fields = ", ".join(
            f"{key}={value}" for key, value in row.get("parsed_fields", {}).items()
        )
        lines.append(
            (
                "| {diagnostic} | {adapter} | {family} | `{ticker}` | "
                "{fields} | {gaps} | {safe} |"
            ).format(
                diagnostic=str(row["diagnostic_name"]).replace("|", "/"),
                adapter=str(row["source_adapter_key"]).replace("|", "/"),
                family=str(row["family_key"]).replace("|", "/"),
                ticker=row["ticker"],
                fields=parsed_fields.replace("|", "/"),
                gaps=", ".join(row.get("evidence_gaps") or []).replace("|", "/"),
                safe="yes" if row["safe_to_apply"] else "no",
            )
        )
    if not diagnostics:
        lines.append("| none | none |  |  | none | none | no |")
    lines.extend(["", "## Next Commands", ""])
    lines.extend(f"- `{command}`" for command in payload["next_commands"])
    lines.append("")
    return "\n".join(lines)


def _write_general_source_template_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    headers = [
        "ticker",
        "source_adapter_key",
        "source_subject",
        "commodity",
        "variety",
        "metric",
        "price_usd_each",
        "as_of_date",
        "region",
        "period_start",
        "period_end",
        "cancellation_count",
        "measurement_year",
        "capacity_gw",
        "threshold",
        "threshold_unit",
        "direction",
        "time_window",
        "source_name",
        "source_url",
        "verification_status",
        "retrieved_at",
        "evidence_notes",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_manifest(path: Path, files: list[Path]) -> None:
    lines = []
    base = path.parent.resolve()
    for file_path in sorted(files, key=lambda item: item.name):
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        try:
            label = file_path.resolve().relative_to(base).as_posix()
        except ValueError:
            label = file_path.as_posix()
        lines.append(f"{digest}  {label}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _write_csv(path: Path, headers: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _csv_headers(rows: list[dict[str, Any]]) -> list[str]:
    headers: list[str] = []
    for row in rows:
        for key in row:
            if key not in headers:
                headers.append(key)
    return headers


def _group_source_key(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
    adapter = _text(row.get("source_adapter_key") or row.get("adapter"))
    subject = _text(row.get("source_subject") or row.get("subject"))
    metric = _text(row.get("metric"))
    region = _text(row.get("region"))
    as_of_or_period = _as_of_or_period(row)
    return (adapter, subject, metric, region, as_of_or_period)


def _as_of_or_period(row: dict[str, Any]) -> str:
    collapsed = _text(row.get("as_of_or_period"))
    if collapsed:
        return collapsed
    as_of_date = _text(row.get("as_of_date"))
    measurement_year = _text(row.get("measurement_year"))
    period_start = _text(row.get("period_start"))
    period_end = _text(row.get("period_end"))
    if as_of_date:
        return as_of_date
    if measurement_year:
        return measurement_year
    if period_start or period_end:
        return f"{period_start}..{period_end}".strip(".")
    return _text(row.get("time_window"))


def _group_source_review_row(rows: list[dict[str, str]]) -> dict[str, Any]:
    first = rows[0] if rows else {}
    adapter = _text(first.get("source_adapter_key"))
    observed_column = _observed_value_column(adapter)
    key = _group_source_key(first)
    values = _unique_nonblank(row.get(observed_column) for row in rows)
    thresholds = _unique_nonblank(
        _threshold_label(row) for row in rows
    )
    tickers = _unique_nonblank(row.get("ticker") for row in rows)
    return {
        "group_id": _group_id(key),
        "source_adapter_key": key[0],
        "adapter": key[0],
        "source_subject": key[1],
        "subject": key[1],
        "metric": key[2],
        "region": key[3],
        "as_of_or_period": key[4],
        "row_count": len(rows),
        "tickers": ";".join(tickers),
        "thresholds": ";".join(thresholds),
        "observed_value_column": observed_column,
        "observed_value": values[0] if len(values) == 1 else "",
        "source_name": _shared_nonblank(rows, "source_name"),
        "source_url": _shared_nonblank(rows, "source_url"),
        "verification_status": _shared_nonblank(rows, "verification_status"),
        "retrieved_at": _shared_nonblank(rows, "retrieved_at"),
        "evidence_notes": _shared_nonblank(rows, "evidence_notes"),
    }


def _group_source_review_headers() -> list[str]:
    return [
        "group_id",
        "source_adapter_key",
        "adapter",
        "source_subject",
        "subject",
        "metric",
        "region",
        "as_of_or_period",
        "row_count",
        "tickers",
        "thresholds",
        "observed_value_column",
        "observed_value",
        "source_name",
        "source_url",
        "verification_status",
        "retrieved_at",
        "evidence_notes",
    ]


def _observed_value_column(adapter: str) -> str:
    if adapter == "commodity_advertised_price_source":
        return "price_usd_each"
    if adapter == "transportation_flight_cancellation_source":
        return "cancellation_count"
    if adapter == "infrastructure_data_center_capacity_source":
        return "capacity_gw"
    return "observed_value"


def _threshold_label(row: dict[str, Any]) -> str:
    threshold = _text(row.get("threshold"))
    unit = _text(row.get("threshold_unit"))
    direction = _text(row.get("direction"))
    return " ".join(part for part in (direction, threshold, unit) if part)


def _unique_nonblank(values: Any) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        text_value = _text(value)
        if not text_value or text_value in seen:
            continue
        seen.add(text_value)
        unique.append(text_value)
    return unique


def _shared_nonblank(rows: list[dict[str, str]], field: str) -> str:
    values = _unique_nonblank(row.get(field) for row in rows)
    return values[0] if len(values) == 1 else ""


def _group_id(key: tuple[str, str, str, str, str]) -> str:
    payload = json.dumps(key, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def _apply_group_values(target: dict[str, str], group: dict[str, str]) -> bool:
    changed = False
    observed_column = _text(group.get("observed_value_column"))
    observed_value = _text(group.get("observed_value"))
    if observed_column and observed_value and not _text(target.get(observed_column)):
        target[observed_column] = observed_value
        changed = True
    for field in (
        "source_name",
        "source_url",
        "verification_status",
        "retrieved_at",
        "evidence_notes",
    ):
        value = _text(group.get(field))
        if value and not _text(target.get(field)):
            target[field] = value
            changed = True
    return changed


def _render_source_intake_markdown(payload: dict[str, Any]) -> str:
    metadata = payload["report_metadata"]
    lines = [
        "# Phase 3BB-R2 General Source Intake",
        "",
        f"- Generated: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety mode: {payload['safety_mode']}",
        f"- Paper safety: {payload['paper_only_safety']}",
        f"- Git commit: `{payload['git_commit']}`",
        f"- Database fingerprint: `{payload['database_fingerprint']}`",
        f"- Data watermark: `{payload['data_watermark']}`",
        f"- Taxonomy version: `{payload['taxonomy_version']}`",
        "- Source readiness schema version: "
        f"`{payload['source_readiness_schema_version']}`",
        f"- Command arguments: `{metadata['command_arguments']}`",
        f"- Evidence dir: `{payload['evidence_dir']}`",
        f"- Recommended next action: {payload['recommended_next_action']}",
        "",
        "## Runtime Identity",
        "",
        f"- Repository root: `{metadata['repository_root']}`",
        f"- Git branch: `{metadata['git_branch']}`",
        f"- Python executable: `{metadata['python_executable']}`",
        f"- Installed package path: `{metadata['installed_package_path']}`",
        f"- Resolved DATABASE_URL: `{metadata['resolved_database_url']}`",
        f"- Migration revision: `{metadata['migration_revision']}`",
        f"- Timezone: `{metadata['timezone']}`",
        "",
        "## Source State",
        "",
    ]
    for key, value in payload["source_state"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Summary", ""])
    for key, value in payload["summary"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Taxonomy Counts",
            "",
            "| Label | Count |",
            "| --- | --- |",
        ]
    )
    for label, count in payload["summary"].get("taxonomy_counts", {}).items():
        lines.append(f"| {label} | {count} |")
    lines.extend(
        [
            "",
            "## Source Readiness Matrix",
            "",
            "| Source | State | Link-safe | Forecast-safe | Blocker |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for row in payload["source_readiness_matrix"]:
        lines.append(
            "| {source} | {state} | {link} | {forecast} | {blocker} |".format(
                source=str(row["source_name"]).replace("|", "/"),
                state=str(row["readiness_state"]).replace("|", "/"),
                link="yes" if row["link_safe"] else "no",
                forecast="yes" if row["forecast_safe"] else "no",
                blocker=str(row["current_blocker"]).replace("|", "/"),
            )
        )
    lines.extend(["", "## Safety Gate", ""])
    for key, value in payload["safety_gate"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Input Rows",
            "",
            "| Status | Adapter | Matched tickers | Missing fields | Safe | Block reason |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in payload["input_rows"][:200]:
        lines.append(
            "| {status} | {adapter} | {tickers} | {missing} | {safe} | {block} |".format(
                status=str(row["status"]).replace("|", "/"),
                adapter=str(row["source_adapter_key"]).replace("|", "/"),
                tickers=", ".join(row.get("matched_tickers") or []).replace("|", "/"),
                missing=", ".join(row.get("missing_fields") or []).replace("|", "/"),
                safe="yes" if row["status"] == "READY_TO_WRITE" else "no",
                block=str(row["block_reason"]).replace("|", "/"),
            )
        )
    if not payload["input_rows"]:
        lines.append("| none | none | none | none | no | No input file was provided. |")
    lines.extend(
        [
            "",
            "## Template",
            "",
            f"- Template rows: {len(payload['template_rows'])}",
            "- Fill `source_name`, `source_url`, and the adapter-specific observed value.",
            "- Rows without a valid http(s) source URL are blocked.",
            "",
            "## Evidence Files Written",
            "",
        ]
    )
    if payload["evidence_files_written"]:
        lines.extend(f"- `{path}`" for path in payload["evidence_files_written"])
    else:
        lines.append("- none")
    lines.extend(["", "## Next Commands", ""])
    lines.extend(f"- `{command}`" for command in payload["next_commands"])
    lines.append("")
    return "\n".join(lines)


def _render_source_next_actions_markdown(payload: dict[str, Any]) -> str:
    metadata = payload["report_metadata"]
    lines = [
        "# Phase 3BB-R2 Next Actions",
        "",
        f"- Generated: {payload['generated_at']}",
        f"- Safety mode: {payload['safety_mode']}",
        f"- Git commit: `{payload['git_commit']}`",
        f"- Database fingerprint: `{payload['database_fingerprint']}`",
        f"- Data watermark: `{payload['data_watermark']}`",
        f"- Taxonomy version: `{payload['taxonomy_version']}`",
        "- Source readiness schema version: "
        f"`{payload['source_readiness_schema_version']}`",
        f"- Command arguments: `{metadata['command_arguments']}`",
        "",
        "## Ranked Actions",
        "",
        (
            "| Priority | Type | Reason | Command | Expected output | Success criteria | "
            "Stop condition | Safe with writer |"
        ),
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for action in payload["next_actions"]:
        lines.append(
            "| {priority} | {kind} | {reason} | {command} | {expected} | {success} | "
            "{stop} | {safe} |".format(
                priority=action["priority"],
                kind=str(action["action_type"]).replace("|", "/"),
                reason=str(action["reason"]).replace("|", "/"),
                command=str(action.get("exact_command") or "n/a").replace("|", "/"),
                expected=str(action["expected_output"]).replace("|", "/"),
                success=str(action["success_criteria"]).replace("|", "/"),
                stop=str(action["stop_condition"]).replace("|", "/"),
                safe="yes" if action["safe_while_another_db_writer_is_active"] else "no",
            )
        )
    lines.extend(
        [
            "",
            "## Safety Confirmation",
            "",
            "- Link writes: blocked",
            "- Feature writes: blocked",
            "- Forecast writes: blocked",
            "- Opportunity writes: blocked",
            "- Paper trade writes: blocked",
            "- Settlement writes: blocked",
            "- Live/demo exchange orders: blocked",
            "",
        ]
    )
    return "\n".join(lines)


def _render_source_availability_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3BB-R2 General Source Availability",
        "",
        f"- Generated: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Evidence dir: `{payload['evidence_dir']}`",
        f"- Recommended next action: {payload['recommended_next_action']}",
        "",
        "## Summary",
        "",
    ]
    for key, value in payload["summary"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Safety Gate", ""])
    for key, value in payload["safety_gate"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Availability Rows",
            "",
            (
                "| Status | Adapter | Target publication | Required value | "
                "Observed value | Remote check | Affected rows | Block reason |"
            ),
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in payload["availability_rows"]:
        remote_check = row.get("remote_check") or {}
        lines.append(
            (
                "| {status} | {adapter} | {publication} | {field} | "
                "{value} | {remote} | {affected} | {block} |"
            ).format(
                status=str(row["availability_status"]).replace("|", "/"),
                adapter=str(row["source_adapter_key"]).replace("|", "/"),
                publication=str(row["target_publication"]).replace("|", "/"),
                field=str(row["required_value_field"]).replace("|", "/"),
                value=str(row.get("observed_value")).replace("|", "/"),
                remote=str(remote_check.get("status")).replace("|", "/"),
                affected=row.get("affected_diagnostic_rows"),
                block=str(row["block_reason"]).replace("|", "/"),
            )
        )
    if not payload["availability_rows"]:
        lines.append("| none | none | none | none | none | none | 0 | No rows. |")
    lines.extend(
        [
            "",
            "## Watch Targets",
            "",
            "| Adapter | Watch target | Source URL | Remote terms matched |",
            "| --- | --- | --- | --- |",
        ]
    )
    for row in payload["availability_rows"]:
        remote_check = row.get("remote_check") or {}
        lines.append(
            "| {adapter} | {target} | {url} | {terms} |".format(
                adapter=str(row["source_adapter_key"]).replace("|", "/"),
                target=str(row["watch_target"]).replace("|", "/"),
                url=str(row.get("source_url") or "").replace("|", "/"),
                terms=", ".join(remote_check.get("matched_watch_terms") or []).replace(
                    "|",
                    "/",
                ),
            )
        )
    lines.extend(["", "## Next Commands", ""])
    lines.extend(f"- `{command}`" for command in payload["next_commands"])
    lines.append("")
    return "\n".join(lines)


def _render_source_evidence_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3BB-R2 General Source Evidence",
        "",
        f"- Generated: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Evidence dir: `{payload['evidence_dir']}`",
        f"- Recommended next action: {payload['recommended_next_action']}",
        "",
        "## Summary",
        "",
    ]
    for key, value in payload["summary"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Safety Gate", ""])
    for key, value in payload["safety_gate"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Source Adapter Templates",
            "",
            "| Adapter | Filename | Required fields |",
            "| --- | --- | --- |",
        ]
    )
    for template in payload["source_adapter_templates"]:
        record = (template.get("records") or [{}])[0]
        lines.append(
            "| {adapter} | `{filename}` | {fields} |".format(
                adapter=template["source_adapter_key"],
                filename=template["filename"],
                fields=", ".join(record.keys()),
            )
        )
    lines.extend(
        [
            "",
            "## Evidence Rows",
            "",
            (
                "| Status | Source adapter | Family | Ticker | Parsed fields | "
                "Source file | Missing fields | Safe | Block reason |"
            ),
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in payload["evidence_rows"]:
        parsed_fields = ", ".join(
            f"{key}={value}" for key, value in row.get("parsed_fields", {}).items()
        )
        lines.append(
            (
                "| {status} | {adapter} | {family} | `{ticker}` | {fields} | "
                "`{source_file}` | {missing} | {safe} | {block} |"
            ).format(
                status=str(row["evidence_status"]).replace("|", "/"),
                adapter=str(row["source_adapter_key"]).replace("|", "/"),
                family=str(row["family_key"]).replace("|", "/"),
                ticker=row["ticker"],
                fields=parsed_fields.replace("|", "/"),
                source_file=str(row["source_file"]).replace("|", "/"),
                missing=", ".join(row.get("missing_evidence_fields") or []),
                safe="yes" if row["safe_to_forecast"] else "no",
                block=str(row["block_reason"]).replace("|", "/"),
            )
        )
    if not payload["evidence_rows"]:
        lines.append("| none | none |  |  | none |  | none | no | No evidence rows. |")
    lines.extend(["", "## Next Commands", ""])
    lines.extend(f"- `{command}`" for command in payload["next_commands"])
    lines.append("")
    return "\n".join(lines)


def _render_r3_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3BB-R3 General Reclassification",
        "",
        f"- Generated: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Recommended next action: {payload['recommended_next_action']}",
        "",
        "## Summary",
        "",
    ]
    for key, value in payload["summary"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Safety Gate",
            "",
        ]
    )
    for key, value in payload["safety_gate"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Reclassification Candidate Sample",
            "",
            "| Proposed | Reasons | Family | Ticker | Legs | "
            "Safe apply | Safe reparse | Preview block |",
            "| --- | --- | --- | --- | ---: | --- | --- | --- |",
        ]
    )
    for row in payload["reclassification_candidates"]:
        preview = row.get("parser_preview") or {}
        block = ", ".join(preview.get("block_reasons") or [])
        lines.append(
            (
                "| {category} | {reasons} | {family} | `{ticker}` | {legs} | "
                "{safe_apply} | {safe_reparse} | {block} |"
            ).format(
                category=row["proposed_category"],
                reasons=", ".join(row.get("leakage_reasons") or []),
                family=str(row["family_key"]).replace("|", "/"),
                ticker=row["ticker"],
                legs=row["leg_count"],
                safe_apply="yes" if row["safe_to_apply"] else "no",
                safe_reparse="yes" if row.get("safe_to_reparse") else "no",
                block=(block or "none").replace("|", "/"),
            )
        )
    if not payload["reclassification_candidates"]:
        lines.append("| none | none |  |  | 0 | no | no | none |")
    lines.extend(
        [
            "",
            "## Manual Review Rows",
            "",
            "| Family | Ticker | Legs | Block reason |",
            "| --- | --- | ---: | --- |",
        ]
    )
    for row in payload["manual_review_rows"]:
        lines.append(
            "| {family} | `{ticker}` | {legs} | {block} |".format(
                family=str(row["family_key"]).replace("|", "/"),
                ticker=row["ticker"],
                legs=row["leg_count"],
                block=str(row["block_reason"]).replace("|", "/"),
            )
        )
    if not payload["manual_review_rows"]:
        lines.append("| none |  | 0 | No manual review rows. |")
    lines.extend(["", "## Next Commands", ""])
    lines.extend(f"- `{command}`" for command in payload["next_commands"])
    lines.append("")
    return "\n".join(lines)


def _render_r3_safe_parser_reparse_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3BB-R3 Safe Parser Reparse",
        "",
        f"- Generated: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Status: {payload['status']}",
        "",
        "## Summary",
        "",
    ]
    for key, value in payload["summary"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Safety Gate", ""])
    for key, value in payload["safety_gate"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Reparsed Tickers",
            "",
            "| Ticker |",
            "| --- |",
        ]
    )
    for ticker in payload["tickers_to_reparse"]:
        lines.append(f"| `{ticker}` |")
    if not payload["tickers_to_reparse"]:
        lines.append("| none |")
    if payload.get("missing_tickers"):
        lines.extend(["", "## Missing Tickers", ""])
        lines.extend(f"- `{ticker}`" for ticker in payload["missing_tickers"])
    lines.extend(["", "## Next Commands", ""])
    lines.extend(f"- `{command}`" for command in payload["next_commands"])
    lines.append("")
    return "\n".join(lines)


def _render_r3_exact_sports_link_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3BB-R3 Exact Sports Link",
        "",
        f"- Generated: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Status: {payload['status']}",
        "",
        "## Summary",
        "",
    ]
    for key, value in payload["summary"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Safety Gate", ""])
    for key, value in payload["safety_gate"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Preview Rows",
            "",
            "| Ticker | Type | Safe | Block reason |",
            "| --- | --- | --- | --- |",
        ]
    )
    for row in payload["preview_rows"]:
        block = ", ".join(row.get("block_reasons") or []) or "none"
        lines.append(
            "| `{ticker}` | {market_type} | {safe} | {block} |".format(
                ticker=row["ticker"],
                market_type=row["market_type"],
                safe="yes" if row["safe_to_link"] else "no",
                block=block.replace("|", "/"),
            )
        )
    if not payload["preview_rows"]:
        lines.append("| none | none | no | no unlinked exact sports rows |")
    lines.extend(["", "## Next Commands", ""])
    lines.extend(f"- `{command}`" for command in payload["next_commands"])
    lines.append("")
    return "\n".join(lines)


def _render_r3_composite_preview_gate_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3BB-R3 Composite Preview Gate",
        "",
        f"- Generated: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Status: {payload['status']}",
        f"- Recommended next action: {payload['recommended_next_action']}",
        "",
        "## Summary",
        "",
    ]
    for key, value in payload["summary"].items():
        if key == "category_counts":
            continue
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Safety Gate", ""])
    for key, value in payload["safety_gate"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Category Counts",
            "",
            "| Category | Unsupported markets | Unsupported legs |",
            "| --- | ---: | ---: |",
        ]
    )
    for category, row in payload["summary"]["category_counts"].items():
        lines.append(f"| {category} | {row['markets']} | {row['legs']} |")
    lines.extend(
        [
            "",
            "## Composite Rows",
            "",
            "| Classification | Categories | Components | Verified | Ticker | Next action |",
            "| --- | --- | ---: | ---: | --- | --- |",
        ]
    )
    for row in payload["rows"][:100]:
        lines.append(
            (
                "| {classification} | {categories} | {components} | {verified} | "
                "`{ticker}` | {next_action} |"
            ).format(
                classification=str(row["classification"]).replace("|", "/"),
                categories=", ".join(row.get("unsupported_categories") or []).replace("|", "/"),
                components=row["component_count"],
                verified=row["verified_component_count"],
                ticker=row["ticker"],
                next_action=str(row["next_action"]).replace("|", "/"),
            )
        )
    if not payload["rows"]:
        lines.append("| none | none | 0 | 0 |  | No unsupported composites in preview. |")
    lines.extend(["", "## Next Commands", ""])
    lines.extend(f"- `{command}`" for command in payload["next_commands"])
    lines.append("")
    return "\n".join(lines)


def _render_r3_composite_operator_preflight_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3BB-R3 Composite Operator Preflight",
        "",
        f"- Generated: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Status: {payload['status']}",
        f"- Preview source: {payload['preview_source_path']}",
        f"- Recommended next action: {payload['recommended_next_action']}",
        "",
        "## Summary",
        "",
    ]
    for key, value in payload["summary"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Safety Gate", ""])
    for key, value in payload["safety_gate"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Rows",
            "",
            "| Ready | Blockers | Components | Ticker | Quote age seconds | "
            "Liquidity | Next action |",
            "| --- | --- | ---: | --- | ---: | ---: | --- |",
        ]
    )
    for row in payload["rows"][:100]:
        snapshot = row.get("composite_snapshot") or {}
        blockers = ", ".join(row.get("block_reasons") or []) or "none"
        lines.append(
            (
                "| {ready} | {blockers} | {components} | `{ticker}` | {age} | "
                "{liquidity} | {next_action} |"
            ).format(
                ready="yes" if row.get("paper_composite_review_ready") else "no",
                blockers=blockers.replace("|", "/"),
                components=row.get("component_count"),
                ticker=row.get("ticker"),
                age=snapshot.get("age_seconds"),
                liquidity=snapshot.get("liquidity_dollars"),
                next_action=str(row.get("next_action") or "").replace("|", "/"),
            )
        )
    if not payload["rows"]:
        lines.append("| no | none | 0 |  |  |  | No rows reviewed. |")
    lines.extend(["", "## Next Commands", ""])
    lines.extend(f"- `{command}`" for command in payload["next_commands"])
    lines.append("")
    return "\n".join(lines)


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
