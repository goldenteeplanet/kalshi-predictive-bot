"""Bounded server-side research captures for evaluating paid data providers."""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kalshi_predictor.economic.source_research import build_economic_source_features
from kalshi_predictor.research.bls import BLSError, BLSOriginal, BLSResearchClient, BLSResponse
from kalshi_predictor.research.fred import FREDError, FREDOriginal, FREDResearchClient, FREDResponse
from kalshi_predictor.research.oddpool import OddpoolError, OddpoolResearchClient, OriginalResponse
from kalshi_predictor.research.synoptic import (
    SynopticError,
    SynopticOriginal,
    SynopticResearchClient,
)
from kalshi_predictor.research.unusual_whales import (
    UnusualWhalesError,
    UnusualWhalesOriginal,
    UnusualWhalesResearchClient,
)


def load_key(path: Path) -> str:
    """Read one explicitly selected local file; never return filesystem error text."""
    try:
        with path.open("rb") as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ValueError
        key = raw.decode("utf-8-sig").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{16,256}", key):
            raise ValueError
        return key
    except (OSError, UnicodeError, ValueError):
        raise ValueError("PROVIDER_KEY_FILE_INVALID") from None


def capture(provider: str, key_file: Path, output: Path, *, limit: int = 5) -> dict:
    """One provider request, no orders, automatic retries or model qualification."""
    if (
        provider not in {"oddpool", "unusual-whales", "synoptic", "fred", "bls"}
        or not 1 <= limit <= 20
    ):
        raise ValueError("PROVIDER_TRIAL_ARGUMENT_INVALID")
    key = load_key(key_file)
    economic_response: BLSResponse | FREDResponse | None = None
    started = datetime.now(UTC)
    result: dict = {
        "provider": provider,
        "started_at": started.isoformat(),
        "research_only": True,
        "runtime_certified": False,
        "requests_budget": 1,
        "state": "UNVERIFIED",
    }
    original: (
        OriginalResponse
        | UnusualWhalesOriginal
        | SynopticOriginal
        | FREDOriginal
        | BLSOriginal
        | None
    ) = None
    try:
        if provider == "oddpool":
            with OddpoolResearchClient(key, request_budget=1) as client:
                page = client.search_markets(
                    series_id="KXTEMPNYCH", exchange="kalshi", status="active", limit=limit
                )
            original = page.original
            result.update(
                record_count=len(page.records),
                has_more=page.has_more,
                selection="active Kalshi KXTEMPNYCH",
                samples=[{"market_id": row.market_id} for row in page.records],
            )
        elif provider == "bls":
            with BLSResearchClient(key, request_budget=1) as bls_client:
                labor = bls_client.observations(series_id="CUUR0000SA0", year=started.year)
            original = labor.original
            economic_response = labor
            labor_results = labor.data["Results"]
            if isinstance(labor_results, list):
                labor_results = labor_results[0]
            labor_rows = labor_results["series"][0]["data"]
            result.update(
                record_count=len(labor_rows[:limit]),
                total_available=len(labor_rows),
                selection="CUUR0000SA0 current-year CPI observations, not seasonally adjusted",
                publication_time_verified=False,
                historical_availability_verified=False,
                samples=labor_rows[:limit],
            )
        elif provider == "fred":
            today = started.date()
            with FREDResearchClient(key, request_budget=1) as fred_client:
                economic = fred_client.observations(
                    series_id="DFF",
                    observation_start=str(today - timedelta(days=7)),
                    observation_end=str(today),
                    realtime_start=str(today),
                    realtime_end=str(today),
                    limit=limit,
                )
            original = economic.original
            economic_response = economic
            result.update(
                record_count=len(economic.data["observations"]),
                selection="DFF observations with explicit current vintage",
                publication_time_verified=False,
                samples=economic.data["observations"],
                total_available=economic.data.get("count"),
            )
        elif provider == "synoptic":
            with SynopticResearchClient(key, request_budget=1) as weather_client:
                weather = weather_client.latest(within_minutes=120)
            original = weather.original
            result.update(
                record_count=len(weather.observations),
                selection="KNYC latest air temperature observations",
                source_status=weather.status,
                samples=[
                    {
                        "sensor_id": row.sensor_id,
                        "observed_at": row.observed_at.isoformat(),
                        "value_c": str(row.value_c) if row.value_c is not None else None,
                        "unit": row.unit,
                        "qc": row.qc,
                    }
                    for row in weather.observations
                ],
            )
        else:
            with UnusualWhalesResearchClient(key, request_budget=1) as client:
                response = client.unusual_markets(limit=limit)
            original = response.original
            if not isinstance(response.data, dict):
                raise ValueError("PROVIDER_TRIAL_RESPONSE_INVALID")
            rows = response.data["data"]
            result.update(
                record_count=len(rows),
                selection="provider-ranked unusual prediction markets",
                venue_mapping_verified=False,
                provider_timestamp=response.provider_timestamp,
                samples=[
                    {name: row.get(name) for name in ("asset_id", "market", "category", "resolves")}
                    for row in rows
                ],
            )
        assert original is not None
        result.update(
            state="CAPTURED",
            received_at=original.received_at.isoformat(),
            source_url=original.url,
            source_sha256=original.sha256,
        )
        if economic_response is not None:
            features = build_economic_source_features(
                economic_response, decision_at=datetime.now(UTC)
            )
            result["research_features"] = {
                "contribution": features.contribution,
                "source_sha256": features.source_sha256,
                "available_at": features.received_at.isoformat(),
                "generated_at": features.decision_at.isoformat(),
                "concept": features.concept,
                "units": features.units,
                "seasonal_adjustment": features.seasonal_adjustment,
                "momentum_score": str(features.momentum_score)
                if features.momentum_score is not None
                else None,
                "momentum_status": features.momentum_status,
                "runtime_certified": False,
            }
    except (OddpoolError, UnusualWhalesError, SynopticError, FREDError, BLSError) as exc:
        result.update(state="PROVIDER_ERROR", error_code=str(exc))
        if isinstance(exc, BLSError):
            result["http_status"] = exc.http_status
    result["completed_at"] = datetime.now(UTC).isoformat()
    encoded = json.dumps(result, indent=2, ensure_ascii=False)
    if key in encoded or (original is not None and key.encode() in original.payload):
        raise ValueError("PROVIDER_SECRET_ECHO_REJECTED")
    output.mkdir(parents=True, exist_ok=True)
    stamp = started.strftime("%Y%m%dT%H%M%S%fZ")
    prefix = output / (stamp + "-" + provider)
    if original is not None:
        with prefix.with_suffix(".original.json").open("xb") as stream:
            stream.write(original.payload)
    with prefix.with_suffix(".summary.json").open("x", encoding="utf-8") as stream:
        stream.write(encoded + "\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "provider", choices=("oddpool", "unusual-whales", "synoptic", "fred", "bls")
    )
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=Path("reports/provider_trial"))
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    try:
        result = capture(args.provider, args.key_file, args.output, limit=args.limit)
    except Exception:
        print("PROVIDER_TRIAL_FAILED")
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0 if result["state"] == "CAPTURED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
