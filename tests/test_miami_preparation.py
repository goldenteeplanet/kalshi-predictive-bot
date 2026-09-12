# ruff: noqa: F811
"""Actual isolated Miami engine path; synthetic policies confer no real authority."""

import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from test_guarded_fee_contract import synthetic_evidence
from test_miami_source_gate import artifact, context, fixtures, grid_context, receipt  # noqa: F401

from kalshi_predictor.config import Settings
from kalshi_predictor.data.schema import (
    AdvancedRiskReservation,
    Base,
    Forecast,
    MarketSnapshot,
    PaperFill,
    PaperOrder,
    PaperPosition,
    WeatherFeature,
)
from kalshi_predictor.overnight_paper import miami_preparation as module
from kalshi_predictor.overnight_paper.miami_binding import MiamiOriginal


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value
        value.rollback()
    engine.dispose()


@pytest.fixture
def prepared_inputs(grid_context, monkeypatch):
    at = fixtures.CUTOFF + timedelta(minutes=30, seconds=2)
    clock = [at]

    def now():
        clock[0] += timedelta(milliseconds=1)
        return clock[0]

    monkeypatch.setattr(module, "utc_now", now)
    market = grid_context.market.artifact.decode()
    market["market"].update(
        volume_fp="20000",
        open_interest_fp="5000",
        liquidity_dollars="50000",
        price_ranges=[dict(start="0", end="1", step="0.01")],
        title="Miami temperature",
        open_time=(at - timedelta(hours=1)).isoformat(),
    )
    event = grid_context.event.artifact.decode()
    event["event"].update(fee_type_override=None, fee_multiplier_override=None)
    series = grid_context.series.artifact.decode()
    series["series"].update(fee_type="quadratic", fee_multiplier="1")
    originals = [
        replace(o, artifact=artifact(data))
        for o, data in zip(
            (grid_context.market, grid_context.event, grid_context.series),
            (market, event, series),
            strict=True,
        )
    ]
    ctx = replace(
        grid_context,
        market=originals[0],
        event=originals[1],
        series=originals[2],
        catalog_receipts=tuple(receipt(o) for o in originals),
    )
    book = MiamiOriginal(
        artifact(
            dict(orderbook_fp=dict(yes_dollars=[["0.39", "100"]], no_dollars=[["0.60", "100"]]))
        ),
        ctx.market.url + "/orderbook",
        at - timedelta(seconds=1),
    )
    fee = synthetic_evidence(monkeypatch, now=at, ticker=market["market"]["ticker"])
    fee["captures"] = []
    for original in originals:
        raw = json.dumps(
            dict(
                url=original.url,
                received_at=original.received_at.isoformat(),
                body=original.artifact.decode(),
            )
        ).encode()
        fee["captures"].append(dict(payload_hex=raw.hex(), sha256=hashlib.sha256(raw).hexdigest()))
    settings = Settings(
        _env_file=None,
        execution_enabled=False,
        execution_dry_run=True,
        execution_kill_switch=True,
        execution_gateway_mode="disabled",
        autopilot_enabled=False,
        learning_mode=False,
        dynamic_position_sizing_mode="shadow",
        advanced_risk_engine_mode="shadow",
        kalshi_api_key_id=None,
        kalshi_private_key_path=None,
        postgres_password="",
        execution_confirmation_token="",
    )
    return dict(
        context=ctx,
        orderbook=book,
        orderbook_receipt=receipt(book),
        settings=settings,
        slippage_allowance=Decimal(".01"),
        uncertainty_buffer=Decimal(".01"),
        fee_evidence=fee,
    )


def test_actual_miami_records_and_engines_no_noaa_or_trade(session, prepared_inputs):
    result = module.prepare_miami_candidate(session, **prepared_inputs)
    assert result.state == "COMPUTED_UNQUALIFIED", result.blockers
    records, engines = result.records, result.engine_outputs
    forecast = session.get(Forecast, records["forecast_id"])
    snapshot = session.get(MarketSnapshot, records["snapshot_id"])
    assert forecast.model_name == "miami_prior_day_increment_grid30_v1"
    assert Decimal(forecast.yes_probability) == Decimal(str(2 / 3))
    assert snapshot.ticker == forecast.ticker
    assert records["sizing_id"] > 0 and records["risk_id"] > 0
    assert records["sizing"] == engines.phase3m.as_dict()
    assert records["risk"] == engines.phase3n.as_dict()
    assert records["sizing_evidence"]["history"]["sample_size"] == 0
    assert not result.paper_eligible and not result.execution_authority
    assert "CERTIFIED_RULE_AND_72H_FINALITY_REQUIRED" in result.blockers
    assert "MODEL_RELEASE_AND_EVALUATION_REQUIRED" in result.blockers
    assert (
        records["forecast_generated_at"]
        <= records["forecast_available_at"]
        <= records["decision_at"]
    )
    for model in (PaperOrder, PaperFill, PaperPosition, AdvancedRiskReservation, WeatherFeature):
        assert session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize("change", ["fee", "wrong_fee", "book", "stale", "unsafe", "credential"])
def test_missing_or_unsafe_evidence_produces_no_forecast(session, prepared_inputs, change):
    args = dict(prepared_inputs)
    if change == "fee":
        args["fee_evidence"] = None
    elif change == "wrong_fee":
        args["fee_evidence"]["captures"].pop()
    elif change == "book":
        args["orderbook"] = replace(args["orderbook"], artifact=artifact({"orderbook_fp": {}}))
        args["orderbook_receipt"] = receipt(args["orderbook"])
    elif change == "stale":
        args["orderbook"] = replace(
            args["orderbook"], received_at=args["orderbook"].received_at - timedelta(minutes=2)
        )
        args["orderbook_receipt"] = receipt(args["orderbook"])
    elif change == "unsafe":
        args["settings"] = args["settings"].model_copy(update={"execution_enabled": True})
    else:
        args["settings"] = args["settings"].model_copy(update={"kalshi_api_key_id": "forbidden"})
    result = module.prepare_miami_candidate(session, **args)
    assert result.state == "BLOCKED"
    assert result.engine_outputs is None
    assert session.scalar(select(func.count()).select_from(Forecast)) == 0


def test_file_sqlite_rejected_before_writes(tmp_path, prepared_inputs):
    engine = create_engine("sqlite:///" + str(tmp_path / "isolated.db"))
    with Session(engine) as session:
        result = module.prepare_miami_candidate(session, **prepared_inputs)
        assert result.state == "BLOCKED" and result.blockers == ("IN_MEMORY_SQLITE_ONLY",)
    engine.dispose()


def test_verified_handoff_checks_persisted_originals(session, prepared_inputs):
    result = module.prepare_miami_candidate(session, **prepared_inputs)
    assert result.state == "COMPUTED_UNQUALIFIED", result.blockers
    module.verify_miami_preparation_handoff(session, result, now=module.utc_now())


@pytest.mark.parametrize("change", ["id", "probability", "decision", "source", "engine"])
def test_handoff_rejects_changed_evidence(session, prepared_inputs, change):
    result = module.prepare_miami_candidate(session, **prepared_inputs)
    assert result.state == "COMPUTED_UNQUALIFIED", result.blockers
    if change == "id":
        result.records["forecast_id"] = True
    elif change == "probability":
        row = session.get(Forecast, result.records["forecast_id"])
        row.yes_probability = "0.99"
    elif change == "decision":
        result = replace(
            result,
            engine_outputs=replace(
                result.engine_outputs, decision=replace(result.engine_outputs.decision, quantity=2)
            ),
        )
    elif change == "source":
        result.source_bundle["available_at"] = "2026-09-10T21:30:00Z"
    else:
        result.records["risk"]["action"] = "ALLOW_CHANGED"
    with pytest.raises(ValueError):
        module.verify_miami_preparation_handoff(session, result, now=module.utc_now())


def test_caller_rollback_removes_successful_preparation(session, prepared_inputs):
    result = module.prepare_miami_candidate(session, **prepared_inputs)
    assert result.state == "COMPUTED_UNQUALIFIED", result.blockers
    session.rollback()
    assert session.scalar(select(func.count()).select_from(Forecast)) == 0
    assert session.scalar(select(func.count()).select_from(MarketSnapshot)) == 0


def test_target_expiry_during_engine_work_rolls_back(session, prepared_inputs, monkeypatch):
    original = module.insert_advanced_risk_decision

    def expire(*args, **kwargs):
        row = original(*args, **kwargs)
        monkeypatch.setattr(module, "utc_now", lambda: fixtures.ORIGIN + timedelta(hours=2))
        return row

    monkeypatch.setattr(module, "insert_advanced_risk_decision", expire)
    result = module.prepare_miami_candidate(session, **prepared_inputs)
    assert result.state == "BLOCKED"
    assert session.scalar(select(func.count()).select_from(Forecast)) == 0
    assert session.scalar(select(func.count()).select_from(MarketSnapshot)) == 0


def test_attached_database_rejected(session, prepared_inputs):
    session.connection().exec_driver_sql("ATTACH DATABASE ':memory:' AS other")
    result = module.prepare_miami_candidate(session, **prepared_inputs)
    assert result.blockers == ("ATTACHED_DATABASE_FORBIDDEN",)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://user:secret@example.invalid/db",
        "postgresql://user@example.invalid/db",
        "sqlite:///:memory:?password=secret",
        "sqlite:///:memory:#token",
    ],
)
def test_unused_database_url_cannot_leak_credentials(session, prepared_inputs, url):
    args = dict(prepared_inputs)
    args["settings"] = args["settings"].model_copy(update={"kalshi_db_url": url})
    result = module.prepare_miami_candidate(session, **args)
    assert result.state == "BLOCKED"
    assert result.records == {} and result.source_bundle is None
    assert "secret" not in str(result)
    assert session.scalar(select(func.count()).select_from(Forecast)) == 0


@pytest.mark.parametrize("change", ["ev", "book", "availability", "sizing_evidence"])
def test_handoff_rejects_mutated_assembly_records(session, prepared_inputs, change):
    result = module.prepare_miami_candidate(session, **prepared_inputs)
    assert result.state == "COMPUTED_UNQUALIFIED", result.blockers
    if change == "ev":
        result.records["ev"]["net_ev"] = Decimal("99")
    elif change == "book":
        result.records["book_qualification"]["executable"] = False
    elif change == "availability":
        result.records["forecast_available_at"] = "2099-01-01T00:00:00+00:00"
    else:
        result.records["sizing_evidence"] = {"forged": True}
    with pytest.raises(ValueError, match="RECORDS_CHANGED"):
        module.verify_miami_preparation_handoff(session, result, now=module.utc_now())


def test_pending_caller_rows_cannot_be_implicitly_flushed(session, prepared_inputs):
    session.add(PaperOrder())
    result = module.prepare_miami_candidate(session, **prepared_inputs)
    assert result.blockers == ("UNFLUSHED_CALLER_CHANGES_FORBIDDEN",)
    assert result.engine_outputs is None
    assert len(session.new) == 1


@pytest.mark.parametrize("mutation", ["missing", "changed", "boolean", "hash"])
def test_handoff_binds_actual_persisted_risk_fee_amount(session, prepared_inputs, mutation):
    from kalshi_predictor.data.schema import AdvancedRiskDecisionLog

    result = module.prepare_miami_candidate(session, **prepared_inputs)
    assert result.state == "COMPUTED_UNQUALIFIED", result.blockers
    row = session.get(AdvancedRiskDecisionLog, result.records["risk_id"])
    payload = json.loads(row.raw_json)
    assert payload["raw"]["estimated_round_trip_fees"] == str(
        result.engine_outputs.risk_request.estimated_round_trip_fees
    )
    if mutation == "missing":
        del payload["raw"]["estimated_round_trip_fees"]
    elif mutation == "changed":
        payload["raw"]["estimated_round_trip_fees"] = "99"
    elif mutation == "boolean":
        payload["raw"]["estimated_round_trip_fees"] = False
    else:
        payload["raw"]["guarded_fee_quote_sha256"] = "f" * 64
    row.raw_json = json.dumps(payload)
    session.flush()
    with pytest.raises(ValueError, match="MIAMI_HANDOFF_FEE_CHANGED"):
        module.verify_miami_preparation_handoff(session, result, now=module.utc_now())
