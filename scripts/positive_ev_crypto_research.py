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

from kalshi_predictor.crypto.named_research_baselines import (
    BASELINE_MODULES,
    capture_named_baselines,
    select_exact_receipt,
)
from kalshi_predictor.crypto.research_provenance import (
    freeze_code,
    freeze_prediction,
    verify_unchanged,
)
from kalshi_predictor.forecasting.crypto_v3_independent import (
    CryptoTarget,
    PriceObservation,
    forecast_independent,
)
from kalshi_predictor.ingest.public_market_discovery import event_markets


def NOW():
    return datetime.now(UTC)


BASE = "https://external-api.kalshi.com/trade-api/v2"


def main(output: Path, *, sol_history: bool = False) -> None:
    output.mkdir(parents=True, exist_ok=False)
    repo = Path(__file__).resolve().parents[1]
    for module_name in (
        "kalshi_predictor.forecasting.crypto_v3_independent",
        "kalshi_predictor.crypto.distribution_model",
        "kalshi_predictor.crypto.research_provenance",
        *BASELINE_MODULES,
    ):
        expected = repo / "src" / Path(*module_name.split(".")).with_suffix(".py")
        actual = Path(sys.modules[module_name].__file__).resolve()
        if actual != expected.resolve():
            raise ValueError("IMPORTED_MODEL_SOURCE_MISMATCH")
    code_proof = freeze_code(
        repo,
        output / "code",
        (
            "scripts/positive_ev_crypto_research.py",
            "src/kalshi_predictor/forecasting/crypto_v3_independent.py",
            "src/kalshi_predictor/crypto/distribution_model.py",
            "src/kalshi_predictor/crypto/research_provenance.py",
            *("src/" + name.replace(".", "/") + ".py" for name in BASELINE_MODULES),
        ),
    )
    (output / "code_provenance.json").write_text(json.dumps(code_proof, indent=2))
    receipts, rows, errors, inputs = [], [], [], {}
    count = 0

    def named_baselines(ticker, market_sha, as_of, book_sha=None):
        market_receipt = next(r for r in receipts if r["sha256"] == market_sha)
        book_receipt = (
            select_exact_receipt(
                receipts,
                sha256=book_sha,
                request_url=f"{BASE}/markets/{ticker}/orderbook?depth=5",
            )
            if book_sha is not None
            else None
        )
        return capture_named_baselines(
            ticker=ticker,
            market_original=(output / market_receipt["path"]).read_bytes(),
            market_sha256=market_sha,
            market_received_at=datetime.fromisoformat(market_receipt["received_at"]),
            model_input_as_of=as_of,
            orderbook_original=(output / book_receipt["path"]).read_bytes()
            if book_receipt
            else None,
            orderbook_sha256=book_sha,
            orderbook_received_at=datetime.fromisoformat(book_receipt["received_at"])
            if book_receipt
            else None,
            orderbook_url=book_receipt["url"] if book_receipt else None,
        )

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
                query = urlencode(
                    {
                        "granularity": 60,
                        "start": datetime.fromtimestamp(start, UTC).isoformat(),
                        "end": datetime.fromtimestamp(end, UTC).isoformat(),
                    }
                )
                candles, received, sha = get(
                    f"https://api.exchange.coinbase.com/products/{symbol}-USD/candles?{query}",
                    f"{symbol}-candles-{page}",
                )
                # Half-open windows prevent boundary duplicates; missing bars fail model validation.
                closed = [
                    r for r in candles if start <= r[0] < end and r[0] + 60 <= received.timestamp()
                ]
                prices.extend(
                    PriceObservation(
                        float(r[4]),
                        datetime.fromtimestamp(r[0] + 60, UTC),
                        received,
                        "coinbase_closed_1m_candles",
                        sha,
                        symbol,
                    )
                    for r in closed
                )
            prices.sort(key=lambda price: price.observed_at)
            listing, book_received, book_sha = get(
                f"{BASE}/events?"
                + urlencode(
                    dict(
                        series_ticker=series,
                        status="open",
                        with_nested_markets="true",
                        limit=2,
                        min_close_ts=int(NOW().timestamp()),
                    )
                ),
                symbol + "-events",
            )
            decision = NOW()
            candidates = []
            for market in event_markets(listing, series):
                try:
                    if market.get("status") not in ("active", "open") or market.get("result"):
                        continue
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
            selection = sorted(candidates, key=lambda item: (item[0], item[1]["ticker"]))[:2]
            selection_proof = {
                "method": "FIRST_TWO_OPEN_EVENTS_VALID_HORIZON_NEAREST_STRIKE_THEN_TICKER_TOP_2",
                "catalog_partial": bool(listing.get("cursor")),
                "ordered_tickers": [item[1]["ticker"] for item in selection],
                "selected_as_of": decision.isoformat(),
            }
            for _, market, target in selection:
                forecast = forecast_independent(prices, target, decision_at=decision)
                ticker = market["ticker"]
                baselines = named_baselines(ticker, book_sha, decision)
                prediction = dict(
                    prices=[asdict(p) for p in prices],
                    target=asdict(target),
                    model_input_as_of=decision,
                    forecast=forecast,
                    named_baselines=baselines,
                    market_receipt_sha256=book_sha,
                    code_provenance=code_proof,
                    selection=selection_proof,
                )
                verify_unchanged(repo, code_proof)
                frozen = freeze_prediction(
                    output
                    / "predictions"
                    / (hashlib.sha256(ticker.encode()).hexdigest() + "-listing"),
                    prediction,
                    model_input_as_of=decision,
                    input_received_at=max(book_received, *(p.received_at for p in prices)),
                    model_committed_at=datetime.fromisoformat(code_proof["commit_recorded_at"]),
                    target_at=target.observation_at,
                )
                inputs[ticker] = dict(
                    prediction, prediction_receipt=frozen, decision_at=frozen["decision_at"]
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
                            named_baselines=baselines,
                            paper_eligible=False,
                            calibrated=False,
                            listing_received_at=book_received.isoformat(),
                            forecast_input_sha256=forecast["input_sha256"],
                            model_input_as_of=frozen["model_input_as_of"],
                            prediction_recorded_at=frozen["prediction_recorded_at"],
                            prediction_sha256=frozen["prediction_sha256"],
                            decision_at=frozen["decision_at"],
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
            baselines = named_baselines(ticker, saved["market_receipt_sha256"], decision, sha)
            view = book.get("orderbook_fp", book.get("orderbook", {}))
            verify_unchanged(repo, code_proof)
            frozen = freeze_prediction(
                output / "predictions" / (hashlib.sha256(ticker.encode()).hexdigest() + "-book"),
                dict(
                    prices=saved["prices"],
                    target=saved["target"],
                    forecast=forecast,
                    named_baselines=baselines,
                    book_receipt_sha256=sha,
                    market_receipt_sha256=saved["market_receipt_sha256"],
                    code_provenance=code_proof,
                    selection={
                        "method": "INDICATIVE_GROSS_DESC_FIRST_2_DISTINCT_TICKERS",
                        "ordered_tickers": selected,
                    },
                ),
                model_input_as_of=decision,
                input_received_at=max(received, *(p.received_at for p in prices)),
                model_committed_at=datetime.fromisoformat(code_proof["commit_recorded_at"]),
                target_at=target.observation_at,
            )
            for row in (r for r in rows if r["ticker"] == ticker):
                opposite = "no" if row["side"] == "YES" else "yes"
                levels = view.get(opposite + "_dollars", [])
                valid = [
                    (float(price), float(quantity))
                    for price, quantity in levels
                    if 0 < float(price) < 1 and float(quantity) >= 1
                ]
                row["decision_at"] = frozen["decision_at"]
                row["model_input_as_of"] = frozen["model_input_as_of"]
                row["prediction_recorded_at"] = frozen["prediction_recorded_at"]
                row["prediction_sha256"] = frozen["prediction_sha256"]
                row["forecast_input_sha256"] = forecast["input_sha256"]
                row["model_comparisons"] = forecast["comparisons"]
                row["named_baselines"] = baselines
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
            saved["final_decision_at"] = frozen["decision_at"]
            saved["final_forecast"] = forecast
            saved["final_named_baselines"] = baselines
            saved["final_prediction_receipt"] = frozen
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(dict(ticker=ticker, error=str(exc)))
            for row in (r for r in rows if r["ticker"] == ticker):
                row["status"] = "RESEARCH_BOOK_CAPTURE_INCOMPLETE"
                row["first_blocker"] = str(exc)
                row["executable_price"] = None
                row["gross_edge"] = None
                row["net_ev"] = None
                row["paper_eligible"] = False
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
        coverage="FIRST_2_OPEN_EVENTS_PER_SERIES_MAX_400_MARKETS; TOP_2_NEAR_SPOT; NOT_EXHAUSTIVE",
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
