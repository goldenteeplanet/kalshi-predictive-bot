"""Real isolated ledger fixtures and intercepted HTTP; no network or runtime writes."""

import json

import httpx
import pytest
import test_overnight_watcher as fixtures
from sqlalchemy import text

from kalshi_predictor.overnight_paper import activation, settlement_runner
from kalshi_predictor.overnight_paper.store import digest, encode

baseline_template = fixtures.baseline_template
prepared = fixtures.prepared


def run(prepared, handler, **kwargs):
    return settlement_runner.run_settlement_cycles(
        session_factory=prepared["session_factory"],
        database_path=prepared["database_path"],
        transport=httpx.MockTransport(handler),
        clock=lambda: fixtures.observation(prepared).captured_at,
        **kwargs,
    )


def test_restart_public_get_settles_once_with_entry_disabled(prepared):
    activation.activate_local_paper(**prepared)
    prepared["settings"].paper_order_creation_enabled = False
    prepared["settings"].paper_order_kill_switch = True
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        assert str(request.url) == fixtures.observation(prepared).source_url
        assert "authorization" not in request.headers
        assert not any("kalshi-access" in name for name in request.headers)
        return httpx.Response(200, content=fixtures.observation(prepared).payload)

    first = run(prepared, handler)
    second = run(prepared, handler)
    assert first.status == second.status == "TRACKED_SETTLEMENTS_EVALUATED"
    assert first.reports[0].paper_evaluations_created == 1
    assert second.reports[0].paper_evaluations_created == 0
    assert len(requests) == 2
    assert fixtures.counts(prepared) == dict(
        paper_orders=1, paper_fills=1, paper_pnl=1, settlements=1
    )


def test_closed_market_keeps_monitoring_then_finalizes(prepared):
    artifacts = iter(
        [
            fixtures.observation(prepared, status="closed"),
            fixtures.observation(prepared),
        ]
    )
    sleeps = []
    report = run(
        prepared,
        lambda req: httpx.Response(200, content=next(artifacts).payload),
        cycles=3,
        interval_seconds=60,
        sleep=sleeps.append,
    )
    assert report.cycles_completed == 2
    assert sleeps == [60]
    assert report.reports[0].shadow_evaluations_created == 0
    assert report.reports[1].shadow_evaluations_created == 1
    assert fixtures.counts(prepared)["paper_orders"] == 0


def test_empty_tracking_is_not_active_and_never_requests(prepared):
    with prepared["session_factory"]() as session:
        session.execute(text("DELETE FROM overnight_shadow"))
        session.commit()
    report = run(prepared, lambda req: pytest.fail("unexpected request"))
    assert report.status == "NO_TRACKED_MARKETS"
    assert report.cycles_completed == 0


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"Location": "https://example.com/orders"}),
        httpx.Response(429),
        httpx.Response(200, content=b"x" * (settlement_runner.MAX_PAYLOAD_BYTES + 1)),
    ],
)
def test_redirect_rate_limit_oversize_fail_without_settlement(prepared, response):
    requests = []

    def handler(request):
        requests.append(request)
        return response

    with pytest.raises((httpx.HTTPStatusError, ValueError)):
        run(prepared, handler)
    assert len(requests) == 1
    assert fixtures.counts(prepared)["settlements"] == 0


def test_missing_authoritative_settlement_timestamp_is_not_invented(prepared):
    raw = fixtures.observation(prepared, settlement_ts=None)
    with pytest.raises(ValueError, match="FINAL_TIMESTAMPS_REQUIRED"):
        run(prepared, lambda req: httpx.Response(200, content=raw.payload))
    assert fixtures.counts(prepared)["settlements"] == 0


def test_stop_and_cycle_bounds(prepared):
    stopped = run(
        prepared, lambda req: pytest.fail("unexpected request"), stop_requested=lambda: True
    )
    assert stopped.status == "STOPPED"
    for cycles in (0, 61, True):
        with pytest.raises(ValueError, match="INVALID_SETTLEMENT_RUN_LIMITS"):
            run(prepared, lambda req: pytest.fail("unexpected request"), cycles=cycles)
    report = run(
        prepared,
        lambda req: httpx.Response(
            200, content=fixtures.observation(prepared, status="determined").payload
        ),
    )
    assert report.status == "BOUNDED_MONITORING_COMPLETE"
    assert report.cycles_completed == 1


def test_more_than_three_linked_paper_markets_fail_before_public_calls(prepared):
    activation.activate_local_paper(**prepared)
    with prepared["session_factory"]() as session:
        original = session.execute(text("SELECT * FROM overnight_shadow LIMIT 1")).mappings().one()
        columns = list(original.keys())
        for index in range(3):
            values = dict(original)
            values.update(id=f"extra-{index}", ticker=f"EXTRA-{index}", paper_order_id=100 + index)
            session.execute(
                text(
                    f"INSERT INTO overnight_shadow ({','.join(columns)}) "
                    f"VALUES ({','.join(':' + key for key in columns)})"
                ),
                values,
            )
        session.commit()
    with pytest.raises(ValueError, match="LINKED_PAPER_MARKET_LIMIT"):
        run(prepared, lambda req: pytest.fail("unexpected request"))


def test_rate_limit_retains_retry_after_response_and_no_retry(prepared):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})

    with pytest.raises(httpx.HTTPStatusError) as caught:
        run(prepared, handler, cycles=60)
    assert caught.value.response.headers["Retry-After"] == "120"
    assert len(calls) == 1


def add_shadow(prepared, ticker, *, evaluated=False):
    with prepared["session_factory"]() as session:
        original = dict(
            session.execute(text("SELECT * FROM overnight_shadow WHERE ticker='BTC-TEST'"))
            .mappings()
            .one()
        )
        payload = json.loads(original["payload"])
        payload["ticker"] = ticker
        original.update(
            id=digest(payload),
            ticker=ticker,
            payload=encode(payload),
            paper_order_id=None,
            evaluation_json="{}" if evaluated else None,
        )
        columns = list(original)
        session.execute(
            text(
                f"INSERT INTO overnight_shadow ({','.join(columns)}) "
                f"VALUES ({','.join(':' + key for key in columns)})"
            ),
            original,
        )
        session.execute(
            text(
                "INSERT INTO markets "
                "(ticker,event_ticker,series_ticker,raw_json,first_seen_at,last_seen_at) "
                "SELECT :ticker,event_ticker,series_ticker,raw_json,first_seen_at,last_seen_at "
                "FROM markets WHERE ticker='BTC-TEST'"
            ),
            {"ticker": ticker},
        )
        session.commit()


def test_evaluated_historical_shadows_do_not_block_pending_paper(prepared):
    activation.activate_local_paper(**prepared)
    for index in range(3):
        add_shadow(prepared, f"HISTORY-{index}", evaluated=True)
    requests = []

    def handler(request):
        requests.append(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(200, content=fixtures.observation(prepared).payload)

    report = run(prepared, handler)
    assert requests == ["BTC-TEST"]
    assert report.status == "TRACKED_SETTLEMENTS_EVALUATED"
    assert report.reports[0].paper_evaluations_created == 1
    assert report.deferred_shadow_count == 0


def test_pending_shadows_are_explicitly_deferred_after_paper_priority(prepared):
    activation.activate_local_paper(**prepared)
    for index in range(4):
        add_shadow(prepared, f"AAA-{index}")
    requests = []

    def handler(request):
        ticker = request.url.path.rsplit("/", 1)[1]
        requests.append(ticker)
        payload = json.loads(fixtures.observation(prepared).payload)
        payload["market"]["ticker"] = ticker
        return httpx.Response(200, json=payload)

    first = run(prepared, handler)
    assert requests == ["BTC-TEST", "AAA-0", "AAA-1"]
    assert first.deferred_shadow_count == 2
    assert first.status == "BOUNDED_MONITORING_WITH_DEFERRED_SHADOWS"
    requests.clear()
    second = run(prepared, handler)
    assert requests == ["BTC-TEST", "AAA-2", "AAA-3"]
    assert second.deferred_shadow_count == 0
    assert second.status == "TRACKED_SETTLEMENTS_EVALUATED"
    assert fixtures.counts(prepared)["paper_pnl"] == 1
