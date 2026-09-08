import sqlite3
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from kalshi_predictor.data.schema import Base
from scripts.local.observation_rehearsal import (
    FLAGS,
    MARKETS,
    Capture,
    NoRedirect,
    allowed_path,
    audit,
    authorizer,
    check_settings,
    import_snapshot,
    validate_market,
)


@pytest.mark.parametrize(
    "path",
    [
        "/portfolio/orders",
        "/markets?status=open",
        "/markets/KXOTHER-1/orderbook?depth=5",
        "/markets/KXTEMPNYCH-X/orderbook?depth=100",
        "https://evil.example/markets",
        "/markets/KXTEMPNYCH-../portfolio/orders",
        MARKETS + "&extra=1",
    ],
)
def test_public_capture_rejects_scope_expansion(path):
    assert not allowed_path(path)


def test_redirects_and_exhausted_budget_fail_before_request(tmp_path):
    with pytest.raises(RuntimeError, match="REDIRECT_REFUSED"):
        NoRedirect().redirect_request(None)
    capture = Capture(tmp_path, 0)
    with pytest.raises(RuntimeError, match="BUDGET"):
        capture.get(MARKETS)
    assert capture.requests == []


def test_failed_request_is_recorded_without_retry(tmp_path, monkeypatch):
    attempts = []

    def fail(*args, **kwargs):
        attempts.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired("public-get", kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", fail)
    capture = Capture(tmp_path, time.monotonic() + 100)
    with pytest.raises(subprocess.TimeoutExpired):
        capture.get(MARKETS)
    assert attempts == [15]
    assert len(capture.requests) == 1 and "error" in capture.requests[0]
    assert (tmp_path / "requests.json").is_file()


def test_market_close_and_lineage_fail_closed():
    at = datetime.now(UTC)
    row = {
        "ticker": "KXTEMPNYCH-1",
        "status": "active",
        "close_time": (at + timedelta(hours=1)).isoformat(),
    }
    validate_market(row, at)
    for override in (
        {"status": "closed"},
        {"close_time": at.isoformat()},
        {"series_ticker": "KXOTHER"},
    ):
        with pytest.raises(RuntimeError):
            validate_market(row | override, at)


@pytest.mark.parametrize("flag", FLAGS)
def test_changed_safety_setting_refuses(flag, tmp_path, monkeypatch):
    db = tmp_path / "paper.db"
    url = "sqlite:///" + db.as_posix()
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("KALSHI_DB_URL", url)
    settings = SimpleNamespace(
        **FLAGS, kalshi_db_url=url, kalshi_api_key_id=None, kalshi_private_key_path=None
    )
    check_settings(settings, db)
    setattr(settings, flag, "changed")
    with pytest.raises(RuntimeError, match="SAFETY_FLAG"):
        check_settings(settings, db)


@pytest.fixture
def db(tmp_path: Path):
    path = tmp_path / "paper.db"
    engine = create_engine("sqlite:///" + path.as_posix())
    Base.metadata.create_all(engine)
    engine.dispose()
    return path


def test_import_keeps_exact_book_prices_and_does_not_fabricate_missing_side(db):
    at = datetime.now(UTC)
    market = {
        "ticker": "KXTEMPNYCH-X",
        "status": "active",
        "close_time": (at + timedelta(hours=1)).isoformat(),
        "yes_bid_dollars": "0.99",
    }
    book = {"orderbook_fp": {"yes_dollars": [["0.30", "4"]], "no_dollars": [["0.60", "7"]]}}
    import_snapshot(db, market, book, at)
    import_snapshot(db, market, {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}, at)
    with sqlite3.connect(db) as connection:
        rows = connection.execute(
            "SELECT best_yes_bid,best_yes_ask,best_no_bid,best_no_ask FROM market_snapshots "
            "ORDER BY id"
        ).fetchall()
    assert rows == [("0.30", "0.40", "0.60", "0.70"), (None, None, None, None)]
    totals = audit(db)
    assert totals["markets"] == 1 and totals["market_snapshots"] == 2
    assert all(
        value == 0 for name, value in totals.items() if name not in {"markets", "market_snapshots"}
    )


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO paper_orders DEFAULT VALUES",
        "DELETE FROM markets",
        "DROP TABLE markets",
        "ATTACH DATABASE ':memory:' AS other",
        "UPDATE paper_orders SET status='changed'",
    ],
)
def test_database_authorizer_blocks_mutation_outside_observations(db, statement):
    with sqlite3.connect(db) as connection:
        connection.set_authorizer(authorizer)
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(statement)
    assert all(value == 0 for value in audit(db).values())
