from __future__ import annotations

import hashlib
import json
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.active_universe import is_inactive_market_status
from kalshi_predictor.data.repositories import decode_json, encode_json
from kalshi_predictor.data.schema import Market
from kalshi_predictor.utils.time import utc_now

WriterMonitor = Callable[[], dict[str, Any]]


@dataclass(frozen=True)
class LineageRepairArtifacts:
    json_path: Path
    markdown_path: Path
    rollback_path: Path


def load_accepted_lineage_plan(
    path: Path,
    *,
    expected_sha256: str,
) -> tuple[dict[str, Any], str]:
    if not path.is_file():
        raise RuntimeError(f"Accepted lineage plan is missing: {path}")
    raw = path.read_bytes()
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    expected = expected_sha256.strip().lower()
    if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
        raise RuntimeError("Accepted lineage plan SHA-256 must be 64 lowercase hex characters.")
    if actual_sha256 != expected:
        raise RuntimeError("Accepted lineage plan artifact SHA-256 mismatch.")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Accepted lineage plan is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Accepted lineage plan must be a JSON object.")
    if payload.get("dry_run") is not True or int(payload.get("database_writes") or 0) != 0:
        raise RuntimeError("Accepted lineage plan must be a zero-write dry-run artifact.")
    return payload, actual_sha256


def validate_accepted_lineage_plan(
    *,
    plan: dict[str, Any],
    accepted_plan: dict[str, Any],
) -> None:
    accepted_tickers = accepted_plan.get("tickers")
    current_tickers = plan.get("tickers")
    if not isinstance(accepted_tickers, list) or accepted_tickers != current_tickers:
        raise RuntimeError("Accepted lineage plan ticker scope or order drifted.")
    accepted_rows = accepted_plan.get("rows")
    current_rows = plan.get("rows")
    if not isinstance(accepted_rows, list) or not isinstance(current_rows, list):
        raise RuntimeError("Accepted lineage plan rows are missing.")
    if len(accepted_rows) != len(current_rows) or len(current_rows) != len(current_tickers):
        raise RuntimeError("Accepted lineage plan row scope drifted.")
    for label, rows in (("accepted", accepted_rows), ("current", current_rows)):
        invalid = [row for row in rows if not isinstance(row, dict) or row.get("action") != "APPLY"]
        if invalid:
            raise RuntimeError(
                f"{label.capitalize()} lineage plan contains blocked or non-APPLY rows."
            )
    fields = ("ticker", "action", "reason", "before", "source", "source_sha256")
    for index, (accepted, current) in enumerate(zip(accepted_rows, current_rows, strict=True)):
        for field in fields:
            if accepted.get(field) != current.get(field):
                ticker = current.get("ticker") or accepted.get("ticker") or f"row {index}"
                raise RuntimeError(f"Accepted lineage plan drift for {ticker}: {field}.")


def fetch_exact_catalog_lineage(
    client: Any,
    *,
    tickers: list[str],
    deadline_monotonic: float,
) -> dict[str, list[dict[str, Any]]]:
    if time.monotonic() >= deadline_monotonic:
        raise TimeoutError("Catalog lineage deadline expired before fetch.")
    page = client.get_markets(status=None, limit=len(tickers), tickers=tickers)
    if time.monotonic() > deadline_monotonic:
        raise TimeoutError("Catalog lineage fetch exceeded its deadline.")
    requested = set(tickers)
    evidence: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in page.get("markets") or []:
        if not isinstance(raw, Mapping):
            continue
        ticker = str(raw.get("ticker") or "").strip()
        if ticker in requested:
            evidence[ticker].append(dict(raw))
    _enrich_from_exact_events(
        client,
        evidence=evidence,
        deadline_monotonic=deadline_monotonic,
    )
    return dict(evidence)


def _enrich_from_exact_events(
    client: Any,
    *,
    evidence: dict[str, list[dict[str, Any]]],
    deadline_monotonic: float,
) -> None:
    events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rows in evidence.values():
        for row in rows:
            if _clean(row.get("series_ticker")) is not None:
                continue
            event_ticker = _clean(row.get("event_ticker"))
            if event_ticker is None:
                row["_lineage_blocker"] = "CATALOG_EVENT_MISSING"
                continue
            events[event_ticker].append(row)

    for event_ticker, rows in events.items():
        _require_deadline(deadline_monotonic)
        event_payload = client.get_event(event_ticker)
        event = event_payload.get("event") if isinstance(event_payload, Mapping) else None
        if not isinstance(event, Mapping) or _clean(event.get("event_ticker")) != event_ticker:
            _block_rows(rows, "EVENT_EVIDENCE_MISMATCH")
            continue
        series_ticker = _clean(event.get("series_ticker"))
        if series_ticker is None:
            _block_rows(rows, "EVENT_SERIES_MISSING")
            continue
        _require_deadline(deadline_monotonic)
        series_payload = client.get_series_by_ticker(series_ticker)
        series = series_payload.get("series") if isinstance(series_payload, Mapping) else None
        if not isinstance(series, Mapping) or _clean(series.get("ticker")) != series_ticker:
            _block_rows(rows, "SERIES_EVIDENCE_MISMATCH")
            continue
        for row in rows:
            row["series_ticker"] = series_ticker
            row["_lineage_evidence"] = "EXACT_EVENT_AND_SERIES_CATALOG"


def build_lineage_repair_plan(
    session: Session,
    *,
    tickers: list[str],
    catalog_evidence: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for ticker in tickers:
        market = session.get(Market, ticker)
        source_rows = catalog_evidence.get(ticker) or []
        row = _plan_row(market, ticker=ticker, source_rows=source_rows)
        rows.append(row)
    return {
        "generated_at": utc_now().isoformat(),
        "mode": "CATALOG_LINEAGE_REPAIR_PLAN",
        "tickers": tickers,
        "summary": {
            "requested": len(tickers),
            "applicable": sum(row["action"] == "APPLY" for row in rows),
            "unchanged": sum(row["action"] == "UNCHANGED" for row in rows),
            "blocked": sum(row["action"] == "BLOCKED" for row in rows),
            "blocked_reasons": dict(
                sorted(Counter(row["reason"] for row in rows if row["action"] == "BLOCKED").items())
            ),
        },
        "rows": rows,
        "safety": {
            "source": "EXACT_KALSHI_CATALOG",
            "ticker_derivation": False,
            "event_fallback": False,
            "historical_rankings_rewritten": False,
            "candidate_selection_changed": False,
            "orders_created": 0,
        },
    }


def apply_lineage_repair(
    session: Session,
    *,
    plan: dict[str, Any],
    accepted_plan: dict[str, Any],
    writer_monitor: WriterMonitor,
) -> dict[str, Any]:
    validate_accepted_lineage_plan(plan=plan, accepted_plan=accepted_plan)
    writer = writer_monitor()
    if int(writer.get("writer_count") or 0) != 0 or not bool(writer.get("safe_to_start_write")):
        raise RuntimeError("Writer gate did not clear catalog lineage repair.")
    applied = 0
    for row in plan.get("rows") or []:
        if row.get("action") != "APPLY":
            continue
        market = session.get(Market, str(row["ticker"]))
        if market is None:
            raise RuntimeError(f"Market disappeared during repair: {row['ticker']}")
        before = row["before"]
        if (
            market.event_ticker != before["event_ticker"]
            or market.series_ticker != before["series_ticker"]
            or market.raw_json != before["raw_json"]
        ):
            raise RuntimeError(f"Market changed after planning: {row['ticker']}")
        source = row["source"]
        if market.event_ticker is None and source["event_ticker"] is not None:
            market.event_ticker = source["event_ticker"]
        if market.series_ticker is None:
            market.series_ticker = source["series_ticker"]
        raw = decode_json(market.raw_json)
        raw.update(
            {
                "event_ticker": market.event_ticker,
                "series_ticker": market.series_ticker,
            }
        )
        market.raw_json = encode_json(raw)
        applied += 1
    return {"applied": applied, "writer_gate": writer}


def write_lineage_repair_artifacts(
    *,
    plan: dict[str, Any],
    output_dir: Path,
    dry_run: bool,
    apply_result: dict[str, Any] | None = None,
) -> LineageRepairArtifacts:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        **plan,
        "dry_run": dry_run,
        "database_writes": 0 if dry_run else int((apply_result or {}).get("applied") or 0),
        "apply_result": apply_result,
    }
    rollback = {
        "generated_at": utc_now().isoformat(),
        "mode": "ROLLBACK_EVIDENCE_ONLY",
        "rows": [
            {
                "ticker": row["ticker"],
                "restore": row["before"],
                "source_sha256": row.get("source_sha256"),
            }
            for row in plan.get("rows") or []
            if row.get("action") == "APPLY"
        ],
    }
    json_path = output_dir / "series_lineage_repair.json"
    markdown_path = output_dir / "series_lineage_repair.md"
    rollback_path = output_dir / "series_lineage_rollback.json"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    rollback_path.write_text(json.dumps(rollback, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return LineageRepairArtifacts(json_path, markdown_path, rollback_path)


def normalize_tickers(raw: str, *, limit: int) -> list[str]:
    tickers = list(dict.fromkeys(part.strip().upper() for part in raw.split(",") if part.strip()))
    if limit < 1:
        raise ValueError("limit must be positive")
    if not tickers:
        raise ValueError("at least one explicit ticker is required")
    if len(tickers) > limit:
        raise ValueError(f"explicit ticker count exceeds limit {limit}")
    return tickers


def _plan_row(
    market: Market | None,
    *,
    ticker: str,
    source_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    if market is None:
        return _blocked(ticker, "MARKET_NOT_FOUND")
    if is_inactive_market_status(market.status):
        return _blocked(ticker, "MARKET_NOT_ACTIVE")
    if market.close_time is not None:
        close_time = market.close_time
        if close_time.tzinfo is None:
            close_time = close_time.replace(tzinfo=UTC)
        if close_time <= utc_now():
            return _blocked(ticker, "MARKET_NOT_ACTIVE")
    if len(source_rows) != 1:
        return _blocked(ticker, "CATALOG_EVIDENCE_NOT_UNIQUE")
    source = source_rows[0]
    if source.get("_lineage_blocker"):
        return _blocked(ticker, str(source["_lineage_blocker"]))
    if is_inactive_market_status(source.get("status")):
        return _blocked(ticker, "CATALOG_MARKET_NOT_ACTIVE")
    source_ticker = str(source.get("ticker") or "").strip()
    series = _clean(source.get("series_ticker"))
    event = _clean(source.get("event_ticker"))
    if source_ticker != ticker:
        return _blocked(ticker, "CATALOG_TICKER_MISMATCH")
    if series is None:
        return _blocked(ticker, "CATALOG_SERIES_MISSING")
    if market.series_ticker not in (None, series):
        return _blocked(ticker, "SERIES_CONFLICT")
    if event is not None and market.event_ticker not in (None, event):
        return _blocked(ticker, "EVENT_CONFLICT")
    before = {
        "event_ticker": market.event_ticker,
        "series_ticker": market.series_ticker,
        "raw_json": market.raw_json,
    }
    source_identity = {
        "event_ticker": event,
        "series_ticker": series,
        "lineage_evidence": source.get("_lineage_evidence") or "EXACT_MARKET_CATALOG",
    }
    identity_matches = market.series_ticker == series and (
        event is None or market.event_ticker == event
    )
    action = "UNCHANGED" if identity_matches else "APPLY"
    if market.event_ticker is None and event is not None:
        action = "APPLY"
    return {
        "ticker": ticker,
        "action": action,
        "reason": "ALREADY_MATCHES" if action == "UNCHANGED" else "SOURCE_BACKED_NULL_REPAIR",
        "before": before,
        "source": source_identity,
        "source_sha256": hashlib.sha256(encode_json(source).encode()).hexdigest(),
    }


def _blocked(ticker: str, reason: str) -> dict[str, Any]:
    return {"ticker": ticker, "action": "BLOCKED", "reason": reason}


def _clean(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _require_deadline(deadline_monotonic: float) -> None:
    if time.monotonic() > deadline_monotonic:
        raise TimeoutError("Catalog lineage fetch exceeded its deadline.")


def _block_rows(rows: list[dict[str, Any]], reason: str) -> None:
    for row in rows:
        row["_lineage_blocker"] = reason


def _render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Series Lineage Repair",
        "",
        f"- Dry run: `{payload['dry_run']}`",
        f"- Requested: `{summary['requested']}`",
        f"- Applicable: `{summary['applicable']}`",
        f"- Blocked: `{summary['blocked']}`",
        "- Candidate selection changed: `false`",
        "- Historical rankings rewritten: `false`",
        "",
        "| Ticker | Action | Reason |",
        "| --- | --- | --- |",
    ]
    lines.extend(
        f"| {row['ticker']} | {row['action']} | {row['reason']} |"
        for row in payload.get("rows") or []
    )
    return "\n".join(lines) + "\n"
