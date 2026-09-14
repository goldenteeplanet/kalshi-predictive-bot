"""Actual SQLite forecast lookup and fee verification; no passing-fee mocks."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from test_overnight_activation import baseline_template  # noqa: F401
from test_paper_release_dataset_store import factory  # noqa: F401

from kalshi_predictor.advanced_risk.cf_costs import cf_risk_cost_scope
from kalshi_predictor.advanced_risk.service import advanced_risk_request_for_paper_decision
from kalshi_predictor.config import Settings
from kalshi_predictor.crypto.cost_record import build_cost_record
from kalshi_predictor.data.schema import Forecast, Market
from kalshi_predictor.overnight_paper.cf_source import CLOCK_BASIS
from kalshi_predictor.paper.fees import verify_fee_quote
from kalshi_predictor.paper.models import PaperDecision

NOW = datetime(2026, 9, 12, 22, tzinfo=UTC)
SYNTHETIC_ACCOUNT = "e" * 64


def seed(session, *, stored_cf=False, declared_cf=False):
    session.add(Market(
        ticker="KXSOLE-SYNTHETIC", event_ticker="SYNTHETIC-EVENT", series_ticker="KXSOLE",
        raw_json="{}", first_seen_at=NOW, last_seen_at=NOW,
    ))
    session.flush()
    forecast = Forecast(
        ticker="KXSOLE-SYNTHETIC", forecasted_at=NOW, model_name="synthetic-unqualified",
        yes_probability="0.6", market_mid_probability="0.5",
        feature_json=json.dumps({"source_kind": CLOCK_BASIS} if stored_cf else {}),
    )
    session.add(forecast)
    session.flush()
    return PaperDecision(
        ticker=forecast.ticker, forecast_id=forecast.id, model_name=forecast.model_name,
        side="BUY_YES", probability=Decimal("0.6"), market_price=Decimal("0.5"),
        limit_price=Decimal("0.5"), edge=Decimal("0.1"), quantity=1,
        reason="synthetic risk request only",
        raw_decision_json={"source_kind": CLOCK_BASIS} if declared_cf else {},
    )


@pytest.mark.parametrize("stored_cf,declared_cf", [(True, False), (False, True), (True, True)])
@pytest.mark.parametrize("default_fee", [Decimal("0"), Decimal("0.07")])
def test_cf_request_refuses_default_fee_without_original_quote(
    factory, stored_cf, declared_cf, default_fee,  # noqa: F811
):
    with factory() as session:
        decision = seed(session, stored_cf=stored_cf, declared_cf=declared_cf)
        with pytest.raises(ValueError, match="CF_RISK_ORIGINAL_COST_RECORD_REQUIRED"):
            advanced_risk_request_for_paper_decision(
                session, decision=decision,
                settings=Settings(paper_default_fee_per_contract=default_fee),
                phase_3m_decision=None, decision_timestamp=NOW,
            )
        for table in ("paper_orders", "paper_fills", "advanced_risk_decisions"):
            assert session.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0


def test_legacy_simulation_fee_stays_explicitly_configured(factory):  # noqa: F811
    with factory() as session:
        decision = seed(session)
        result = advanced_risk_request_for_paper_decision(
            session, decision=decision,
            settings=Settings(paper_default_fee_per_contract=Decimal("0.012")),
            phase_3m_decision=None, decision_timestamp=NOW,
        )
        assert result.estimated_round_trip_fees == Decimal("0.012")


def test_cf_fee_label_does_not_replace_actual_quote_validation(factory):  # noqa: F811
    with factory() as session:
        decision = seed(session, stored_cf=True)
        decision.raw_decision_json["guarded_fee_contract"] = {
            "status": "CERTIFIED", "simulated_charge": "0",
        }
        with pytest.raises(ValueError, match="CF_RISK_ORIGINAL_COST_RECORD_REQUIRED"):
            advanced_risk_request_for_paper_decision(
                session, decision=decision, settings=Settings(),
                phase_3m_decision=None, decision_timestamp=NOW,
            )


def unknown_record(session, decision):
    forecast = session.get(Forecast, decision.forecast_id)
    forecast.feature_json = json.dumps(dict(
        source_kind=CLOCK_BASIS, model_artifact_sha256="a" * 64,
        cf_input_sha256="b" * 64, cf_target_sha256="c" * 64,
        source_hashes=["d" * 64],
    ))
    scope = cf_risk_cost_scope(
        forecast=forecast, market=session.get(Market, decision.ticker),
        decision=decision, at=NOW,
        account_identity_sha256=SYNTHETIC_ACCOUNT,
    )
    return build_cost_record(
        decision=scope, selected_probability=Decimal(scope["selected_probability"]),
        executable_price=decision.limit_price, side=decision.side.removeprefix("BUY_"),
        account_identity_sha256=SYNTHETIC_ACCOUNT,
    )


def test_actual_canonical_unknown_cost_refuses_configured_defaults(factory):  # noqa: F811
    with factory() as session:
        decision = seed(session, stored_cf=True)
        record = unknown_record(session, decision)
        assert record["assessment"]["full_net_ev"] is None
        with pytest.raises(ValueError, match="CF_RISK_SUPPORTED_COST_REQUIRED:exchange_fee"):
            advanced_risk_request_for_paper_decision(
                session, decision=decision, settings=Settings(),
                phase_3m_decision=None, decision_timestamp=NOW, cf_cost_record=record,
                cf_account_identity_sha256=SYNTHETIC_ACCOUNT,
            )


def test_canonical_stored_fee_verdict_is_not_authority(factory):  # noqa: F811
    with factory() as session:
        decision = seed(session, stored_cf=True)
        record = unknown_record(session, decision)
        record["assessment"]["exchange_fee"].update(
            value="0", status="CERTIFIED", paper_support=True,
        )
        with pytest.raises(ValueError, match="COST_RECORD_RECOMPUTATION_MISMATCH"):
            advanced_risk_request_for_paper_decision(
                session, decision=decision, settings=Settings(),
                phase_3m_decision=None, decision_timestamp=NOW, cf_cost_record=record,
                cf_account_identity_sha256=SYNTHETIC_ACCOUNT,
            )


def test_record_cannot_be_reused_at_another_executable_price(factory):  # noqa: F811
    with factory() as session:
        decision = seed(session, stored_cf=True)
        record = unknown_record(session, decision)
        decision = replace(decision, limit_price=Decimal("0.49"))
        with pytest.raises(ValueError, match="COST_RECORD_DECISION_MISMATCH"):
            advanced_risk_request_for_paper_decision(
                session, decision=decision, settings=Settings(),
                phase_3m_decision=None, decision_timestamp=NOW, cf_cost_record=record,
                cf_account_identity_sha256=SYNTHETIC_ACCOUNT,
            )


def test_incomplete_legacy_quote_has_explicit_rejection():
    with pytest.raises(ValueError, match="COMPLETE_FEE_CONTRACT_REQUIRED"):
        verify_fee_quote(
            {"status": "CERTIFIED", "simulated_charge": "0"},
            ticker="KXSOLE-SYNTHETIC", side="BUY_YES", quantity=1,
            price=Decimal("0.5"), simulator_floor=Decimal("0"), now=NOW,
        )


@pytest.mark.parametrize("account,error", [
    (None, "CF_RISK_ACCOUNT_IDENTITY_REQUIRED"),
    ("f" * 64, "CF_RISK_ACCOUNT_IDENTITY_MISMATCH"),
])
def test_record_needs_the_independently_selected_account(factory, account, error):  # noqa: F811
    with factory() as session:
        decision = seed(session, stored_cf=True)
        record = unknown_record(session, decision)
        with pytest.raises(ValueError, match=error):
            advanced_risk_request_for_paper_decision(
                session, decision=decision, settings=Settings(),
                phase_3m_decision=None, decision_timestamp=NOW, cf_cost_record=record,
                cf_account_identity_sha256=account,
            )
