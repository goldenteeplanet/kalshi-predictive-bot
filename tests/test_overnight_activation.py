"""Synthetic fixtures only. No external market or runtime database is modified."""

import hashlib
import json
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from test_phase_3n_advanced_risk import _config, _request

from kalshi_predictor.advanced_risk.engine import AdvancedRiskEngine
from kalshi_predictor.advanced_risk.repository import insert_advanced_risk_decision
from kalshi_predictor.config import Settings
from kalshi_predictor.data.schema import Base, Forecast, Market, MarketSnapshot
from kalshi_predictor.overnight_paper import activation
from kalshi_predictor.overnight_paper.boundary import ExecutionMode, LocalPaperAuthorization
from kalshi_predictor.overnight_paper.qualification import (
    COLLECTOR_GATES,
    EvidenceReference,
    GateEvidence,
    compute_net_ev,
    decision_fingerprint,
)
from kalshi_predictor.overnight_paper.store import initialize_store, record_shadow
from kalshi_predictor.paper.models import BUY_YES, PaperDecision
from kalshi_predictor.position_sizing.repository import insert_position_sizing_decision
from kalshi_predictor.position_sizing.sizer import (
    DynamicPositionSizer,
    PositionSizingConfig,
    PositionSizingInput,
)


def ref(name, payload):
    return EvidenceReference(name, hashlib.sha256(payload).hexdigest(), payload)


@pytest.fixture(scope="module")
def baseline_template(tmp_path_factory):
    path = tmp_path_factory.mktemp("activation-template") / "baseline.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    return path


@pytest.fixture
def prepared(tmp_path, baseline_template, monkeypatch):
    path = tmp_path / "paper.db"
    shutil.copyfile(baseline_template, path)
    engine = create_engine(f"sqlite:///{path}")
    initialize_store(path)
    factory = sessionmaker(engine)
    now = datetime.now(UTC)
    settings = Settings(
        _env_file=None,
        execution_enabled=False,
        execution_dry_run=True,
        execution_kill_switch=True,
        execution_gateway_mode="disabled",
        autopilot_enabled=False,
        autopilot_dry_run=True,
        paper_order_creation_enabled=True,
        paper_order_kill_switch=False,
        learning_mode=False,
    )
    size = DynamicPositionSizer(PositionSizingConfig()).decide(
        PositionSizingInput(
            confidence_score=0.55,
            opportunity_score=0.55,
            liquidity_score=0.8,
            current_drawdown_fraction=0.0,
            max_drawdown_fraction=0.2,
            historical_accuracy=0.55,
            historical_sample_size=30,
            decision_timestamp=now,
        )
    )
    request = _request(phase_3m_contracts=1)
    request = replace(
        request,
        decision_timestamp=now,
        instrument_id="BTC-TEST",
        portfolio_snapshot=replace(request.portfolio_snapshot, captured_at=now),
        market_snapshot=replace(request.market_snapshot, captured_at=now),
    )
    risk = AdvancedRiskEngine(_config()).decide(request)
    book = {"yes": [[20, 100]], "no": [[79, 100]]}
    with factory() as session:
        session.add(
            Market(
                ticker="BTC-TEST",
                event_ticker="E",
                series_ticker="S",
                status="active",
                close_time=now + timedelta(hours=1),
                expiration_time=now + timedelta(hours=2),
                first_seen_at=now,
                last_seen_at=now,
                raw_json="{}",
            )
        )
        forecast = Forecast(
            ticker="BTC-TEST",
            forecasted_at=now,
            model_name="ensemble_v2",
            yes_probability="0.7",
            feature_json="{}",
        )
        snapshot = MarketSnapshot(
            ticker="BTC-TEST",
            captured_at=now,
            status="active",
            raw_market_json="{}",
            raw_orderbook_json=json.dumps(book),
        )
        session.add_all([forecast, snapshot])
        session.flush()
        size_row = insert_position_sizing_decision(
            session,
            size,
            ticker="BTC-TEST",
            model_name="ensemble_v2",
            strategy_id="s",
            instrument="BTC-TEST",
            trade_intent_id="i",
            order_correlation_id=None,
        )
        risk_row = insert_advanced_risk_decision(
            session, risk, request, ticker="BTC-TEST", position_sizing_decision_id=size_row.id
        )
        decision = PaperDecision(
            "BTC-TEST",
            forecast.id,
            "ensemble_v2",
            BUY_YES,
            Decimal("0.7"),
            Decimal("0.21"),
            Decimal("0.21"),
            Decimal("0.49"),
            1,
            "synthetic fixture",
            {"position_sizing_decision_id": size_row.id, "advanced_risk_decision_id": risk_row.id},
        )
        inputs = dict(
            ticker="BTC-TEST",
            category="crypto",
            code_sha="a" * 40,
            side=BUY_YES,
            executable_price="0.21",
            latest_settlement_at=(now + timedelta(hours=2)).isoformat(),
            event_id="E",
            series="S",
            forecast_id=forecast.id,
            snapshot_id=snapshot.id,
            rule_version="fixture-v1",
            source_hashes=["fixture"],
            model_version="fixture-model",
            config_hash=decision_fingerprint(settings.model_dump(mode="json")),
            phase3m_hash=decision_fingerprint(size.as_dict()),
            phase3n_hash=decision_fingerprint(risk.as_dict()),
        )
        session.commit()
    key = decision_fingerprint(inputs)
    source = ref(
        "synthetic-fixture",
        json.dumps(
            {
                "url": "https://external-api.kalshi.com/trade-api/v2/markets/BTC-TEST/orderbook",
                "received_at": now.isoformat(),
                "body": book,
                "synthetic_fixture": True,
            }
        ).encode(),
    )
    evidence = []
    for gate in COLLECTOR_GATES:
        report = dict(
            schema="overnight-paper-gate-v1",
            gate=gate,
            decision_id=key,
            ticker="BTC-TEST",
            category="crypto",
            verifier="test-fixture-v1",
            verdict="PASS",
            validated_at=now.isoformat(),
            valid_until=(now + timedelta(seconds=60)).isoformat(),
            sources=[source.sha256],
        )
        evidence.append(
            GateEvidence(
                gate,
                key,
                "crypto",
                "BTC-TEST",
                "test-fixture-v1",
                ref(f"gate-{gate}", json.dumps(report).encode()),
                sources=(source,),
            )
        )
    ev = compute_net_ev(
        model_probability=Decimal("0.7"),
        executable_price=Decimal("0.21"),
        estimated_fee=max(settings.paper_default_fee_per_contract, Decimal("0.02")),
        slippage_allowance=Decimal("0.01"),
        uncertainty_buffer=Decimal("0.03"),
    )
    args = dict(
        ticker="BTC-TEST",
        category="crypto",
        decision_inputs=inputs,
        decision_id=key,
        evidence=tuple(evidence),
        ev=ev,
        minimum_net_ev=settings.paper_min_edge,
        phase3m=size,
        phase3n=risk,
        mode=ExecutionMode.LOCAL_PAPER,
    )
    payload = dict(
        ticker="BTC-TEST",
        event_ticker="E",
        series_ticker="S",
        model="ensemble_v2",
        model_version="fixture-model",
        forecast="0.7",
        snapshot=book,
        price="0.21",
        net_ev=str(ev.net_ev),
        sizing=size.as_dict(),
        risk=risk.as_dict(),
        source_provenance={"fixture": source.sha256},
        settlement_rule_version="fixture-v1",
        decision_at=now.isoformat(),
        forecast_at=now.isoformat(),
        source_updated_at=now.isoformat(),
        snapshot_at=now.isoformat(),
        close_time=(now + timedelta(hours=1)).isoformat(),
        side=BUY_YES,
        expected_settlement_at=(now + timedelta(hours=2)).isoformat(),
        latest_settlement_at=(now + timedelta(hours=2)).isoformat(),
        qualification_inputs=inputs,
    )
    import sqlite3

    with sqlite3.connect(path) as db:
        shadow_id = record_shadow(db, payload)
    objective = b"synthetic local-paper authorization"
    result = dict(
        session_factory=factory,
        database_path=path,
        authorization=LocalPaperAuthorization(
            now, now + timedelta(days=1), hashlib.sha256(objective).hexdigest()
        ),
        objective_bytes=objective,
        release=Mock(spec=activation.ExactReleaseEvidence),
        qualification_args=args,
        shadow_payload=payload,
        shadow_id=shadow_id,
        decision=decision,
        settings=settings,
        now=now,
    )
    result["release"].sha = "a" * 40
    monkeypatch.setattr(GateEvidence, "verified", lambda *args, **kwargs: True)
    # Isolate transaction mechanics from the separately tested source/model engines.
    # Passing fixtures are synthetic and never release/activation evidence.
    monkeypatch.setattr(activation, "_revalidate_engines", lambda *args: None)
    # Transaction fixture does not certify source/model gates. Semantic validators
    # are independently tested and intentionally block unsupported runtime evidence.
    monkeypatch.setattr(
        activation,
        "qualify_candidate",
        lambda **kwargs: Mock(
            status=("PAPER_ELIGIBLE" if kwargs.get("evidence") else "PAPER_NOT_READY")
        ),
    )
    yield result
    engine.dispose()


def count(prepared):
    with prepared["session_factory"]() as session:
        return session.execute(text("SELECT count(*) FROM paper_orders")).scalar_one()


def test_real_local_simulator_and_shadow_link(prepared):
    result = activation.activate_local_paper(**prepared)
    assert result.fill_created
    assert result.actual_simulated_fee == prepared["settings"].paper_default_fee_per_contract
    assert count(prepared) == 1
    with pytest.raises(ValueError, match="SHADOW_ALREADY_ACTIVATED"):
        activation.activate_local_paper(**prepared)
    assert count(prepared) == 1


def test_ledger_quantity_deviation_rolls_back_everything(prepared, monkeypatch):
    original = activation.create_paper_order

    def altered(*args, **kwargs):
        order = original(*args, **kwargs)
        order.quantity = 3
        return order

    monkeypatch.setattr(activation, "create_paper_order", altered)
    with pytest.raises(ValueError, match="CHANGED_QUANTITY"):
        activation.activate_local_paper(**prepared)
    assert count(prepared) == 0
    with prepared["session_factory"]() as session:
        assert session.execute(text("SELECT count(*) FROM paper_fills")).scalar_one() == 0
        assert (
            session.execute(text("SELECT paper_order_id FROM overnight_shadow")).scalar_one()
            is None
        )


def test_shadow_and_engine_changes_block_before_order(prepared):
    prepared["decision"] = replace(prepared["decision"], probability=Decimal("0.9"))
    with pytest.raises(ValueError, match="FORECAST_MISMATCH"):
        activation.activate_local_paper(**prepared)
    assert count(prepared) == 0


def test_missing_readiness_and_exchange_flags_block(prepared):
    prepared["qualification_args"]["evidence"] = ()
    with pytest.raises(ValueError, match="TWELVE_GATES"):
        activation.activate_local_paper(**prepared)
    assert count(prepared) == 0


def test_release_failure_precedes_every_database_write(prepared):
    prepared["release"].verify.side_effect = ValueError("EXACT_SHA_CLEAN_RELEASE_REQUIRED")
    with pytest.raises(ValueError, match="EXACT_SHA"):
        activation.activate_local_paper(**prepared)
    assert count(prepared) == 0


def test_changed_engine_on_atomic_revalidation_rolls_back(prepared, monkeypatch):
    def changed(*args):
        raise ValueError("ENGINE_REVALIDATION_REQUIRES_NEW_SHADOW")

    monkeypatch.setattr(activation, "_revalidate_engines", changed)
    with pytest.raises(ValueError, match="ENGINE_REVALIDATION"):
        activation.activate_local_paper(**prepared)
    assert count(prepared) == 0


def test_release_artifacts_require_success_at_exact_sha(tmp_path, monkeypatch):
    monkeypatch.setattr(activation, "_verify_import_origins", lambda repository: None)
    sha = "a" * 40
    tests = ref("pytest.log", b"7000 passed, 1 skipped in 500.0s")
    lint = ref("ruff.log", b"All checks passed!")
    checks = ref(
        "ci.json",
        json.dumps(
            {"check_runs": [{"name": "test", "head_sha": sha, "conclusion": "success"}]}
        ).encode(),
    )
    payload = dict(
        sha=sha,
        artifacts={"pytest": tests.sha256, "ruff": lint.sha256, "hosted_checks": checks.sha256},
        pytest_command="pytest",
        lint_command="ruff check .",
        required_checks=["test"],
    )
    release = activation.ExactReleaseEvidence(
        tmp_path, sha, ref("release.json", json.dumps(payload).encode()), tests, lint, checks
    )
    monkeypatch.setattr(
        activation.subprocess,
        "check_output",
        lambda args, **kwargs: sha if "rev-parse" in args else "",
    )
    release.verify()
    with pytest.raises(ValueError, match="EXACT_SHA"):
        replace(release, sha="b" * 40).verify()
    (tmp_path / "pyproject.toml").write_text("[tool.mypy]\ncheck_untyped_defs=true\n")
    with pytest.raises(ValueError, match="MYPY_EVIDENCE"):
        release.verify()


def test_longstop_not_optimistic_eta_enforces_hard_72_hours(prepared):
    from kalshi_predictor.data.schema import Market

    with prepared["session_factory"]() as session:
        market = session.get(Market, "BTC-TEST")
        market.expiration_time = prepared["now"] + timedelta(hours=73)
        session.commit()
    with pytest.raises(ValueError, match="HARD_SETTLEMENT_LONGSTOP_EXCEEDED"):
        activation.activate_local_paper(**prepared)
    assert count(prepared) == 0


def test_missing_verified_longstop_blocks_even_with_short_eta(prepared):
    from kalshi_predictor.data.schema import Market

    with prepared["session_factory"]() as session:
        market = session.get(Market, "BTC-TEST")
        market.expiration_time = None
        session.commit()
    with pytest.raises(ValueError, match="VERIFIED_SETTLEMENT_LONGSTOP_REQUIRED"):
        activation.activate_local_paper(**prepared)
    assert count(prepared) == 0


def test_caller_supplied_historical_cache_cannot_bless_engine_revalidation(prepared):
    prepared["decision"] = replace(
        prepared["decision"],
        raw_decision_json={
            **prepared["decision"].raw_decision_json,
            "position_sizing_historical_evidence_cache": {
                "accuracy": 1.0,
                "sample_count": 99999,
            },
        },
    )
    with pytest.raises(ValueError, match="UNVERIFIED_HISTORY_CACHE"):
        activation.activate_local_paper(**prepared)
    assert count(prepared) == 0


def test_release_rejects_artifacts_for_another_checkout(tmp_path):
    with pytest.raises(ValueError, match="NOT_RUNNING_CHECKOUT"):
        activation._verify_import_origins(tmp_path)
