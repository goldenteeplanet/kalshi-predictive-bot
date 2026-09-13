"""Bounded public capture for the existing weather preparation path."""

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.overnight_paper.discovery import PublicArchive
from kalshi_predictor.overnight_paper.qualification import EvidenceReference
from kalshi_predictor.overnight_paper.source_health import MAX_FORECAST_AGE_SECONDS, aware


def collect_weather_preparation(
    archive_root: Path, *, ticker: str
) -> tuple[EvidenceReference, ...]:
    """Fetch exact public originals; skip the book when either provider clock fails.

    This capture is diagnostic and cannot certify a rule or model. It uses the
    existing paced collector, its global 429 stop, and no account configuration.
    """
    if not re.fullmatch(r"KXTEMPNYCH-[A-Za-z0-9_.-]+", ticker):
        raise ValueError("SUPPORTED_EXACT_WEATHER_TICKER_REQUIRED")
    public = PublicArchive(archive_root, max_requests=7, seconds=60)
    sources: list[EvidenceReference] = []

    def get(path: str) -> Any:
        body = public.get(path)
        receipt = public.receipts[-1]
        envelope = {
            "url": receipt["url"],
            "received_at": receipt["received_at"],
            "body": body,
            "original_body_sha256": receipt["sha256"],
        }
        payload = json.dumps(
            envelope, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        file = archive_root / f"envelope-{len(sources) + 1:03d}.json"
        file.write_bytes(payload)
        sources.append(EvidenceReference(str(file), hashlib.sha256(payload).hexdigest(), payload))
        return body

    try:
        market = get("/markets/" + ticker)["market"]
        if market["ticker"] != ticker or market["status"] not in {"open", "active"}:
            raise ValueError("CURRENT_EXACT_MARKET_REQUIRED")
        event = get("/events/" + market["event_ticker"])["event"]
        if event["series_ticker"] != "KXTEMPNYCH":
            raise ValueError("SUPPORTED_WEATHER_SERIES_REQUIRED")
        get("/series/KXTEMPNYCH")
        station = get("https://api.weather.gov/stations/KNYC")
        if station["properties"]["stationIdentifier"] != "KNYC":
            raise ValueError("STATION_IDENTITY_MISMATCH")
        lon, lat = station["geometry"]["coordinates"][:2]
        point = get(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}")
        hourly = get(point["properties"]["forecastHourly"])["properties"]
        now = datetime.now(UTC)
        clocks = [hourly.get(key) for key in ("updateTime", "generatedAt")]
        if all(clocks) and all(
            0 <= (now - aware(value)).total_seconds() <= MAX_FORECAST_AGE_SECONDS
            for value in clocks
        ):
            get("/markets/" + ticker + "/orderbook")
        return tuple(sources)
    finally:
        (archive_root / "source_bundle.json").write_text(
            json.dumps(
                [
                    {
                        "artifact": item.artifact,
                        "sha256": item.sha256,
                        "original_utf8": item.payload.decode("utf-8"),
                    }
                    for item in sources
                ],
                indent=2,
            ),
            encoding="utf-8",
        )
