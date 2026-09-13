import copy
import json
from datetime import timedelta
from decimal import ROUND_CEILING, Inexact, localcontext

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_current_research_dashboard import NOW, add_scan, database

from kalshi_predictor.overnight_paper.current_assessment_view import (
    latest_assessment_batch,
    render_assessment_batch,
)
from kalshi_predictor.overnight_paper.current_research_dashboard import current_research_snapshot
from kalshi_predictor.overnight_paper.current_research_store import append_current_record
from kalshi_predictor.overnight_paper.dashboard import create_router
from kalshi_predictor.ui.positive_ev import render_research


def record(**changes):
    row = dict(
        ticker="KXBTC-EXAMPLE-B76750",
        side="NO",
        assessed_at=NOW.isoformat(),
        scan_sha256="b" * 64,
        forecast_probability="0.73273093467024886",
        executable_price="0.64",
        gross_edge="0.09273093467024886",
        after_fee="0.07273093467024886",
        after_execution="0.07273093467024886",
        scope="CURRENT_UNCALIBRATED_RESEARCH",
        paper_eligible=False,
        execution_authority=False,
        full_net_ev=None,
        fee=dict(
            value="0.02",
            status="ESTIMATED_WITH_SUPPORT",
            method="PUBLIC_GENERAL_TAKER_CENT_PAPER_V1",
            scope="LOCAL_PAPER_MODEL_NOT_ACCOUNT_INVOICE",
            unit="USD_PER_ONE_DOLLAR_PAYOUT",
            paper_support=True,
            exact_account_fee_certified=False,
            execution_authority=False,
        ),
        snapshot_impact=dict(
            value="0",
            status="CERTIFIED",
            method="ONE_CONTRACT_SNAPSHOT_VWAP",
            scope="CONDITIONAL_SIMULATED_FILL_AT_CAPTURED_BOOK",
            unit="USD_PER_ONE_DOLLAR_PAYOUT",
            fill_status="BOOK_FILL_PRICE_KNOWN",
            paper_support=True,
            execution_authority=False,
        ),
    )
    row.update(changes)
    return row


def envelopes(*rows):
    return [dict(record_kind="ASSESSMENT", record=r, recorded_at=r["assessed_at"]) for r in rows]


def test_latest_batch_is_chronological_and_not_positive_edge_selection():
    old = record(
        assessed_at=(NOW - timedelta(minutes=1)).isoformat(),
        forecast_probability="0.99",
        gross_edge="0.35",
        after_fee="0.33",
        after_execution="0.33",
    )
    latest = record(
        forecast_probability="0.5", gross_edge="-0.14", after_fee="-0.16", after_execution="-0.16"
    )
    before = copy.deepcopy(envelopes(old, latest))
    result = latest_assessment_batch(before, now=NOW)
    assert result["rows"][0]["after_recorded_costs"] == "-0.16"
    assert result["batch_completeness"] == "UNKNOWN" and not result["original_replay"]
    assert before == envelopes(old, latest)


def test_exact_example_arithmetic_and_stale_status():
    result = latest_assessment_batch(envelopes(record()), now=NOW + timedelta(minutes=6))
    row = result["rows"][0]
    assert row["gross_edge"] == "0.09273093467024886"
    assert row["after_recorded_costs"] == "0.07273093467024886"
    assert row["full_net_ev"] is None and not row["paper_eligible"]
    assert result["freshness"] == "STALE" and result["age_seconds"] == 360


def test_tiny_finite_forecast_is_valid_and_side_complement_is_checked():
    row = record(side="YES", forecast_probability="1e-80", forecast={"probability_yes": "1e-80"},
                 gross_edge="-0.64", after_fee="-0.66", after_execution="-0.66")
    result = latest_assessment_batch(envelopes(row), now=NOW)
    assert result["rows"][0]["after_recorded_costs"] == "-0.66"
    row = record(forecast={"probability_yes": "0.26726906532975114"})
    assert latest_assessment_batch(envelopes(row), now=NOW)["rows"]
    row["forecast"]["probability_yes"] = "0.73273093467024886"
    result = latest_assessment_batch(envelopes(row), now=NOW)
    assert result["status"] == "INVALID_OR_AMBIGUOUS" and not result["rows"]


def test_caller_decimal_rounding_traps_and_exponent_limits_do_not_change_capture_arithmetic():
    row = record(side="YES", forecast_probability="1e-80", forecast={"probability_yes": "1e-80"},
                 gross_edge="-0.64", after_fee="-0.66", after_execution="-0.66")
    expected = latest_assessment_batch(envelopes(row), now=NOW)
    with localcontext() as context:
        context.prec = 3
        context.rounding = ROUND_CEILING
        context.traps[Inexact] = True
        context.Emin = -9
        context.Emax = 9
        assert latest_assessment_batch(envelopes(row), now=NOW) == expected


def test_html_limits_twenty_rows_without_changing_api_batch_or_ranking_on_edge():
    rows = [record(ticker=f"BTC-{i:02}") for i in range(22)]
    batch = latest_assessment_batch(envelopes(*reversed(rows)), now=NOW)
    html = render_assessment_batch(batch)
    assert len(batch["rows"]) == 22
    assert "Showing 20 of 22 unique rows; 22 recorded rows" in html
    assert "BTC-00" in html and "BTC-19" in html and "BTC-20" not in html


@pytest.mark.parametrize(
    "field",
    [
        "forecast_probability",
        "executable_price",
        "gross_edge",
        "after_fee",
        "after_execution",
        "fee",
        "snapshot_impact",
    ],
)
def test_missing_inputs_never_reconstruct_unrecorded_edge(field):
    row = record(**{field: None})
    result = latest_assessment_batch(envelopes(row), now=NOW)
    assert result["rows"][0]["after_recorded_costs"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("gross_edge", "0.1"),
        ("after_fee", "0.09"),
        ("after_execution", "0.08"),
        ("forecast_probability", "NaN"),
        ("gross_edge", "Infinity"),
        ("forecast_probability", "1e-999999999"),
        ("executable_price", True),
        ("forecast_probability", "1.1"),
        ("gross_edge", 0.09),
    ],
)
def test_invalid_numbers_and_arithmetic_fail_closed(field, value):
    result = latest_assessment_batch(envelopes(record(**{field: value})), now=NOW)
    assert result["status"] == "INVALID_OR_AMBIGUOUS" and not result["rows"]


@pytest.mark.parametrize("component", ["fee", "snapshot_impact"])
def test_unknown_component_labels_do_not_claim_supported_aftercost(component):
    row = record()
    row[component]["status"] = "UNKNOWN"
    result = latest_assessment_batch(envelopes(row), now=NOW)
    assert result["rows"][0]["after_recorded_costs"] is None


@pytest.mark.parametrize("defect", ["hash", "conflict", "future", "oversize"])
def test_ambiguous_latest_batch_is_not_arbitrarily_selected(defect):
    first, second = record(), record()
    if defect == "hash":
        second["scan_sha256"] = "c" * 64
    if defect == "conflict":
        second["executable_price"] = "0.65"
    rows = envelopes(first, second)
    if defect == "future":
        rows[0]["recorded_at"] = (NOW - timedelta(seconds=1)).isoformat()
    if defect == "oversize":
        rows *= 301
    result = latest_assessment_batch(rows, now=NOW)
    assert result["status"] == "INVALID_OR_AMBIGUOUS" and not result["rows"]


def test_identical_duplicates_deduplicate_but_completeness_stays_unknown():
    result = latest_assessment_batch(envelopes(record(), record()), now=NOW)
    assert len(result["rows"]) == 1 and result["recorded_row_count"] == 2
    assert result["batch_completeness"] == "UNKNOWN"


def test_ticker_is_escaped_and_no_canonical_net_claim_from_tampered_field():
    batch = latest_assessment_batch(
        envelopes(record(ticker="<script>alert(1)</script>", full_net_ev="999")), now=NOW
    )
    html = render_assessment_batch(batch)
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "Full net EV: Unknown" in html and "Paper eligibility: No" in html
    assert "999" not in html


def test_snapshot_uses_same_journal_and_keeps_legacy_scan_separate(tmp_path):
    with database(tmp_path / "db") as db:
        db.execute("BEGIN")
        add_scan(db, NOW - timedelta(hours=1), 0, "legacy")
        append_current_record(
            db, kind="ASSESSMENT", identity="new", payload=record(), recorded_at=NOW
        )
        before = list(db.execute("SELECT * FROM overnight_sprint_cycles"))
        result = current_research_snapshot(db, now=NOW)
        assert result["latest_scan_funnel"]["markets_scanned"] == 0
        assert result["latest_scan_freshness"] == "STALE"
        assert result["latest_assessment_batch"]["rows"][0]["after_recorded_costs"] is not None
        assert result["scan_count"] == 1 and result["assessment_count"] == 1
        assert before == list(db.execute("SELECT * FROM overnight_sprint_cycles"))


def test_api_and_html_include_latest_captured_values_without_database_mutation(
    tmp_path, monkeypatch
):
    path = tmp_path / "db"
    with database(path) as db:
        db.execute("BEGIN")
        append_current_record(
            db, kind="ASSESSMENT", identity="new", payload=record(), recorded_at=NOW
        )
    before = path.read_bytes()
    monkeypatch.setenv("OVERNIGHT_PAPER_DB", str(path))
    app = FastAPI()
    app.include_router(create_router())
    with TestClient(app) as client:
        body = client.get("/api/paper-live").json()
        batch = body["current_research"]["latest_assessment_batch"]
        assert batch["rows"][0]["after_recorded_costs"] == "0.07273093467024886"
        page = client.get("/paper-live").text
        assert "Latest recorded assessment batch" in page and "0.07273093467024886" in page
        assert "not live quotes or paper-ready" in page
        assert body["paper_mode"] == "NOT_ACTIVE"
        json.dumps(body, allow_nan=False)
    assert path.read_bytes() == before


def test_positive_ev_links_existing_readonly_page_and_preserves_historical_report():
    report = {"fresh": False, "generated_at": "earlier", "rows": [],
              "top_opportunity": "old report", "metrics": {"positive_gross_ev": 7}}
    before = copy.deepcopy(report)
    html = render_research(report)
    assert "href='/paper-live'>Current assessments and shadow lifecycle" in html
    assert "Earlier research report" in html and "old report" in html
    assert "Stale or unavailable snapshot" in html and report == before
