#!/usr/bin/env python3
"""Link exact supported weather series and refresh only discovered NOAA locations."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, or_, select

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import Market, WeatherMarketLink
from kalshi_predictor.weather.linker import link_weather_markets


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--series", default="KXTEMPNYCH,KXRAINAUSM,KXRAINSTPM")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--per-command-timeout", type=int, default=45)
    parser.add_argument("--feature-limit", type=int, default=200)
    args = parser.parse_args()
    if args.feature_limit < 1:
        parser.error("--feature-limit must be positive")
    series = tuple(
        dict.fromkeys(
            value.strip() for value in args.series.split(",") if value.strip()
        )
    )
    engine = init_db()
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        tickers = list(
            session.scalars(
                select(Market.ticker)
                .where(
                    or_(
                        Market.series_ticker.in_(series),
                        *[Market.ticker.like(f"{value}-%") for value in series],
                        *[Market.event_ticker.like(f"{value}-%") for value in series],
                    ),
                    func.lower(func.coalesce(Market.status, "")).in_(("active", "open")),
                    or_(Market.close_time.is_(None), Market.close_time > datetime.now(UTC)),
                )
                .order_by(Market.close_time, Market.ticker)
                .limit(300)
            )
        )
        link_result = link_weather_markets(session, tickers=tickers, limit=300)
        session.commit()
        locations = _supported_locations(session, tickers)
    engine.dispose()

    commands: list[dict[str, object]] = []
    executable = Path(sys.executable).with_name("kalshi-bot")
    for location in locations:
        for command in (
            [str(executable), "ingest-weather", "--location-key", location],
            [
                str(executable),
                "build-weather-features",
                "--location-key",
                location,
                "--limit",
                str(args.feature_limit),
            ],
        ):
            try:
                result = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=args.per_command_timeout,
                )
                exit_code = result.returncode
                stdout = result.stdout[-2000:]
                stderr = result.stderr[-2000:]
            except subprocess.TimeoutExpired as error:
                exit_code = 124
                stdout = str(error.stdout or "")[-2000:]
                stderr = str(error.stderr or "")[-2000:]
            commands.append(
                {
                    "location_key": location,
                    "command": command[1],
                    "exit_code": exit_code,
                    "stdout": stdout,
                    "stderr": stderr,
                }
            )
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "series": series,
        "active_exact_tickers": tickers,
        "active_exact_ticker_count": len(tickers),
        "link_result": {
            "markets_scanned": link_result.markets_scanned,
            "links_created": link_result.links_created,
            "by_location_key": link_result.by_location_key,
            "excluded_by_classification": link_result.excluded_by_classification,
            "existing_links_classified": link_result.existing_links_classified,
        },
        "supported_locations": locations,
        "feature_limit": args.feature_limit,
        "commands": commands,
        "status": (
            "COMPLETE"
            if all(row["exit_code"] == 0 for row in commands)
            else "COMPLETE_WITH_ERRORS"
        ),
    }
    _write_atomic(args.output, payload)
    print(json.dumps(payload))
    return 0 if payload["status"] == "COMPLETE" else 1


def _supported_locations(session, tickers: list[str]) -> list[str]:
    if not tickers:
        return []
    rows = session.scalars(
        select(WeatherMarketLink)
        .where(WeatherMarketLink.ticker.in_(tickers))
        .order_by(WeatherMarketLink.id.desc())
    )
    latest: dict[str, WeatherMarketLink] = {}
    for row in rows:
        latest.setdefault(row.ticker, row)
    return sorted(
        {
            row.location_key
            for row in latest.values()
            if decode_json(row.raw_json).get("point_forecast_eligible") is True
        }
    )


def _write_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
