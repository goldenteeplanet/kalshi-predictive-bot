"""Metadata-only source inventory and honest tournament evidence status."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from kalshi_predictor.data_sources.contracts import (
    CapturedSourceEvidence,
    CaptureOutcome,
    ClockBasis,
    ConfigurationSource,
    SourceProvenance,
    assess_source,
)
from kalshi_predictor.data_sources.registry import PROVIDERS, credential_metadata
from kalshi_predictor.overnight_paper.dashboard import snapshot as paper_snapshot
from kalshi_predictor.ui.provider_research import _read
from kalshi_predictor.ui.provider_research import snapshot as capture_snapshot

_CAPTURE_NAMES = {
    "BLS": "bls",
    "FRED": "fred",
    "SYNOPTIC": "synoptic",
    "UNUSUAL_WHALES": "unusual-whales",
    "ODDPOOL": "oddpool",
}


def source_snapshot(
    report_directory: Path | None = None,
    capture_directory: Path | None = None,
    paper_database_path: Path | None = None,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    # Same read-only verifier as /paper-live. Never decode ledger truth here.
    paper = paper_snapshot(paper_database_path)
    weather = {
        key: paper.get(key)
        for key in (
            "weather_evidence_at",
            "weather_evidence_kind",
            "weather_evidence_ticker",
            "weather_evidence_state",
            "weather_source_state",
            "weather_provider_updated_at",
            "weather_provider_generated_at",
            "weather_last_attempt_blockers",
            "weather_preparation_state",
            "weather_actual_forecast_count",
            "last_capture_at",
            "capture_state",
        )
    }
    weather["verification_error"] = (
        "PAPER_DASHBOARD_EVIDENCE_INVALID"
        if paper.get("first_blocker") == "PAPER_DASHBOARD_EVIDENCE_INVALID"
        else None
    )
    weather["scope"] = "LATEST_SAME_LEDGER_WEATHER_ATTEMPT_NOT_SERVICE_STATUS"
    directory = (
        report_directory or Path(__file__).resolve().parents[3] / "reports" / "api_tournament"
    )
    aliases: list[str] = []
    inventory_status = "UNAVAILABLE"
    try:
        root = directory.resolve(strict=True)
        inventory = json.loads(_read(root / "credential_readiness.json", root, 65_536))
        if not isinstance(inventory, list) or len(inventory) > 100:
            raise ValueError
        for row in inventory:
            if row.get("configured") is True:
                names = row.get("aliases")
                if not isinstance(names, list) or any(
                    not isinstance(s, str) or len(s) > 100 for s in names
                ):
                    raise ValueError
                aliases.extend(names)
        inventory_status = "RECORDED_METADATA"
    except (OSError, ValueError, TypeError, AttributeError):
        aliases = []
    captures = {row["provider"]: row for row in capture_snapshot(capture_directory)["providers"]}
    rows = []
    for name, definition in PROVIDERS.items():
        credential = credential_metadata(
            name,
            available_aliases=aliases,
            configuration_source=ConfigurationSource.LOCAL_SECRET_FILE,
        )
        capture = captures.get(_CAPTURE_NAMES.get(name, ""))
        evidence = None
        if capture and capture["state"] == "RECORDED_RESEARCH":
            timestamps = [
                sample["observed_at"] for sample in capture["samples"] if sample.get("observed_at")
            ]
            provider_at = max(timestamps) if timestamps else capture.get("provider_timestamp")
            evidence = CapturedSourceEvidence(
                name,
                CaptureOutcome.SUCCESS,
                datetime.fromisoformat(provider_at) if provider_at else None,
                datetime.fromisoformat(capture["received_at"]),
                SourceProvenance(
                    name, capture["source_sha256"], _CAPTURE_NAMES[name] + ".research"
                ),
                clock_basis=ClockBasis.PROVIDER_TIMESTAMP if provider_at else ClockBasis.UNKNOWN,
                http_status=200,
                schema_validated=True,
                record_count=capture["record_count"],
            )
        elif capture and capture["state"] == "PROVIDER_ERROR":
            code = capture["error_code"]
            outcome = (
                CaptureOutcome.AUTH_FAILURE
                if any(s in code for s in ("AUTHENTICATION", "ACCESS_DENIED"))
                else CaptureOutcome.RATE_LIMITED
                if any(s in code for s in ("RATE_", "QUOTA_"))
                else CaptureOutcome.API_FAILURE
            )
            evidence = CapturedSourceEvidence(name, outcome, None, None, None)
        health = assess_source(definition, credential, evidence, now=now)
        rows.append(
            {
                **asdict(health),
                "credential_alias": credential.alias_used,
                "credential_notice": credential.notice,
                "capture_status": capture["state"] if capture else "NO_CAPTURE_IN_THIS_VIEW",
                "forecast_count": 0,
                "independent_evaluated_events": 0,
                "brier_delta": None,
                "log_loss_delta": None,
                "ece_delta": None,
                "shadow_pnl_delta": None,
                "paper_pnl_delta": None,
                "monthly_cost": None,
                "api_value_score": None,
                "recommendation": "RIGHTS_PENDING"
                if definition.rights_corpus
                else "NOT_ENOUGH_DATA",
                "payment_recommendation": "DO_NOT_PAY_YET",
            }
        )
        if name == "NWS":
            rows[-1]["weather_attempt"] = weather
            rows[-1]["forecast_count"] = weather["weather_actual_forecast_count"]
            if weather["weather_evidence_at"] is not None:
                rows[-1]["capture_status"] = weather["weather_evidence_state"]
                if weather["weather_source_state"] == "STALE":
                    rows[-1]["health_status"] = "STALE"
                    rows[-1]["reason"] = "VERIFIED_WEATHER_PROVIDER_CLOCK_STALE"
    return {
        "viewed_at": now.isoformat(),
        "inventory_status": inventory_status,
        "scope": "NEW_PROVIDER_RESEARCH_CAPTURES",
        "research_only": True,
        "evaluation_status": "NO_PAIRED_REAL_EVENT_EVIDENCE",
        "providers": rows,
        "weather_attempt": weather,
    }


def render_sources(payload: dict[str, Any]) -> str:
    weather = payload["weather_attempt"]
    labels = (
        ("Recorded attempt", "weather_evidence_at"),
        ("Ticker", "weather_evidence_ticker"),
        ("Preparation state", "weather_preparation_state"),
        ("Provider clock state", "weather_source_state"),
        ("NWS updateTime", "weather_provider_updated_at"),
        ("NWS generatedAt", "weather_provider_generated_at"),
        ("Latest receipt", "last_capture_at"),
        ("Attempt recency", "weather_evidence_state"),
        ("Actual forecasts in this attempt", "weather_actual_forecast_count"),
        ("Verification error", "verification_error"),
    )
    weather_html = (
        "<section><h2>Latest same-ledger weather attempt</h2><dl>"
        + "".join(
            "<dt>"
            + escape(label)
            + "</dt><dd>"
            + escape(str(weather[key]) if weather[key] is not None else "Unknown")
            + "</dd>"
            for label, key in labels
        )
        + "</dl><p>Exact refusal reasons: "
        + escape(", ".join(weather["weather_last_attempt_blockers"] or []) or "None recorded")
        + "</p><p>This is a recorded attempt, not evidence of a running service. "
        "A refusal produces no source-value metrics or paper readiness.</p></section>"
    )
    rows = []
    for item in payload["providers"]:
        cells = [
            item["provider_name"],
            item["credential_status"],
            item["health_status"],
            item["reason"],
            item["capture_status"],
            ", ".join(item["supported_market_families"]),
            str(item["independent_evaluated_events"]),
            "Not measured",
            "Unknown",
            item["recommendation"],
        ]
        rows.append(
            "<tr>" + "".join("<td>" + escape(str(cell)) + "</td>" for cell in cells) + "</tr>"
        )
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Data sources</title><link rel='stylesheet' href='/static/styles.css'>"
        "</head><body><main><h1>Data sources</h1>"
        "<p>Local paper research. No real money. This view covers new provider captures; "
        "it is not a running-service status.</p>"
        "<p>Successful access does not establish freshness, settlement authority, or predictive "
        "value. Supported families describe research scope, not unlocked markets.</p>"
        "<p>No paired real-event evaluations are recorded for these integrations. Model "
        "improvement, cost and value scores remain unknown. Do not pay or upgrade yet.</p>"
        "<p><a href='/research/providers'>View captured samples and timestamps</a> · "
        "<a href='/paper-live'>Paper readiness</a></p>"
        + weather_html
        + "<table><thead><tr><th>Provider</th><th>Credential</th><th>Health</th>"
        "<th>Exact reason</th><th>Capture</th><th>Research families</th>"
        "<th>Independent N</th><th>Improvement</th><th>Cost</th><th>Recommendation</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></main></body></html>"
    )


def create_router(
    report_directory: Path | None = None, capture_directory: Path | None = None
) -> APIRouter:
    router = APIRouter()

    def current() -> dict[str, Any]:
        raw = os.environ.get("OVERNIGHT_PAPER_DB")
        return source_snapshot(report_directory, capture_directory, Path(raw) if raw else None)

    @router.get("/api/data-sources")
    def api() -> dict[str, Any]:
        return current()

    @router.get("/data-sources", response_class=HTMLResponse)
    def page() -> str:
        return render_sources(current())

    return router
