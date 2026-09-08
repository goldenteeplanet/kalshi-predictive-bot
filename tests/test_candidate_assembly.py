"""Actual existing weather computation assembled into real qualification gates."""

import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from test_overnight_provenance import artifact
from test_paper_release_all_gates import _git, _prepare_committed_fixture
from test_paper_release_preparation import original_inputs
from test_paper_release_rules_timing import fixture

from kalshi_predictor.config import Settings
from kalshi_predictor.data.schema import Base, PaperFill, PaperOrder, PaperPosition
from kalshi_predictor.overnight_paper import rule_verifier
from kalshi_predictor.overnight_paper.boundary import LocalPaperAuthorization
from kalshi_predictor.overnight_paper.candidate_assembly import (
    WEATHER_MODEL_ENTRYPOINT,
    assemble_weather_candidate,
    weather_model_code_bundle,
)
from kalshi_predictor.overnight_paper.preparation import prepare_weather_candidate
from kalshi_predictor.overnight_paper.provenance import canonical_hash
from kalshi_predictor.overnight_paper.qualification import EvidenceReference, qualify_candidate
from kalshi_predictor.overnight_paper.store import REQUIRED_DECISION


def assembly_inputs(session, repository):
    ticker, sources, receipt = original_inputs()
    settings = Settings(
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
    )
    adjusted = []
    for source in sources:
        row = json.loads(source.payload)
        if "market" in row["body"]:
            row["body"]["market"]["open_time"] = (receipt - timedelta(hours=1)).isoformat()
        frozen = artifact(row)
        adjusted.append(EvidenceReference(source.artifact, frozen.sha256, frozen.payload))
    prep = prepare_weather_candidate(
        session,
        ticker=ticker,
        source_envelopes=tuple(adjusted),
        settings=settings,
        slippage_allowance=Decimal("0.01"),
        uncertainty_buffer=Decimal("0.01"),
    )
    assert prep.state == "COMPUTED_UNQUALIFIED", prep.blockers
    rows = {
        json.loads(source.payload)["url"]: json.loads(source.payload)["body"] for source in adjusted
    }
    market = next(row["market"] for row in rows.values() if "market" in row)
    event = next(row["event"] for row in rows.values() if "event" in row)
    _, policy, document = fixture()
    policy = replace(
        policy,
        ticker=ticker,
        event_id=market["event_ticker"],
        series=event["series_ticker"],
        observation_time=market["close_time"],
        effective_from=(receipt - timedelta(days=1)).isoformat(),
        effective_to=(receipt + timedelta(days=1)).isoformat(),
    )
    now = datetime.now(UTC)
    parameters = settings.model_dump(mode="json")
    dependencies, code = weather_model_code_bundle(repository)
    model = artifact(
        dict(
            name=prep.decision.model_name,
            version="2.0.0",
            model_kind="fixed_heuristic",
            training_cutoff=None,
            training_dataset_hashes=[],
            frozen_at=(receipt - timedelta(days=1)).isoformat(),
            created_at=(receipt - timedelta(days=1)).isoformat(),
            available_at=(receipt - timedelta(days=1)).isoformat(),
            parameters=parameters,
            parameters_sha256=canonical_hash(parameters),
            code_sha256=hashlib.sha256(code).hexdigest(),
            code_dependencies=dependencies,
            model_entrypoint=WEATHER_MODEL_ENTRYPOINT,
        )
    )
    authorization = LocalPaperAuthorization(
        receipt - timedelta(hours=1),
        receipt + timedelta(hours=12),
        "a" * 64,
        max_new_positions=1,
        max_open_positions=1,
    )
    return dict(
        preparation=prep,
        model=model,
        model_code=code,
        settings=settings,
        repository=repository,
        code_sha=_git(repository, "rev-parse", "HEAD"),
        authorization=authorization,
        rule_documents=(document,),
        now=now,
    ), policy


@pytest.fixture
def assembled_inputs(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        args, policy = assembly_inputs(session, Path(__file__).resolve().parents[1])
        monkeypatch.setattr(rule_verifier, "CERTIFIED_RULE_POLICIES", (policy,))
        yield args, session
        session.rollback()
    engine.dispose()


def test_actual_preparation_assembles_originals_and_all_nonrelease_gates(assembled_inputs):
    args, session = assembled_inputs
    args = args | {"model_evaluation_head_sha256": "e" * 64}
    candidate = assemble_weather_candidate(**args)
    result = qualify_candidate(**candidate.qualification_args)
    assert all(passed for name, passed in result.gates if name != "NO_EXCHANGE_PATH"), (
        result.blockers
    )
    inputs = candidate.qualification_args["decision_inputs"]
    assert inputs["model_evaluation_head_sha256"] == "e" * 64
    assert candidate.shadow_payload["model_evaluation_required"] is True
    assert (
        candidate.evaluation_observation.decode()["decision_id"]
        == candidate.qualification_args["decision_id"]
    )
    assert candidate.shadow_payload["qualification_inputs"] == inputs
    assert not REQUIRED_DECISION - candidate.shadow_payload.keys()
    assert (
        candidate.decision.raw_decision_json["position_sizing_decision_id"]
        == args["preparation"].records["sizing_id"]
    )
    assert (
        candidate.decision.raw_decision_json["advanced_risk_decision_id"]
        == args["preparation"].records["risk_id"]
    )
    for source in candidate.qualification_args["evidence"][0].sources:
        row = json.loads(source.payload)
        original = bytes.fromhex(row["original_envelope_payload_hex"])
        assert hashlib.sha256(original).hexdigest() == row["original_envelope_sha256"]
        assert json.loads(original)["body"] == row["body"]
        if row["clock_basis"] == "public_rest_receipt":
            assert row["provider_generated_at"] is None and row["provider_updated_at"] is None
    for cls in (PaperOrder, PaperFill, PaperPosition):
        assert session.scalar(select(func.count()).select_from(cls)) == 0


def test_missing_model_or_rules_is_precise_and_never_invented(assembled_inputs, monkeypatch):
    args, _ = assembled_inputs
    with pytest.raises(ValueError, match="FROZEN_ORIGINAL_MODEL_REQUIRED"):
        assemble_weather_candidate(**(args | {"model": None}))
    monkeypatch.setattr(rule_verifier, "CERTIFIED_RULE_POLICIES", ())
    with pytest.raises(ValueError, match="NO_UNAMBIGUOUS_CERTIFIED_RULE"):
        assemble_weather_candidate(**args)


def test_changed_model_parameters_or_prepared_book_fail(assembled_inputs):
    args, _ = assembled_inputs
    model = args["model"].decode()
    model["parameters"]["paper_min_edge"] = "0.01"
    with pytest.raises(ValueError, match="ASSEMBLY_PROVENANCE_INVALID"):
        assemble_weather_candidate(**(args | {"model": artifact(model)}))
    prep = args["preparation"]
    bad = replace(prep, records=prep.records | {"book": {"different": True}})
    with pytest.raises(ValueError, match="ORIGINAL_PREPARATION_BOOK_MISMATCH"):
        assemble_weather_candidate(**(args | {"preparation": bad}))


def test_nonempty_credentials_refused_before_artifact_creation(assembled_inputs):
    args, _ = assembled_inputs
    settings = args["settings"].model_copy(update={"kalshi_api_key_id": "synthetic-unused-key"})
    with pytest.raises(ValueError, match="CREDENTIAL_FREE_LOCAL_SETTINGS_REQUIRED"):
        assemble_weather_candidate(**(args | {"settings": settings}))


@pytest.mark.parametrize("mutation", ["omitted", "swapped", "unrelated", "entrypoint"])
def test_frozen_code_must_match_actual_complete_dependency_closure(assembled_inputs, mutation):
    args, _ = assembled_inputs
    row = args["model"].decode()
    code = args["model_code"]
    if mutation == "omitted":
        row["code_dependencies"].pop(next(iter(row["code_dependencies"])))
    elif mutation == "swapped":
        row["code_dependencies"][next(iter(row["code_dependencies"]))] = "0" * 64
    elif mutation == "entrypoint":
        row["model_entrypoint"] = "unrelated:forecast"
    else:
        code = b"unrelated code with a valid self hash"
        row["code_sha256"] = hashlib.sha256(code).hexdigest()
    with pytest.raises(
        ValueError, match="DEPENDENCY_MISMATCH|CODE_BUNDLE_MISMATCH|ENTRYPOINT_REQUIRED"
    ):
        assemble_weather_candidate(**(args | {"model": artifact(row), "model_code": code}))


def _run_committed_assembly(repository):
    from kalshi_predictor.overnight_paper import provenance

    for module in provenance.AUDITED_BOUNDARY_SHA256:
        importlib.import_module(module)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        args, policy = assembly_inputs(session, repository)
        rule_verifier.CERTIFIED_RULE_POLICIES = (policy,)
        candidate = assemble_weather_candidate(**args)
        result = qualify_candidate(**candidate.qualification_args)
        assert all(passed for _, passed in result.gates), result.blockers
        print(
            json.dumps({"all12": True, "synthetic_only": True, "decision_id": result.decision_id})
        )
        session.rollback()
    engine.dispose()


def test_actual_assembly_passes_all12_in_committed_isolated_release(tmp_path):
    repository = tmp_path / "assembly-release"
    _prepare_committed_fixture(repository)
    source = Path(__file__).parent
    for name in ("test_candidate_assembly.py", "test_paper_release_preparation.py"):
        shutil.copyfile(source / name, repository / "tests" / name)
    _git(repository, "add", "tests")
    _git(repository, "commit", "--quiet", "-m", "Synthetic actual preparation assembly fixture")
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
            "from test_candidate_assembly import _run_committed_assembly; "
            "_run_committed_assembly(Path.cwd())",
        ],
        cwd=repository,
        env=environment,
        text=True,
        capture_output=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1])["all12"]
