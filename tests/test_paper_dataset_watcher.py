"""Actual verified prospective originals, public final adapter, and isolated SQLite."""

import hashlib
import json
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from test_paper_release_dataset import observation, stamp

from kalshi_predictor.data.schema import Base, Market
from kalshi_predictor.overnight_paper.dataset_store import load_dataset, persist_dataset_record
from kalshi_predictor.overnight_paper.settlement_runner import _tracked, run_settlement_cycles
from kalshi_predictor.overnight_paper.store import initialize_store
from kalshi_predictor.overnight_paper.watcher import (
    PublicMarketObservation,
    reconcile_public_settlements,
)


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "dataset.db"
    engine = create_engine(f"sqlite+pysqlite:///{path}")
    Base.metadata.create_all(engine)
    initialize_store(path)
    factory = sessionmaker(engine)
    original = observation(8)
    row = original.decode()
    identity = row["identity"]
    with factory() as session:
        session.add(
            Market(
                ticker=identity["ticker"],
                event_ticker=identity["event_id"],
                series_ticker=identity["series"],
                raw_json="{}",
                first_seen_at=stamp(8),
                last_seen_at=stamp(8),
            )
        )
        persist_dataset_record(
            session, dataset="paper-release", record=original, recorded_at=stamp(8)
        )
        session.commit()
    market = dict(
        ticker=identity["ticker"],
        event_ticker=identity["event_id"],
        series_ticker=identity["series"],
        close_time=row["decision"]["close_time"],
        status="finalized",
        result="yes",
        settlement_ts=stamp(8, 3).isoformat(),
        settlement_value_dollars="1",
    )
    raw = json.dumps({"market": market}).encode()
    source = PublicMarketObservation(
        identity["ticker"],
        "https://external-api.kalshi.com/trade-api/v2/markets/" + identity["ticker"],
        stamp(8, 4),
        hashlib.sha256(raw).hexdigest(),
        raw,
    )
    yield factory, path, source
    engine.dispose()


def run(ledger, source=None):
    factory, path, original = ledger
    item = source or original
    return reconcile_public_settlements(
        session_factory=factory, database_path=path, observations=(item,), now=item.captured_at
    )


def records(ledger):
    with ledger[0]() as session:
        return load_dataset(session, dataset="paper-release")


def test_dataset_only_final_replay_preserves_first_original_and_chain(ledger):
    first = run(ledger)
    assert first.shadow_evaluations_created == first.paper_evaluations_created == 0
    chain = records(ledger)
    assert len(chain) == 2
    row = chain[-1].decode()["record"]
    assert row["kind"] == "outcome-v1"
    assert row["outcome"]["available_at"] == ledger[2].captured_at.isoformat()
    changed = json.loads(ledger[2].payload)
    changed["market"]["title"] = "harmless later metadata"
    raw = json.dumps(changed).encode()
    later = replace(
        ledger[2],
        captured_at=ledger[2].captured_at + timedelta(seconds=5),
        payload=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
    )
    run(ledger, later)
    assert records(ledger) == chain
    with ledger[0]() as session:
        assert session.execute(text("SELECT count(*) FROM settlements")).scalar_one() == 1
        assert session.execute(text("SELECT count(*) FROM paper_orders")).scalar_one() == 0


def test_runner_tracks_prospective_refusal_without_shadow_and_stops_after_final(ledger):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=ledger[2].payload)

    args = dict(
        session_factory=ledger[0],
        database_path=ledger[1],
        transport=httpx.MockTransport(handler),
        clock=lambda: ledger[2].captured_at,
    )
    run_settlement_cycles(**args)
    assert len(records(ledger)) == 2 and len(calls) == 1
    restarted = run_settlement_cycles(**args)
    assert restarted.status == "NO_TRACKED_MARKETS" and len(calls) == 1


@pytest.mark.parametrize(
    "field,value",
    [("event_ticker", "wrong"), ("series_ticker", "wrong"), ("close_time", "2026-09-08T02:01:00Z")],
)
def test_dataset_identity_mismatch_rolls_back_entire_transaction(ledger, field, value):
    before = records(ledger)
    body = json.loads(ledger[2].payload)
    body["market"][field] = value
    raw = json.dumps(body).encode()
    bad = replace(ledger[2], payload=raw, sha256=hashlib.sha256(raw).hexdigest())
    with pytest.raises(ValueError, match="IDENTITY"):
        run(ledger, bad)
    assert records(ledger) == before
    with ledger[0]() as session:
        assert session.execute(text("SELECT count(*) FROM settlements")).scalar_one() == 0


def test_conflicting_final_does_not_replace_dataset_result(ledger):
    run(ledger)
    before = records(ledger)
    body = json.loads(ledger[2].payload)
    body["market"].update(result="no", settlement_value_dollars="0")
    raw = json.dumps(body).encode()
    with pytest.raises(ValueError, match="CORRECTION"):
        run(ledger, replace(ledger[2], payload=raw, sha256=hashlib.sha256(raw).hexdigest()))
    assert records(ledger) == before


def test_failure_after_outcome_append_rolls_back_settlement_and_dataset(ledger):
    """Corrupt shadow is checked after the dataset join; no partial write survives."""
    with ledger[0]() as session:
        session.execute(
            text(
                "INSERT INTO overnight_shadow(id,ticker,event_ticker,decision_at,payload) "
                "VALUES('invalid',:ticker,'x',:at,'{}')"
            ),
            {"ticker": ledger[2].ticker, "at": stamp(8).isoformat()},
        )
        session.commit()
    before = records(ledger)
    with pytest.raises(ValueError, match="SHADOW_PAYLOAD_INTEGRITY"):
        run(ledger)
    assert records(ledger) == before
    with ledger[0]() as session:
        assert session.execute(text("SELECT count(*) FROM settlements")).scalar_one() == 0
        assert (
            session.execute(
                text(
                    "SELECT count(*) FROM overnight_sprint_cycles "
                    "WHERE id LIKE 'settlement-final:%'"
                )
            ).scalar_one()
            == 0
        )


def test_tracking_unions_dataset_and_shadows_with_paper_priority_and_exact_deferral(ledger):
    with ledger[0]() as session:
        for ticker, order in (
            ("ZZZ-PAPER", 1),
            (ledger[2].ticker, None),
            *((f"AAA-SHADOW-{index}", None) for index in range(4)),
        ):
            session.execute(
                text(
                    "INSERT INTO overnight_shadow"
                    "(id,ticker,event_ticker,decision_at,payload,paper_order_id) "
                    "VALUES(:ticker,:ticker,'event',:at,'{}',:order)"
                ),
                {"ticker": ticker, "at": stamp(8).isoformat(), "order": order},
            )
        session.commit()
    tickers, deferred = _tracked(ledger[0], ledger[1].resolve())
    assert len(tickers) == 3 and tickers[0] == "ZZZ-PAPER"
    assert tickers[1] == "AAA-SHADOW-0"
    # One dataset ticker + four shadows; duplicate dataset/shadow ticker counted once.
    assert deferred == 3
