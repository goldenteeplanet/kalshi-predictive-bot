"""Synthetic public capture through real gates, owned fill, settlement and restart.

Only public HTTP responses, simulation clocks and the explicitly synthetic policy
registry are injected. This fixture is not production model or rule certification.
"""

import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from test_overnight_provenance import artifact
from test_paper_release_all_gates import _git, _prepare_committed_fixture


def _prepare_lifecycle(repository, database):
    import test_candidate_assembly as assembly_fixture
    import test_paper_release_preparation as preparation_fixture

    from kalshi_predictor.config import Settings
    from kalshi_predictor.data.schema import Base
    from kalshi_predictor.overnight_paper import (
        preparation,
        provenance,
        rule_verifier,
    )
    from kalshi_predictor.overnight_paper.activation import ExactReleaseEvidence
    from kalshi_predictor.overnight_paper.boundary import (
        LocalPaperAuthorization,
        authorization_fingerprint,
    )
    from kalshi_predictor.overnight_paper.candidate_assembly import assemble_weather_candidate
    from kalshi_predictor.overnight_paper.dataset_store import persist_dataset_record
    from kalshi_predictor.overnight_paper.evaluation_dataset import build_policy, join_outcome
    from kalshi_predictor.overnight_paper.provenance import canonical_hash
    from kalshi_predictor.overnight_paper.qualification import EvidenceReference, qualify_candidate
    from kalshi_predictor.overnight_paper.store import initialize_store

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
                final_args, final_policy = args, policy

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
    return dict(
        factory=factory,
        engine=engine,
        current=current,
        final_args=final_args,
        head=head,
        release=release,
        authorization=authorization,
        objective=objective,
    )


def _run_driver_lifecycle(repository, database):
    import test_paper_release_preparation as preparation_fixture

    from kalshi_predictor.overnight_paper import (
        acquisition,
        discovery,
        monitoring,
        preparation,
        preparation_runner,
        settlement_runner,
        supervisor,
        weather_driver,
    )
    from kalshi_predictor.overnight_paper.runtime_owner import acquire_runtime_owner

    context = _prepare_lifecycle(repository, database)
    factory, engine = context["factory"], context["engine"]
    current, args = context["current"], context["final_args"]
    simulated = [current]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return simulated[0] if tz else simulated[0].replace(tzinfo=None)

    requests = []
    client = httpx.Client
    finalized = [False]
    admission_diagnostics = []
    original_admit = supervisor.admit_prepared_candidate

    def observed_admit(**kwargs):
        started = time.perf_counter()
        try:
            admitted = original_admit(**kwargs)
        except Exception as exc:
            admission_diagnostics.append((time.perf_counter() - started, repr(exc)))
            raise
        admission_diagnostics.append((time.perf_counter() - started, repr(admitted)))
        return admitted

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(supervisor, "admit_prepared_candidate", observed_admit)
        patch.setattr(preparation_fixture, "datetime", Clock)
        ticker, originals, _ = preparation_fixture.original_inputs()
        bodies = {json.loads(s.payload)["url"]: json.loads(s.payload)["body"] for s in originals}
        market_url = discovery.BASE + "/markets/" + ticker
        market = bodies[market_url]["market"]
        market["open_time"] = (current - timedelta(hours=1)).isoformat()

        def handler(request):
            requests.append(str(request.url))
            assert request.method == "GET"
            assert "authorization" not in request.headers
            assert not any("kalshi-access" in name for name in request.headers)
            with pytest.raises(ValueError, match="ALREADY_ACTIVE"):
                with acquire_runtime_owner(database):
                    pytest.fail("public capture or settlement lost the single runtime owner")
            if finalized[0]:
                assert str(request.url) == market_url
                body = {
                    "market": market
                    | dict(
                        status="finalized",
                        result="yes",
                        settlement_ts=(simulated[0] - timedelta(minutes=30)).isoformat(),
                        settlement_value_dollars="1.00",
                    )
                }
            else:
                body = bodies[str(request.url)]
            return httpx.Response(200, json=body)

        for module in (acquisition, discovery, settlement_runner, weather_driver):
            patch.setattr(module, "datetime", Clock)
        for module, name in (
            (preparation, "utc_now"),
            (preparation_runner, "utc_now"),
            (monitoring, "_now"),
            (supervisor, "_now"),
        ):
            patch.setattr(module, name, lambda: simulated[0])
        patch.setattr("kalshi_predictor.weather.repository.utc_now", lambda: simulated[0])
        patch.setattr("kalshi_predictor.forecasting.weather_v2.utc_now", lambda: simulated[0])
        patch.setattr(discovery.time, "sleep", lambda _: None)
        patch.setattr(
            discovery.httpx,
            "Client",
            lambda **kwargs: client(**{**kwargs, "transport": httpx.MockTransport(handler)}),
        )
        result = weather_driver.run_weather_driver(
            session_factory=factory,
            database_path=database,
            archive_root=repository.parent / "public-capture",
            ticker=ticker,
            settings=args["settings"],
            repository=repository,
            code_sha=context["release"].sha,
            authorization=context["authorization"],
            objective_bytes=context["objective"],
            release=context["release"],
            model=args["model"],
            model_code=args["model_code"],
            rule_documents=args["rule_documents"],
            model_evaluation_head_sha256=context["head"],
            entries_enabled=True,
            monitoring_cycles=1,
        )
        assert result.preparation_state == "COMPUTED_UNQUALIFIED", result
        assert result.assembly_blockers == (), result
        assert result.supervisor.admission_state == "PAPER_FILLED", admission_diagnostics
        assert result.state == result.supervisor.state == "STOPPED"
        assert result.generation == result.supervisor.generation
        assert len(requests) == 8 and requests[6].endswith("/orderbook")
        assert requests[7] == market_url
        with factory() as session:
            preparation_record = json.loads(
                session.execute(
                    text("SELECT payload FROM overnight_sprint_cycles WHERE id=:id"),
                    {"id": result.preparation_checkpoint},
                ).scalar_one()
            )
            assert len(preparation_record["original_sources"]) == 7
            for source in preparation_record["original_sources"]:
                raw = source["original_utf8"].encode()
                assert hashlib.sha256(raw).hexdigest() == source["sha256"]
                envelope = json.loads(raw)
                assert datetime.fromisoformat(envelope["received_at"]) == current
            qualification = json.loads(
                session.execute(
                    text(
                        "SELECT payload FROM overnight_sprint_cycles "
                        "WHERE id LIKE 'release-qualification:%'"
                    )
                ).scalar_one()
            )["qualification"]
            assert len(qualification["gates"]) == 12
            assert all(passed for _, passed in qualification["gates"])
            assert session.execute(text("SELECT count(*) FROM paper_fills")).scalar_one() == 1

        # Restart the actual owner/runner twice, after a real public final response.
        # The second restart is settlement replay, with entry disabled throughout.
        finalized[0] = True
        simulated[0] = current + timedelta(hours=3)
        generations = {result.generation}
        for _ in range(2):
            settled = supervisor.run_paper_supervisor(
                session_factory=factory,
                database_path=database,
                settings=args["settings"],
                code_sha=context["release"].sha,
                entries_enabled=False,
                cycles=1,
            )
            assert settled.state == "STOPPED" and not settled.entries_enabled
            generations.add(settled.generation)
        assert len(generations) == 3
        with factory() as session:
            counts = {
                table: session.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
                for table in ("paper_orders", "paper_fills", "paper_pnl", "settlements")
            }
            assert counts == dict(paper_orders=1, paper_fills=1, paper_pnl=1, settlements=1)
            assert (
                session.execute(
                    text(
                        "SELECT count(*) FROM overnight_sprint_cycles "
                        "WHERE id LIKE 'paper-evaluation:%'"
                    )
                ).scalar_one()
                == 1
            )
            assert (
                session.execute(
                    text("SELECT count(*) FROM overnight_shadow WHERE evaluation_json IS NOT NULL")
                ).scalar_one()
                == 1
            )
    engine.dispose()
    print(json.dumps(dict(synthetic_only=True, all12=True, driver_lifecycle=counts)))


def test_actual_public_driver_positive_lifecycle(tmp_path):
    repository = tmp_path / "driver-release"
    _prepare_committed_fixture(repository)
    for name in (
        "test_candidate_assembly.py",
        "test_paper_release_preparation.py",
        "test_paper_release_full_lifecycle.py",
        "test_paper_weather_driver_lifecycle.py",
    ):
        shutil.copyfile(Path(__file__).parent / name, repository / "tests" / name)
    _git(repository, "add", "tests")
    _git(repository, "commit", "--quiet", "-m", "Synthetic driver lifecycle fixture only")
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
            "from test_paper_weather_driver_lifecycle import _run_driver_lifecycle; "
            "_run_driver_lifecycle(Path.cwd(), Path.cwd().parent / 'paper.db')",
        ],
        cwd=repository,
        env=environment,
        text=True,
        capture_output=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1])["all12"]
