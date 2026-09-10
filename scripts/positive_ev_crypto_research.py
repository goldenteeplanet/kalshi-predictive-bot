"""Bounded public-only prospective crypto research capture; no account or DB access."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

from kalshi_predictor.crypto.research_provenance import freeze_code, verify_unchanged
from kalshi_predictor.forecasting.crypto_v3_independent import (
    CryptoTarget,
    PriceObservation,
    forecast_independent,
)


def NOW():
    return datetime.now(UTC)


BASE = "https://api.elections.kalshi.com/trade-api/v2"


def main(output: Path, *, sol_history: bool = False) -> None:
    output.mkdir(parents=True, exist_ok=False)
    repo = Path(__file__).resolve().parents[1]
    for module_name in (
        "kalshi_predictor.forecasting.crypto_v3_independent",
        "kalshi_predictor.crypto.distribution_model",
        "kalshi_predictor.crypto.research_provenance",
    ):
        expected = repo / "src" / Path(*module_name.split(".")).with_suffix(".py")
        actual = Path(sys.modules[module_name].__file__).resolve()
        if actual != expected.resolve():
            raise ValueError("IMPORTED_MODEL_SOURCE_MISMATCH")
    code_proof = freeze_code(repo, output / "code", (
        "scripts/positive_ev_crypto_research.py",
        "src/kalshi_predictor/forecasting/crypto_v3_independent.py",
        "src/kalshi_predictor/crypto/distribution_model.py",
        "src/kalshi_predictor/crypto/research_provenance.py",
    ))
    (output / "code_provenance.json").write_text(json.dumps(code_proof, indent=2))
    receipts, rows, errors, inputs = [], [], [], {}
    count = 0

    def get(url, label):
        nonlocal count
        count += 1
        if count > (7 if sol_history else 12):
            raise RuntimeError("REQUEST_BUDGET")
        started = NOW()
        req = urllib.request.Request(url, headers={"User-Agent": "Dejoia-readonly-research/1"})
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                data = response.read(2_000_001)
                status = response.status
        except urllib.error.HTTPError as exc:
            data, status = exc.read(2_000_001), exc.code
        received = NOW()
        if len(data) > 2_000_000:
            raise ValueError("RESPONSE_SIZE_CAP")
        original = output / (label + ".json")
        original.write_bytes(data)
        sha = hashlib.sha256(data).hexdigest()
        receipts.append(
            dict(
                url=url,
                status=status,
                started_at=started.isoformat(),
                received_at=received.isoformat(),
                sha256=sha,
                path=original.name,
            )
        )
        if status != 200:
            raise ValueError(f"HTTP_{status}")
        return json.loads(data), received, sha

    universe = (
        ("BTC", "KXBTC"),
        ("ETH", "KXETH"),
        ("SOL", "KXSOLE"),
        ("XRP", "KXXRP"),
        ("DOGE", "KXDOGE"),
    )
    for symbol, series in (("SOL", "KXSOLE"),) if sol_history else universe:
        try:
            prices = []
            anchor = int(NOW().timestamp() // 60) * 60
            for page in range(4 if sol_history else 1):
                end = anchor - page * 300 * 60
                start = end - 300 * 60
                query = urlencode({"granularity": 60,
                                   "start": datetime.fromtimestamp(start, UTC).isoformat(),
                                   "end": datetime.fromtimestamp(end, UTC).isoformat()})
                candles, received, sha = get(
                    f"https://api.exchange.coinbase.com/products/{symbol}-USD/candles?{query}",
                    f"{symbol}-candles-{page}",
                )
                # Half-open windows prevent boundary duplicates; missing bars fail model validation.
                closed = [r for r in candles if start <= r[0] < end
                          and r[0] + 60 <= received.timestamp()]
                prices.extend(PriceObservation(
                    float(r[4]),
                    datetime.fromtimestamp(r[0] + 60, UTC),
                    received,
                    "coinbase_closed_1m_candles",
                    sha,
                    symbol,
                )
                for r in closed)
            prices.sort(key=lambda price: price.observed_at)
            listing, book_received, book_sha = get(
                f"{BASE}/markets?series_ticker={series}&status=open&limit=100", symbol + "-markets"
            )
            decision = NOW()
            candidates = []
            for market in listing.get("markets", []):
                try:
                    close = datetime.fromisoformat(market["close_time"].replace("Z", "+00:00"))
                    horizon = (close - decision).total_seconds()
                    if not 0 < horizon <= min(72 * 3600, len(prices) * 60):
                        continue
                    comparator = {
                        "greater": "ABOVE",
                        "greater_or_equal": "AT_OR_ABOVE",
                        "less": "BELOW",
                        "less_or_equal": "AT_OR_BELOW",
                        "between": "RANGE",
                    }.get(market.get("strike_type"))
                    if comparator is None:
                        continue
                    floor, cap = market.get("floor_strike"), market.get("cap_strike")
                    strike = floor if floor is not None else cap
                    if strike is None:
                        continue
                    target = CryptoTarget(
                        symbol,
                        comparator,
                        close,
                        threshold=None if comparator == "RANGE" else float(strike),
                        lower=float(floor) if comparator == "RANGE" else None,
                        upper=float(cap) if comparator == "RANGE" else None,
                        benchmark="DECLARED_CLOSE_TIME_TERMINAL_PROXY",
                    )
                    candidates.append((abs(float(strike) / prices[-1].price - 1), market, target))
                except (ValueError, KeyError, TypeError):
                    continue
            for _, market, target in sorted(candidates, key=lambda item: item[0])[:2]:
                forecast = forecast_independent(prices, target, decision_at=decision)
                ticker = market["ticker"]
                inputs[ticker] = dict(
                    prices=[asdict(p) for p in prices],
                    target=asdict(target),
                    decision_at=decision,
                    forecast=forecast,
                    market_receipt_sha256=book_sha,
                )
                for side in ("YES", "NO"):
                    p = forecast["probability"] if side == "YES" else 1 - forecast["probability"]
                    field = "yes_ask_dollars" if side == "YES" else "no_ask_dollars"
                    value = market.get(field)
                    ask = float(value) if value is not None else None
                    if ask is not None and not 0 < ask < 1:
                        ask = None
                    rows.append(
                        dict(
                            ticker=ticker,
                            event_id=market.get("event_ticker"),
                            category="crypto",
                            symbol=symbol,
                            side=side,
                            model="crypto_v3_independent",
                            independent_probability=p,
                            executable_price=None,
                            indicative_listing_ask=ask,
                            gross_edge=None,
                            indicative_gross_edge=p - ask if ask else None,
                            fees=None,
                            slippage=None,
                            uncertainty=None,
                            net_ev=None,
                            settlement_eta=(target.observation_at - decision).total_seconds(),
                            horizon_role="UNCERTIFIED_CLOSE_TIME_PROXY",
                            data_sources=forecast["sources"],
                            first_blocker="SETTLEMENT_ALIGNMENT_AND_MODEL_RELEASE_MISSING",
                            status="PROXY_SHADOW_UNQUALIFIED",
                            model_comparisons=forecast["comparisons"],
                            paper_eligible=False,
                            calibrated=False,
                            listing_received_at=book_received.isoformat(),
                            forecast_input_sha256=forecast["input_sha256"],
                        )
                    )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(dict(symbol=symbol, error=str(exc)))

    ranked = sorted(
        (r for r in rows if r["indicative_gross_edge"] is not None),
        key=lambda r: r["indicative_gross_edge"],
        reverse=True,
    )
    selected = list(dict.fromkeys(r["ticker"] for r in ranked))[:2]
    for ticker in selected:
        try:
            book, received, sha = get(
                f"{BASE}/markets/{ticker}/orderbook?depth=5", ticker + "-book"
            )
            saved = inputs[ticker]
            prices = [PriceObservation(**p) for p in saved["prices"]]
            target = CryptoTarget(**saved["target"])
            decision = NOW()
            forecast = forecast_independent(prices, target, decision_at=decision)
            view = book.get("orderbook_fp", book.get("orderbook", {}))
            for row in (r for r in rows if r["ticker"] == ticker):
                opposite = "no" if row["side"] == "YES" else "yes"
                levels = view.get(opposite + "_dollars", [])
                valid = [
                    (float(price), float(quantity))
                    for price, quantity in levels
                    if 0 < float(price) < 1 and float(quantity) >= 1
                ]
                row["decision_at"] = decision.isoformat()
                row["forecast_input_sha256"] = forecast["input_sha256"]
                row["model_comparisons"] = forecast["comparisons"]
                row["book_received_at"] = received.isoformat()
                row["book_receipt_sha256"] = sha
                row["independent_probability"] = (
                    forecast["probability"] if row["side"] == "YES" else 1 - forecast["probability"]
                )
                if valid:
                    row["executable_price"] = 1 - max(p for p, _ in valid)
                    row["gross_edge"] = row["independent_probability"] - row["executable_price"]
                    row["total_cost_break_even_budget"] = row["gross_edge"]
                    row["quote_status"] = "OBSERVED_ONE_CONTRACT_DEPTH_NOT_FILL_GUARANTEE"
                else:
                    row["quote_status"] = "NO_ONE_CONTRACT_OPPOSITE_DEPTH"
            saved["final_decision_at"], saved["final_forecast"] = decision, forecast
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(dict(ticker=ticker, error=str(exc)))
    verify_unchanged(repo, code_proof)
    manifest = dict(
        schema="prospective-crypto-cohort-v1",
        generated_at=NOW(),
        inputs=inputs,
        receipts=receipts,
        outcomes=None,
        code_provenance=code_proof,
        promotion_authority=False,
        capture_scope="SOL_1200_MINUTES" if sol_history else "FIVE_ASSET_300_MINUTES",
        event_independence="REPEATED_EVENT_CAPTURES_ARE_NOT_INDEPENDENT_SAMPLES",
    )
    encoded = json.dumps(manifest, indent=2, default=str, allow_nan=False).encode()
    (output / "cohort.json").write_bytes(encoded)
    report = dict(
        schema="independent-research-v1",
        generated_at=NOW().isoformat(),
        rows=rows,
        errors=errors,
        requests=count,
        independent_forecasts=len(inputs),
        positive_gross_observed=sum(
            r["gross_edge"] is not None and r["gross_edge"] > 0 for r in rows
        ),
        positive_net_ev=None,
        cohort_sha256=hashlib.sha256(encoded).hexdigest(),
        execution_enabled=False,
        paper_positions_created=0,
        coverage="FIRST_100_OPEN_MARKETS_PER_SERIES; TOP_2_NEAR_SPOT; NOT_EXHAUSTIVE",
    )
    (output / "report.json").write_text(
        json.dumps(report, indent=2, allow_nan=False), encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--sol-history", action="store_true")
    args = parser.parse_args()
    main(args.output, sol_history=args.sol_history)
