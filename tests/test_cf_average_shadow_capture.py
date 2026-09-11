import importlib.util
import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from test_cf_process_inputs import fixture
from test_settlement_target_research_router import NOW

from kalshi_predictor.crypto import research_shadow as S

SPEC = importlib.util.spec_from_file_location(
    "cf_average_shadow_capture",
    Path(__file__).resolve().parents[1] / "scripts/cf_average_shadow_capture.py",
)
C = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(C)


def harness(tmp_path, monkeypatch):
    output = tmp_path / "capture"
    current = [NOW]
    monkeypatch.setattr(S, "now", lambda: current[0])
    rule = b"Synthetic terms used only by isolated test"
    monkeypatch.setattr(C, "TERMS_SHA", S.sha(rule))
    rr = S.encode(
        dict(
            url=C.TERMS_URL,
            sha256=S.sha(rule),
            http_status=200,
            requested_at=NOW.isoformat(),
            received_at=NOW.isoformat(),
        )
    )
    target = NOW + timedelta(hours=1)
    event = C.event_for(target)
    rows = []
    for low in (90, 100, 101):
        rows.append(
            dict(
                ticker=f"{event}-B{low}",
                event_ticker=event,
                market_type="binary",
                status="active",
                close_time=target.isoformat(),
                strike_type="between",
                floor_strike=str(low),
                cap_strike=str(low + 1),
                custom_strike={},
                price_level_structure="linear_cent",
                price_ranges=[dict(start="0", end="1", step="0.01")],
                rules_primary="Synthetic closed range rule",
            )
        )
    catalog = dict(markets=rows, cursor="")
    cf, _ = fixture()
    book = {"orderbook_fp": {"yes_dollars": [["0.30", "4.00"]], "no_dollars": [["0.40", "5.00"]]}}
    calls = []
    mutator = [None]

    def transport(url, timeout):
        assert 0 < timeout <= 10
        calls.append(url)
        path = urlsplit(url).path
        if "/cfbenchmarks/" in path:
            body = cf
        elif path.endswith("/markets"):
            assert parse_qs(urlsplit(url).query) == {"event_ticker": [event], "limit": ["1000"]}
            body = catalog
        elif path.endswith("/orderbook"):
            body = book
        else:
            assert (output / "selection.json").exists()
            assert not (output / "research.db").exists()
            ticker = path.rsplit("/", 1)[-1]
            body = {"market": next(r for r in rows if r["ticker"] == ticker)}
        if mutator[0]:
            changed = mutator[0](url, body)
            if changed is not None:
                return changed
        return 200, S.encode(body)

    plan = dict(
        schema="cf-average-prospective-slot-v1",
        target_at=target.isoformat(),
        event_ticker=event,
        not_before=NOW.isoformat(),
        not_after=(NOW + timedelta(seconds=50)).isoformat(),
        max_gets=6,
        symbol="SOL",
        benchmark="SOLUSD_RTI",
        selection="nearest_two_range_midpoints_then_ticker",
        hypotheses=[
            dict(name="LEFT_CLOSED_RIGHT_OPEN", include_start=True, include_end=False),
            dict(name="LEFT_OPEN_RIGHT_CLOSED", include_start=False, include_end=True),
        ],
        rounding="HALF_EVEN",
        decimal_places=4,
        rule_authority="DECLARED_UNCERTIFIED",
        net_costs="UNKNOWN",
        source_sha256={k: v["sha256"] for k, v in C.source_proof().items()},
        terms_sha256=C.TERMS_SHA,
    )

    def run():
        return C.capture(
            output,
            target,
            rule,
            rr,
            transport,
            protocol_raw=S.encode(plan),
            clock=lambda: current[0],
        )

    return run, output, calls, catalog, mutator, current, plan


def test_six_get_actual_adapter_average_four_durable_decisions_and_external_pins(
    tmp_path, monkeypatch
):
    run, out, calls, _, _, _, _ = harness(tmp_path, monkeypatch)
    result = run()
    assert len(calls) == 6
    assert result["event_count"] == 1 and len(result["decisions"]) == 4
    selected = json.loads((out / "selection.json").read_bytes())["selected"]
    assert [x.rsplit("-", 1)[1] for x in selected] == ["B100", "B101"]
    assert result["execution_authority"] is False
    for item in result["decisions"]:
        d = item["decision"]
        assert d["forecast"]["model"] == "crypto_settlement_average_research_v1"
        assert d["cf_evidence"]["prices"] == 3600
        assert d["journal_completion"]["status"] == "COMPLETE_RESEARCH"
        assert all(x["net_ev"] is None and x["exchange_fee"] is None for x in d["rows"])
    pins = json.loads((out / "shadow-pins.json").read_bytes())
    with sqlite3.connect(out / "research.db") as db:
        assert set(
            x[0] for x in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ) == {"research_shadow", "research_completion"}
        for pin in pins["decisions"]:
            assert (
                db.execute(
                    "SELECT payload_sha FROM research_shadow WHERE id=?", (pin["decision_id"],)
                ).fetchone()[0]
                == pin["payload_sha256"]
            )
            assert (
                db.execute(
                    "SELECT payload_sha FROM research_completion WHERE id=?", (pin["decision_id"],)
                ).fetchone()[0]
                == pin["completion_sha256"]
            )
    completion = json.loads((out / "completion.json").read_bytes())
    assert all(S.sha((out / k).read_bytes()) == v for k, v in completion["files"].items())
    with pytest.raises(FileExistsError):
        run()
    assert len(calls) == 6


def test_partial_catalog_fails_before_cf_and_no_journal(tmp_path, monkeypatch):
    run, out, calls, catalog, _, _, _ = harness(tmp_path, monkeypatch)
    catalog["cursor"] = "more"
    with pytest.raises(ValueError, match="COMPLETE_EVENT"):
        run()
    assert len(calls) == 1 and not (out / "research.db").exists()


def test_http_error_original_retained_both_selected_captured_no_fallback(tmp_path, monkeypatch):
    run, out, calls, _, mutator, _, _ = harness(tmp_path, monkeypatch)
    mutator[0] = lambda url, body: (
        (429, b'{"error":"bounded"}') if "B100/orderbook" in url else None
    )
    with pytest.raises(ValueError, match="SELECTED_ORIGINAL"):
        run()
    assert len(calls) == 6
    assert (out / "0-book.original").read_bytes() == b'{"error":"bounded"}'
    assert json.loads((out / "0-book.receipt.json").read_bytes())["http_status"] == 429
    assert not (out / "research.db").exists()


def test_original_market_semantics_changed_after_selection_rejected(tmp_path, monkeypatch):
    run, out, calls, _, mutator, _, _ = harness(tmp_path, monkeypatch)

    def change(url, body):
        if "market" in body:
            return 200, S.encode({"market": dict(body["market"], cap_strike="999")})

    mutator[0] = change
    with pytest.raises(ValueError, match="SEMANTICS_CHANGED"):
        run()
    assert len(calls) == 6 and not (out / "research.db").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_gets", True),
        ("decimal_places", True),
        ("benchmark", "BRTI"),
        ("rule_authority", "CERTIFIED"),
        ("source_sha256", {}),
        ("terms_sha256", "0" * 64),
    ],
)
def test_predeclared_plan_mismatch_no_get_no_directory(tmp_path, monkeypatch, field, value):
    run, out, calls, _, _, _, plan = harness(tmp_path, monkeypatch)
    plan[field] = value
    with pytest.raises(ValueError, match="PLAN"):
        run()
    assert not calls and not out.exists()


def test_early_or_late_start_never_requests(tmp_path, monkeypatch):
    run, out, calls, _, _, current, _ = harness(tmp_path, monkeypatch)
    for offset in (-1, 50):
        current[0] = NOW + timedelta(seconds=offset)
        with pytest.raises(ValueError, match="FUTURE_WINDOW"):
            run()
    assert not calls and not out.exists()


def test_final_fsync_crossing_deadline_records_failure_not_success(tmp_path, monkeypatch):
    run, out, calls, _, _, current, _ = harness(tmp_path, monkeypatch)
    persist = C.persist

    def delayed(path, raw):
        persist(path, raw)
        if path.name == "completion.json":
            current[0] = NOW + timedelta(seconds=51)

    monkeypatch.setattr(C, "persist", delayed)
    with pytest.raises(ValueError, match="DEADLINE"):
        run()
    assert (out / "failure.json").exists()
    assert len(calls) == 6


def test_catalog_ticker_path_injection_refused_before_further_gets(tmp_path, monkeypatch):
    run, out, calls, catalog, _, _, _ = harness(tmp_path, monkeypatch)
    catalog["markets"][0]["ticker"] += "/../../account/limits"
    with pytest.raises(ValueError, match="EXACT_FUTURE"):
        run()
    assert len(calls) == 1


def test_future_cf_level_cannot_select_markets_or_trigger_book_requests(tmp_path, monkeypatch):
    run, out, calls, _, mutator, _, _ = harness(tmp_path, monkeypatch)

    def future(url, body):
        if "/cfbenchmarks/" in url:
            changed = json.loads(S.encode(body))
            changed["data"]["serverTime"] = (NOW + timedelta(seconds=1)).isoformat()
            return 200, S.encode(changed)

    mutator[0] = future
    with pytest.raises(ValueError, match="CF_SELECTION"):
        run()
    assert len(calls) == 2 and not (out / "selection.json").exists()


def test_extreme_finite_strike_exponent_rejected_before_fraction_allocation(tmp_path, monkeypatch):
    run, out, calls, catalog, _, _, _ = harness(tmp_path, monkeypatch)
    catalog["markets"][0]["cap_strike"] = "1e999999999"
    with pytest.raises(ValueError, match="FINITE_RANGE"):
        run()
    assert len(calls) == 1


def test_per_request_reservation_durable_before_transport_and_deadline_rechecked(
    tmp_path, monkeypatch
):
    run, out, calls, _, _, current, _ = harness(tmp_path, monkeypatch)
    persist = C.persist

    def expire(path, raw):
        persist(path, raw)
        if path.name == "catalog.reservation.json":
            current[0] = NOW + timedelta(seconds=51)

    monkeypatch.setattr(C, "persist", expire)
    with pytest.raises(ValueError, match="DEADLINE"):
        run()
    assert not calls
    reservation = json.loads((out / "catalog.reservation.json").read_bytes())
    assert reservation["attempt"] == 1
    assert reservation["status"] == "RESERVED_BEFORE_TRANSPORT_SEND_UNCONFIRMED"
    assert json.loads((out / "failure.json").read_bytes())["requests"] == 0


def test_backward_response_clock_within_window_preserves_original_but_refuses(
    tmp_path, monkeypatch
):
    run, out, calls, _, mutator, current, _ = harness(tmp_path, monkeypatch)
    persist = C.persist

    def advance(path, raw):
        persist(path, raw)
        if path.name == "catalog.reservation.json":
            current[0] = NOW + timedelta(seconds=2)

    monkeypatch.setattr(C, "persist", advance)

    def backwards(url, body):
        current[0] = NOW + timedelta(seconds=1)

    mutator[0] = backwards
    with pytest.raises(ValueError, match="CLOCK_REVERSED"):
        run()
    assert len(calls) == 1
    assert (out / "catalog.original").exists()
    assert not (out / "cf.reservation.json").exists()
