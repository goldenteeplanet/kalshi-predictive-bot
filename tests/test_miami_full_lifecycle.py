"""Synthetic dated Miami originals through real admission, settlement and evaluation.

Simulation clocks, HTTP responses and explicitly synthetic policies are fixtures.
No readiness verifier, sizing/risk engine or paper writer is replaced.
"""

import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_paper_release_all_gates import _fixture_git_env, _git, _prepare_committed_fixture


def _run(repository, database):
    import httpx
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session, sessionmaker
    from test_miami_preparation import prepared_inputs
    from test_miami_source_gate import artifact, context, fixtures, grid_context, receipt
    from test_overnight_provenance import artifact as canonical_artifact
    from test_paper_release_rules_timing import fixture

    from kalshi_predictor.data.schema import Base
    from kalshi_predictor.overnight_paper import (
        activation,
        miami_driver,
        miami_preparation,
        miami_preparation_runner,
        monitoring,
        provenance,
        rule_verifier,
        settlement_runner,
        supervisor,
    )
    from kalshi_predictor.overnight_paper.boundary import (
        LocalPaperAuthorization,
        authorization_fingerprint,
    )
    from kalshi_predictor.overnight_paper.candidate_assembly import (
        MIAMI_MODEL_ENTRYPOINT,
        assemble_miami_candidate,
        miami_model_code_bundle,
    )
    from kalshi_predictor.overnight_paper.dataset_store import load_dataset, persist_dataset_record
    from kalshi_predictor.overnight_paper.evaluation_dataset import (
        build_policy,
        evaluate_dataset,
        join_outcome,
    )
    from kalshi_predictor.overnight_paper.provenance import canonical_hash
    from kalshi_predictor.overnight_paper.qualification import EvidenceReference, qualify_candidate
    from kalshi_predictor.overnight_paper.store import initialize_store

    for name in provenance.AUDITED_BOUNDARY_SHA256:
        importlib.import_module(name)
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    initialize_store(database)
    factory = sessionmaker(engine)
    origin = fixtures.ORIGIN
    current = origin + timedelta(minutes=35, seconds=2)
    simulated = [current]
    objective = b"Synthetic Miami full lifecycle in isolated temporary DB only"
    auth = LocalPaperAuthorization(
        current - timedelta(hours=1),
        current + timedelta(hours=12),
        hashlib.sha256(objective).hexdigest(),
        max_new_positions=1,
        max_open_positions=1,
        isolated_database_path=str(database.resolve()),
        database_id="synthetic-miami-full-lifecycle",
    )
    sha = _git(repository, "rev-parse", "HEAD")
    deps, code = miami_model_code_bundle(repository)
    model = None
    head = None

    def progress(stage):
        with database.with_suffix(".progress.jsonl").open("a", encoding="utf8") as stream:
            stream.write(
                json.dumps(dict(stage=stage, executed_at=datetime.now().isoformat())) + "\n"
            )

    def append(record, at):
        with factory.begin() as session:
            return persist_dataset_record(
                session, dataset="paper-release", record=record, recorded_at=at
            )

    def inputs(at, patch):
        patch.setattr(fixtures, "ORIGIN", at)
        patch.setattr(fixtures, "CUTOFF", at + timedelta(minutes=5))
        ctx = context.__wrapped__()
        target = at + timedelta(hours=1)
        eastern = target - timedelta(hours=4)  # All fixture dates are September EDT.
        event = "KXTEMPMIAH-" + eastern.strftime("%y%b%d%H").upper()
        ticker = event + "-T81.99"
        raw = ctx.market.artifact.decode()
        raw["market"].update(
            ticker=ticker,
            event_ticker=event,
            close_time=target.isoformat(),
            rules_primary=(
                f"If the temperature recorded at Miami, FL for {eastern.strftime('%b')} "
                f"{eastern.day}, {eastern.year} at {eastern.hour - 12} PM EDT "
                "as reported by Synoptic Data, is above 81.99°, then the market resolves to Yes."
            ),
        )
        market = replace(
            ctx.market, artifact=artifact(raw), url=ctx.market.url.rsplit("/", 1)[0] + "/" + ticker
        )
        event_raw = ctx.event.artifact.decode()
        event_raw["event"]["event_ticker"] = event
        ev = replace(
            ctx.event,
            artifact=artifact(event_raw),
            url=ctx.event.url.rsplit("/", 1)[0] + "/" + event,
        )
        ctx = replace(
            ctx,
            market=market,
            event=ev,
            catalog_receipts=tuple(receipt(o) for o in (market, ev, ctx.series)),
        )
        grid = grid_context.__wrapped__(ctx, SimpleNamespace(param="linux"))
        captured = prepared_inputs.__wrapped__(grid, patch)
        captured["settings"] = captured["settings"].model_copy(
            update={"paper_order_creation_enabled": True, "paper_order_kill_switch": False}
        )
        _, policy, document = fixture()
        policy = replace(
            policy,
            ticker=ticker,
            event_id=event,
            series="KXTEMPMIAH",
            observation_time=target.isoformat(),
            effective_from=(at - timedelta(days=1)).isoformat(),
            effective_to=(at + timedelta(days=1)).isoformat(),
        )
        patch.setattr(rule_verifier, "CERTIFIED_RULE_POLICIES", (policy,))
        return captured, document

    for age in (6, 4, 3):
        at = origin - timedelta(days=age)
        with pytest.MonkeyPatch.context() as patch:
            captured, document = inputs(at, patch)
            config = captured["settings"].model_dump(mode="json")
            if model is None:
                frozen = (at - timedelta(days=1)).isoformat()
                model = canonical_artifact(
                    dict(
                        name="miami_prior_day_increment_grid30_v1",
                        version="1",
                        model_kind="fixed_heuristic",
                        training_cutoff=None,
                        training_dataset_hashes=[],
                        created_at=frozen,
                        frozen_at=frozen,
                        available_at=frozen,
                        parameters=config,
                        parameters_sha256=canonical_hash(config),
                        code_sha256=hashlib.sha256(code).hexdigest(),
                        code_dependencies=deps,
                        model_entrypoint=MIAMI_MODEL_ENTRYPOINT,
                    )
                )
            prior_engine = create_engine("sqlite:///:memory:")
            Base.metadata.create_all(prior_engine)
            with Session(prior_engine) as session:
                prepared = miami_preparation.prepare_miami_candidate(session, **captured)
                assert prepared.state == "COMPUTED_UNQUALIFIED", prepared.blockers
                decision_at = miami_preparation.utc_now()
                candidate = assemble_miami_candidate(
                    session=session,
                    preparation=prepared,
                    model=model,
                    model_code=code,
                    settings=captured["settings"],
                    repository=repository,
                    code_sha=sha,
                    now=decision_at,
                    rule_documents=(document,),
                    authorization=replace(
                        auth,
                        created_at=decision_at - timedelta(hours=1),
                        expires_at=decision_at + timedelta(hours=1),
                    ),
                )
                assert all(
                    passed for _, passed in qualify_candidate(**candidate.qualification_args).gates
                )
                obs = candidate.evaluation_observation
                assert obs is not None
                head = append(obs, decision_at)
                row = obs.decode()
                final_at = row["event_window_end"]
                outcome = join_outcome(
                    observation=obs,
                    outcome_artifact=canonical_artifact(
                        row["identity"]
                        | dict(
                            result="yes",
                            status="final",
                            final_at=final_at,
                            available_at=final_at,
                            source_url="https://example.invalid/synthetic-miami",
                            provider_payload={"result": "yes"},
                            provider_payload_sha256=canonical_hash({"result": "yes"}),
                        )
                    ),
                )
                head = append(outcome, datetime.fromisoformat(final_at))
                progress("historical_origin_" + str(age))
            prior_engine.dispose()
            if age == 6:
                head = append(
                    build_policy(
                        committed_at=current - timedelta(days=5),
                        train_end=current - timedelta(days=5, hours=1),
                        holdout_start=current - timedelta(days=4, hours=1),
                        holdout_end=current - timedelta(days=2),
                        model_name=model.decode()["name"],
                        model_version="1",
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

    with factory() as session:
        evaluated = evaluate_dataset(load_dataset(session, dataset="paper-release"), as_of=current)
        assert evaluated.ready, evaluated.blockers
        progress("chronological_model_evaluation_ready")
    with factory.begin() as session:
        marker = dict(
            kind="LOCAL_PAPER_AUTHORIZATION_BASELINE_V1",
            database_path=str(database.resolve()),
            database_id=auth.database_id,
            authorization_sha256=authorization_fingerprint(auth),
            objective_sha256=auth.objective_sha256,
            baseline_paper_orders=0,
            baseline_paper_fills=0,
        )
        session.execute(
            text("INSERT INTO overnight_sprint_cycles VALUES(:id,:at,:payload)"),
            dict(
                id="authorization-baseline:" + auth.database_id,
                at=current.isoformat(),
                payload=json.dumps(marker),
            ),
        )

    def reference(name, raw):
        return EvidenceReference(name, hashlib.sha256(raw).hexdigest(), raw)

    tests = reference("synthetic-tests", b"1 passed (synthetic evidence only)")
    lint = reference("synthetic-lint", b"All checks passed!")
    checks = reference(
        "synthetic-hosted",
        json.dumps(
            dict(check_runs=[dict(name="synthetic", head_sha=sha, conclusion="success")])
        ).encode(),
    )
    release = activation.ExactReleaseEvidence(
        repository,
        sha,
        reference(
            "synthetic-release",
            json.dumps(
                dict(
                    sha=sha,
                    pytest_command="pytest",
                    lint_command="ruff check .",
                    required_checks=["synthetic"],
                    artifacts=dict(
                        pytest=tests.sha256, ruff=lint.sha256, hosted_checks=checks.sha256
                    ),
                )
            ).encode(),
        ),
        tests,
        lint,
        checks,
    )

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return simulated[0] if tz else simulated[0].replace(tzinfo=None)

    calls = []
    client = httpx.Client
    with pytest.MonkeyPatch.context() as patch:
        captured, document = inputs(origin, patch)
        for module in (miami_preparation, miami_driver, miami_preparation_runner):
            patch.setattr(module, "utc_now", lambda: simulated[0])
        patch.setattr(monitoring, "_now", lambda: simulated[0])
        # Match the explicitly simulated UTC clock; do not silently combine it
        # with wall-real duration of expensive fixture source audits. This does
        # not certify production execution latency or change the 60-second gate.
        patch.setattr(
            monitoring, "time", SimpleNamespace(monotonic=lambda: simulated[0].timestamp())
        )
        patch.setattr(supervisor, "_now", lambda: simulated[0])
        patch.setattr(settlement_runner, "datetime", Clock)
        patch.setattr("kalshi_predictor.paper.simulator.utc_now", lambda: simulated[0])
        patch.setattr("kalshi_predictor.paper.ledger.utc_now", lambda: simulated[0])
        market = captured["context"].market.artifact.decode()["market"]

        def handler(request):
            assert request.method == "GET" and "authorization" not in request.headers
            assert str(request.url) == captured["context"].market.url
            calls.append(str(request.url))
            row = market
            if simulated[0] > current + timedelta(hours=1):
                row = market | dict(
                    status="finalized",
                    result="yes",
                    settlement_ts=(simulated[0] - timedelta(minutes=30)).isoformat(),
                    settlement_value_dollars="1.00",
                )
            return httpx.Response(200, json=dict(market=row))

        patch.setattr(
            settlement_runner.httpx,
            "Client",
            lambda **kwargs: client(**{**kwargs, "transport": httpx.MockTransport(handler)}),
        )
        progress("before_actual_driver")
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
            authorization=auth,
            objective_bytes=objective,
            model=model,
            model_code=code,
            release=release,
            rule_documents=(document,),
            entries_enabled=True,
            model_evaluation_head_sha256=head,
        )
        assert result.assembly_blockers == (), result
        assert result.supervisor.admission_state == "PAPER_FILLED", result
        progress("actual_driver_paper_filled")
        simulated[0] = current + timedelta(hours=3)
        for _ in range(2):
            replay = supervisor.run_paper_supervisor(
                session_factory=factory,
                database_path=database,
                settings=captured["settings"],
                code_sha=sha,
                entries_enabled=False,
                cycles=1,
            )
            assert replay.state == "STOPPED"
            progress("settlement_restart_complete")
        with factory() as session:
            counts = {
                name: session.execute(text("SELECT count(*) FROM " + name)).scalar_one()
                for name in ("paper_orders", "paper_fills", "paper_pnl", "settlements")
            }
            assert counts == dict(paper_orders=1, paper_fills=1, paper_pnl=1, settlements=1)
            assert (
                session.execute(
                    text("SELECT count(*) FROM overnight_shadow WHERE evaluation_json IS NOT NULL")
                ).scalar_one()
                == 1
            )
            records = [r.decode()["record"] for r in load_dataset(session, dataset="paper-release")]
            assert len([r for r in records if r["kind"] == "outcome-v1"]) == 4
    engine.dispose()
    print(
        json.dumps(
            dict(
                synthetic_only=True,
                counts=counts,
                public_mock_requests=len(calls),
                real_model_release=True,
            )
        )
    )


def test_miami_owned_positive_lifecycle(tmp_path):
    repository = tmp_path / "committed-miami-lifecycle"
    _prepare_committed_fixture(repository)
    for name in (
        "test_miami_full_lifecycle.py",
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
    _git(repository, "commit", "--quiet", "-m", "Synthetic Miami lifecycle only")
    env = _fixture_git_env(repository)
    env["PYTHONPATH"] = os.pathsep.join((str(repository / "src"), str(repository / "tests")))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; from test_miami_full_lifecycle import _run; "
            "_run(Path.cwd(),Path.cwd().parent/'miami.db')",
        ],
        cwd=repository,
        env=env,
        text=True,
        capture_output=True,
        timeout=480,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout.strip().splitlines()[-1])["real_model_release"] is True
