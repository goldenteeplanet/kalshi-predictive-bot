"""Actual existing NOAA/features/model/services; no paper orders or mocked verifiers."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from test_overnight_provenance import artifact
from test_overnight_qualification import structured_gate

from kalshi_predictor.config import Settings
from kalshi_predictor.data.repositories import insert_market_snapshot
from kalshi_predictor.data.schema import (
    Base,
    Forecast,
    MarketSnapshot,
    PaperFill,
    PaperOrder,
    PaperPosition,
    WeatherFeature,
)
from kalshi_predictor.overnight_paper.preparation import prepare_weather_candidate
from kalshi_predictor.overnight_paper.qualification import PUBLIC_BASE, EvidenceReference


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value
        value.rollback()
    engine.dispose()


def original_inputs(*, stale=False, missing_clock=False, occurrence_offset=0, rule_offset=0):
    import json

    reference, inputs = structured_gate()
    now = datetime.now(UTC) - timedelta(seconds=1)
    target = (now + timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
    local = target.astimezone(ZoneInfo("America/New_York"))
    ticker = "KXTEMPNYCH-" + local.strftime("%y%b%d%H").upper() + "-T70"
    event = ticker.rsplit("-", 1)[0]
    stated = local + timedelta(hours=rule_offset)
    phrase = (
        f"{stated.strftime('%b')} {stated.day}, {stated.year} "
        f"{stated.hour % 12 or 12} {stated.strftime('%p %Z')}"
    )
    envelopes = []
    for source in reference.sources:
        raw = source.payload.decode().replace(inputs["ticker"], ticker)
        raw = raw.replace(inputs["event_id"], event)
        row = json.loads(raw)
        row["received_at"] = now.isoformat()
        if "market" in row["body"]:
            row["body"]["market"].update(
                title="New York temperature above 70F",
                close_time=target.isoformat(),
                occurrence_datetime=(target + timedelta(minutes=occurrence_offset)).isoformat(),
                expected_expiration_time=(target + timedelta(minutes=5)).isoformat(),
                floor_strike=70,
                strike_type="greater",
                rules_primary=(
                    f"If the temperature recorded at Central Park, New York City for {phrase} "
                    "as reported by The Weather Company (for coordinates KNYC), "
                    "is above 70 degrees, then the market resolves to Yes."
                ),
                volume_24h_fp="20000",
            )
        if "event" in row["body"]:
            row["body"]["event"]["strike_date"] = target.isoformat()
        if row["url"].endswith("/orderbook"):
            row["body"]["orderbook_fp"]["yes_dollars"] = [["0.49", "1000"]]
        if "forecast/hourly" in row["url"]:
            props = row["body"]["properties"]
            props["generatedAt"] = now.isoformat()
            props["updateTime"] = (now - timedelta(hours=2) if stale else now).isoformat()
            if missing_clock:
                props.pop(missing_clock if isinstance(missing_clock, str) else "updateTime")
            props["periods"] = [
                {
                    "startTime": target.isoformat(),
                    "endTime": (target + timedelta(hours=1)).isoformat(),
                    "temperature": 90,
                    "temperatureUnit": "F",
                }
            ]
        value = artifact(row)
        envelopes.append(EvidenceReference(source.artifact, value.sha256, value.payload))
    return ticker, tuple(envelopes), now


def compute(session, **source_options):
    ticker, envelopes, _ = original_inputs(**source_options)
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
        weather_v2_knyc_observation_enabled=False,
    )
    return prepare_weather_candidate(
        session,
        ticker=ticker,
        source_envelopes=envelopes,
        settings=settings,
        slippage_allowance=Decimal("0.01"),
        uncertainty_buffer=Decimal("0.01"),
    )


def test_actual_existing_weather_computation_preserves_clocks_and_has_no_trade(session):
    result = compute(session)
    assert result.state == "COMPUTED_UNQUALIFIED", result.blockers
    assert result.decision is not None
    assert result.phase3m is not None and result.phase3n is not None
    assert result.risk_request is not None and result.forecast_output is not None
    assert result.records["sizing"]["proposed_contracts"] == 1
    assert result.records["risk"]["action"] == "ALLOW"
    assert "MODEL_LINEAGE_AND_EVALUATION_REQUIRED" in result.blockers
    assert "SETTLEMENT_RULE_QUALIFICATION_REQUIRED" in result.blockers
    forecast = session.get(Forecast, result.records["forecast_id"])
    snapshot = session.get(MarketSnapshot, result.records["snapshot_id"])
    assert forecast.forecasted_at >= snapshot.captured_at
    assert datetime.fromisoformat(
        result.records["forecast_generated_at"]
    ) >= datetime.fromisoformat(result.records["feature_available_at"])
    assert result.records["forecast_available_at"] >= result.records["forecast_generated_at"]
    assert result.records["sizing_evidence"]["history"]["sample_size"] == 0
    for model in (PaperOrder, PaperFill, PaperPosition):
        assert session.scalar(select(func.count()).select_from(model)) == 0
    assert session.get(WeatherFeature, result.records["feature_id"]) is not None
    assert all(source.valid() for source in result.source_envelopes)


@pytest.mark.parametrize(
    "options", [{"stale": True}, {"missing_clock": True}, {"missing_clock": "generatedAt"}]
)
def test_stale_or_missing_provider_clock_stops_before_parser_fallback(session, options):
    result = compute(session, **options)
    assert result.state == "BLOCKED"
    assert session.scalar(select(func.count()).select_from(Forecast)) == 0
    assert session.scalar(select(func.count()).select_from(WeatherFeature)) == 0
    assert session.scalar(select(func.count()).select_from(MarketSnapshot)) == 0


def test_newer_book_cannot_replace_original_decision_snapshot(session):
    import json

    ticker, sources, receipt = original_inputs()
    rows = {
        json.loads(source.payload)["url"]: json.loads(source.payload)["body"] for source in sources
    }
    insert_market_snapshot(
        session,
        rows[f"{PUBLIC_BASE}/markets/{ticker}"]["market"],
        rows[f"{PUBLIC_BASE}/markets/{ticker}/orderbook"],
        receipt + timedelta(minutes=5),
    )
    result = prepare_weather_candidate(
        session,
        ticker=ticker,
        source_envelopes=sources,
        settings=Settings(
            _env_file=None,
            execution_enabled=False,
            execution_dry_run=True,
            execution_kill_switch=True,
            execution_gateway_mode="disabled",
            autopilot_enabled=False,
        ),
        slippage_allowance=Decimal("0.01"),
        uncertainty_buffer=Decimal("0.01"),
    )
    assert result.blockers == ("SNAPSHOT_SELECTION_MISMATCH",)
    assert session.scalar(select(func.count()).select_from(Forecast)) == 0


def test_rehashed_wrong_station_is_rejected(session):
    import json

    ticker, sources, _ = original_inputs()
    changed = []
    for source in sources:
        payload = json.loads(source.payload)
        if payload["url"].endswith("/stations/KNYC"):
            payload["body"]["properties"]["stationIdentifier"] = "KLGA"
        body = artifact(payload)
        changed.append(EvidenceReference(source.artifact, body.sha256, body.payload))
    result = prepare_weather_candidate(
        session,
        ticker=ticker,
        source_envelopes=tuple(changed),
        settings=Settings(
            _env_file=None,
            execution_enabled=False,
            execution_dry_run=True,
            execution_kill_switch=True,
            execution_gateway_mode="disabled",
            autopilot_enabled=False,
        ),
        slippage_allowance=Decimal("0.01"),
        uncertainty_buffer=Decimal("0.01"),
    )
    assert result.blockers == ("STATION_IDENTITY_MISMATCH",)
    assert session.scalar(select(func.count()).select_from(Forecast)) == 0


def test_disabled_execution_is_required_before_persistence(session):
    ticker, sources, _ = original_inputs()
    settings = Settings(_env_file=None, execution_enabled=True, execution_dry_run=True)
    result = prepare_weather_candidate(
        session,
        ticker=ticker,
        source_envelopes=sources,
        settings=settings,
        slippage_allowance=Decimal("0.01"),
        uncertainty_buffer=Decimal("0.01"),
    )
    assert result.blockers == ("LOCAL_ONLY_SETTINGS_REQUIRED",)
    assert session.scalar(select(func.count()).select_from(MarketSnapshot)) == 0


def test_occurrence_five_minutes_after_observation_does_not_change_target(session):
    result = compute(session, occurrence_offset=5)
    assert result.state == "COMPUTED_UNQUALIFIED", result.blockers
    evidence = result.records["analytical_observation_evidence"]
    observed = datetime.fromisoformat(evidence["observation_time"])
    occurrence = datetime.fromisoformat(evidence["occurrence_datetime"])
    assert occurrence - observed == timedelta(minutes=5)
    assert evidence["basis"] == "EXACT_TEMPERATURE_TICKER_AND_PRIMARY_RULE"
    assert evidence["settlement_certified"] is False
    assert datetime.fromisoformat(result.records["analytical_target"]) == observed


def test_primary_rule_hour_must_match_exact_ticker_time(session):
    result = compute(session, rule_offset=1)
    assert result.blockers == ("PRIMARY_RULE_OBSERVATION_TIME_MISMATCH",)
    assert session.scalar(select(func.count()).select_from(MarketSnapshot)) == 0


def fee_ready_inputs(monkeypatch):
    """Synthetic explicit fee metadata; never enrich an operational capture."""
    import hashlib
    import json

    from test_guarded_fee_contract import synthetic_evidence

    ticker, envelopes, receipt = original_inputs()
    evidence = synthetic_evidence(monkeypatch, receipt, ticker)
    adjusted, captures = [], []
    for source in envelopes:
        row = json.loads(source.payload)
        body = row["body"]
        if "event" in body:
            body["event"].update(fee_type_override=None, fee_multiplier_override=None)
        if "series" in body:
            body["series"].update(fee_type="quadratic", fee_multiplier="1")
        raw = json.dumps(row).encode()
        sha = hashlib.sha256(raw).hexdigest()
        adjusted.append(EvidenceReference(source.artifact, sha, raw))
        if any(key in body for key in ("market", "event", "series")):
            captures.append(dict(payload_hex=raw.hex(), sha256=sha))
    evidence["captures"] = captures
    return ticker, tuple(adjusted), receipt, evidence


def test_fee_evidence_flows_through_side_selection_and_phase3n(session, monkeypatch):
    from kalshi_predictor.paper.fees import CONTRACT_KEY, single_buy_fees

    ticker, envelopes, _, evidence = fee_ready_inputs(monkeypatch)
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
        weather_v2_knyc_observation_enabled=False,
        paper_default_fee_per_contract=Decimal(".04"),
    )
    result = prepare_weather_candidate(
        session,
        ticker=ticker,
        source_envelopes=envelopes,
        settings=settings,
        slippage_allowance=Decimal(".01"),
        uncertainty_buffer=Decimal(".01"),
        fee_evidence=evidence,
    )
    assert result.state == "COMPUTED_UNQUALIFIED", result.blockers
    contract = result.decision.raw_decision_json[CONTRACT_KEY]
    expected = max(
        Decimal(".04"),
        single_buy_fees(result.decision.limit_price, Decimal(1), Decimal(".07"))["estimated_fee"],
    )
    assert Decimal(contract["simulated_charge"]) == expected
    assert result.records["ev"]["estimated_fee"] == expected
    assert result.risk_request.estimated_round_trip_fees == expected
    assert contract["side"] == result.decision.side
    assert contract["price"] == str(result.decision.limit_price)
    assert "FEE_EVIDENCE_REQUIRED_LEGACY_CONFIGURED_DIAGNOSTIC" not in result.blockers
