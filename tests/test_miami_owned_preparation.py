# ruff: noqa: F811
"""Genuine owned-file Miami records, atomic journal and bounded captured driver."""

import hashlib
import json
import shutil
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker
from test_miami_preparation import prepared_inputs  # noqa: F401
from test_miami_source_gate import context, fixtures, grid_context  # noqa: F401
from test_overnight_activation import baseline_template  # noqa: F401

from kalshi_predictor.data.schema import Forecast, MarketSnapshot, PaperOrder
from kalshi_predictor.overnight_paper import miami_driver as driver
from kalshi_predictor.overnight_paper import miami_preparation as prep
from kalshi_predictor.overnight_paper import miami_preparation_runner as runner
from kalshi_predictor.overnight_paper.boundary import (
    LocalPaperAuthorization,
    authorization_fingerprint,
)
from kalshi_predictor.overnight_paper.miami_storage import (
    MiamiOwnedStorage,
    owned_miami_factory,
    verify_miami_storage,
)
from kalshi_predictor.overnight_paper.runtime_owner import acquire_runtime_owner
from kalshi_predictor.overnight_paper.store import initialize_store


@pytest.fixture
def owned_inputs(tmp_path, baseline_template, prepared_inputs, monkeypatch):
    path = tmp_path / "miami.db"
    shutil.copyfile(baseline_template, path)
    initialize_store(path)
    engine = create_engine(f"sqlite:///{path}")
    at = fixtures.CUTOFF + timedelta(minutes=30, seconds=2)
    objective = b"synthetic Miami storage only"
    auth = LocalPaperAuthorization(
        at - timedelta(hours=1),
        at + timedelta(hours=1),
        hashlib.sha256(objective).hexdigest(),
        isolated_database_path=str(path.resolve()),
        database_id="synthetic-miami-db",
        max_new_positions=1,
    )
    marker = dict(
        kind="LOCAL_PAPER_AUTHORIZATION_BASELINE_V1",
        database_id=auth.database_id,
        database_path=str(path.resolve()),
        authorization_sha256=authorization_fingerprint(auth),
        objective_sha256=auth.objective_sha256,
        baseline_paper_orders=0,
        baseline_paper_fills=0,
    )
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO overnight_sprint_cycles VALUES(:id,:at,:payload)"),
            dict(
                id="authorization-baseline:" + auth.database_id,
                at=at.isoformat(),
                payload=json.dumps(marker),
            ),
        )
    monkeypatch.setattr(runner, "utc_now", prep.utc_now)
    monkeypatch.setattr(driver, "utc_now", prep.utc_now)
    yield (
        dict(
            session_factory=sessionmaker(engine),
            database_path=path,
            authorization=auth,
            cycle_id="test-cycle",
            **prepared_inputs,
        ),
        objective,
    )
    engine.dispose()


def test_owned_cycle_preserves_real_ids_and_historical_replay(owned_inputs):
    args, _ = owned_inputs
    with acquire_runtime_owner(args["database_path"]) as owner:
        first = runner.run_miami_preparation_live_cycle(**args, runtime_owner=owner)
        assert first.record["state"] == "COMPUTED_UNQUALIFIED", first.record["blockers"]
        result = first.live_result
        with Session(result.owned_storage.engine) as session:
            prep.verify_miami_preparation_handoff(session, result, now=prep.utc_now())
            assert session.get(Forecast, result.records["forecast_id"]).ticker == result.ticker
            assert (
                session.get(MarketSnapshot, result.records["snapshot_id"]).ticker == result.ticker
            )
            assert session.scalar(select(func.count()).select_from(PaperOrder)) == 0
        repeated = runner.run_miami_preparation_live_cycle(**args, runtime_owner=owner)
        assert repeated.record == first.record
        assert repeated.live_result is None
        with pytest.raises(ValueError, match="INPUT_CONFLICT"):
            runner.run_miami_preparation_live_cycle(
                **(args | {"uncertainty_buffer": Decimal(".02")}), runtime_owner=owner
            )
    with Session(result.owned_storage.engine) as session:
        with pytest.raises(ValueError, match="NOT_ACTIVE"):
            prep.verify_miami_preparation_handoff(session, result, now=prep.utc_now())


def test_checkpoint_failure_rolls_back_actual_rows(owned_inputs, monkeypatch):
    args, _ = owned_inputs

    def failure(*args, **kwargs):
        raise RuntimeError("INJECTED_JOURNAL_FAILURE")

    monkeypatch.setattr(runner, "_checkpoint", failure)
    with acquire_runtime_owner(args["database_path"]) as owner:
        with pytest.raises(RuntimeError, match="INJECTED"):
            runner.run_miami_preparation_live_cycle(**args, runtime_owner=owner)
    with args["session_factory"]() as session:
        assert session.scalar(select(func.count()).select_from(Forecast)) == 0
        assert (
            session.execute(
                text(
                    "SELECT count(*) FROM overnight_sprint_cycles "
                    "WHERE id LIKE 'miami-preparation:%'"
                )
            ).scalar_one()
            == 0
        )


@pytest.mark.parametrize("change", ["forged", "engine", "authorization", "attached", "baseline"])
def test_owned_storage_rejects_identity_changes(owned_inputs, change):
    args, _ = owned_inputs
    with acquire_runtime_owner(args["database_path"]) as owner:
        factory, storage = owned_miami_factory(
            args["session_factory"],
            database_path=args["database_path"],
            owner=owner,
            authorization=args["authorization"],
        )
        with factory() as session:
            if change == "forged":
                storage = MiamiOwnedStorage(
                    storage.database_path, owner, storage.authorization, storage.engine
                )
            elif change == "engine":
                session.close()
                session = args["session_factory"]()
            elif change == "authorization":
                factory2, storage = owned_miami_factory(
                    args["session_factory"],
                    database_path=args["database_path"],
                    owner=owner,
                    authorization=replace(args["authorization"], database_id="other"),
                )
                session.close()
                session = factory2()
            elif change == "attached":
                session.execute(text("ATTACH ':memory:' AS unexpected"))
            else:
                session.execute(text("DELETE FROM overnight_sprint_cycles"))
            try:
                with pytest.raises(ValueError):
                    verify_miami_storage(session, storage, now=prep.utc_now())
            finally:
                session.rollback()
                session.close()


def test_default_api_still_rejects_file_and_missing_fee_journals_no_forecast(owned_inputs):
    args, _ = owned_inputs
    with Session(args["session_factory"].kw["bind"]) as session:
        result = prep.prepare_miami_candidate(
            session,
            **{
                k: v
                for k, v in args.items()
                if k not in ("session_factory", "database_path", "authorization", "cycle_id")
            },
        )
        assert result.state == "BLOCKED"
        assert "IN_MEMORY_SQLITE_ONLY" in result.blockers
    with acquire_runtime_owner(args["database_path"]) as owner:
        result = runner.run_miami_preparation_live_cycle(
            **(args | {"fee_evidence": None}), runtime_owner=owner
        )
        assert result.record["state"] == "BLOCKED"
        assert result.record["records"] == {}
    with args["session_factory"]() as session:
        assert session.scalar(select(func.count()).select_from(Forecast)) == 0


def test_credentials_rejected_before_any_journal(owned_inputs):
    args, _ = owned_inputs
    settings = args["settings"].model_copy(
        update={"kalshi_db_url": "postgresql://u:secret@invalid/db"}
    )
    with acquire_runtime_owner(args["database_path"]) as owner:
        with pytest.raises(ValueError, match="DATABASE_URL_FORBIDDEN") as caught:
            runner.run_miami_preparation_live_cycle(
                **(args | {"settings": settings}), runtime_owner=owner
            )
    assert "secret" not in str(caught.value)
    with args["session_factory"]() as session:
        assert (
            session.execute(text("SELECT count(*) FROM overnight_sprint_cycles")).scalar_one() == 1
        )


def test_captured_driver_preserves_owner_and_defaultdisabled_handoff(owned_inputs, monkeypatch):
    args, objective = owned_inputs
    calls = []

    def supervisor(**kw):
        calls.append(kw)
        assert kw["entries_enabled"] is False
        assert kw["candidate"] is None  # no model/release authority supplied
        with pytest.raises(ValueError, match="ALREADY_ACTIVE"):
            with acquire_runtime_owner(args["database_path"]):
                pytest.fail("owner released")
        with kw["session_factory"]() as session:
            assert session.scalar(select(func.count()).select_from(Forecast)) == 1
            assert session.scalar(select(func.count()).select_from(PaperOrder)) == 0
        return "synthetic-supervisor-sentinel"

    monkeypatch.setattr(driver, "run_paper_supervisor", supervisor)
    result = driver.run_miami_driver(
        **{
            k: v
            for k, v in args.items()
            if k not in ("cycle_id", "slippage_allowance", "uncertainty_buffer")
        },
        repository=Path(__file__).resolve().parents[1],
        code_sha="a" * 40,
        objective_bytes=objective,
    )
    assert result.preparation_state == "COMPUTED_UNQUALIFIED", result.assembly_blockers
    assert result.assembly_blockers
    assert len(calls) == 1
    assert result.supervisor == "synthetic-supervisor-sentinel"


def test_owned_result_assembles_against_exact_destination_records(owned_inputs, monkeypatch):
    from test_miami_source_gate import artifact
    from test_paper_release_rules_timing import fixture

    from kalshi_predictor.overnight_paper import rule_verifier
    from kalshi_predictor.overnight_paper.candidate_assembly import (
        MIAMI_MODEL_ENTRYPOINT,
        assemble_miami_candidate,
        miami_model_code_bundle,
    )
    from kalshi_predictor.overnight_paper.provenance import canonical_hash
    from kalshi_predictor.overnight_paper.qualification import qualify_candidate

    args, _ = owned_inputs
    repository = Path(__file__).resolve().parents[1]
    with acquire_runtime_owner(args["database_path"]) as owner:
        cycle = runner.run_miami_preparation_live_cycle(**args, runtime_owner=owner)
        result = cycle.live_result
        assert result.state == "COMPUTED_UNQUALIFIED", result.blockers
        config = args["settings"].model_dump(mode="json")
        dependencies, code = miami_model_code_bundle(repository)
        model = artifact(
            dict(
                name=result.engine_outputs.forecast_output.model_name,
                version="1",
                model_kind="fixed_heuristic",
                training_cutoff=None,
                training_dataset_hashes=[],
                created_at=fixtures.ORIGIN.isoformat(),
                frozen_at=fixtures.ORIGIN.isoformat(),
                available_at=fixtures.ORIGIN.isoformat(),
                parameters=config,
                parameters_sha256=canonical_hash(config),
                code_sha256=hashlib.sha256(code).hexdigest(),
                code_dependencies=dependencies,
                model_entrypoint=MIAMI_MODEL_ENTRYPOINT,
            )
        )
        _, policy, document = fixture()
        at = prep.utc_now()
        policy = replace(
            policy,
            ticker=result.ticker,
            event_id=result.ticker.rsplit("-", 1)[0],
            series="KXTEMPMIAH",
            observation_time=result.records["observation_time"],
            effective_from=(at - timedelta(days=1)).isoformat(),
            effective_to=(at + timedelta(days=1)).isoformat(),
        )
        monkeypatch.setattr(rule_verifier, "CERTIFIED_RULE_POLICIES", (policy,))
        with Session(result.owned_storage.engine) as session:
            candidate = assemble_miami_candidate(
                session=session,
                preparation=result,
                model=model,
                model_code=code,
                settings=args["settings"],
                repository=repository,
                code_sha="a" * 40,
                now=at,
                authorization=args["authorization"],
                rule_documents=(document,),
                include_evaluation_observation=False,
            )
            assert candidate.decision.forecast_id == result.records["forecast_id"]
            gates = dict(qualify_candidate(**candidate.qualification_args).gates)
            assert gates["FRESH_ANALYTICAL_SOURCE"] and gates["FULL_PROVENANCE"]
            row = session.get(Forecast, candidate.decision.forecast_id)
            assert row.ticker == result.ticker
            assert Decimal(row.yes_probability) == candidate.decision.probability
            assert session.scalar(select(func.count()).select_from(PaperOrder)) == 0
            row.yes_probability = ".99"
            session.flush()
            with pytest.raises(ValueError):
                prep.verify_miami_preparation_handoff(session, result, now=prep.utc_now())
            session.rollback()


def test_captured_driver_reaches_actual_supervisor_without_network_or_orders(
    owned_inputs, monkeypatch
):
    import httpx

    from kalshi_predictor.overnight_paper import supervisor

    args, objective = owned_inputs

    def no_network(*args, **kwargs):
        pytest.fail("Empty-ledger bounded driver must not request network")

    monkeypatch.setattr(httpx.Client, "send", no_network)
    monkeypatch.setattr(supervisor, "_now", prep.utc_now)
    result = driver.run_miami_driver(
        **{
            k: v
            for k, v in args.items()
            if k not in ("cycle_id", "slippage_allowance", "uncertainty_buffer")
        },
        repository=Path(__file__).resolve().parents[1],
        code_sha="a" * 40,
        objective_bytes=objective,
    )
    assert result.supervisor.state == "STOPPED"
    assert result.supervisor.cycles_completed == 1
    with args["session_factory"]() as session:
        assert session.scalar(select(func.count()).select_from(PaperOrder)) == 0
        assert (
            session.execute(
                text(
                    "SELECT count(*) FROM overnight_sprint_cycles WHERE id LIKE 'runtime-health:%'"
                )
            ).scalar_one()
            >= 2
        )


def test_expired_handoff_keeps_historical_rows_without_current_eligibility(owned_inputs):
    args, _ = owned_inputs
    with acquire_runtime_owner(args["database_path"]) as owner:
        cycle = runner.run_miami_preparation_live_cycle(**args, runtime_owner=owner)
        result = cycle.live_result
        with Session(result.owned_storage.engine) as session:
            with pytest.raises(ValueError, match="STALE_OR_FUTURE"):
                prep.verify_miami_preparation_handoff(
                    session, result, now=prep.utc_now() + timedelta(minutes=2)
                )
            assert session.get(Forecast, result.records["forecast_id"]) is not None
            assert session.scalar(select(func.count()).select_from(PaperOrder)) == 0
