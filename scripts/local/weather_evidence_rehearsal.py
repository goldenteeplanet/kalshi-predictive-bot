"""Bounded public evidence archive. No application, credential, or order imports.

The standalone SQLite schema is diagnostic evidence, never a trading database.
NOAA grid forecasts are analytical context, not TWC/Synoptic settlement values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener

BASE = "https://external-api.kalshi.com/trade-api/v2"
SERIES = "KXTEMPNYCH"
MAX_FORECAST_AGE_SECONDS = 1800  # Existing phase_3u default; never promote older data.
MAX_REQUESTS = 25
TABLES = {"sources", "forecasts", "settlement_lineage"}


def authorizer(action, table, *rest):
    if action == sqlite3.SQLITE_INSERT:
        return sqlite3.SQLITE_OK if table in TABLES else sqlite3.SQLITE_DENY
    if action in (
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_TRANSACTION,
    ):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def now() -> datetime:
    return datetime.now(UTC)


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("TIMEZONE_REQUIRED")
    return parsed.astimezone(UTC)


def allowed(url: str) -> bool:
    return url in {
        f"{BASE}/series/{SERIES}",
        f"{BASE}/markets?series_ticker={SERIES}&status=open&limit=100",
        "https://api.weather.gov/stations/KNYC",
        "https://weather.com/kalshi",
    } or bool(
        re.fullmatch(re.escape(BASE) + r"/events/KXTEMPNYCH-[A-Za-z0-9.-]+", url)
        or re.fullmatch(r"https://api\.weather\.gov/points/-?\d+\.\d+,-?\d+\.\d+", url)
        or re.fullmatch(r"https://api\.weather\.gov/gridpoints/OKX/\d+,\d+/forecast/hourly", url)
    )


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError("REDIRECT_REFUSED")


def fetch(url: str) -> None:
    if not allowed(url):
        raise ValueError("ENDPOINT_REFUSED")
    request = Request(url, headers={"User-Agent": "KalshiObservationEvidence/1.0"}, method="GET")
    with build_opener(NoRedirect()).open(request, timeout=15) as response:
        body = response.read(1_000_001)
        if len(body) > 1_000_000:
            raise ValueError("RESPONSE_TOO_LARGE")
        print(json.dumps({"body": body.decode(), "server_date": response.headers.get("Date")}))


def forecast_status(issued: str, valid: str, reference: datetime) -> str:
    try:
        age = (reference - timestamp(issued)).total_seconds()
        valid_time = timestamp(valid)
    except (ValueError, TypeError, AttributeError):
        return "TIMESTAMP_INVALID"
    if age < 0:
        return "FUTURE_ISSUE_TIME"
    if age > MAX_FORECAST_AGE_SECONDS:
        return "FORECAST_STALE"
    if valid_time + timedelta(hours=1) <= reference:
        return "VALID_PERIOD_EXPIRED"
    return "ANALYTICAL_ONLY"


def lineage(market: dict, event: dict, series: dict) -> dict:
    exact = (
        event.get("event_ticker") == market.get("event_ticker")
        and bool(market.get("event_ticker"))
        and event.get("series_ticker") == series.get("ticker") == SERIES
        and market.get("series_ticker", SERIES) == SERIES
        and str(market.get("ticker", "")).startswith(SERIES + "-")
    )
    rules = market.get("rules_primary", "")
    sources = series.get("settlement_sources", [])
    twc = any(s.get("name") == "The Weather Company" for s in sources)
    station = "KNYC" if "(for coordinates KNYC)" in rules else None
    supported = exact and twc and "The Weather Company" in rules and station == "KNYC"
    return {
        "ticker": market["ticker"],
        "event_ticker": market.get("event_ticker"),
        "series_ticker": series.get("ticker"),
        "catalog_identity_verified": exact,
        "station": station,
        "rule_source": "The Weather Company" if supported else None,
        "settlement_sources": sources,
        "rules_primary": rules,
        "rules_secondary": market.get("rules_secondary"),
        "source_transition_notice": series.get("product_metadata", {}).get("important_info"),
        "contract_terms_url": series.get("contract_terms_url"),
        "close_time": market.get("close_time"),
        "occurrence_datetime": market.get("occurrence_datetime"),
        "status": "RULE_LINEAGE_CAPTURED" if supported else "IDENTITY_OR_SOURCE_CONFLICT",
        "settlement_value_verified": False,
        "blocker": "Final provider value and effective source mapping require certification",
    }


def run(root: Path) -> dict:
    root = root.absolute()
    if root.exists() or "onedrive" in str(root).lower():
        raise ValueError("NEW_UNSYNCED_DIRECTORY_REQUIRED")
    if any(
        p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in (root, *root.parents)
    ):
        raise ValueError("LINKED_PATH_REFUSED")
    root.mkdir(parents=True)
    started = now()
    deadline = time.monotonic() + 570
    db = sqlite3.connect(root / "evidence.db")
    db.executescript(
        "CREATE TABLE sources(id INTEGER PRIMARY KEY, url TEXT, received_at TEXT, "
        "sha256 TEXT, body BLOB, error TEXT);"
        "CREATE TABLE forecasts(id INTEGER PRIMARY KEY, source_id INTEGER, "
        "issued_at TEXT, valid_at TEXT, status TEXT, payload TEXT);"
        "CREATE TABLE settlement_lineage(ticker TEXT PRIMARY KEY, payload TEXT);"
    )
    db.set_authorizer(authorizer)
    requests = []
    blockers = []

    def get(url: str, *, raw: bool = False):
        remaining = deadline - time.monotonic()
        if not allowed(url) or len(requests) >= MAX_REQUESTS or remaining <= 0:
            raise RuntimeError("REQUEST_SCOPE_OR_BUDGET_REFUSED")
        entry = {"url": url, "method": "GET", "started_at": now().isoformat()}
        requests.append(entry)
        body = b""
        try:
            output = subprocess.run(
                [sys.executable, "-I", str(Path(__file__).resolve()), "--get", url],
                capture_output=True,
                check=True,
                timeout=min(15, remaining),
                env={
                    k: v
                    for k, v in os.environ.items()
                    if k.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"}
                },
            )
            envelope = json.loads(output.stdout)
            body = envelope["body"].encode()
            entry.update(status=200, server_date=envelope["server_date"])
            return body.decode() if raw else json.loads(body)
        except Exception as exc:
            entry["error"] = str(exc)
            if isinstance(exc, subprocess.CalledProcessError):
                entry["error"] += ": " + exc.stderr.decode(errors="replace")[-1200:]
            raise
        finally:
            entry.update(received_at=now().isoformat(), sha256=hashlib.sha256(body).hexdigest())
            entry["id"] = db.execute(
                "INSERT INTO sources(url, received_at, sha256, body, error) VALUES(?,?,?,?,?)",
                (url, entry["received_at"], entry["sha256"], body, entry.get("error")),
            ).lastrowid
            db.commit()

    rows = []
    try:
        series = get(f"{BASE}/series/{SERIES}")["series"]
        markets = get(f"{BASE}/markets?series_ticker={SERIES}&status=open&limit=100")["markets"]
        selected = sorted(
            (
                m
                for m in markets
                if m.get("status") == "active"
                and timestamp(m["close_time"]) >= now() + timedelta(minutes=15)
            ),
            key=lambda m: m["ticker"],
        )[:3]
        if not selected:
            raise RuntimeError("NO_ELIGIBLE_MARKETS_IN_BOUNDED_PAGE")
        events = {}
        for market in selected:
            event_id = market["event_ticker"]
            if event_id not in events:
                events[event_id] = get(f"{BASE}/events/{event_id}")["event"]
            row = lineage(market, events[event_id], series)
            row["source_ids"] = [r["id"] for r in requests]
            rows.append(row)
            db.execute(
                "INSERT INTO settlement_lineage VALUES(?,?)", (row["ticker"], json.dumps(row))
            )
        db.commit()
        if not all(r["status"] == "RULE_LINEAGE_CAPTURED" for r in rows):
            raise RuntimeError("IDENTITY_OR_SOURCE_CONFLICT")
        station = get("https://api.weather.gov/stations/KNYC")
        if station["properties"]["stationIdentifier"] != "KNYC":
            raise RuntimeError("STATION_IDENTITY_MISMATCH")
        lon, lat = station["geometry"]["coordinates"][:2]
        points = get(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}")
        forecast = get(points["properties"]["forecastHourly"])
        source_id = requests[-1]["id"]
        properties = forecast["properties"]
        issued = properties.get("generatedAt") or properties.get("updateTime")
        periods = properties.get("periods", [])[:24]
        if not periods:
            blockers.append("FORECAST_PERIODS_MISSING")
        for period in periods:
            status = forecast_status(issued, period.get("startTime"), now())
            payload = {
                "provider": "NOAA/NWS",
                "station_context": "KNYC",
                "role": "ANALYTICAL_GRID_FORECAST_NOT_SETTLEMENT",
                "period": period,
                "provider_update_time": properties.get("updateTime"),
                "provider_update_status": forecast_status(
                    properties.get("updateTime"), period.get("startTime"), now()
                ),
                "source_ids": [r["id"] for r in requests],
            }
            db.execute(
                "INSERT INTO forecasts(source_id,issued_at,valid_at,status,payload) "
                "VALUES(?,?,?,?,?)",
                (source_id, issued, period.get("startTime"), status, json.dumps(payload)),
            )
        db.commit()
    except Exception as exc:
        blockers.append(str(exc))
    else:
        try:
            get("https://weather.com/kalshi", raw=True)
        except Exception:
            blockers.append("PUBLIC_SETTLEMENT_PAGE_UNAVAILABLE")
    blockers.extend(
        [
            "FINAL_SETTLEMENT_VALUE_UNVERIFIED",
            "SOURCE_TRANSITION_NOT_CERTIFIED",
            "EXECUTABLE_BOOK_AND_24_CYCLE_SOAK_NOT_ESTABLISHED",
        ]
    )
    counts = {
        name: db.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
        for name in ("sources", "forecasts", "settlement_lineage")
    }
    states = dict(db.execute("SELECT status,count(*) FROM forecasts GROUP BY status"))
    blockers.extend(state for state in states if state != "ANALYTICAL_ONLY")
    if not counts["forecasts"]:
        blockers.append("FORECAST_EVIDENCE_MISSING")
    hashes_verified = all(
        hashlib.sha256(body).hexdigest() == digest
        for body, digest in db.execute("SELECT body,sha256 FROM sources")
    )
    db.set_authorizer(None)
    integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    actual_tables = {
        row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert actual_tables == TABLES and hashes_verified and integrity == "ok"
    db.close()
    result = {
        "started_at": started.isoformat(),
        "finished_at": now().isoformat(),
        "database": str(root / "evidence.db"),
        "database_role": "DIAGNOSTIC_ONLY",
        "counts": counts,
        "forecast_states": states,
        "lineage": rows,
        "blockers": blockers,
        "trading_readiness": "BLOCKED",
        "orders_created": 0,
        "execution_enabled": False,
        "thresholds_changed": False,
        "integrity": integrity,
        "source_hashes_verified": hashes_verified,
        "tables": sorted(actual_tables),
        "requests": requests,
        "database_sha256": hashlib.sha256((root / "evidence.db").read_bytes()).hexdigest(),
        "limits": {
            "requests": 25,
            "request_seconds": 15,
            "total_seconds": 570,
            "markets": 3,
            "response_bytes": 1_000_000,
            "forecast_max_age_seconds": MAX_FORECAST_AGE_SECONDS,
        },
    }
    (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--get")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.get:
        fetch(args.get)
    elif args.root and args.worker:
        print(json.dumps(run(args.root), indent=2))
    elif args.root:
        subprocess.run(
            [
                sys.executable,
                "-I",
                str(Path(__file__).resolve()),
                "--worker",
                "--root",
                str(args.root),
            ],
            check=True,
            timeout=600,
        )
    else:
        parser.error("--root required")
