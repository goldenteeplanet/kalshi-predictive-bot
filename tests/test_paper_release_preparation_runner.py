"""Actual existing-stack computation and durable restart in a temporary ledger."""

import hashlib
import json
import shutil
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from test_overnight_activation import baseline_template  # noqa: F401
from test_paper_release_preparation import original_inputs

from kalshi_predictor.config import Settings
from kalshi_predictor.overnight_paper.preparation_runner import run_weather_preparation_cycle
from kalshi_predictor.overnight_paper.provenance import Artifact, canonical_hash
from kalshi_predictor.overnight_paper.store import initialize_store
from kalshi_predictor.utils.time import utc_now


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


def artifact(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return Artifact(hashlib.sha256(raw).hexdigest(), raw)


@pytest.fixture(scope="module")
def real_model_bundle():
    from kalshi_predictor.overnight_paper.candidate_assembly import weather_model_code_bundle

    repository = Path(__file__).resolve().parents[1]
    dependencies, code = weather_model_code_bundle(repository)
    return repository, dependencies, code


def frozen_for(cycle, bundle):
    from kalshi_predictor.overnight_paper.preparation_runner import FrozenWeatherExecution

    repository, dependencies, code = bundle
    at = (utc_now() - timedelta(minutes=1)).isoformat()
    params = cycle["settings"].model_dump(mode="json")
    entry = "kalshi_predictor.forecasting.weather_v2:WeatherV2Forecaster.forecast"
    model = artifact(
        dict(
            name="weather_v2",
            version="test-frozen",
            model_kind="fixed_heuristic",
            training_cutoff=None,
            training_dataset_hashes=[],
            created_at=at,
            frozen_at=at,
            available_at=at,
            parameters=params,
            parameters_sha256=canonical_hash(params),
            model_entrypoint=entry,
            code_sha256=hashlib.sha256(code).hexdigest(),
            code_dependencies=dependencies,
        )
    )
    procedure = artifact(
        dict(
            kind="weather-paired-procedure-v1",
            name="weather-market-contrast",
            version="1",
            created_at=at,
            frozen_at=at,
            available_at=at,
            source_id="NWS",
            variant_methods={"off": "same_book_midpoint", "on": entry},
            contrast_type="MARKET_BASELINE_VS_WEATHER_V2",
            model_artifact_sha256=model.sha256,
            settings_sha256=canonical_hash(params),
        )
    )
    return FrozenWeatherExecution(procedure, model, code, repository)


def test_frozen_execution_receipt_actual_forecast_replay(cycle, real_model_bundle):
    frozen = frozen_for(cycle, real_model_bundle)
    first = run_weather_preparation_cycle(**cycle, frozen_execution=frozen)
    assert first["state"] == "COMPUTED_UNQUALIFIED", first["blockers"]
    receipt = first["execution_receipt"]
    assert first["execution_receipt_sha256"] == canonical_hash(receipt)
    assert receipt["forecast_original"] == first["records"]["forecast"]
    assert receipt["forecast_sha256"] == canonical_hash(first["records"]["forecast"])
    assert receipt["procedure_sha256"] == frozen.procedure.sha256
    assert receipt["dependencies_before"] == receipt["dependencies_after"]
    assert (
        receipt["source_off_probability"] == first["records"]["forecast"]["market_mid_probability"]
    )
    assert receipt["atomic_filesystem_immutability"] is False
    assert receipt["runtime_certified"] is False
    assert receipt["source_on_probability"] == first["records"]["forecast"]["yes_probability"]
    assert run_weather_preparation_cycle(**cycle, frozen_execution=frozen) == first


def test_no_frozen_input_no_receipt(cycle):
    assert "execution_receipt" not in run_weather_preparation_cycle(**cycle)


def test_frozen_stale_source_has_no_execution_claim(cycle, real_model_bundle):
    frozen = frozen_for(cycle, real_model_bundle)
    _, sources, _ = original_inputs(stale=True)
    result = run_weather_preparation_cycle(
        **{**cycle, "source_envelopes": sources}, frozen_execution=frozen
    )
    assert result["state"] == "BLOCKED"
    assert result["execution_receipt"] is None


@pytest.mark.parametrize("change", ["settings", "code", "procedure", "future"])
def test_frozen_mismatch_rejected_without_forecast(cycle, real_model_bundle, change):
    from dataclasses import replace

    frozen = frozen_for(cycle, real_model_bundle)
    if change == "code":
        frozen = replace(frozen, model_code=b"changed")
    elif change == "settings":
        cycle = {
            **cycle,
            "settings": cycle["settings"].model_copy(
                update={"weather_v2_max_adjustment": Decimal("0.123")}
            ),
        }
    else:
        row = frozen.procedure.decode()
        if change == "procedure":
            row["variant_methods"]["off"] = "weather_v2"
        else:
            row["available_at"] = (utc_now() + timedelta(days=1)).isoformat()
        frozen = replace(frozen, procedure=artifact(row))
    with pytest.raises(ValueError, match="FROZEN_WEATHER"):
        run_weather_preparation_cycle(**cycle, frozen_execution=frozen)
    with cycle["session_factory"]() as session:
        assert session.execute(text("SELECT count(*) FROM forecasts")).scalar_one() == 0


def test_after_execution_settings_change_rolls_back(cycle, real_model_bundle, monkeypatch):
    from kalshi_predictor.overnight_paper import preparation

    frozen = frozen_for(cycle, real_model_bundle)
    original = preparation.prepare_weather_candidate

    def changed(session, **kwargs):
        result = original(session, **kwargs)
        assert result.forecast_output is not None
        kwargs["settings"].weather_v2_max_adjustment = Decimal("0.123")
        return result

    monkeypatch.setattr(preparation, "prepare_weather_candidate", changed)
    with pytest.raises(ValueError, match="FROZEN_WEATHER_PROCEDURE_OR_MODEL_MISMATCH"):
        run_weather_preparation_cycle(**cycle, frozen_execution=frozen)
    with cycle["session_factory"]() as session:
        assert session.execute(text("SELECT count(*) FROM forecasts")).scalar_one() == 0
        assert (
            session.execute(text("SELECT count(*) FROM overnight_sprint_cycles")).scalar_one() == 0
        )


def test_fee_contract_survives_durable_preparation_replay(cycle, monkeypatch):
    from test_paper_release_preparation import fee_ready_inputs

    ticker, sources, _, evidence = fee_ready_inputs(monkeypatch)
    cycle.update(ticker=ticker, source_envelopes=sources, fee_evidence=evidence)
    first = run_weather_preparation_cycle(**cycle)
    assert first["state"] == "COMPUTED_UNQUALIFIED", first["blockers"]
    contract = first["records"]["fee_contract"]
    assert contract is not None
    assert Decimal(first["records"]["ev"]["estimated_fee"]) == Decimal(contract["simulated_charge"])
    assert Decimal(first["records"]["risk_request"]["estimated_round_trip_fees"]) == Decimal(
        contract["simulated_charge"]
    )
    assert run_weather_preparation_cycle(**cycle) == first
    with cycle["session_factory"]() as session:
        assert session.execute(text("SELECT count(*) FROM paper_fills")).scalar_one() == 0
