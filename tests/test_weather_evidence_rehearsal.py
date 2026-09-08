import json
import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from scripts.local import weather_evidence_rehearsal as evidence


@pytest.mark.parametrize(
    "url",
    [
        evidence.BASE + "/portfolio/orders",
        "https://api.weather.gov.evil.example/stations/KNYC",
        "https://api.weather.gov/gridpoints/OKX/34,45/forecast/hourly?key=secret",
        "http://api.weather.gov/stations/KNYC",
        "https://api.weather.gov/points/40.7,-73.9/../../orders",
    ],
)
def test_endpoint_scope(url):
    assert not evidence.allowed(url)


@pytest.mark.parametrize(
    ("issued", "expected"),
    [
        (None, "TIMESTAMP_INVALID"),
        ("2026-09-08T01:00:00", "TIMESTAMP_INVALID"),
        ("2026-09-08T02:00:01Z", "FUTURE_ISSUE_TIME"),
        ("2026-09-08T01:29:59Z", "FORECAST_STALE"),
        ("2026-09-08T01:30:00Z", "ANALYTICAL_ONLY"),
    ],
)
def test_freshness_never_uses_receipt_time(issued, expected):
    assert (
        evidence.forecast_status(
            issued, "2026-09-08T02:00:00Z", datetime(2026, 9, 8, 2, tzinfo=UTC)
        )
        == expected
    )


def test_lineage_does_not_certify_settlement_and_rejects_conflict():
    market = {
        "ticker": "KXTEMPNYCH-X-T1",
        "event_ticker": "KXTEMPNYCH-X",
        "rules_primary": "The Weather Company (for coordinates KNYC)",
    }
    event = {"event_ticker": market["event_ticker"], "series_ticker": evidence.SERIES}
    series = {"ticker": evidence.SERIES, "settlement_sources": [{"name": "The Weather Company"}]}
    result = evidence.lineage(market, event, series)
    assert result["status"] == "RULE_LINEAGE_CAPTURED"
    assert result["settlement_value_verified"] is False
    event["event_ticker"] = "OTHER"
    assert evidence.lineage(market, event, series)["status"] == "IDENTITY_OR_SOURCE_CONFLICT"
    event["event_ticker"] = market["event_ticker"]
    series["settlement_sources"] = [{"name": "Synoptic Data"}]
    assert evidence.lineage(market, event, series)["status"] == "IDENTITY_OR_SOURCE_CONFLICT"


def test_database_guard_denies_orders_ddl_attach_and_mutation():
    db = sqlite3.connect(":memory:")
    db.executescript("CREATE TABLE sources(x); CREATE TABLE orders(x);")
    db.set_authorizer(evidence.authorizer)
    db.execute("INSERT INTO sources VALUES(1)")
    for sql in (
        "INSERT INTO orders VALUES(1)",
        "DELETE FROM sources",
        "UPDATE sources SET x=2",
        "CREATE TABLE more(x)",
        "ATTACH DATABASE ':memory:' AS other",
    ):
        with pytest.raises(sqlite3.DatabaseError):
            db.execute(sql)
    db.close()


def test_isolated_run_retains_failure_and_never_creates_trading_tables(tmp_path, monkeypatch):
    calls = []

    def response(command, **kwargs):
        calls.append(command[-1])
        assert kwargs["timeout"] <= 15
        assert "KALSHI_API_KEY_ID" not in kwargs["env"]
        return SimpleNamespace(stdout=json.dumps({"body": "{}", "server_date": None}).encode())

    monkeypatch.setenv("KALSHI_API_KEY_ID", "not-for-public-data")
    monkeypatch.setattr(evidence.subprocess, "run", response)
    root = tmp_path / "new"
    result = evidence.run(root)
    assert result["trading_readiness"] == "BLOCKED"
    assert result["counts"]["forecasts"] == 0
    assert result["source_hashes_verified"]
    assert set(result["tables"]) == evidence.TABLES
    assert len(calls) == 1
    with pytest.raises(ValueError, match="NEW_UNSYNCED"):
        evidence.run(root)


def test_redirect_denied():
    with pytest.raises(RuntimeError, match="REDIRECT_REFUSED"):
        evidence.NoRedirect().redirect_request(None)


@pytest.mark.parametrize(
    ("generated", "updated", "expected"),
    [
        ("2026-09-08T01:59:00Z", "2026-09-08T01:00:00Z", "FORECAST_STALE"),
        (None, "2026-09-08T01:59:00Z", "TIMESTAMP_INVALID"),
        ("2026-09-08T01:59:00Z", None, "TIMESTAMP_INVALID"),
        ("2026-09-08T01:59:00Z", "2026-09-08T02:01:00Z", "FUTURE_ISSUE_TIME"),
        ("2026-09-08T01:59:00Z", "2026-09-08T01:30:00Z", "ANALYTICAL_ONLY"),
    ],
)
def test_provider_freshness_requires_both_clocks(generated, updated, expected):
    assert (
        evidence.provider_status(
            generated, updated, "2026-09-08T02:00:00Z", datetime(2026, 9, 8, 2, tzinfo=UTC)
        )
        == expected
    )
