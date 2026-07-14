import csv
import io
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import EconomicEvent
from kalshi_predictor.economic.discovery import run_phase3bd_economic_market_discovery
from kalshi_predictor.economic.features import build_economic_features
from kalshi_predictor.economic.repository import insert_economic_event, normalize_event_key
from kalshi_predictor.kalshi.client import KalshiClient
from kalshi_predictor.opportunities.reports import generate_opportunities_report
from kalshi_predictor.utils.decimals import decimal_to_str
from kalshi_predictor.utils.time import parse_datetime, utc_now

BLS_API_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
BLS_SOURCE_URL = "https://www.bls.gov/developers/api_signature_v2.htm"
FRED_FED_UPPER_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFEDTARU"
FRED_FED_UPPER_SOURCE_URL = "https://fred.stlouisfed.org/series/DFEDTARU"
FRED_GDP_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=A191RL1Q225SBEA"
FRED_GDP_SOURCE_URL = "https://fred.stlouisfed.org/series/A191RL1Q225SBEA"
HTTP_USER_AGENT = "Mozilla/5.0 DejoiaEconomicActuals/1.0"
UTC = ZoneInfo("UTC")
TRADING_ECONOMICS_CALENDAR_SCHEMA_URL = (
    "https://docs.tradingeconomics.com/economic_calendar/schema/"
)
TRADING_ECONOMICS_CALENDAR_COUNTRY_DOC_URL = (
    "https://docs.tradingeconomics.com/economic_calendar/country/"
)
TRADING_ECONOMICS_ENV_NAMES = (
    "TRADING_ECONOMICS_API_KEY",
    "TRADINGECONOMICS_API_KEY",
    "TE_API_KEY",
)
CONSENSUS_SOURCE_SETUP_URL = "configure TRADING_ECONOMICS_API_KEY or --input-file"


@dataclass(frozen=True)
class EconomicValueObservation:
    event_key: str
    source: str
    source_url: str
    event_time: datetime
    category: str
    title: str
    actual_value: str | None
    forecast_value: str | None
    previous_value: str | None
    raw_json: Mapping[str, Any]


@dataclass(frozen=True)
class EconomicValueFetchResult:
    source: str
    url: str
    attempted: bool
    succeeded: bool
    observations: list[EconomicValueObservation]
    error: str | None = None


@dataclass(frozen=True)
class Phase3BDR3Artifacts:
    json_path: Path
    markdown_path: Path
    payload: dict[str, Any]


@dataclass(frozen=True)
class Phase3BDR4Artifacts:
    json_path: Path
    markdown_path: Path
    payload: dict[str, Any]


ValueFetcher = Callable[[], list[EconomicValueFetchResult]]


def fetch_official_economic_values() -> list[EconomicValueFetchResult]:
    with httpx.Client(
        timeout=httpx.Timeout(30),
        follow_redirects=True,
        headers={"User-Agent": HTTP_USER_AGENT},
    ) as client:
        return [
            _fetch_bls_values(client),
            _fetch_fred_series(
                client,
                source="fred_fed_target_upper",
                csv_url=FRED_FED_UPPER_CSV_URL,
                source_url=FRED_FED_UPPER_SOURCE_URL,
                series_id="DFEDTARU",
                event_key="fed",
                category="fed",
                title="Federal funds target range upper limit",
            ),
            _fetch_fred_series(
                client,
                source="fred_gdp_growth",
                csv_url=FRED_GDP_CSV_URL,
                source_url=FRED_GDP_SOURCE_URL,
                series_id="A191RL1Q225SBEA",
                event_key="gdp",
                category="gdp",
                title="Real gross domestic product percent change",
            ),
        ]


def fetch_verified_consensus_values(
    *,
    input_file: Path | None = None,
    trading_economics_api_key: str | None = None,
    country: str = "united states",
    days_back: int = 90,
    days_ahead: int = 14,
    min_importance: int = 2,
) -> list[EconomicValueFetchResult]:
    results: list[EconomicValueFetchResult] = []
    api_key = trading_economics_api_key or _env_api_key()
    if api_key:
        with httpx.Client(
            timeout=httpx.Timeout(30),
            follow_redirects=True,
            headers={"User-Agent": HTTP_USER_AGENT},
        ) as client:
            results.append(
                _fetch_trading_economics_consensus(
                    client,
                    api_key=api_key,
                    country=country,
                    days_back=days_back,
                    days_ahead=days_ahead,
                    min_importance=min_importance,
                )
            )
    if input_file is not None:
        results.append(_load_verified_consensus_file(input_file))
    if not results:
        results.append(
            EconomicValueFetchResult(
                source="verified_consensus_source",
                url=CONSENSUS_SOURCE_SETUP_URL,
                attempted=False,
                succeeded=False,
                observations=[],
                error=(
                    "No verified consensus source configured. Set "
                    "TRADING_ECONOMICS_API_KEY or pass --input-file with source_url "
                    "and consensus values."
                ),
            )
        )
    return results


def run_phase3bd_r3_economic_value_capture(
    session: Session,
    *,
    value_fetcher: ValueFetcher | None = None,
    kalshi_client: KalshiClient | None = None,
    opportunity_output_path: str | Path = "reports/opportunities_economic_v1.md",
    max_series: int = 24,
    markets_per_series: int = 50,
    snapshot_series_limit: int = 8,
    forecast_limit: int = 500,
    opportunity_limit: int = 75,
) -> dict[str, Any]:
    generated_at = utc_now()
    fetch_results = (value_fetcher or fetch_official_economic_values)()
    observations = [
        observation
        for result in fetch_results
        for observation in result.observations
    ]
    inserted, skipped_existing = _insert_value_observations(session, observations)
    feature_summary = build_economic_features(session)
    discovery_payload = run_phase3bd_economic_market_discovery(
        session,
        client=kalshi_client,
        max_series=max_series,
        markets_per_series=markets_per_series,
        snapshot_series_limit=snapshot_series_limit,
        forecast_limit=forecast_limit,
        include_orderbooks=False,
    )
    opportunity_path, opportunity_summary = generate_opportunities_report(
        session,
        model_name="economic_v1",
        limit=opportunity_limit,
        output_path=opportunity_output_path,
    )
    consensus_missing = sum(
        1 for observation in observations if observation.forecast_value is None
    )
    payload = {
        "phase": "3BD-R3",
        "generated_at": generated_at.isoformat(),
        "mode": "PAPER_READ_ONLY_VALUE_CAPTURE",
        "live_demo_execution": "blocked",
        "summary": {
            "sources_attempted": len(fetch_results),
            "sources_succeeded": sum(1 for result in fetch_results if result.succeeded),
            "value_observations_seen": len(observations),
            "value_observations_inserted": inserted,
            "value_observations_skipped_existing": skipped_existing,
            "actual_value_observations": sum(
                1 for observation in observations if observation.actual_value is not None
            ),
            "consensus_value_observations": sum(
                1 for observation in observations if observation.forecast_value is not None
            ),
            "consensus_missing_from_official_sources": consensus_missing,
            "features_inserted": feature_summary.features_inserted,
            "economic_links": discovery_payload["summary"]["links_created"]
            + discovery_payload["summary"]["links_skipped_existing"],
            "forecasts_inserted": discovery_payload["summary"]["forecasts_inserted"],
            "opportunity_markets_scanned": opportunity_summary.markets_scanned,
            "rankings_inserted": opportunity_summary.rankings_inserted,
            "opportunities_detected": opportunity_summary.opportunities_detected,
            "top_opportunity_ticker": opportunity_summary.top_opportunity_ticker,
            "top_opportunity_score": str(opportunity_summary.top_opportunity_score or "n/a"),
            "status": _status(fetch_results, observations, consensus_missing),
        },
        "sources": [_fetch_result_payload(result) for result in fetch_results],
        "observations": [_observation_payload(observation) for observation in observations],
        "phase3bd_discovery_summary": discovery_payload["summary"],
        "opportunity_report": str(opportunity_path),
        "recommended_next_action": _recommended_next_action(
            observations=observations,
            consensus_missing=consensus_missing,
            opportunities_detected=opportunity_summary.opportunities_detected,
        ),
    }
    return payload


def run_phase3bd_r4_verified_consensus_source(
    session: Session,
    *,
    input_file: Path | None = None,
    trading_economics_api_key: str | None = None,
    country: str = "united states",
    days_back: int = 90,
    days_ahead: int = 14,
    min_importance: int = 2,
    value_fetcher: ValueFetcher | None = None,
    kalshi_client: KalshiClient | None = None,
    opportunity_output_path: str | Path = "reports/opportunities_economic_v1.md",
    max_series: int = 24,
    markets_per_series: int = 50,
    snapshot_series_limit: int = 8,
    forecast_limit: int = 500,
    opportunity_limit: int = 75,
) -> dict[str, Any]:
    generated_at = utc_now()
    fetch_results = (
        value_fetcher()
        if value_fetcher is not None
        else fetch_verified_consensus_values(
            input_file=input_file,
            trading_economics_api_key=trading_economics_api_key,
            country=country,
            days_back=days_back,
            days_ahead=days_ahead,
            min_importance=min_importance,
        )
    )
    observations = [
        observation
        for result in fetch_results
        for observation in result.observations
    ]
    inserted = skipped_existing = 0
    features_inserted = forecasts_inserted = rankings_inserted = 0
    markets_scanned = opportunities_detected = 0
    top_opportunity_ticker = None
    top_opportunity_score = "n/a"
    discovery_summary: dict[str, Any] = {}
    opportunity_path: str | None = None
    if observations:
        inserted, skipped_existing = _insert_value_observations(session, observations)
        feature_summary = build_economic_features(session)
        features_inserted = feature_summary.features_inserted
        discovery_payload = run_phase3bd_economic_market_discovery(
            session,
            client=kalshi_client,
            max_series=max_series,
            markets_per_series=markets_per_series,
            snapshot_series_limit=snapshot_series_limit,
            forecast_limit=forecast_limit,
            include_orderbooks=False,
        )
        discovery_summary = discovery_payload["summary"]
        opportunity_path_obj, opportunity_summary = generate_opportunities_report(
            session,
            model_name="economic_v1",
            limit=opportunity_limit,
            output_path=opportunity_output_path,
        )
        opportunity_path = str(opportunity_path_obj)
        forecasts_inserted = discovery_summary.get("forecasts_inserted", 0)
        rankings_inserted = opportunity_summary.rankings_inserted
        markets_scanned = opportunity_summary.markets_scanned
        opportunities_detected = opportunity_summary.opportunities_detected
        top_opportunity_ticker = opportunity_summary.top_opportunity_ticker
        top_opportunity_score = str(opportunity_summary.top_opportunity_score or "n/a")
    consensus_rows = sum(
        1 for observation in observations if observation.forecast_value is not None
    )
    actual_consensus_rows = sum(
        1
        for observation in observations
        if observation.actual_value is not None and observation.forecast_value is not None
    )
    attempted_sources = sum(1 for result in fetch_results if result.attempted)
    succeeded_sources = sum(1 for result in fetch_results if result.succeeded)
    payload = {
        "phase": "3BD-R4",
        "generated_at": generated_at.isoformat(),
        "mode": "PAPER_READ_ONLY_VERIFIED_CONSENSUS_SOURCE",
        "live_demo_execution": "blocked",
        "summary": {
            "sources_configured": len(fetch_results),
            "sources_attempted": attempted_sources,
            "sources_succeeded": succeeded_sources,
            "value_observations_seen": len(observations),
            "value_observations_inserted": inserted,
            "value_observations_skipped_existing": skipped_existing,
            "consensus_value_observations": consensus_rows,
            "actual_and_consensus_observations": actual_consensus_rows,
            "consensus_only_observations": consensus_rows - actual_consensus_rows,
            "features_inserted": features_inserted,
            "economic_links": (
                discovery_summary.get("links_created", 0)
                + discovery_summary.get("links_skipped_existing", 0)
            ),
            "forecasts_inserted": forecasts_inserted,
            "opportunity_markets_scanned": markets_scanned,
            "rankings_inserted": rankings_inserted,
            "opportunities_detected": opportunities_detected,
            "top_opportunity_ticker": top_opportunity_ticker,
            "top_opportunity_score": top_opportunity_score,
            "status": _r4_status(
                fetch_results=fetch_results,
                observations=observations,
                actual_consensus_rows=actual_consensus_rows,
            ),
        },
        "sources": [_fetch_result_payload(result) for result in fetch_results],
        "observations": [_observation_payload(observation) for observation in observations],
        "phase3bd_discovery_summary": discovery_summary,
        "opportunity_report": opportunity_path,
        "recommended_next_action": _r4_recommended_next_action(
            fetch_results=fetch_results,
            observations=observations,
            actual_consensus_rows=actual_consensus_rows,
            opportunities_detected=opportunities_detected,
        ),
    }
    return payload


def write_phase3bd_r3_economic_value_capture_report(
    *,
    session: Session,
    output_dir: Path,
    value_fetcher: ValueFetcher | None = None,
    kalshi_client: KalshiClient | None = None,
    opportunity_output_path: str | Path = "reports/opportunities_economic_v1.md",
    max_series: int = 24,
    markets_per_series: int = 50,
    snapshot_series_limit: int = 8,
    forecast_limit: int = 500,
    opportunity_limit: int = 75,
) -> Phase3BDR3Artifacts:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = run_phase3bd_r3_economic_value_capture(
        session,
        value_fetcher=value_fetcher,
        kalshi_client=kalshi_client,
        opportunity_output_path=opportunity_output_path,
        max_series=max_series,
        markets_per_series=markets_per_series,
        snapshot_series_limit=snapshot_series_limit,
        forecast_limit=forecast_limit,
        opportunity_limit=opportunity_limit,
    )
    json_path = output_dir / "phase3bd_r3_economic_value_capture.json"
    markdown_path = output_dir / "phase3bd_r3_economic_value_capture.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(_markdown(payload), encoding="utf-8")
    return Phase3BDR3Artifacts(json_path=json_path, markdown_path=markdown_path, payload=payload)


def write_phase3bd_r4_verified_consensus_source_report(
    *,
    session: Session,
    output_dir: Path,
    input_file: Path | None = None,
    trading_economics_api_key: str | None = None,
    country: str = "united states",
    days_back: int = 90,
    days_ahead: int = 14,
    min_importance: int = 2,
    value_fetcher: ValueFetcher | None = None,
    kalshi_client: KalshiClient | None = None,
    opportunity_output_path: str | Path = "reports/opportunities_economic_v1.md",
    max_series: int = 24,
    markets_per_series: int = 50,
    snapshot_series_limit: int = 8,
    forecast_limit: int = 500,
    opportunity_limit: int = 75,
) -> Phase3BDR4Artifacts:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = run_phase3bd_r4_verified_consensus_source(
        session,
        input_file=input_file,
        trading_economics_api_key=trading_economics_api_key,
        country=country,
        days_back=days_back,
        days_ahead=days_ahead,
        min_importance=min_importance,
        value_fetcher=value_fetcher,
        kalshi_client=kalshi_client,
        opportunity_output_path=opportunity_output_path,
        max_series=max_series,
        markets_per_series=markets_per_series,
        snapshot_series_limit=snapshot_series_limit,
        forecast_limit=forecast_limit,
        opportunity_limit=opportunity_limit,
    )
    json_path = output_dir / "phase3bd_r4_verified_consensus_source.json"
    markdown_path = output_dir / "phase3bd_r4_verified_consensus_source.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(_r4_markdown(payload), encoding="utf-8")
    return Phase3BDR4Artifacts(json_path=json_path, markdown_path=markdown_path, payload=payload)


def _fetch_bls_values(client: httpx.Client) -> EconomicValueFetchResult:
    try:
        now = utc_now()
        response = client.post(
            BLS_API_URL,
            json={
                "seriesid": [
                    "CUUR0000SA0L1E",
                    "CES0000000001",
                    "LNS14000000",
                ],
                "startyear": str(now.year - 2),
                "endyear": str(now.year),
            },
        )
        response.raise_for_status()
        observations = parse_bls_value_response(response.json())
        return EconomicValueFetchResult(
            source="bls_values_api",
            url=BLS_SOURCE_URL,
            attempted=True,
            succeeded=True,
            observations=observations,
        )
    except Exception as exc:
        return EconomicValueFetchResult(
            source="bls_values_api",
            url=BLS_SOURCE_URL,
            attempted=True,
            succeeded=False,
            observations=[],
            error=str(exc),
        )


def _fetch_trading_economics_consensus(
    client: httpx.Client,
    *,
    api_key: str,
    country: str,
    days_back: int,
    days_ahead: int,
    min_importance: int,
) -> EconomicValueFetchResult:
    now = utc_now()
    start_date = (now - timedelta(days=days_back)).date().isoformat()
    end_date = (now + timedelta(days=days_ahead)).date().isoformat()
    country_slug = quote(country.lower(), safe="")
    url = (
        f"https://api.tradingeconomics.com/calendar/country/{country_slug}/"
        f"{start_date}/{end_date}"
    )
    try:
        response = client.get(
            url,
            params={
                "c": api_key,
                "importance": str(min_importance),
                "values": "true",
                "f": "json",
            },
        )
        response.raise_for_status()
        observations = parse_trading_economics_calendar_values(
            response.json(),
            source_url=TRADING_ECONOMICS_CALENDAR_SCHEMA_URL,
            request_url=url,
        )
        return EconomicValueFetchResult(
            source="trading_economics_calendar_consensus",
            url=TRADING_ECONOMICS_CALENDAR_SCHEMA_URL,
            attempted=True,
            succeeded=True,
            observations=observations,
        )
    except Exception as exc:
        return EconomicValueFetchResult(
            source="trading_economics_calendar_consensus",
            url=TRADING_ECONOMICS_CALENDAR_SCHEMA_URL,
            attempted=True,
            succeeded=False,
            observations=[],
            error=str(exc),
        )


def parse_trading_economics_calendar_values(
    payload: Any,
    *,
    source_url: str = TRADING_ECONOMICS_CALENDAR_SCHEMA_URL,
    request_url: str | None = None,
) -> list[EconomicValueObservation]:
    rows = payload if isinstance(payload, list) else payload.get("events", [])
    if not isinstance(rows, list):
        return []
    observations: list[EconomicValueObservation] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        event_key = _classify_consensus_event(row)
        event_time = parse_datetime(row.get("Date") or row.get("date") or row.get("event_time"))
        forecast_value = _consensus_number(
            row.get("ForecastValue")
            or row.get("forecast_value")
            or row.get("Forecast")
            or row.get("forecast")
            or row.get("Consensus")
            or row.get("consensus")
        )
        if event_key is None or event_time is None or forecast_value is None:
            continue
        actual_value = _consensus_number(row.get("ActualValue") or row.get("Actual"))
        previous_value = _consensus_number(row.get("PreviousValue") or row.get("Previous"))
        title = str(row.get("Event") or row.get("event") or event_key.upper()).strip()
        observations.append(
            EconomicValueObservation(
                event_key=event_key,
                source="trading_economics_calendar_consensus",
                source_url=source_url,
                event_time=event_time,
                category=event_key,
                title=title,
                actual_value=actual_value,
                forecast_value=forecast_value,
                previous_value=previous_value,
                raw_json={
                    "provider": "Trading Economics",
                    "country": row.get("Country"),
                    "category": row.get("Category"),
                    "event": row.get("Event"),
                    "ticker": row.get("Ticker"),
                    "symbol": row.get("Symbol"),
                    "unit": row.get("Unit"),
                    "importance": row.get("Importance"),
                    "te_forecast_value": _consensus_number(row.get("TEForecastValue")),
                    "consensus_value_field": "ForecastValue",
                    "source_url": source_url,
                    "request_url": request_url,
                },
            )
        )
    return observations


def _load_verified_consensus_file(input_file: Path) -> EconomicValueFetchResult:
    try:
        rows = _consensus_file_rows(input_file)
        observations: list[EconomicValueObservation] = []
        errors: list[str] = []
        for index, row in enumerate(rows):
            observation, error = _consensus_file_observation(row)
            if observation is None:
                errors.append(f"row {index}: {error}")
                continue
            observations.append(observation)
        return EconomicValueFetchResult(
            source="verified_consensus_file",
            url=str(input_file),
            attempted=True,
            succeeded=not errors or bool(observations),
            observations=observations,
            error="; ".join(errors[:5]) if errors else None,
        )
    except Exception as exc:
        return EconomicValueFetchResult(
            source="verified_consensus_file",
            url=str(input_file),
            attempted=True,
            succeeded=False,
            observations=[],
            error=str(exc),
        )


def _consensus_file_rows(input_file: Path) -> list[Mapping[str, Any]]:
    suffix = input_file.suffix.lower()
    text = input_file.read_text(encoding="utf-8")
    if suffix == ".csv":
        return [dict(row) for row in csv.DictReader(io.StringIO(text))]
    payload = json.loads(text)
    rows = payload.get("events") if isinstance(payload, Mapping) else payload
    if isinstance(rows, Mapping):
        rows = [rows]
    if not isinstance(rows, list):
        raise ValueError("verified consensus file must contain a list or events list")
    return [row for row in rows if isinstance(row, Mapping)]


def _consensus_file_observation(
    row: Mapping[str, Any],
) -> tuple[EconomicValueObservation | None, str | None]:
    source_url = str(row.get("source_url") or row.get("url") or "").strip()
    if not source_url:
        return None, "source_url is required for verified consensus rows"
    event_time = parse_datetime(
        row.get("event_time")
        or row.get("date")
        or row.get("timestamp")
        or row.get("released_at")
    )
    if event_time is None:
        return None, "event_time/date is required"
    event_key = str(
        row.get("event_key")
        or row.get("key")
        or row.get("category")
        or _classify_consensus_event(row)
        or ""
    )
    event_key = event_key.strip().lower()
    if not event_key:
        return None, "event_key or classifiable event/category is required"
    forecast_value = _consensus_number(
        row.get("forecast_value")
        or row.get("forecast")
        or row.get("consensus_value")
        or row.get("consensus")
    )
    if forecast_value is None:
        return None, "forecast_value/consensus is required"
    source = str(row.get("source") or "verified_consensus_file")
    title = str(row.get("title") or row.get("event") or row.get("Event") or event_key.upper())
    return (
        EconomicValueObservation(
            event_key=event_key,
            source=source,
            source_url=source_url,
            event_time=event_time,
            category=str(row.get("category") or event_key),
            title=title,
            actual_value=_consensus_number(row.get("actual_value") or row.get("actual")),
            forecast_value=forecast_value,
            previous_value=_consensus_number(row.get("previous_value") or row.get("previous")),
            raw_json={
                "provider": row.get("provider") or source,
                "source_url": source_url,
                "verification_status": row.get("verification_status") or "VERIFIED_MANUAL",
                "raw_row": dict(row),
            },
        ),
        None,
    )


def parse_bls_value_response(payload: Mapping[str, Any]) -> list[EconomicValueObservation]:
    series_rows = {
        str(series.get("seriesID")): _bls_monthly_rows(series.get("data") or [])
        for series in ((payload.get("Results") or {}).get("series") or [])
        if isinstance(series, Mapping)
    }
    observations: list[EconomicValueObservation] = []
    cpi = _cpi_yoy_observation(series_rows.get("CUUR0000SA0L1E") or {})
    if cpi is not None:
        observations.append(cpi)
    jobs = _jobs_observation(
        payroll_rows=series_rows.get("CES0000000001") or {},
        unemployment_rows=series_rows.get("LNS14000000") or {},
    )
    if jobs is not None:
        observations.append(jobs)
    return observations


def _fetch_fred_series(
    client: httpx.Client,
    *,
    source: str,
    csv_url: str,
    source_url: str,
    series_id: str,
    event_key: str,
    category: str,
    title: str,
) -> EconomicValueFetchResult:
    try:
        response = client.get(csv_url)
        response.raise_for_status()
        observation = parse_fred_csv_observation(
            response.text,
            source=source,
            source_url=source_url,
            series_id=series_id,
            event_key=event_key,
            category=category,
            title=title,
        )
        return EconomicValueFetchResult(
            source=source,
            url=source_url,
            attempted=True,
            succeeded=True,
            observations=[observation] if observation is not None else [],
        )
    except Exception as exc:
        return EconomicValueFetchResult(
            source=source,
            url=source_url,
            attempted=True,
            succeeded=False,
            observations=[],
            error=str(exc),
        )


def parse_fred_csv_observation(
    csv_text: str,
    *,
    source: str,
    source_url: str,
    series_id: str,
    event_key: str,
    category: str,
    title: str,
) -> EconomicValueObservation | None:
    rows = []
    for row in csv.DictReader(io.StringIO(csv_text)):
        value = _decimal(row.get(series_id))
        date_value = _date(row.get("observation_date"))
        if value is not None and date_value is not None:
            rows.append((date_value, value))
    rows.sort(key=lambda item: item[0])
    if not rows:
        return None
    latest_date, latest_value = rows[-1]
    previous_value = rows[-2][1] if len(rows) > 1 else None
    return EconomicValueObservation(
        event_key=event_key,
        source=source,
        source_url=source_url,
        event_time=latest_date.replace(tzinfo=UTC),
        category=category,
        title=title,
        actual_value=decimal_to_str(latest_value),
        forecast_value=None,
        previous_value=decimal_to_str(previous_value),
        raw_json={
            "provider": "FRED",
            "series_id": series_id,
            "source_url": source_url,
            "consensus_status": "not_available_from_official_source",
            "latest_observation_date": latest_date.date().isoformat(),
        },
    )


def _insert_value_observations(
    session: Session,
    observations: list[EconomicValueObservation],
) -> tuple[int, int]:
    inserted = 0
    skipped_existing = 0
    for observation in observations:
        existing = session.scalar(
            select(EconomicEvent)
            .where(EconomicEvent.event_key == normalize_event_key(observation.event_key))
            .where(EconomicEvent.source == observation.source)
            .where(EconomicEvent.event_time == observation.event_time)
            .where(EconomicEvent.title == observation.title)
            .order_by(EconomicEvent.id.desc())
            .limit(1)
        )
        if existing is not None and _same_values(existing, observation):
            skipped_existing += 1
            continue
        insert_economic_event(
            session,
            event_key=observation.event_key,
            source=observation.source,
            event_time=observation.event_time,
            category=observation.category,
            title=observation.title,
            actual_value=observation.actual_value,
            forecast_value=observation.forecast_value,
            previous_value=observation.previous_value,
            raw_json=dict(observation.raw_json),
        )
        inserted += 1
    return inserted, skipped_existing


def _same_values(existing: EconomicEvent, observation: EconomicValueObservation) -> bool:
    return (
        existing.actual_value == decimal_to_str(observation.actual_value)
        and existing.forecast_value == decimal_to_str(observation.forecast_value)
        and existing.previous_value == decimal_to_str(observation.previous_value)
    )


def _bls_monthly_rows(rows: list[Mapping[str, Any]]) -> dict[tuple[int, int], Decimal]:
    monthly: dict[tuple[int, int], Decimal] = {}
    for row in rows:
        period = str(row.get("period") or "")
        if not period.startswith("M"):
            continue
        year = _int(row.get("year"))
        month = _int(period[1:])
        value = _decimal(row.get("value"))
        if year is None or month is None or value is None:
            continue
        monthly[(year, month)] = value
    return monthly


def _cpi_yoy_observation(
    rows: dict[tuple[int, int], Decimal],
) -> EconomicValueObservation | None:
    latest_key = _latest_key_with_prior_year(rows)
    if latest_key is None:
        return None
    actual = _year_over_year(rows, latest_key)
    previous_key = _previous_month_key(latest_key)
    previous = _year_over_year(rows, previous_key) if previous_key in rows else None
    if actual is None:
        return None
    year, month = latest_key
    return EconomicValueObservation(
        event_key="cpi",
        source="bls_core_cpi_actuals",
        source_url=BLS_SOURCE_URL,
        event_time=_month_period_time(year, month),
        category="cpi",
        title=f"Core CPI year-over-year for {year}-{month:02d}",
        actual_value=decimal_to_str(_quantize(actual, "0.01")),
        forecast_value=None,
        previous_value=decimal_to_str(_quantize(previous, "0.01")),
        raw_json={
            "provider": "BLS",
            "series_id": "CUUR0000SA0L1E",
            "series_name": "CPI-U all items less food and energy",
            "calculation": "year_over_year_percent_change",
            "consensus_status": "not_available_from_official_source",
            "period": f"{year}-{month:02d}",
            "source_url": BLS_SOURCE_URL,
        },
    )


def _jobs_observation(
    *,
    payroll_rows: dict[tuple[int, int], Decimal],
    unemployment_rows: dict[tuple[int, int], Decimal],
) -> EconomicValueObservation | None:
    latest_key = max(payroll_rows) if payroll_rows else None
    if latest_key is None:
        return None
    previous_key = _previous_month_key(latest_key)
    prior_key = _previous_month_key(previous_key)
    if previous_key not in payroll_rows:
        return None
    actual_change = payroll_rows[latest_key] - payroll_rows[previous_key]
    previous_change = (
        payroll_rows[previous_key] - payroll_rows[prior_key]
        if prior_key in payroll_rows
        else None
    )
    unemployment_rate = unemployment_rows.get(latest_key)
    year, month = latest_key
    return EconomicValueObservation(
        event_key="jobs",
        source="bls_payroll_actuals",
        source_url=BLS_SOURCE_URL,
        event_time=_month_period_time(year, month),
        category="jobs",
        title=f"Nonfarm payroll monthly change for {year}-{month:02d}",
        actual_value=decimal_to_str(actual_change),
        forecast_value=None,
        previous_value=decimal_to_str(previous_change),
        raw_json={
            "provider": "BLS",
            "payroll_series_id": "CES0000000001",
            "unemployment_series_id": "LNS14000000",
            "payroll_units": "thousands_of_jobs",
            "calculation": "latest_payroll_level_minus_prior_month_level",
            "unemployment_rate": decimal_to_str(unemployment_rate),
            "consensus_status": "not_available_from_official_source",
            "period": f"{year}-{month:02d}",
            "source_url": BLS_SOURCE_URL,
        },
    )


def _year_over_year(
    rows: dict[tuple[int, int], Decimal],
    key: tuple[int, int],
) -> Decimal | None:
    prior_key = (key[0] - 1, key[1])
    if key not in rows or prior_key not in rows or rows[prior_key] == 0:
        return None
    return ((rows[key] / rows[prior_key]) - Decimal("1")) * Decimal("100")


def _latest_key_with_prior_year(rows: dict[tuple[int, int], Decimal]) -> tuple[int, int] | None:
    for key in sorted(rows, reverse=True):
        if (key[0] - 1, key[1]) in rows:
            return key
    return None


def _previous_month_key(key: tuple[int, int]) -> tuple[int, int]:
    year, month = key
    if month == 1:
        return year - 1, 12
    return year, month - 1


def _month_period_time(year: int, month: int) -> datetime:
    return datetime(year, month, 1, 12, 30, tzinfo=UTC)


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


def _int(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _decimal(value: Any) -> Decimal | None:
    if value is None or str(value).strip() in {"", "."}:
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


def _consensus_number(value: Any) -> str | None:
    if value is None or str(value).strip() in {"", "-", "None", "null"}:
        return None
    if isinstance(value, (int, float, Decimal)):
        return decimal_to_str(value)
    text = str(value).strip().replace(",", "")
    multiplier = Decimal("1")
    if text.upper().endswith("K"):
        text = text[:-1]
    elif text.upper().endswith("M"):
        text = text[:-1]
        multiplier = Decimal("1000")
    text = text.replace("%", "").replace("+", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return decimal_to_str(Decimal(match.group(0)) * multiplier)
    except (InvalidOperation, ValueError):
        return None


def _classify_consensus_event(row: Mapping[str, Any]) -> str | None:
    text = " ".join(
        str(row.get(key) or "")
        for key in (
            "event_key",
            "Event",
            "event",
            "Category",
            "category",
            "Ticker",
            "ticker",
            "Symbol",
            "symbol",
            "title",
        )
    ).casefold()
    if any(term in text for term in ("core cpi", "consumer price", "inflation", "cpi")):
        return "cpi"
    if any(term in text for term in ("non farm", "nonfarm", "payroll", "unemployment", "jobs")):
        return "jobs"
    if any(term in text for term in ("fomc", "interest rate", "fed funds", "federal reserve")):
        return "fed"
    if any(term in text for term in ("gdp", "gross domestic product")):
        return "gdp"
    return None


def _env_api_key() -> str | None:
    for name in TRADING_ECONOMICS_ENV_NAMES:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _quantize(value: Decimal | None, places: str) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(Decimal(places))


def _observation_payload(observation: EconomicValueObservation) -> dict[str, Any]:
    payload = asdict(observation)
    payload["event_time"] = observation.event_time.isoformat()
    payload["raw_json"] = dict(observation.raw_json)
    return payload


def _fetch_result_payload(result: EconomicValueFetchResult) -> dict[str, Any]:
    return {
        "source": result.source,
        "url": result.url,
        "attempted": result.attempted,
        "succeeded": result.succeeded,
        "observations": len(result.observations),
        "error": result.error,
    }


def _status(
    fetch_results: list[EconomicValueFetchResult],
    observations: list[EconomicValueObservation],
    consensus_missing: int,
) -> str:
    if not any(result.succeeded for result in fetch_results):
        return "BLOCKED_BY_SOURCE_FETCH"
    if not observations:
        return "WAITING_FOR_VALUE_OBSERVATIONS"
    if consensus_missing == len(observations):
        return "ACTIVE_ACTUALS_ONLY_CONSENSUS_MISSING"
    return "ACTIVE_WITH_ACTUAL_AND_CONSENSUS"


def _r4_status(
    *,
    fetch_results: list[EconomicValueFetchResult],
    observations: list[EconomicValueObservation],
    actual_consensus_rows: int,
) -> str:
    if not any(result.attempted for result in fetch_results):
        return "BLOCKED_BY_MISSING_CONSENSUS_SOURCE"
    if not any(result.succeeded for result in fetch_results):
        return "BLOCKED_BY_CONSENSUS_FETCH"
    if not observations:
        return "WAITING_FOR_VERIFIED_CONSENSUS_ROWS"
    if actual_consensus_rows == 0:
        return "CONSENSUS_READY_WAITING_FOR_ACTUALS"
    return "ACTIVE_WITH_VERIFIED_CONSENSUS"


def _recommended_next_action(
    *,
    observations: list[EconomicValueObservation],
    consensus_missing: int,
    opportunities_detected: int,
) -> str:
    if opportunities_detected > 0:
        return "Review economic_v1 opportunity cards; keep paper/read-only gates in place."
    if observations and consensus_missing == len(observations):
        return (
            "Official actual/previous values are captured. Add a verified consensus "
            "source before using actual-vs-consensus surprises."
        )
    return "Keep Phase 3BD-R3 in the safe refresh loop and rerank when new values arrive."


def _r4_recommended_next_action(
    *,
    fetch_results: list[EconomicValueFetchResult],
    observations: list[EconomicValueObservation],
    actual_consensus_rows: int,
    opportunities_detected: int,
) -> str:
    if not any(result.attempted for result in fetch_results):
        return (
            "Set TRADING_ECONOMICS_API_KEY or pass --input-file with verified "
            "source_url, actual, forecast/consensus, and previous values."
        )
    if not any(result.succeeded for result in fetch_results):
        return "Fix the verified consensus source/API credentials, then rerun Phase 3BD-R4."
    if not observations:
        return (
            "No CPI/jobs/GDP/Fed consensus rows matched; widen date range or review "
            "source mapping."
        )
    if actual_consensus_rows == 0:
        return (
            "Consensus rows are loaded; wait for actual releases or add actual "
            "values before trading signals."
        )
    if opportunities_detected > 0:
        return "Review economic_v1 opportunities; execution remains paper/read-only."
    return (
        "Verified actual-vs-consensus signals are active; continue refreshing around "
        "release windows."
    )


def _markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 3BD-R3 Economic Actual/Consensus Value Capture",
        "",
        f"Generated: {payload['generated_at']}",
        "",
        "Mode: PAPER / READ ONLY. Live/demo execution remains blocked.",
        "",
        "## Summary",
        "",
        f"- Status: {summary['status']}",
        f"- Sources succeeded: {summary['sources_succeeded']} / {summary['sources_attempted']}",
        f"- Value observations seen: {summary['value_observations_seen']}",
        f"- Value observations inserted: {summary['value_observations_inserted']}",
        f"- Existing observations skipped: {summary['value_observations_skipped_existing']}",
        f"- Actual observations: {summary['actual_value_observations']}",
        f"- Consensus observations: {summary['consensus_value_observations']}",
        "- Consensus missing from official sources: "
        f"{summary['consensus_missing_from_official_sources']}",
        f"- Economic forecasts inserted: {summary['forecasts_inserted']}",
        f"- Rankings inserted: {summary['rankings_inserted']}",
        f"- Opportunities detected: {summary['opportunities_detected']}",
        "",
        "## Value Observations",
        "",
        "| Key | Time | Actual | Consensus | Previous | Source | Title |",
        "| --- | --- | ---: | ---: | ---: | --- | --- |",
    ]
    for observation in payload["observations"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    observation["event_key"],
                    observation["event_time"],
                    observation.get("actual_value") or "n/a",
                    observation.get("forecast_value") or "missing",
                    observation.get("previous_value") or "n/a",
                    observation["source"],
                    observation["title"].replace("|", "\\|"),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Sources",
            "",
            "| Source | Observations | Status | URL |",
            "| --- | ---: | --- | --- |",
        ]
    )
    for source in payload["sources"]:
        status = "ok" if source["succeeded"] else f"error: {source['error']}"
        lines.append(
            f"| {source['source']} | {source['observations']} | {status} | {source['url']} |"
        )
    lines.extend(
        [
            "",
            "## Next Action",
            "",
            payload["recommended_next_action"],
            "",
        ]
    )
    return "\n".join(lines)


def _r4_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 3BD-R4 Verified Consensus Source Integration",
        "",
        f"Generated: {payload['generated_at']}",
        "",
        "Mode: PAPER / READ ONLY. Live/demo execution remains blocked.",
        "",
        "## Summary",
        "",
        f"- Status: {summary['status']}",
        f"- Sources attempted: {summary['sources_attempted']}",
        f"- Sources succeeded: {summary['sources_succeeded']}",
        f"- Consensus observations: {summary['consensus_value_observations']}",
        f"- Actual + consensus observations: {summary['actual_and_consensus_observations']}",
        f"- Consensus-only observations: {summary['consensus_only_observations']}",
        f"- Value observations inserted: {summary['value_observations_inserted']}",
        f"- Existing observations skipped: {summary['value_observations_skipped_existing']}",
        f"- Features inserted: {summary['features_inserted']}",
        f"- Economic forecasts inserted: {summary['forecasts_inserted']}",
        f"- Rankings inserted: {summary['rankings_inserted']}",
        f"- Opportunities detected: {summary['opportunities_detected']}",
        "",
        "## Value Observations",
        "",
        "| Key | Time | Actual | Consensus | Previous | Source | URL | Title |",
        "| --- | --- | ---: | ---: | ---: | --- | --- | --- |",
    ]
    for observation in payload["observations"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    observation["event_key"],
                    observation["event_time"],
                    observation.get("actual_value") or "n/a",
                    observation.get("forecast_value") or "missing",
                    observation.get("previous_value") or "n/a",
                    observation["source"],
                    observation["source_url"],
                    observation["title"].replace("|", "\\|"),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Sources",
            "",
            "| Source | Attempted | Observations | Status | URL |",
            "| --- | --- | ---: | --- | --- |",
        ]
    )
    for source in payload["sources"]:
        status = "ok" if source["succeeded"] else f"error: {source['error']}"
        lines.append(
            "| "
            + " | ".join(
                [
                    source["source"],
                    str(source["attempted"]),
                    str(source["observations"]),
                    status,
                    source["url"],
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Next Action",
            "",
            payload["recommended_next_action"],
            "",
        ]
    )
    return "\n".join(lines)
