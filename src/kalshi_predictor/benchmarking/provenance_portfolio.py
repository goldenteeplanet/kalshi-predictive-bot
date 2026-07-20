from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from kalshi_predictor.benchmarking.portfolio import write_portfolio_benchmark

GENESIS = "GENESIS"
SYNTHETIC_ATTRIBUTION = {
    "SYN-BTC": {
        "forecast_id": "forecast:crypto:1",
        "feature_ref": {"table": "crypto_features", "id": "synthetic:crypto:1"},
        "observation_ref": {"table": "crypto_prices", "id": "synthetic:crypto-price:1"},
        "model_name": "crypto_v2", "model_version": "2.0.0",
        "orderbook_ref": {"table": "synthetic_orderbooks", "id": "synthetic:book:btc"},
    },
    "SYN-NYC-WEATHER": {
        "forecast_id": "forecast:weather:1",
        "feature_ref": {"table": "weather_features", "id": "synthetic:weather:1"},
        "observation_ref": {"table": "weather_forecasts", "id": "synthetic:noaa:1"},
        "model_name": "weather_v2", "model_version": "2.0.0",
        "orderbook_ref": {"table": "synthetic_orderbooks", "id": "synthetic:book:weather"},
    },
    "SYN-SPORTS": {
        "forecast_id": "forecast:sports:1",
        "feature_ref": {"table": "sports_features", "id": "synthetic:sports:1"},
        "observation_ref": {"table": "sports_events", "id": "synthetic:sports-event:1"},
        "model_name": "sports_v1", "model_version": "1.0.0",
        "orderbook_ref": {"table": "synthetic_orderbooks", "id": "synthetic:book:sports"},
    },
}


def build_provenance_aware_portfolio_report(base_report: Mapping[str, Any]) -> dict[str, Any]:
    decisions = base_report.get("allocation_decisions")
    categories = base_report.get("episode", {}).get("categories")
    if not isinstance(decisions, list) or not isinstance(categories, Mapping):
        raise ValueError("PMB-9 report lacks allocation decisions or categories")
    chain = []
    trade_logs = []
    previous = GENESIS
    for index, decision in enumerate(decisions):
        if not isinstance(decision, Mapping):
            raise ValueError(f"decision {index} must be an object")
        ticker = str(decision.get("ticker") or "")
        attribution = SYNTHETIC_ATTRIBUTION.get(ticker)
        if attribution is None:
            raise ValueError(f"exact synthetic attribution missing for {ticker}")
        timestamp = str(decision.get("timestamp") or "")
        envelope = {
            "decision_index": index,
            "ticker": ticker,
            "category": str(categories.get(ticker) or ""),
            "decision_timestamp": timestamp,
            "forecast_id": attribution["forecast_id"],
            "feature_ref": attribution["feature_ref"],
            "observation_ref": attribution["observation_ref"],
            "model_name": attribution["model_name"],
            "model_version": attribution["model_version"],
            "orderbook_ref": {
                **attribution["orderbook_ref"], "captured_at": timestamp,
            },
            "previous_digest": previous,
        }
        digest = _digest(envelope)
        chain.append({**envelope, "digest": digest})
        previous = digest
        if decision.get("status") == "ALLOCATED":
            trade_logs.append({
                "timestamp": timestamp,
                "ticker": ticker,
                "category": categories[ticker],
                "allocated_capital": decision.get("allocated_capital"),
                "forecast_id": attribution["forecast_id"],
                "feature_ref": attribution["feature_ref"],
                "observation_ref": attribution["observation_ref"],
                "model_name": attribution["model_name"],
                "model_version": attribution["model_version"],
                "orderbook_ref": envelope["orderbook_ref"],
                "provenance_digest": digest,
            })
    continuity = verify_portfolio_provenance_chain(chain)
    category_coverage = {
        category: sum(row["category"] == category for row in chain)
        for category in ("crypto", "weather", "sports")
    }
    canonical = json.dumps(chain, sort_keys=True, separators=(",", ":")).encode()
    return {
        "phase": "PMB-10",
        "mode": "LOCAL_SYNTHETIC_PROVENANCE_AWARE_PORTFOLIO_REPLAY",
        "database_access": False,
        "database_writes": 0,
        "external_replay_data_used": False,
        "execution_enabled": False,
        "base_phase": base_report.get("phase"),
        "portfolio": dict(base_report),
        "decision_provenance": chain,
        "trade_logs": trade_logs,
        "summary": {
            "decisions": len(chain),
            "attributed_trades": len(trade_logs),
            "chain_valid": continuity["valid"],
            "chain_failures": continuity["failures"],
            "category_coverage": category_coverage,
            "all_categories_covered": all(category_coverage.values()),
            "deterministic_provenance_digest": hashlib.sha256(canonical).hexdigest(),
        },
    }


def write_provenance_aware_portfolio_benchmark(output_dir: Path) -> Path:
    base_path = write_portfolio_benchmark(output_dir / "base")
    base = json.loads(base_path.read_text(encoding="utf-8"))
    report = build_provenance_aware_portfolio_report(base)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb10_provenance_aware_portfolio_replay.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def verify_portfolio_provenance_chain(chain: list[Mapping[str, Any]]) -> dict[str, Any]:
    failures = []
    previous = GENESIS
    for index, row in enumerate(chain):
        raw = {key: value for key, value in row.items() if key != "digest"}
        if row.get("previous_digest") != previous:
            failures.append({"index": index, "failure": "PREVIOUS_DIGEST_MISMATCH"})
        if row.get("digest") != _digest(raw):
            failures.append({"index": index, "failure": "DIGEST_MISMATCH"})
        for field in (
            "forecast_id", "feature_ref", "observation_ref", "model_name",
            "model_version", "orderbook_ref",
        ):
            if not row.get(field):
                failures.append({"index": index, "failure": f"{field.upper()}_MISSING"})
        previous = str(row.get("digest") or "")
    return {"valid": not failures and bool(chain), "failures": failures}


def _digest(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()
