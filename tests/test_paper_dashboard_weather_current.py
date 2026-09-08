"""Latest same-ledger weather originals override historical diagnostics."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from kalshi_predictor.overnight_paper.dashboard import render, snapshot
from kalshi_predictor.overnight_paper.qualification import decision_fingerprint


@pytest.fixture
def evidence(tmp_path):
    path = tmp_path / "paper.db"
    now = datetime.now(UTC) - timedelta(seconds=2)
    ticker = "KXTEMPNYCH-26SEP0810-T70"
    base = "https://external-api.kalshi.com/trade-api/v2"
    hourly = "https://api.weather.gov/gridpoints/OKX/34,45/forecast/hourly"
    sources = []
    for url, body in (
        (base + "/markets/" + ticker, {"market": {"ticker": ticker}}),
        ("https://api.weather.gov/stations/KNYC", {"properties": {"stationIdentifier": "KNYC"}}),
        (
            "https://api.weather.gov/points/40.7833,-73.9667",
            {"properties": {"forecastHourly": hourly}},
        ),
        (
            hourly,
            {
                "properties": {
                    "generatedAt": (now - timedelta(minutes=10)).isoformat(),
                    "updateTime": (now - timedelta(hours=3)).isoformat(),
                }
            },
        ),
    ):
        raw = json.dumps({"url": url, "body": body, "received_at": now.isoformat()})
        sources.append(
            {
                "artifact": "synthetic",
                "sha256": hashlib.sha256(raw.encode()).hexdigest(),
                "original_utf8": raw,
            }
        )
    request = {"ticker": ticker, "source_hashes": [s["sha256"] for s in sources]}
    preparation = dict(
        kind="PAPER_RELEASE_PREPARATION",
        request=request,
        request_id=decision_fingerprint(request),
        started_at=now.isoformat(),
        finished_at=now.isoformat(),
        state="REJECTED",
        blockers=["PROVIDER_CLOCK_STALE"],
        original_sources=sources,
    )
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE overnight_shadow(id,event_ticker,payload,evaluation_json,paper_order_id);"
            "CREATE TABLE overnight_history(event_ticker);"
            "CREATE TABLE overnight_sprint_cycles(id PRIMARY KEY,captured_at,payload);"
            "CREATE TABLE paper_orders(id,ticker,side,quantity,status,limit_price,model_name);"
        )
        for key, kind in (
            ("old-weather", "WEATHER_DIAGNOSTIC"),
            ("old-crypto", "CRYPTO_FINAL_DIAGNOSTIC"),
        ):
            db.execute(
                "INSERT INTO overnight_sprint_cycles VALUES(?,?,?)",
                (
                    key,
                    (now - timedelta(days=1)).isoformat(),
                    json.dumps(
                        {
                            "kind": kind,
                            "result": {"forecast_updated_at": "2026-09-07T18:19:00Z"},
                            "examples": [{"synthetic": True}],
                        }
                    ),
                ),
            )
        db.execute(
            "INSERT INTO overnight_sprint_cycles VALUES(?,?,?)",
            (
                "weather-preparation:test",
                now.isoformat(),
                json.dumps(preparation),
            ),
        )
    return path, now, preparation


def test_new_blocked_preparation_overrides_old_diagnostics_without_writes(evidence):
    path, now, preparation = evidence
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    view = snapshot(path)
    assert view["first_blocker"] == "PROVIDER_CLOCK_STALE"
    assert view["weather_provider_updated_at"] == (now - timedelta(hours=3)).isoformat()
    assert view["weather_source_state"] == "STALE"
    assert view["last_capture_at"] == now.isoformat()
    assert view["capture_state"] == "CURRENT_CAPTURE"
    assert view["qualification_current"] is False and view["runtime_state"] == "UNVERIFIED"
    assert len(view["historical_diagnostics"]) == 2
    assert "INDEPENDENT_CF_SOURCE_REPRODUCTION_MISSING" not in view["blockers"]
    assert view["independent_final_reproductions"] is None
    assert "Historical diagnostics" in render(view)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("mutation", ["hash", "ticker", "request", "forecast_identity"])
def test_malformed_newest_originals_fail_closed_without_fallback(evidence, mutation):
    path, now, preparation = evidence
    if mutation == "request":
        preparation["request_id"] = "0" * 64
    else:
        source = preparation["original_sources"][0 if mutation != "forecast_identity" else 2]
        row = json.loads(source["original_utf8"])
        if mutation == "forecast_identity":
            row["body"]["properties"]["forecastHourly"] = "https://example.invalid/forecast/hourly"
        else:
            row["body"]["market"]["ticker"] = "UNRELATED"
        source["original_utf8"] = json.dumps(row)
        if mutation != "hash":
            source["sha256"] = hashlib.sha256(source["original_utf8"].encode()).hexdigest()
            preparation["request"]["source_hashes"] = [
                s["sha256"] for s in preparation["original_sources"]
            ]
            preparation["request_id"] = decision_fingerprint(preparation["request"])
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE overnight_sprint_cycles SET payload=? WHERE id='weather-preparation:test'",
            (json.dumps(preparation),),
        )
    before = path.read_bytes()
    view = snapshot(path)
    assert view["first_blocker"] == "PAPER_DASHBOARD_EVIDENCE_INVALID"
    assert view["weather_provider_updated_at"] is None and view["paper_mode"] == "UNVERIFIED"
    assert path.read_bytes() == before


def test_old_preparation_retains_clocks_but_is_explicitly_historical(evidence):
    path, now, preparation = evidence
    earlier = now - timedelta(minutes=5)
    preparation.update(started_at=earlier.isoformat(), finished_at=earlier.isoformat())
    for source in preparation["original_sources"]:
        row = json.loads(source["original_utf8"])
        row["received_at"] = earlier.isoformat()
        source["original_utf8"] = json.dumps(row)
        source["sha256"] = hashlib.sha256(source["original_utf8"].encode()).hexdigest()
    preparation["request"]["source_hashes"] = [s["sha256"] for s in preparation["original_sources"]]
    preparation["request_id"] = decision_fingerprint(preparation["request"])
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE overnight_sprint_cycles SET captured_at=?,payload=? "
            "WHERE id='weather-preparation:test'",
            (earlier.isoformat(), json.dumps(preparation)),
        )
    view = snapshot(path)
    assert view["capture_state"] == view["weather_evidence_state"] == "HISTORICAL"
    assert view["first_blocker"] == "PROVIDER_CLOCK_STALE"
    assert view["fast_candidates_available"] is None


def test_new_driver_binds_same_ledger_preparation_and_blocker(evidence):
    path, now, preparation = evidence
    driver = dict(
        kind="PAPER_WEATHER_DRIVER_V1",
        generation="test",
        ticker=preparation["request"]["ticker"],
        database_id="test-ledger",
        database_path=str(path.resolve()),
        preparation_checkpoint="weather-preparation:test",
        preparation_state="REJECTED",
        assembly_blockers=["PROVIDER_CLOCK_STALE"],
        original_sources=preparation["original_sources"],
    )
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO overnight_sprint_cycles VALUES(?,?,?)",
            (
                "authorization-baseline:test-ledger",
                now.isoformat(),
                json.dumps(
                    {
                        "kind": "LOCAL_PAPER_AUTHORIZATION_BASELINE_V1",
                        "database_id": "test-ledger",
                        "database_path": str(path.resolve()),
                    }
                ),
            ),
        )
        db.execute(
            "INSERT INTO overnight_sprint_cycles VALUES(?,?,?)",
            (
                "weather-driver:test",
                (now + timedelta(seconds=1)).isoformat(),
                json.dumps(driver),
            ),
        )
    view = snapshot(path)
    assert view["weather_evidence_kind"] == "PAPER_WEATHER_DRIVER_V1"
    assert view["first_blocker"] == "PROVIDER_CLOCK_STALE"
    driver["database_id"] = "other"
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE overnight_sprint_cycles SET payload=? WHERE id='weather-driver:test'",
            (json.dumps(driver),),
        )
    assert snapshot(path)["first_blocker"] == "PAPER_DASHBOARD_EVIDENCE_INVALID"


def test_final_admission_rejection_after_passing_qualification_is_first_blocker(evidence):
    import os

    from kalshi_predictor.overnight_paper.qualification import GATE_NAMES

    path, now, _ = evidence
    at = (now + timedelta(seconds=1)).isoformat()
    info = path.stat()
    inputs = {"decision_at": now.isoformat()}
    qualification = {
        "kind": "PAPER_RELEASE_QUALIFICATION",
        "decision_inputs": inputs,
        "qualification": {
            "decision_id": decision_fingerprint(inputs),
            "status": "PAPER_ELIGIBLE",
            "gates": [[name, True] for name in GATE_NAMES],
            "blockers": [],
        },
        "shadow_payload": {},
    }
    baseline = {
        "kind": "LOCAL_PAPER_AUTHORIZATION_BASELINE_V1",
        "database_id": "synthetic-ledger",
        "database_path": str(path),
    }
    event = {
        "kind": "PAPER_RUNTIME_HEALTH_V1",
        "generation": "test",
        "sequence": 0,
        "pid": os.getpid(),
        "entries_enabled": False,
        "state": "ENTRY_DISABLED",
        "captured_at": at,
        "database_path": str(path),
        "database_id": "synthetic-ledger",
        "database_file_identity": [info.st_dev, info.st_ino],
        "blockers": ["MODEL_NOT_READY"],
    }
    with sqlite3.connect(path) as db:
        for key, clock, payload in (
            ("authorization-baseline:synthetic-ledger", now.isoformat(), baseline),
            ("qualification:test", now.isoformat(), qualification),
            ("runtime-health:test:000000", at, event),
        ):
            db.execute(
                "INSERT INTO overnight_sprint_cycles VALUES(?,?,?)",
                (key, clock, json.dumps(payload)),
            )
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    view = snapshot(path)
    assert view["last_qualification_status"] == "PAPER_ELIGIBLE"
    assert view["first_blocker"] == "MODEL_NOT_READY"
    assert view["runtime_blocker_event"] == {
        "at": at,
        "state": "ENTRY_DISABLED",
        "blockers": ["MODEL_NOT_READY"],
    }
    assert view["weather_source_state"] == "STALE"
    assert view["runtime_current_monitor_verified"] is False
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
