import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from kalshi_predictor.crypto.cf_process_inputs import digest
from kalshi_predictor.crypto.cost_evidence import OriginalBook
from kalshi_predictor.crypto.current_event_selection import (
    POLICY,
    CurrentEventDiscovery,
    CurrentEventSelectionInputs,
    _geometry,
    event_discovery_rows,
    event_discovery_url,
    seed_discovery_url,
    seed_event,
    select_near_benchmark_contracts,
)
from kalshi_predictor.crypto.current_market_scan import evaluate_paginated_current_research

NOW = datetime(2026, 9, 13, 5, 30, tzinfo=UTC)
EVENT = "KXBTC-26SEP1302"


def encoded(value):
    return json.dumps(value).encode()


def discovery(rows, *, cursor=""):
    raw = encoded({"markets": rows, "cursor": cursor})
    original = OriginalBook(event_discovery_url("KXBTC", EVENT), raw, NOW - timedelta(seconds=3))
    receipt = encoded(
        dict(
            url=original.url,
            method="GET",
            http_status=200,
            original_complete=True,
            source_sha256=digest(raw),
            requested_at=(NOW - timedelta(seconds=4)).isoformat(),
            received_at=original.received_at.isoformat(),
        )
    )
    return CurrentEventDiscovery(original, receipt, EVENT)


def market(strike, bid="0.01", ask="0.02"):
    return dict(
        ticker=EVENT + "-B" + str(strike),
        event_ticker=EVENT,
        status="active",
        market_type="binary",
        close_time="2026-09-13T06:00:00Z",
        strike_type="between",
        floor_strike=str(strike),
        cap_strike=str(strike + 1),
        yes_bid_dollars=bid,
        yes_ask_dollars=ask,
    )


def inputs(rows):
    original = encoded(
        {
            "data": {
                "serverTime": (NOW - timedelta(seconds=1)).isoformat(),
                "payload": [
                    {
                        "time": int((NOW - timedelta(seconds=3600 - i)).timestamp() * 1000),
                        "value": "100.5",
                    }
                    for i in range(3600)
                ],
            }
        }
    )
    receipt = encoded(
        dict(
            schema="cf-response-receipt-v1",
            method="GET",
            http_status=200,
            profile="LATEST_1HZ",
            index_id="BRTI",
            source_sha256=digest(original),
            url="https://external-api.kalshi.com/trade-api/v2/cfbenchmarks/values?id=BRTI",
            requested_at=(NOW - timedelta(seconds=2)).isoformat(),
            received_at=(NOW - timedelta(seconds=1)).isoformat(),
            recorded_at=NOW.isoformat(),
        )
    )
    protocol = encoded(
        dict(
            schema="current-event-selection-protocol-v1",
            asset="BTC",
            event_ticker=EVENT,
            declared_at=(NOW - timedelta(seconds=10)).isoformat(),
            not_before=(NOW - timedelta(seconds=10)).isoformat(),
            not_after=(NOW + timedelta(seconds=60)).isoformat(),
            policy=POLICY,
            max_contracts=2,
            shortlist_size=4,
        )
    )
    return CurrentEventSelectionInputs(
        discovery(rows), original, receipt, protocol, digest(protocol), NOW
    )


def select(value):
    return select_near_benchmark_contracts(
        discovery=value.discovery,
        asset="BTC",
        cf_original=value.cf_original,
        cf_receipt=value.cf_receipt,
        protocol_original=value.protocol_original,
        protocol_sha256=value.protocol_sha256,
        selected_at=value.selected_at,
    )


def test_deep_tail_page_order_does_not_hide_nearest_event_strikes():
    rows = [market(i) for i in range(150, 170)] + [market(i) for i in range(98, 103)]
    result = select(inputs(rows))
    reverse = select(inputs(list(reversed(rows))))
    assert result["selected"] == reverse["selected"]
    assert result["selected"][0] == EVENT + "-B100"
    assert all(int(t.split("-B")[1]) < 150 for t in result["shortlist"])
    assert result["universe_count"] == 25 and len(result["scores"]) == 25
    assert result["event_original_json"] != reverse["event_original_json"]
    assert result["forecast_cf_requires_new_target_bound_request"]
    assert not result["paper_eligible"] and not result["liquidity_verified"]


@pytest.mark.parametrize("defect", ["cursor", "duplicate", "scope", "future", "stale", "row_bound"])
def test_event_original_fail_closed(defect):
    rows = [market(100)]
    if defect == "duplicate":
        rows *= 2
    if defect == "scope":
        rows[0]["event_ticker"] = "KXETH-26SEP1302"
    if defect == "row_bound":
        rows = [market(i) for i in range(201)]
    evidence = discovery(rows, cursor="more" if defect == "cursor" else "")
    if defect in ("future", "stale"):
        evidence = replace(
            evidence,
            original=replace(
                evidence.original,
                received_at=NOW + timedelta(seconds=1)
                if defect == "future"
                else NOW - timedelta(seconds=301),
            ),
        )
    with pytest.raises(ValueError):
        event_discovery_rows(evidence, series="KXBTC", assessed_at=NOW)


def test_protocol_must_precede_original_acquisition_and_receipt_is_bound():
    value = inputs([market(100)])
    p = json.loads(value.protocol_original)
    p["declared_at"] = p["not_before"] = (NOW - timedelta(seconds=1)).isoformat()
    raw = encoded(p)
    with pytest.raises(ValueError, match="CHRONOLOGY"):
        select(replace(value, protocol_original=raw, protocol_sha256=digest(raw)))
    r = json.loads(value.cf_receipt)
    r["source_sha256"] = "a" * 64
    with pytest.raises(ValueError, match="RECEIPT"):
        select(replace(value, cf_receipt=encoded(r)))


def test_metadata_tiebreak_is_fixed_and_missing_quotes_are_not_liquidity():
    rows = [market(100), market(99, None, None), market(101, "0.4", "0.5"), market(102)]
    result = select(inputs(rows))
    assert result["selected"] == [EVENT + "-B100", EVENT + "-B102"]
    assert not next(r for r in result["scores"] if r["ticker"].endswith("B99"))[
        "metadata_two_sided"
    ]


def test_selected_subset_scan_has_full_universe_manifest_and_no_faked_series_pages():
    value = inputs([market(i) for i in range(200)])
    report = evaluate_paginated_current_research(
        discovery_pages={},
        books={},
        fee_originals={},
        assessed_at=NOW,
        event_selections={"KXBTC": value},
    )
    assert report["version"] == "EVENT_SELECTED_CURRENT_RESEARCH_V1"
    assert len(report["rows"]) == 4 and report["funnel"]["markets_scanned"] == 2
    family = next(f for f in report["families"] if f["asset"] == "BTC")
    assert family["selection_universe_count"] == 200
    assert len(family["selection_manifest"]["scores"]) == 199  # nonpositive strike excluded
    assert family["assessment_scope"] == "PREDECLARED_SELECTED_SUBSET_OF_COMPLETE_EVENT"
    with pytest.raises(ValueError, match="SCOPE_INVALID|COUNT_OR_SCOPE"):
        evaluate_paginated_current_research(
            discovery_pages={"KXBTC": ()},
            books={},
            fee_originals={},
            assessed_at=NOW,
            event_selections={"KXBTC": value},
        )


def test_actual_preserved_doge_custom_strike_uses_reviewed_grammar():
    row = json.loads(
        (Path(__file__).parent / "fixtures/current_event_doge_market.json").read_bytes()
    )["market"]
    distance, center = _geometry(row, Decimal("0.084782"), asset="DOGE", as_of=NOW)
    assert distance == 0 and center > 0
    row["custom_strike"]["cap_strike"] = "0.9"
    with pytest.raises(ValueError):
        _geometry(row, Decimal("0.084782"), asset="DOGE", as_of=NOW)


def test_seed_filters_use_whole_second_close_window_without_status():
    start, end = NOW + timedelta(minutes=20), NOW + timedelta(minutes=80)
    url = seed_discovery_url("KXBTC", not_before=start, not_after=end)
    assert "status=" not in url and "limit=1" in url
    raw = encoded({"markets": [market(100)], "cursor": "partial-family"})
    original = OriginalBook(url, raw, NOW)
    receipt = encoded(
        dict(
            url=url,
            method="GET",
            http_status=200,
            original_complete=True,
            source_sha256=digest(raw),
            requested_at=NOW.isoformat(),
            received_at=NOW.isoformat(),
        )
    )
    assert (
        seed_event(
            original, receipt, series="KXBTC", not_before=start, not_after=end, assessed_at=NOW
        )
        == EVENT
    )
    with pytest.raises(ValueError):
        seed_discovery_url("KXBTC", not_before=start + timedelta(microseconds=1), not_after=end)


def test_book_before_frozen_selection_is_rejected_without_replacement():
    value = inputs([market(100), market(101), market(102)])
    chosen = select(value)["selected"]
    book = OriginalBook(
        "https://external-api.kalshi.com/trade-api/v2/markets/" + chosen[0] + "/orderbook?depth=10",
        b'{"orderbook_fp":{"yes_dollars":[["0.3","2"]],"no_dollars":[["0.6","2"]]}}',
        NOW - timedelta(seconds=1),
    )
    result = evaluate_paginated_current_research(
        discovery_pages={},
        books={chosen[0]: book},
        fee_originals={},
        assessed_at=NOW,
        event_selections={"KXBTC": value},
    )
    assert result["funnel"]["book_valid_independent_of_forecast"] == 0
    assert {r["ticker"] for r in result["rows"]} == set(chosen)
    assert any("BOOK_MUST_FOLLOW" in blocker for r in result["rows"] for blocker in r["blockers"])


@pytest.mark.parametrize("defect", ["wrong_index", "future_tick", "bad_policy", "mixed_close"])
def test_source_identity_and_policy_fail_closed(defect):
    value = inputs([market(100), market(101)])
    if defect == "wrong_index":
        receipt = json.loads(value.cf_receipt)
        receipt["index_id"] = "DOGEUSD_RTI"
        value = replace(value, cf_receipt=encoded(receipt))
    elif defect == "future_tick":
        value = replace(value, selected_at=NOW - timedelta(seconds=2))
    elif defect == "bad_policy":
        plan = json.loads(value.protocol_original)
        plan["policy"] = "HIGHEST_LATER_EV"
        raw = encoded(plan)
        value = replace(value, protocol_original=raw, protocol_sha256=digest(raw))
    else:
        rows = [market(100), market(101)]
        rows[1]["close_time"] = "2026-09-13T07:00:00Z"
        value = replace(value, discovery=discovery(rows))
    with pytest.raises(ValueError):
        select(value)
