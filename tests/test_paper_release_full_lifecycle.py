"""Synthetic actual-service lifecycle in a temporary committed source release.

Only clocks and the synthetic rule registry are injected. All semantic gates,
model evaluation, sizing, risk, ledger, fill and settlement implementations run.
This fixture provides no evidence about production model performance.
"""

import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from test_overnight_provenance import artifact
from test_paper_release_all_gates import _git, _prepare_committed_fixture


def _run_lifecycle(repository, database):
    import test_candidate_assembly as assembly_fixture
    import test_paper_release_preparation as preparation_fixture

    from kalshi_predictor.config import Settings
    from kalshi_predictor.data.schema import Base
    from kalshi_predictor.overnight_paper import (
        monitoring,
        preparation,
        provenance,
        rule_verifier,
        settlement_runner,
    )
    from kalshi_predictor.overnight_paper.activation import ExactReleaseEvidence
    from kalshi_predictor.overnight_paper.boundary import (
        LocalPaperAuthorization,
        authorization_fingerprint,
    )
    from kalshi_predictor.overnight_paper.candidate_assembly import assemble_weather_candidate
    from kalshi_predictor.overnight_paper.coordinator import admit_prepared_candidate
    from kalshi_predictor.overnight_paper.dataset_store import load_dataset, persist_dataset_record
    from kalshi_predictor.overnight_paper.evaluation_dataset import build_policy, join_outcome
    from kalshi_predictor.overnight_paper.provenance import canonical_hash
    from kalshi_predictor.overnight_paper.qualification import EvidenceReference, qualify_candidate
    from kalshi_predictor.overnight_paper.runtime_owner import acquire_runtime_owner
    from kalshi_predictor.overnight_paper.store import initialize_store
    from kalshi_predictor.overnight_paper.watcher import (
        PublicMarketObservation,
        reconcile_public_settlements,
    )

    for module in provenance.AUDITED_BOUNDARY_SHA256:
        importlib.import_module(module)
    engine = create_engine(f"sqlite+pysqlite:///{database}")
    Base.metadata.create_all(engine)
    initialize_store(database)
    factory = sessionmaker(engine)
    current = datetime.now(UTC)
    objective = b"Synthetic lifecycle only: one isolated local paper experiment"
    authorization = LocalPaperAuthorization(
        current - timedelta(hours=1),
        current + timedelta(hours=12),
        hashlib.sha256(objective).hexdigest(),
        isolated_database_path=str(database.resolve()),
        database_id="synthetic-semantic-lifecycle-0001",
        max_new_positions=1,
        max_open_positions=1,
    )
    model = None
    head = None

    def append(session, record, at):
        return persist_dataset_record(
            session, dataset="paper-release", record=record, recorded_at=at
        )

    def final_outcome(observation):
        row = observation.decode()
        final_at = row["event_window_end"]
        return join_outcome(
            observation=observation,
            outcome_artifact=artifact(
                row["identity"]
                | dict(
                    result="yes",
                    status="final",
                    final_at=final_at,
                    available_at=final_at,
                    source_url="https://example.invalid/synthetic-weather-result",
                    provider_payload={"result": "yes"},
                    provider_payload_sha256=canonical_hash({"result": "yes"}),
                )
            ),
        )

    # Execute the existing service at explicit simulation clocks. Forecasts are
    # captured at execution in that clock; no persisted forecast is backdated.
    for age in (6, 4, 3, 0):
        at = current - timedelta(days=age)

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None, instant=at):
                return instant if tz else instant.replace(tzinfo=None)

        with pytest.MonkeyPatch.context() as patch, factory() as session:
            patch.setattr(assembly_fixture, "datetime", Clock)
            patch.setattr(preparation_fixture, "datetime", Clock)
            patch.setattr(preparation, "utc_now", lambda instant=at: instant)
            patch.setattr("kalshi_predictor.weather.repository.utc_now", lambda instant=at: instant)
            patch.setattr(
                "kalshi_predictor.forecasting.weather_v2.utc_now", lambda instant=at: instant
            )
            patch.setattr(
                assembly_fixture,
                "Settings",
                lambda **kwargs: Settings(
                    **kwargs, paper_order_creation_enabled=True, paper_order_kill_switch=False
                ),
            )
            args, policy = assembly_fixture.assembly_inputs(session, repository, patch)
            if model is None:
                model = args["model"]
            args.update(
                model=model,
                authorization=replace(
                    authorization,
                    created_at=at - timedelta(hours=1),
                    expires_at=at + timedelta(hours=12),
                ),
            )
            if head is not None:
                args["model_evaluation_head_sha256"] = head
            patch.setattr(rule_verifier, "CERTIFIED_RULE_POLICIES", (policy,))
            candidate = assemble_weather_candidate(**args)
            qualification = qualify_candidate(**candidate.qualification_args)
            assert all(passed for _, passed in qualification.gates), qualification.blockers
            if age:
                observation = candidate.evaluation_observation
                assert observation is not None
                head = append(session, observation, at)
                final_at = datetime.fromisoformat(observation.decode()["event_window_end"])
                head = append(session, final_outcome(observation), final_at)
                if age == 6:
                    head = append(
                        session,
                        build_policy(
                            committed_at=current - timedelta(days=5),
                            train_end=current - timedelta(days=5, hours=1),
                            holdout_start=current - timedelta(days=4, hours=1),
                            holdout_end=current - timedelta(days=2),
                            model_name=model.decode()["name"],
                            model_version=model.decode()["version"],
                            model_artifact_sha256=model.sha256,
                            minimum_train_events=1,
                            minimum_holdout_events=2,
                            minimum_brier_improvement=0.01,
                            minimum_log_loss_improvement=0.01,
                            maximum_ece=0.5,
                            minimum_mean_net_ev=0.01,
                            minimum_mean_simulated_pnl=0.01,
                            calibration_bin_count=5,
                        ),
                        current - timedelta(days=5),
                    )
                session.commit()
            else:
                session.commit()
                final_candidate, final_args, final_policy = candidate, args, policy

    rule_verifier.CERTIFIED_RULE_POLICIES = (final_policy,)
    marker = dict(
        kind="LOCAL_PAPER_AUTHORIZATION_BASELINE_V1",
        database_id=authorization.database_id,
        database_path=str(database.resolve()),
        objective_sha256=authorization.objective_sha256,
        authorization_sha256=authorization_fingerprint(authorization),
        baseline_paper_orders=0,
        baseline_paper_fills=0,
    )
    with factory.begin() as session:
        session.execute(
            text("INSERT INTO overnight_sprint_cycles VALUES(:id,:at,:payload)"),
            dict(
                id="authorization-baseline:" + authorization.database_id,
                at=current.isoformat(),
                payload=json.dumps(marker),
            ),
        )

    def reference(name, raw):
        return EvidenceReference(name, hashlib.sha256(raw).hexdigest(), raw)

    # Explicit synthetic release-report originals exercise its real parser; this
    # test does not claim these fixture-hosted checks are production CI evidence.
    sha = _git(repository, "rev-parse", "HEAD")
    test_output = reference("synthetic-pytest", b"1 passed (synthetic release fixture)")
    lint_output = reference("synthetic-ruff", b"All checks passed!")
    checks = reference(
        "synthetic-hosted",
        json.dumps(
            dict(check_runs=[dict(name="synthetic-lifecycle", head_sha=sha, conclusion="success")])
        ).encode(),
    )
    report = reference(
        "synthetic-release",
        json.dumps(
            dict(
                sha=sha,
                pytest_command="pytest",
                lint_command="ruff check .",
                required_checks=["synthetic-lifecycle"],
                artifacts=dict(
                    pytest=test_output.sha256, ruff=lint_output.sha256, hosted_checks=checks.sha256
                ),
            )
        ).encode(),
    )
    release = ExactReleaseEvidence(repository, sha, report, test_output, lint_output, checks)
    admission = dict(
        session_factory=factory,
        database_path=database,
        authorization=authorization,
        objective_bytes=objective,
        release=release,
        settings=final_args["settings"],
        now=current,
        candidate=final_candidate,
        entries_enabled=True,
    )
    shadow = admit_prepared_candidate(**(admission | {"entries_enabled": False}))
    assert shadow.state == "SHADOW_ONLY", shadow
    requests = []
    client = httpx.Client
    inputs = final_candidate.qualification_args["decision_inputs"]

    def public_handler(request):
        requests.append(request)
        assert request.method == "GET" and "authorization" not in request.headers
        assert not any("kalshi-access" in key for key in request.headers)
        return httpx.Response(
            200,
            json={
                "market": {
                    "ticker": inputs["ticker"],
                    "event_ticker": inputs["event_id"],
                    "series_ticker": inputs["series"],
                    "status": "active",
                    "close_time": inputs["close_time"],
                }
            },
        )

    with ExitStack() as owned:
        clock = owned.enter_context(pytest.MonkeyPatch.context())
        clock.setattr(monitoring, "_now", lambda: current)
        clock.setattr(settlement_runner, "datetime", Clock)
        clock.setattr(
            settlement_runner.httpx,
            "Client",
            lambda **kwargs: client(**{**kwargs, "transport": httpx.MockTransport(public_handler)}),
        )
        owner = owned.enter_context(acquire_runtime_owner(database))
        owned.callback(monitoring.revoke_monitoring, owner)
        monitored = monitoring.monitor_owned_once(
            owner=owner, session_factory=factory, code_sha=sha
        )
        assert monitored.report.cycles_completed == 1 and len(requests) == 1
        permit = next(item for item in monitored.permits if item.shadow_id == shadow.shadow_id)
        admission["monitoring_permit"] = permit
        first = admit_prepared_candidate(**admission)
        assert first.state == "PAPER_FILLED", first
        replay = admit_prepared_candidate(**admission)
        assert replay.state == "EXISTING_EXPERIMENT" and replay.order_id == first.order_id
        inputs = final_candidate.qualification_args["decision_inputs"]
        settled_at = current + timedelta(hours=3)
        raw = json.dumps(
            dict(
                market=dict(
                    ticker=inputs["ticker"],
                    event_ticker=inputs["event_id"],
                    series_ticker=inputs["series"],
                    status="finalized",
                    result="yes",
                    close_time=inputs["close_time"],
                    settlement_ts=(settled_at - timedelta(minutes=30)).isoformat(),
                    settlement_value_dollars="1.00",
                )
            )
        ).encode()
        observation = PublicMarketObservation(
            inputs["ticker"],
            "https://external-api.kalshi.com/trade-api/v2/markets/" + inputs["ticker"],
            settled_at,
            hashlib.sha256(raw).hexdigest(),
            raw,
        )
        for _ in range(2):
            reconcile_public_settlements(
                session_factory=factory,
                database_path=database,
                observations=(observation,),
                now=settled_at,
            )
        with factory() as session:
            counts = {
                table: session.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
                for table in ("paper_orders", "paper_fills", "paper_pnl", "settlements")
            }
            assert counts == dict(paper_orders=1, paper_fills=1, paper_pnl=1, settlements=1)
            final_records = [
                item.decode()["record"] for item in load_dataset(session, dataset="paper-release")
            ]
            decision_outcomes = [
                item
                for item in final_records
                if item.get("kind") == "outcome-v1"
                and item["decision_id"] == canonical_hash(inputs)
            ]
            assert len(decision_outcomes) == 1
            assert decision_outcomes[0]["outcome"]["result"] == "yes"
    engine.dispose()
    print(json.dumps(dict(synthetic_only=True, all12=True, lifecycle=counts)))


def test_actual_weather_semantic_lifecycle(tmp_path):
    repository = tmp_path / "semantic-release"
    _prepare_committed_fixture(repository)
    for name in (
        "test_candidate_assembly.py",
        "test_guarded_fee_contract.py",
        "test_paper_release_preparation.py",
        "test_paper_release_full_lifecycle.py",
    ):
        shutil.copyfile(Path(__file__).parent / name, repository / "tests" / name)
    _git(repository, "add", "tests")
    _git(repository, "commit", "--quiet", "-m", "Synthetic lifecycle fixture only")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(repository / "src"), str(repository / "tests"))
    )
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; "
            "from test_paper_release_full_lifecycle import _run_lifecycle; "
            "_run_lifecycle(Path.cwd(), Path.cwd().parent / 'paper.db')",
        ],
        cwd=repository,
        env=environment,
        text=True,
        capture_output=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1])["all12"]
