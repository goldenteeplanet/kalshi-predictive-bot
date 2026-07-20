from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings
from kalshi_predictor.crypto.linker import detect_crypto_market, link_crypto_markets
from kalshi_predictor.crypto.semantics import EXACT_LINK, parse_crypto_market_terms
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import Market, MarketLeg
from kalshi_predictor.weather.repository import insert_weather_market_link
from kalshi_predictor.weather.temperature_contracts import (
    parse_point_temperature_ticker,
    validate_point_temperature_market,
)
from kalshi_predictor.utils.time import parse_datetime, utc_now


def preview_exact_links(
    session: Session, *, gh1r_report: Path, settings: Settings
) -> dict[str, Any]:
    source = json.loads(gh1r_report.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for model, candidates in source["eligibility"]["ineligible_rows"].items():
        for candidate in candidates:
            ticker = candidate["ticker"]
            market = session.get(Market, ticker)
            if market is None:
                rows.append(_row(ticker, model, False, "MARKET_METADATA_MISSING"))
                continue
            raw = decode_json(market.raw_json)
            metadata = {**raw, "ticker": market.ticker, "series_ticker": market.series_ticker,
                        "event_ticker": market.event_ticker, "close_time": market.close_time,
                        "rules_primary": market.rules_primary, "rules_secondary": market.rules_secondary}
            if model == "crypto_v2":
                legs = list(session.scalars(select(MarketLeg).where(MarketLeg.ticker == ticker)))
                terms = parse_crypto_market_terms(market, legs=legs)
                symbol, confidence, reason = detect_crypto_market(market, legs=legs)
                safe = terms.status == EXACT_LINK and symbol is not None and confidence >= settings.crypto_v2_min_link_confidence
                rows.append(_row(ticker, model, safe, reason, symbol=symbol,
                                 confidence=str(confidence), semantic_status=terms.status))
            else:
                contract = parse_point_temperature_ticker(ticker)
                if contract is None:
                    rows.append(_row(ticker, model, False, "TICKER_PARSE_FAILED"))
                    continue
                validation = validate_point_temperature_market(
                    contract, metadata, series_scope="KXTEMPNYCH"
                )
                rows.append(_row(
                    ticker, model, validation.passed,
                    "EXACT_METADATA_MATCH" if validation.passed else ",".join(validation.blockers),
                    location_key=contract.location_key, weather_metric="TEMPERATURE",
                    target_operator=contract.contract_kind,
                    target_value=str(contract.discrete_threshold_f),
                    target_time=contract.target_utc_time.isoformat(), confidence="1.0",
                ))
    safe_rows = [row for row in rows if row["safe_to_apply"]]
    return {
        "phase": "GH-1S", "generated_at": utc_now().isoformat(),
        "mode": "EXACT_LINK_PREVIEW_NO_WRITE", "database_writes": 0,
        "execution_enabled": False, "thresholds_changed": False, "rows": rows,
        "summary": {"rows_scanned": len(rows), "safe_to_apply": len(safe_rows),
                    "blocked": len(rows) - len(safe_rows),
                    "safe_by_model": dict(Counter(row["model"] for row in safe_rows))},
    }


def apply_exact_links(
    session: Session, *, preview: dict[str, Any], database_path: Path, backup_dir: Path,
    existing_backup_path: Path | None = None,
) -> dict[str, Any]:
    safe = [row for row in preview["rows"] if row["safe_to_apply"]]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = existing_backup_path or backup_dir / f"kalshi_phase1_pre_gh1s_{stamp}.db"
    if existing_backup_path is not None:
        if not backup_path.is_file() or backup_path.stat().st_size == 0:
            raise ValueError("The pre-created backup is missing or empty.")
    else:
        source = sqlite3.connect(database_path)
        destination = sqlite3.connect(backup_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
    crypto = [row["ticker"] for row in safe if row["model"] == "crypto_v2"]
    weather = [row for row in safe if row["model"] == "weather_v2"]
    crypto_result = link_crypto_markets(session, tickers=crypto, limit=len(crypto)) if crypto else None
    weather_written = 0
    for row in weather:
        insert_weather_market_link(
            session, ticker=row["ticker"], location_key=row["location_key"],
            weather_metric=row["weather_metric"], target_operator=row["target_operator"],
            target_value=row["target_value"], target_time=parse_datetime(row["target_time"]),
            confidence=row["confidence"], reason="GH-1S exact market metadata validation",
            raw_json={"phase": "GH-1S", "preview_reason": row["reason"]},
        )
        weather_written += 1
    session.commit()
    return {"backup_path": str(backup_path), "crypto_links_written": getattr(crypto_result, "links_created", 0),
            "crypto_links_already_present": getattr(crypto_result, "already_linked", 0),
            "weather_links_written": weather_written, "total_safe_rows": len(safe),
            "execution_enabled": False}


def _row(ticker: str, model: str, safe: bool, reason: str, **extra: Any) -> dict[str, Any]:
    return {"ticker": ticker, "model": model, "safe_to_apply": safe, "reason": reason, **extra}
