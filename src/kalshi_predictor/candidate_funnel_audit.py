from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import MarketRanking
from kalshi_predictor.utils.time import utc_now

AUDIT_VERSION = "candidate_funnel_audit_v1"


@dataclass(frozen=True)
class CandidateFunnelArtifacts:
    json_path: Path
    markdown_path: Path


def write_candidate_funnel_audit(
    session: Session,
    *,
    gh2_report_path: Path,
    crypto_r5_path: Path,
    output_dir: Path = Path("reports/candidate_funnel"),
) -> CandidateFunnelArtifacts:
    gh2 = _read_object(gh2_report_path)
    r5 = _read_object(crypto_r5_path)
    tickers = _candidate_tickers(gh2, r5)
    rankings = latest_ranking_evidence(session, tickers)
    payload = build_candidate_funnel_audit(
        gh2_payload=gh2,
        crypto_r5_payload=r5,
        ranking_evidence=rankings,
        sources={"gh2": str(gh2_report_path), "crypto_r5": str(crypto_r5_path)},
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "candidate_funnel_audit.json"
    markdown_path = output_dir / "candidate_funnel_audit.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return CandidateFunnelArtifacts(json_path=json_path, markdown_path=markdown_path)


def build_candidate_funnel_audit(
    *,
    gh2_payload: dict[str, Any],
    crypto_r5_payload: dict[str, Any],
    ranking_evidence: dict[str, dict[str, Any]],
    sources: dict[str, str] | None = None,
) -> dict[str, Any]:
    crypto_source = _crypto_rows(crypto_r5_payload)
    weather_source = list((gh2_payload.get("weather_gate") or {}).get("weather_rows") or [])
    manifest_scope = {
        str(ticker)
        for ticker in (gh2_payload.get("candidate_alignment") or {}).get("tickers") or []
        if ticker
    }
    if manifest_scope:
        crypto_source = [
            row for row in crypto_source if str(row.get("ticker")) in manifest_scope
        ]
        weather_source = [
            row for row in weather_source if str(row.get("ticker")) in manifest_scope
        ]
    rows = [
        _crypto_candidate(row, ranking_evidence.get(str(row.get("ticker"))) or {})
        for row in crypto_source
        if row.get("ticker")
    ]
    rows.extend(
        _weather_candidate(row, ranking_evidence.get(str(row.get("ticker"))) or {})
        for row in weather_source
        if row.get("ticker")
    )
    rows.sort(key=lambda row: (str(row["category"]), str(row["ticker"])))
    blocker_counts = Counter(str(row["first_blocker"]) for row in rows)
    return {
        "audit_version": AUDIT_VERSION,
        "generated_at": utc_now().isoformat(),
        "mode": "READ_ONLY_ARTIFACT_AND_DATABASE_DIAGNOSTIC",
        "safety": {
            "database_writes": 0,
            "order_apis_imported": False,
            "order_creation": False,
            "gate_thresholds_changed": False,
            "fee_adjusted_ev_is_diagnostic_only": True,
        },
        "sources": sources or {},
        "source_generated_at": {
            "gh2": gh2_payload.get("generated_at"),
            "crypto_r5": crypto_r5_payload.get("generated_at"),
        },
        "summary": {
            "candidate_count": len(rows),
            "manifest_scope_count": len(manifest_scope),
            "crypto_candidates": sum(row["category"] == "crypto" for row in rows),
            "weather_candidates": sum(row["category"] == "weather" for row in rows),
            "paper_ready_candidates": sum(bool(row["paper_ready"]) for row in rows),
            "first_blocker_counts": dict(sorted(blocker_counts.items())),
        },
        "candidates": rows,
    }


def latest_ranking_evidence(
    session: Session, tickers: list[str]
) -> dict[str, dict[str, Any]]:
    if not tickers:
        return {}
    rankings = session.scalars(
        select(MarketRanking)
        .where(MarketRanking.ticker.in_(tickers))
        .order_by(MarketRanking.ticker, desc(MarketRanking.ranked_at), desc(MarketRanking.id))
    ).all()
    evidence: dict[str, dict[str, Any]] = {}
    for ranking in rankings:
        if ranking.ticker in evidence:
            continue
        raw = _decode_object(ranking.raw_json)
        evidence[ranking.ticker] = {
            "ranked_at": ranking.ranked_at.isoformat(),
            "forecast_model": ranking.forecast_model,
            "forecast_probability": ranking.forecast_probability,
            "best_side": ranking.best_side,
            "best_price": ranking.best_price,
            "estimated_edge": ranking.estimated_edge,
            "liquidity": ranking.liquidity,
            "liquidity_score": ranking.liquidity_score,
            "spread": ranking.spread,
            "time_to_close_minutes": ranking.time_to_close_minutes,
            "opportunity_score": ranking.opportunity_score,
            "price_tick_size": raw.get("price_tick_size"),
            "price_tick_valid": raw.get("price_tick_valid"),
            "gross_expected_value": raw.get("gross_expected_value"),
            "estimated_taker_fee": raw.get("estimated_taker_fee"),
            "fee_adjusted_expected_value": raw.get("fee_adjusted_expected_value"),
        }
    return evidence


def _crypto_candidate(source: dict[str, Any], ranking: dict[str, Any]) -> dict[str, Any]:
    source_blocker = str(
        source.get("blocked_reason")
        or source.get("readiness_status")
        or "UNKNOWN"
    )
    blocker = _canonical_crypto_blocker(source_blocker, source, ranking)
    return _candidate(
        category="crypto",
        source=source,
        ranking=ranking,
        first_blocker=blocker,
        source_blocker=source_blocker,
        paper_ready=source_blocker == "PAPER_READY",
        next_condition=_first_text(
            source.get("what_would_make_paper_ready"),
            source.get("what_would_make_tradable"),
        ),
    )


def _weather_candidate(source: dict[str, Any], ranking: dict[str, Any]) -> dict[str, Any]:
    source_blocker = str(source.get("first_blocker") or "UNKNOWN")
    return _candidate(
        category="weather",
        source=source,
        ranking=ranking,
        first_blocker=source_blocker,
        source_blocker=source_blocker,
        paper_ready=bool(source.get("paper_ready")),
        next_condition=_weather_next_condition(source_blocker),
    )


def _candidate(
    *,
    category: str,
    source: dict[str, Any],
    ranking: dict[str, Any],
    first_blocker: str,
    source_blocker: str,
    paper_ready: bool,
    next_condition: str | None,
) -> dict[str, Any]:
    return {
        "ticker": source.get("ticker"),
        "category": category,
        "paper_ready": paper_ready,
        "first_blocker": first_blocker,
        "source_blocker": source_blocker,
        "next_condition": next_condition,
        "model": {
            "forecast_probability": ranking.get("forecast_probability")
            or source.get("side_probability")
            or source.get("forecast_probability"),
            "forecast_model": ranking.get("forecast_model"),
        },
        "price_and_ev": {
            "best_side": ranking.get("best_side") or source.get("best_side"),
            "best_price": ranking.get("best_price") or source.get("best_price"),
            "price_tick_size": ranking.get("price_tick_size"),
            "price_tick_valid": ranking.get("price_tick_valid"),
            "estimated_edge": ranking.get("estimated_edge") or source.get("estimated_edge"),
            "gross_expected_value": ranking.get("gross_expected_value")
            or source.get("expected_value")
            or source.get("raw_ev"),
            "estimated_taker_fee": ranking.get("estimated_taker_fee"),
            "fee_adjusted_expected_value": ranking.get("fee_adjusted_expected_value")
            or source.get("executable_ev"),
            "fee_adjusted_ev_role": "DIAGNOSTIC_ONLY_EXISTING_GATES_UNCHANGED",
        },
        "market_quality": {
            "freshness": source.get("freshness_issue") or _weather_freshness(source),
            "executable_book": source.get("executable_book")
            if "executable_book" in source
            else source.get("book_usable"),
            "book_reason": source.get("book_reason"),
            "liquidity": ranking.get("liquidity") or source.get("liquidity"),
            "liquidity_score": ranking.get("liquidity_score")
            or source.get("liquidity_score"),
            "spread": ranking.get("spread") or source.get("spread"),
            "time_to_close_minutes": ranking.get("time_to_close_minutes"),
            "current_window_eligible": source.get("current_window_eligible"),
            "verified_kalshi_url": source.get("verified_kalshi_url"),
            "settlement_terms_known": source.get("settlement_terms_known"),
        },
        "downstream_gates": {
            "phase3m_nonzero_size": source.get("phase3m_nonzero_size"),
            "phase3n_approved": source.get("phase3n_approved"),
            "risk_eligible": _first_present(
                source, "risk_eligible", "phase3s_proceed"
            ),
        },
        "evidence_timestamps": {
            "ranking": ranking.get("ranked_at") or source.get("latest_ranking_at"),
            "forecast": source.get("latest_forecast_at"),
            "snapshot": source.get("latest_snapshot_at") or source.get("snapshot_captured_at"),
        },
    }


def _canonical_crypto_blocker(
    source_blocker: str, source: dict[str, Any], ranking: dict[str, Any]
) -> str:
    if source_blocker in {"WATCH_NO_POSITIVE_EXPECTED_VALUE", "EV_NOT_POSITIVE"}:
        return "EV_NOT_POSITIVE"
    if source_blocker not in {"", "UNKNOWN", "WATCH_ONLY"}:
        return source_blocker
    if ranking.get("price_tick_valid") is False:
        return "PRICE_TICK_INVALID"
    if source.get("book_usable") is False:
        return "NO_EXECUTABLE_BOOK"
    return "UNKNOWN"


def _crypto_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    keys = (
        "blocked_active_pure_examples",
        "best_ev_candidates",
        "positive_ev_preflight_candidates",
    )
    for key in keys:
        for row in payload.get(key) or []:
            ticker = str(row.get("ticker") or "")
            if not ticker:
                continue
            rows[ticker] = {**rows.get(ticker, {}), **row}
    return list(rows.values())


def _candidate_tickers(gh2: dict[str, Any], r5: dict[str, Any]) -> list[str]:
    tickers = list((gh2.get("candidate_alignment") or {}).get("tickers") or [])
    tickers.extend(row.get("ticker") for row in _crypto_rows(r5))
    tickers.extend(
        row.get("ticker") for row in (gh2.get("weather_gate") or {}).get("weather_rows") or []
    )
    return list(dict.fromkeys(str(item) for item in tickers if item))


def _weather_freshness(source: dict[str, Any]) -> str | None:
    checks = (
        source.get("snapshot_fresh"),
        source.get("weather_source_forecast_fresh"),
        source.get("weather_feature_fresh"),
    )
    if all(value is True for value in checks):
        return "FRESH"
    if any(value is False for value in checks):
        return "STALE_OR_MISSING"
    return None


def _weather_next_condition(blocker: str) -> str | None:
    return {
        "MARKET_WINDOW_INELIGIBLE": "Wait for the next naturally scheduled eligible market window.",
        "EXECUTABLE_EV_NOT_POSITIVE": (
            "Wait for price or forecast movement to produce positive executable EV."
        ),
        "EV_NOT_POSITIVE": "Wait for price or forecast movement to produce positive EV.",
        "SNAPSHOT_MISSING": "Wait for a fresh market snapshot.",
        "SNAPSHOT_STALE": "Wait for the scheduled snapshot refresh.",
    }.get(blocker)


def _first_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, list) and value:
            return str(value[0])
        if isinstance(value, str) and value:
            return value
    return None


def _first_present(source: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in source:
            return source[key]
    return None


def _read_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object at {path}")
    return payload


def _decode_object(value: str | None) -> dict[str, Any]:
    try:
        payload = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Candidate Funnel Audit",
        "",
        "Read-only diagnostic. Existing safety and profitability gates are unchanged.",
        "",
        f"- Candidates: `{summary['candidate_count']}`",
        f"- Paper-ready: `{summary['paper_ready_candidates']}`",
        f"- First blockers: `{summary['first_blocker_counts']}`",
        "",
        "| Ticker | Category | First blocker | Gross EV | Fee-adjusted EV | Next condition |",
        "| --- | --- | --- | ---: | ---: | --- |",
    ]
    for row in payload["candidates"]:
        ev = row["price_and_ev"]
        lines.append(
            f"| {row['ticker']} | {row['category']} | {row['first_blocker']} | "
            f"{ev['gross_expected_value'] or 'n/a'} | "
            f"{ev['fee_adjusted_expected_value'] or 'n/a'} | "
            f"{row['next_condition'] or 'Review source evidence'} |"
        )
    return "\n".join(lines) + "\n"
