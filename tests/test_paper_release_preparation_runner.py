"""Actual existing-stack computation and durable restart in a temporary ledger."""

import shutil
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from test_overnight_activation import baseline_template  # noqa: F401
from test_paper_release_preparation import original_inputs

from kalshi_predictor.config import Settings
from kalshi_predictor.overnight_paper.preparation_runner import run_weather_preparation_cycle
from kalshi_predictor.overnight_paper.store import initialize_store


@pytest.fixture
def cycle(tmp_path, baseline_template):  # noqa: F811
    path = tmp_path / "prepared.db"
    shutil.copyfile(baseline_template, path)
    initialize_store(path)
    engine = create_engine(f"sqlite:///{path}")
    ticker, sources, _ = original_inputs()
    yield dict(
        session_factory=sessionmaker(engine),
        database_path=path,
        cycle_id="fixture",
        ticker=ticker,
        source_envelopes=sources,
        settings=Settings(
            _env_file=None,
            kalshi_api_key_id=None,
            kalshi_private_key_path=None,
            postgres_password="",
            execution_confirmation_token="",
            execution_enabled=False,
            execution_dry_run=True,
            execution_kill_switch=True,
            execution_gateway_mode="disabled",
            autopilot_enabled=False,
            learning_mode=False,
            dynamic_position_sizing_mode="shadow",
            advanced_risk_engine_mode="shadow",
            weather_v2_knyc_observation_enabled=False,
        ),
        slippage_allowance=Decimal("0.01"),
        uncertainty_buffer=Decimal("0.01"),
    )
    engine.dispose()


def test_computation_replay_keeps_original_ids_clocks_and_no_orders(cycle):
    first = run_weather_preparation_cycle(**cycle)
    assert first["state"] == "COMPUTED_UNQUALIFIED", first["blockers"]
    assert first["current_eligibility"] is False
    assert first["records"]["sizing"]["proposed_contracts"] == 1
    assert first["records"]["risk"]["action"] == "ALLOW"
    assert run_weather_preparation_cycle(**cycle) == first
    with cycle["session_factory"]() as session:
        assert session.execute(text("SELECT count(*) FROM forecasts")).scalar_one() == 1
        assert session.execute(text("SELECT count(*) FROM paper_orders")).scalar_one() == 0
        assert session.execute(text("SELECT count(*) FROM paper_fills")).scalar_one() == 0
        assert (
            session.execute(
                text(
                    "SELECT count(*) FROM overnight_sprint_cycles "
                    "WHERE id='weather-preparation:fixture'"
                )
            ).scalar_one()
            == 1
        )


def test_changed_input_cannot_reuse_cycle(cycle):
    run_weather_preparation_cycle(**cycle)
    with pytest.raises(ValueError, match="PREPARATION_CYCLE_INPUT_CONFLICT"):
        run_weather_preparation_cycle(**{**cycle, "uncertainty_buffer": Decimal("0.02")})


def test_stale_source_rejection_is_durable_without_normalized_rows(cycle):
    _, sources, _ = original_inputs(stale=True)
    result = run_weather_preparation_cycle(**{**cycle, "source_envelopes": sources})
    assert result["state"] == "BLOCKED"
    assert result["blockers"]
    assert set(result["records"]) == {"analytical_observation_evidence"}
    assert len(result["original_sources"]) == len(sources)
    with cycle["session_factory"]() as session:
        assert session.execute(text("SELECT count(*) FROM forecasts")).scalar_one() == 0
        assert session.execute(text("SELECT count(*) FROM paper_orders")).scalar_one() == 0


def test_account_material_refused_before_journaling(cycle):
    sensitive = cycle["settings"].model_copy(update={"execution_confirmation_token": "test-only"})
    with pytest.raises(ValueError, match="ACCOUNT_CONFIGURATION_FORBIDDEN"):
        run_weather_preparation_cycle(**{**cycle, "settings": sensitive})
    with cycle["session_factory"]() as session:
        assert (
            session.execute(text("SELECT count(*) FROM overnight_sprint_cycles")).scalar_one() == 0
        )


def test_live_result_is_never_reconstructed_from_replay(cycle):
    from kalshi_predictor.overnight_paper.preparation_runner import (
        run_weather_preparation_live_cycle,
    )

    first = run_weather_preparation_live_cycle(**cycle)
    assert first.live_result is not None
    assert first.live_result.phase3m is not None
    replay = run_weather_preparation_live_cycle(**cycle)
    assert replay.record == first.record
    assert replay.live_result is None


def test_standalone_preparation_obeys_runtime_owner_and_accepts_held_owner(cycle):
    from kalshi_predictor.overnight_paper.runtime_owner import acquire_runtime_owner

    with acquire_runtime_owner(cycle["database_path"]) as owner:
        with pytest.raises(ValueError, match="ALREADY_ACTIVE"):
            run_weather_preparation_cycle(**cycle)
        result = run_weather_preparation_cycle(**cycle, runtime_owner=owner)
        assert result["state"] == "COMPUTED_UNQUALIFIED"

