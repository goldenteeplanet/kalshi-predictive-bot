from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text

from kalshi_predictor.config import Settings
from kalshi_predictor.data.backend import database_url_from_settings
from kalshi_predictor.data.db import get_session_factory, make_sqlite_read_only_engine
from kalshi_predictor.kalshi.client import KalshiClient
from kalshi_predictor.utils.time import utc_now
from kalshi_predictor.weather_identity_evidence import (
    BoundedProtocolCache,
    collect_weather_identity_evidence,
)

WEATHER_IDENTITY_FIELDS = (
    "authoritative_identity_verified",
    "evidence_class",
    "source_identity",
    "source_sha256",
    "fetched_at",
    "freshness_status",
)


def append_weather_identity_shadow(
    *,
    report_path: Path,
    markdown_path: Path,
    settings: Settings,
    writer_monitor: Callable[[], dict[str, Any]],
    collector: Callable[[list[str]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Append shadow identity evidence after GH-2 releases its writer lock."""
    report = _read_json(report_path)
    weather_gate = report.get("weather_gate") or {}
    weather_rows = list(weather_gate.get("weather_rows") or [])
    tickers = _unique_tickers(weather_rows, limit=6)
    monitor = writer_monitor()
    if int(monitor.get("writer_count") or 0) != 0 or not bool(
        monitor.get("safe_to_start_write", False)
    ):
        shadow = _deferred_payload(tickers, "DEFERRED_ACTIVE_WRITER", monitor)
    elif not tickers:
        shadow = _deferred_payload(tickers, "NO_WEATHER_DIAGNOSTIC_ROWS", monitor)
    elif collector is not None:
        shadow = {**collector(tickers), "writer_monitor_before_collection": monitor}
    else:
        shadow = _collect_read_only(tickers, settings=settings, monitor=monitor)

    original_authority = {
        "candidate_alignment": report.get("candidate_alignment"),
        "paper_readiness": report.get("paper_readiness"),
        "weather_status": weather_gate.get("status"),
        "weather_next_action": weather_gate.get("next_action"),
        "weather_blockers": [row.get("first_blocker") for row in weather_rows],
        "weather_paper_ready": [row.get("paper_ready") for row in weather_rows],
    }
    report["weather_gate"] = _enrich_weather_gate(weather_gate, shadow)
    assert original_authority == {
        "candidate_alignment": report.get("candidate_alignment"),
        "paper_readiness": report.get("paper_readiness"),
        "weather_status": report["weather_gate"].get("status"),
        "weather_next_action": report["weather_gate"].get("next_action"),
        "weather_blockers": [
            row.get("first_blocker") for row in report["weather_gate"]["weather_rows"]
        ],
        "weather_paper_ready": [
            row.get("paper_ready") for row in report["weather_gate"]["weather_rows"]
        ],
    }
    _write_json_atomic(report_path, report)
    _append_markdown_summary(markdown_path, shadow)
    return shadow


def _collect_read_only(
    tickers: list[str],
    *,
    settings: Settings,
    monitor: dict[str, Any],
) -> dict[str, Any]:
    max_age = timedelta(minutes=15)
    cache = BoundedProtocolCache(max_entries=max(3, len(tickers) * 3), max_age=max_age)
    engine = make_sqlite_read_only_engine(database_url_from_settings(settings))
    session_factory = get_session_factory(engine)
    try:
        with KalshiClient(settings=settings) as client, session_factory() as session:
            query_only = int(session.execute(text("PRAGMA query_only")).scalar_one())
            connection = session.connection().connection.driver_connection
            if connection is None:
                raise RuntimeError("READ_ONLY_SQLITE_CONNECTION_UNAVAILABLE")
            changes_before = int(connection.total_changes)
            payload = collect_weather_identity_evidence(
                session,
                client,
                tickers=tickers,
                deadline_monotonic=time.monotonic() + 30,
                max_age=max_age,
                cache=cache,
            )
            changes_after = int(connection.total_changes)
        payload["database_census"] = {
            "open_mode": "mode=ro",
            "query_only": query_only,
            "total_changes_before": changes_before,
            "total_changes_after": changes_after,
            "database_writes": changes_after - changes_before,
        }
        payload["writer_monitor_before_collection"] = monitor
        return payload
    finally:
        engine.dispose()


def _deferred_payload(
    tickers: list[str], reason: str, monitor: dict[str, Any]
) -> dict[str, Any]:
    return {
        "generated_at": utc_now().isoformat(),
        "mode": "SHADOW_ONLY_AUTHORITATIVE_WEATHER_IDENTITY",
        "status": "DEFERRED",
        "tickers": tickers,
        "rows": [
            {
                "ticker": ticker,
                "authoritative_identity_verified": False,
                "evidence_class": None,
                "source_identity": None,
                "source_sha256": None,
                "fetched_at": None,
                "freshness_status": "NOT_VERIFIED",
                "reason": reason,
            }
            for ticker in tickers
        ],
        "summary": {
            "requested": len(tickers),
            "authoritative_identity_verified": 0,
            "blocked": len(tickers),
            "reasons": {reason: len(tickers)} if tickers else {},
        },
        "writer_monitor_before_collection": monitor,
        "safety": {"diagnostic_only": True, "database_opened": False, "database_writes": 0},
    }


def _enrich_weather_gate(
    weather_gate: dict[str, Any], shadow: dict[str, Any]
) -> dict[str, Any]:
    evidence = {row["ticker"]: row for row in shadow.get("rows") or []}
    rows = []
    for source in weather_gate.get("weather_rows") or []:
        row = dict(source)
        identity = evidence.get(row.get("ticker"), {})
        for field in WEATHER_IDENTITY_FIELDS:
            row[field] = identity.get(field)
        row["authoritative_identity_reason"] = identity.get("reason", "NOT_COLLECTED")
        rows.append(row)
    summary = dict(weather_gate.get("summary") or {})
    summary["authoritative_identity_shadow"] = dict(shadow.get("summary") or {})
    summary["authoritative_identity_shadow_status"] = shadow.get("status", "COMPLETE")
    return {**weather_gate, "summary": summary, "weather_rows": rows}


def _append_markdown_summary(path: Path, shadow: dict[str, Any]) -> None:
    start = "<!-- weather-identity-shadow:start -->"
    end = "<!-- weather-identity-shadow:end -->"
    summary = shadow.get("summary") or {}
    block = "\n".join(
        [
            start,
            "## Authoritative Weather Identity (Shadow Only)",
            "",
            f"- Status: `{shadow.get('status', 'COMPLETE')}`",
            f"- Requested: `{summary.get('requested', 0)}`",
            f"- Verified: `{summary.get('authoritative_identity_verified', 0)}`",
            f"- Blocked: `{summary.get('blocked', 0)}`",
            "- Candidate selection and paper readiness: `UNCHANGED`",
            end,
        ]
    )
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if start in existing and end in existing:
        prefix, remainder = existing.split(start, 1)
        _, suffix = remainder.split(end, 1)
        rendered = prefix.rstrip() + "\n\n" + block + suffix
    else:
        rendered = existing.rstrip() + "\n\n" + block + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(path)


def _unique_tickers(rows: list[dict[str, Any]], *, limit: int) -> list[str]:
    result: list[str] = []
    for row in rows:
        ticker = row.get("ticker")
        if not isinstance(ticker, str) or not ticker or ticker in result:
            continue
        result.append(ticker)
        if len(result) >= limit:
            break
    return result


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("GH-2 report must be a JSON object")
    return payload


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
