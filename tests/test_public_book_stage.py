import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.schema import MarketSnapshot
from kalshi_predictor.ingest.public_book_stage import stage_public_books, validate_public_stage
from kalshi_predictor.ingest.websocket_orderbooks import drain_staged_websocket_orderbooks

TICKER = "KXTEMPMIAH-26SEP1108-T80.99"


def source():
    market = {
        "ticker": TICKER,
        "status": "active",
        "close_time": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    book = {"orderbook_fp": {"yes_dollars": [["0.68", "50"]], "no_dollars": [["0.22", "50"]]}}
    calls = []

    def get(url):
        calls.append(url)
        return json.dumps(book if "/orderbook?" in url else {"market": market}).encode(), 200

    return get, calls


def test_public_originals_reach_guarded_writer_without_auth(tmp_path):
    get, calls = source()
    stage = tmp_path / "stage"
    result = stage_public_books(
        tickers=[TICKER], staging_dir=stage, evidence_dir=tmp_path / "evidence", get=get
    )
    assert result["status"] == "COMPLETE" and len(calls) == 2
    factory = get_session_factory(init_db(f"sqlite:///{tmp_path / 'db.sqlite'}"))
    drained = drain_staged_websocket_orderbooks(
        session_factory=factory,
        staging_dir=stage,
        writer_monitor_fn=lambda: {"safe_to_start_write": True},
    )
    assert drained["snapshots_inserted"] == 1
    with factory() as session:
        row = session.scalar(select(MarketSnapshot))
        assert json.loads(row.raw_orderbook_json)["orderbook_fp"]["yes_dollars"] == [["0.68", "50"]]


@pytest.mark.parametrize("damage", ["altered_book", "wrong_ticker", "stale"])
def test_original_binding_and_freshness_refuse_corrupt_stage(tmp_path, damage):
    get, _ = source()
    stage = tmp_path / "stage"
    stage_public_books(
        tickers=[TICKER], staging_dir=stage, evidence_dir=tmp_path / "evidence", get=get
    )
    path = next(stage.glob("*.json"))
    payload = json.loads(path.read_bytes())
    if damage == "altered_book":
        payload["orderbook"]["orderbook_fp"]["yes_dollars"] = []
    elif damage == "wrong_ticker":
        payload["ticker"] = "KXTEMPMIAH-WRONG"
    as_of = datetime.now(UTC) + timedelta(minutes=2 if damage == "stale" else 0)
    with pytest.raises(ValueError):
        validate_public_stage(payload, as_of=as_of)


def test_transport_failure_no_retry_or_stage(tmp_path):
    calls = []

    def get(url):
        calls.append(url)
        raise OSError("offline")

    result = stage_public_books(
        tickers=[TICKER],
        staging_dir=tmp_path / "stage",
        evidence_dir=tmp_path / "evidence",
        get=get,
    )
    assert len(calls) == 1 and result["requests"] == 1 and not result["staged"]
    with pytest.raises(FileExistsError):
        stage_public_books(
            tickers=[TICKER],
            staging_dir=tmp_path / "stage",
            evidence_dir=tmp_path / "evidence",
            get=get,
        )
    assert len(calls) == 1


def test_stale_stage_quarantined_once_with_original_bytes(tmp_path, monkeypatch):
    from pathlib import Path

    from kalshi_predictor.ingest import websocket_orderbooks

    get, _ = source()
    stage = tmp_path / "stage"
    stage_public_books(
        tickers=[TICKER], staging_dir=stage, evidence_dir=tmp_path / "evidence", get=get
    )
    original_path = next(stage.glob("*.json"))
    original = original_path.read_bytes()
    later = datetime.now(UTC) + timedelta(minutes=2)
    monkeypatch.setattr(websocket_orderbooks, "utc_now", lambda: later)
    factory = get_session_factory(init_db(f"sqlite:///{tmp_path / 'db.sqlite'}"))
    result = drain_staged_websocket_orderbooks(
        session_factory=factory,
        staging_dir=stage,
        writer_monitor_fn=lambda: {"safe_to_start_write": True},
    )
    assert result["snapshots_inserted"] == 0
    assert "PUBLIC_ORIGINAL_STALE" in result["errors"][0]
    assert Path(result["quarantined_files"][0]).read_bytes() == original
    again = drain_staged_websocket_orderbooks(
        session_factory=factory,
        staging_dir=stage,
        writer_monitor_fn=lambda: {"safe_to_start_write": True},
    )
    assert again["files_seen"] == 0 and again["errors"] == []
