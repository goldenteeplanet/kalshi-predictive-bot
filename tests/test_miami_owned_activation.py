"""Synthetic committed Miami driver reaches real activation; no gate is bypassed.

The fixture deliberately has no certified model evaluation. Activation must pass
actual destination rows and engine revalidation, then reject MODEL_RELEASE_REQUIRED.
No production registry/settings, network request, order, or fill is used.
"""

import copy
import hashlib
import importlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_paper_release_all_gates import _fixture_git_env, _git, _prepare_committed_fixture


def _run_miami_activation_fixture(repository, database):
    import httpx
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from test_miami_preparation import prepared_inputs
    from test_miami_source_gate import artifact, context, fixtures, grid_context
    from test_paper_release_rules_timing import fixture

    from kalshi_predictor.data.schema import Base
    from kalshi_predictor.overnight_paper import (
        activation,
        miami_driver,
        miami_preparation,
        miami_preparation_runner,
        provenance,
        rule_verifier,
        supervisor,
    )
    from kalshi_predictor.overnight_paper.boundary import (
        LocalPaperAuthorization,
        authorization_fingerprint,
    )
    from kalshi_predictor.overnight_paper.candidate_assembly import (
        MIAMI_MODEL_ENTRYPOINT,
        miami_model_code_bundle,
    )
    from kalshi_predictor.overnight_paper.provenance import canonical_hash
    from kalshi_predictor.overnight_paper.qualification import EvidenceReference
    from kalshi_predictor.overnight_paper.store import initialize_store

    for module in provenance.AUDITED_BOUNDARY_SHA256:
        importlib.import_module(module)
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    initialize_store(database)
    factory = sessionmaker(engine)
    with pytest.MonkeyPatch.context() as patch:
        original = context.__wrapped__()
        grid = grid_context.__wrapped__(original, SimpleNamespace(param="linux"))
        captured = prepared_inputs.__wrapped__(grid, patch)
        # These flags exist only in this temporary synthetic fixture. Changing
        # them after preparation would correctly break the bound settings hash.
        captured["settings"] = captured["settings"].model_copy(
            update={
                "paper_order_creation_enabled": True,
                "paper_order_kill_switch": False,
            }
        )
        current = miami_preparation.utc_now()
        for module in (miami_driver, miami_preparation_runner):
            patch.setattr(module, "utc_now", miami_preparation.utc_now)
        patch.setattr(supervisor, "_now", miami_preparation.utc_now)

        def no_network(*args, **kwargs):
            raise AssertionError("NETWORK_FORBIDDEN_IN_SYNTHETIC_ACTIVATION")

        patch.setattr(httpx.Client, "send", no_network)
        objective = b"Synthetic same-file activation checks only; no order authority"
        authorization = LocalPaperAuthorization(
            current - timedelta(hours=1),
            current + timedelta(hours=1),
            hashlib.sha256(objective).hexdigest(),
            max_new_positions=1,
            max_open_positions=1,
            isolated_database_path=str(database.resolve()),
            database_id="synthetic-miami-activation",
        )
        with factory.begin() as session:
            marker = dict(
                kind="LOCAL_PAPER_AUTHORIZATION_BASELINE_V1",
                database_path=str(database.resolve()),
                database_id=authorization.database_id,
                authorization_sha256=authorization_fingerprint(authorization),
                objective_sha256=authorization.objective_sha256,
                baseline_paper_orders=0,
                baseline_paper_fills=0,
            )
            session.execute(
                text("INSERT INTO overnight_sprint_cycles VALUES(:id,:at,:payload)"),
                dict(
                    id="authorization-baseline:" + authorization.database_id,
                    at=current.isoformat(),
                    payload=json.dumps(marker),
                ),
            )
        dependencies, code = miami_model_code_bundle(repository)
        config = captured["settings"].model_dump(mode="json")
        model = artifact(
            dict(
                name="miami_prior_day_increment_grid30_v1",
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
        market = captured["context"].market.artifact.decode()["market"]
        target = captured["context"].frozen_prediction.decode()["prediction"]["forecasts"][0][
            "target_at"
        ]
        _, policy, document = fixture()
        policy = replace(
            policy,
            ticker=market["ticker"],
            event_id=market["event_ticker"],
            series="KXTEMPMIAH",
            observation_time=target,
            effective_from=(current - timedelta(days=1)).isoformat(),
            effective_to=(current + timedelta(days=1)).isoformat(),
        )
        patch.setattr(rule_verifier, "CERTIFIED_RULE_POLICIES", (policy,))

        def ref(name, raw):
            return EvidenceReference(name, hashlib.sha256(raw).hexdigest(), raw)

        sha = _git(repository, "rev-parse", "HEAD")
        tests = ref("synthetic-pytest", b"1 passed (synthetic evidence fixture only)")
        lint = ref("synthetic-ruff", b"All checks passed!")
        checks = ref(
            "synthetic-hosted",
            json.dumps(
                dict(
                    check_runs=[
                        dict(name="synthetic-miami-activation", head_sha=sha, conclusion="success")
                    ]
                )
            ).encode(),
        )
        report = ref(
            "synthetic-release",
            json.dumps(
                dict(
                    sha=sha,
                    pytest_command="pytest",
                    lint_command="ruff check .",
                    required_checks=["synthetic-miami-activation"],
                    artifacts=dict(
                        pytest=tests.sha256, ruff=lint.sha256, hosted_checks=checks.sha256
                    ),
                )
            ).encode(),
        )
        release = activation.ExactReleaseEvidence(repository, sha, report, tests, lint, checks)
        real_supervisor = miami_driver.run_paper_supervisor
        outcomes = []
        executed_checks = []
        expected_checks = [
            "_validate_shadow_inputs",
            "_revalidate_engines",
            "_validate_engine_records",
        ]
        for name in expected_checks:
            original_check = getattr(activation, name)

            def observed_check(*args, _original=original_check, _name=name, **kwargs):
                result = _original(*args, **kwargs)
                executed_checks.append(_name)
                return result

            patch.setattr(activation, name, observed_check)

        def checked_supervisor(**kwargs):
            result = real_supervisor(**kwargs)
            assert result.admission_state == "SHADOW_ONLY", result
            candidate = kwargs["candidate"]
            decision, args = candidate.decision, candidate.qualification_args
            with factory() as session:
                shadow_id = session.execute(text("SELECT id FROM overnight_shadow")).scalar_one()

            def counts():
                with factory() as session:
                    return {
                        name: session.execute(text("SELECT count(*) FROM " + name)).scalar_one()
                        for name in (
                            "forecasts",
                            "market_snapshots",
                            "position_sizing_decisions",
                            "advanced_risk_decisions",
                            "paper_orders",
                            "paper_fills",
                            "advanced_risk_reservations",
                            "advanced_risk_high_water_marks",
                            "overnight_sprint_cycles",
                        )
                    }

            before = counts()
            from kalshi_predictor.overnight_paper.store import record_shadow

            for mutation in (
                "missing_basis",
                "wrong_basis",
                "missing_available",
                "missing_provider",
                "provider",
                "availability",
                "updated",
                "original",
                "duplicate_timestamp",
                "timestamp_hash",
            ):
                changed = copy.deepcopy(candidate.shadow_payload)
                if mutation == "missing_basis":
                    del changed["source_clock_basis"]
                elif mutation == "wrong_basis":
                    changed["source_clock_basis"] = "provider-generated-v1"
                elif mutation == "missing_available":
                    del changed["source_available_at"]
                elif mutation == "missing_provider":
                    del changed["source_provider_updated_at"]
                elif mutation == "provider":
                    changed["source_provider_updated_at"] = changed["source_updated_at"]
                elif mutation == "availability":
                    changed["source_available_at"] = changed["decision_at"]
                elif mutation == "updated":
                    changed["source_updated_at"] = changed["decision_at"]
                else:
                    source = next(
                        s
                        for s in changed["source_provenance"]
                        if s.get("role") == "ANALYTICAL_SOURCE"
                    )
                    if mutation == "original":
                        source["available_at"] = changed["decision_at"]
                    else:
                        timestamps = changed["qualification_inputs"]["source_timestamps"]
                        original_timestamp = next(
                            t for t in timestamps if t["sha256"] == canonical_hash(source)
                        )
                        if mutation == "duplicate_timestamp":
                            timestamps.append(copy.deepcopy(original_timestamp))
                        else:
                            original_timestamp["sha256"] = "f" * 64
                with sqlite3.connect(":memory:") as rejected_db:
                    with pytest.raises(ValueError, match="MIAMI_SHADOW_"):
                        record_shadow(rejected_db, changed)
                with factory() as session:
                    with pytest.raises(ValueError, match="MIAMI_SHADOW_"):
                        activation._validate_shadow_inputs(
                            session, decision, changed, args, miami_preparation.utc_now()
                        )
            assert executed_checks == []

            def activate():
                return activation.activate_local_paper(
                    session_factory=factory,
                    database_path=database,
                    authorization=authorization,
                    objective_bytes=objective,
                    release=release,
                    qualification_args=args,
                    shadow_payload=candidate.shadow_payload,
                    shadow_id=shadow_id,
                    decision=decision,
                    settings=captured["settings"],
                    now=miami_preparation.utc_now(),
                )

            # Reaching this blocker proves real destination identity, exact book,
            # sizing/risk recomputation and persisted engine checks all passed.
            for _ in range(2):
                with pytest.raises(ValueError, match="^MODEL_RELEASE_REQUIRED:") as caught:
                    activate()
                outcomes.append(str(caught.value))
                assert counts() == before
            assert executed_checks == expected_checks * 2
            replay = miami_preparation_runner.run_miami_preparation_live_cycle(
                session_factory=factory,
                database_path=database,
                runtime_owner=kwargs["runtime_owner"],
                authorization=authorization,
                cycle_id=kwargs["runtime_owner"].generation,
                **(
                    captured
                    | dict(
                        slippage_allowance=captured[
                            "settings"
                        ].advanced_risk_estimated_slippage_per_contract,
                        uncertainty_buffer=captured[
                            "settings"
                        ].advanced_risk_gap_tail_buffer_per_contract,
                    )
                ),
            )
            assert replay.live_result is None
            assert replay.record["records"]["forecast_id"] == decision.forecast_id
            with factory.begin() as session:
                previous = session.execute(
                    text("SELECT yes_probability FROM forecasts WHERE id=:id"),
                    dict(id=decision.forecast_id),
                ).scalar_one()
                session.execute(
                    text("UPDATE forecasts SET yes_probability=:value WHERE id=:id"),
                    dict(id=decision.forecast_id, value=".99"),
                )
            with pytest.raises(ValueError, match="APPLICATION_INPUT_IDENTITY_MISMATCH"):
                activate()
            with factory.begin() as session:
                session.execute(
                    text("UPDATE forecasts SET yes_probability=:value WHERE id=:id"),
                    dict(id=decision.forecast_id, value=previous),
                )
            assert counts() == before
            assert before["paper_orders"] == before["paper_fills"] == 0
            return result

        patch.setattr(miami_driver, "run_paper_supervisor", checked_supervisor)
        result = miami_driver.run_miami_driver(
            session_factory=factory,
            database_path=database,
            **{
                k: v
                for k, v in captured.items()
                if k not in ("slippage_allowance", "uncertainty_buffer")
            },
            repository=repository,
            code_sha=sha,
            authorization=authorization,
            objective_bytes=objective,
            model=model,
            model_code=code,
            release=release,
            rule_documents=(document,),
            entries_enabled=False,
        )
        assert result.assembly_blockers == ()
        assert len(outcomes) == 2
    engine.dispose()
    print(
        json.dumps(
            dict(
                synthetic_only=True,
                destination_engine_checks=True,
                exact_blocker=outcomes[0],
                orders_created=0,
                replay_historical_only=True,
            )
        )
    )


def test_committed_miami_driver_reaches_actual_activation_checks(tmp_path):
    repository = tmp_path / "miami-activation-release"
    _prepare_committed_fixture(repository)
    for name in (
        "test_miami_owned_activation.py",
        "test_miami_preparation.py",
        "test_miami_source_gate.py",
        "test_miami_binding.py",
        "test_guarded_fee_contract.py",
    ):
        shutil.copyfile(Path(__file__).parent / name, repository / "tests" / name)
    (repository / "scripts").mkdir()
    for name in ("positive_ev_miami_research.py", "positive_ev_miami_half_hour_research.py"):
        shutil.copyfile(
            Path(__file__).resolve().parents[1] / "scripts" / name, repository / "scripts" / name
        )
    _git(repository, "add", ".")
    _git(repository, "commit", "--quiet", "-m", "Synthetic Miami activation test only")
    env = _fixture_git_env(repository)
    env["PYTHONPATH"] = os.pathsep.join((str(repository / "src"), str(repository / "tests")))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; "
            "from test_miami_owned_activation import _run_miami_activation_fixture; "
            '_run_miami_activation_fixture(Path.cwd(), Path.cwd().parent / "miami.db")',
        ],
        cwd=repository,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1])["destination_engine_checks"]
