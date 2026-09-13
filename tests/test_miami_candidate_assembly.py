# ruff: noqa: F811
"""Miami original preparation reaches the shared guarded assembly with real engines."""

import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from test_miami_preparation import prepared_inputs, session  # noqa: F401
from test_miami_source_gate import artifact, context, fixtures, grid_context  # noqa: F401
from test_paper_release_rules_timing import fixture

from kalshi_predictor.overnight_paper import rule_verifier
from kalshi_predictor.overnight_paper.boundary import LocalPaperAuthorization
from kalshi_predictor.overnight_paper.candidate_assembly import (
    MIAMI_MODEL_ENTRYPOINT,
    assemble_miami_candidate,
    miami_model_code_bundle,
)
from kalshi_predictor.overnight_paper.miami_preparation import prepare_miami_candidate
from kalshi_predictor.overnight_paper.provenance import canonical_hash
from kalshi_predictor.overnight_paper.qualification import qualify_candidate


@pytest.fixture
def assembly_inputs(session, prepared_inputs, monkeypatch):
    prep = prepare_miami_candidate(session, **prepared_inputs)
    assert prep.state == "COMPUTED_UNQUALIFIED", prep.blockers
    at = fixtures.datetime.fromisoformat(prep.records["computation_finished_at"]) + timedelta(
        milliseconds=1
    )
    frozen = fixtures.ORIGIN.isoformat()
    repo = Path(__file__).resolve().parents[1]
    dependencies, code = miami_model_code_bundle(repo)
    settings = prepared_inputs["settings"]
    config = settings.model_dump(mode="json")
    model = artifact(
        dict(
            name=prep.engine_outputs.forecast_output.model_name,
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
            code_dependencies=dependencies,
            model_entrypoint=MIAMI_MODEL_ENTRYPOINT,
        )
    )
    _, policy, document = fixture()
    policy = replace(
        policy,
        ticker=prep.ticker,
        event_id=prep.ticker.rsplit("-", 1)[0],
        series="KXTEMPMIAH",
        observation_time=prep.records["observation_time"],
        effective_from=(at - timedelta(days=1)).isoformat(),
        effective_to=(at + timedelta(days=1)).isoformat(),
    )
    monkeypatch.setattr(rule_verifier, "CERTIFIED_RULE_POLICIES", (policy,))
    args = dict(
        session=session,
        preparation=prep,
        model=model,
        model_code=code,
        settings=settings,
        repository=repo,
        code_sha="a" * 40,
        now=at,
        rule_documents=(document,),
        authorization=LocalPaperAuthorization(
            at - timedelta(hours=1),
            at + timedelta(hours=1),
            "a" * 64,
            max_new_positions=1,
            max_open_positions=1,
        ),
        include_evaluation_observation=False,
    )
    return args, policy


def test_miami_actual_engines_assemble_with_source_and_provenance_gates(assembly_inputs):
    args, _ = assembly_inputs
    candidate = assemble_miami_candidate(**args)
    result = qualify_candidate(**candidate.qualification_args)
    gates = dict(result.gates)
    assert gates["FRESH_ANALYTICAL_SOURCE"], result.blockers
    assert gates["FULL_PROVENANCE"], result.blockers
    assert result.status.value == "PAPER_NOT_READY"
    assert candidate.decision.model_name == "miami_prior_day_increment_grid30_v1"
    inputs = candidate.qualification_args["decision_inputs"]
    assert inputs["feature_id"] is None  # no fabricated NOAA feature row
    assert inputs["station"] is None
    assert inputs["miami_input_sha256"]
    assert inputs["origin_at"] == args["preparation"].records["origin_at"]
    assert candidate.evaluation_observation is None


@pytest.mark.parametrize("change", ["model", "rule", "probability", "stale", "code", "72h"])
def test_miami_assembly_preserves_missing_and_changed_gates(assembly_inputs, monkeypatch, change):
    args, policy = assembly_inputs
    args = dict(args)
    if change == "model":
        args["model"] = None
    elif change == "rule":
        monkeypatch.setattr(rule_verifier, "CERTIFIED_RULE_POLICIES", ())
    elif change == "probability":
        args["preparation"].records["forecast"]["yes_probability"] = ".99"
    elif change == "stale":
        args["now"] += timedelta(minutes=2)
    elif change == "code":
        args["model_code"] += b"changed"
    else:
        monkeypatch.setattr(
            rule_verifier,
            "CERTIFIED_RULE_POLICIES",
            (replace(policy, final_settlement_seconds=73 * 3600),),
        )
    with pytest.raises(ValueError):
        assemble_miami_candidate(**args)


def test_miami_assembly_cannot_change_preparation_settings(assembly_inputs):
    args, _ = assembly_inputs
    args = args | {
        "settings": args["settings"].model_copy(update={"paper_min_edge": Decimal(".001")})
    }
    with pytest.raises(ValueError, match="SETTINGS_CHANGED"):
        assemble_miami_candidate(**args)


@pytest.mark.parametrize("change", ["probability", "context", "source_kind", "duplicates"])
def test_bundle_gate4_rehashed_decision_cannot_borrow_namespace(assembly_inputs, change):
    from kalshi_predictor.overnight_paper.qualification import EvidenceReference

    args, _ = assembly_inputs
    candidate = assemble_miami_candidate(**args)
    inputs = dict(candidate.qualification_args["decision_inputs"])
    evidence = next(e for e in candidate.qualification_args["evidence"] if e.gate == 4)
    if change == "probability":
        inputs["forecast_probability"] = ".99"
    elif change == "context":
        inputs["miami_context_sha256"] = "f" * 64
    elif change == "source_kind":
        inputs["source_kind"] = "nws"
    else:
        evidence = replace(evidence, sources=evidence.sources * 2)
    decision_id = canonical_hash(inputs)
    report = json.loads(evidence.reference.payload)
    report["decision_id"] = decision_id
    report["sources"] = [r.sha256 for r in evidence.sources]
    changed = artifact(report)
    evidence = replace(
        evidence,
        decision_id=decision_id,
        reference=EvidenceReference("changed", changed.sha256, changed.payload),
    )
    assert not evidence.verified(inputs)


def test_miami_assembly_rejects_secret_url_before_artifact_creation(assembly_inputs):
    args, _ = assembly_inputs
    args = args | {
        "settings": args["settings"].model_copy(
            update={"kalshi_db_url": "postgresql://user:private@example.invalid/db"}
        )
    }
    with pytest.raises(ValueError, match="DATABASE_URL_FORBIDDEN") as caught:
        assemble_miami_candidate(**args)
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("change", ["string_hashes", "duplicate_hashes", "overflow"])
def test_bundle_gate4_rejects_namespace_types_and_nonfinite(assembly_inputs, change):
    from kalshi_predictor.overnight_paper.miami_provenance import MiamiBundleGateContext
    from kalshi_predictor.overnight_paper.provenance import Artifact
    from kalshi_predictor.overnight_paper.qualification import EvidenceReference

    args, _ = assembly_inputs
    candidate = assemble_miami_candidate(**args)
    inputs = dict(candidate.qualification_args["decision_inputs"])
    evidence = next(e for e in candidate.qualification_args["evidence"] if e.gate == 4)
    if change == "string_hashes":
        inputs["source_hashes"] = ",".join(inputs["source_hashes"])
    elif change == "duplicate_hashes":
        inputs["source_hashes"] = inputs["source_hashes"] * 2
    else:
        old = evidence.context.source
        row = json.loads(old.payload)
        row["unused_overflow"] = "overflow_marker"
        raw = json.dumps(row).replace('"overflow_marker"', "1e999").encode()
        changed_source = Artifact(hashlib.sha256(raw).hexdigest(), raw)
        inputs["source_hashes"] = [
            changed_source.sha256 if sha == old.sha256 else sha for sha in inputs["source_hashes"]
        ]
        evidence = replace(
            evidence,
            context=MiamiBundleGateContext(changed_source),
            sources=(EvidenceReference("changed", changed_source.sha256, changed_source.payload),),
        )
    decision_id = canonical_hash(inputs)
    report = json.loads(evidence.reference.payload)
    report.update(decision_id=decision_id, sources=[r.sha256 for r in evidence.sources])
    changed = artifact(report)
    evidence = replace(
        evidence,
        decision_id=decision_id,
        reference=EvidenceReference("changed", changed.sha256, changed.payload),
    )
    assert not evidence.verified(inputs)


def test_nested_market_json_nonfinite_is_rejected_after_exact_rebinding(assembly_inputs):
    from kalshi_predictor.overnight_paper.miami_provenance import (
        MiamiBundleGateContext,
        _context,
        verify_miami_bundle_gate4,
        verify_miami_provenance_source,
    )
    from kalshi_predictor.overnight_paper.provenance import Artifact

    args, _ = assembly_inputs
    candidate = assemble_miami_candidate(**args)
    inputs = dict(candidate.qualification_args["decision_inputs"])
    evidence = next(e for e in candidate.qualification_args["evidence"] if e.gate == 4)
    source = json.loads(evidence.context.source.payload)
    row = source["body"]["market"]["artifact"]
    raw = bytes.fromhex(row["payload_hex"])[:-1] + b',"unused_overflow":1e999}'
    row.update(payload_hex=raw.hex(), sha256=hashlib.sha256(raw).hexdigest())
    receipt_row = source["body"]["catalog_receipts"][0]
    receipt = json.loads(bytes.fromhex(receipt_row["payload_hex"]))
    receipt["sha256"] = row["sha256"]
    receipt_artifact = artifact(receipt)
    receipt_row.update(payload_hex=receipt_artifact.payload.hex(), sha256=receipt_artifact.sha256)
    # Resign every affected namespace so the test isolates strict parsed-number validation.
    checked = verify_miami_provenance_source(source, decision_at=args["now"], now=args["now"])
    inputs.update(checked["inputs"], miami_input_sha256=checked["input_sha256"])
    inputs["miami_context_sha256"] = _context(source["body"]).fingerprint()
    raw = json.dumps(source).encode()
    changed = Artifact(hashlib.sha256(raw).hexdigest(), raw)
    inputs["source_hashes"] = [changed.sha256]
    with pytest.raises(ValueError):
        verify_miami_bundle_gate4(
            MiamiBundleGateContext(changed),
            inputs=inputs,
            sources=((changed.sha256, changed.payload),),
            now=args["now"],
        )


def _run_committed_miami_assembly(repository):
    import importlib
    from types import SimpleNamespace

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from test_paper_release_all_gates import _git

    from kalshi_predictor.data.schema import Base
    from kalshi_predictor.overnight_paper import provenance

    for module in provenance.AUDITED_BOUNDARY_SHA256:
        importlib.import_module(module)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db, pytest.MonkeyPatch.context() as monkeypatch:
        original = context.__wrapped__()
        grid = grid_context.__wrapped__(original, SimpleNamespace(param="linux"))
        prepared = prepared_inputs.__wrapped__(grid, monkeypatch)
        args, _ = assembly_inputs.__wrapped__(db, prepared, monkeypatch)
        args["repository"] = repository
        args["code_sha"] = _git(repository, "rev-parse", "HEAD")
        candidate = assemble_miami_candidate(**args)
        result = qualify_candidate(**candidate.qualification_args)
        assert all(passed for _, passed in result.gates), result.blockers
        print(json.dumps(dict(all12=True, synthetic_only=True, decision_id=result.decision_id)))
        db.rollback()
    engine.dispose()


def test_miami_actual_assembly_all12_in_committed_synthetic_release(tmp_path):
    import os
    import shutil
    import subprocess
    import sys

    from test_paper_release_all_gates import _fixture_git_env, _git, _prepare_committed_fixture

    repository = tmp_path / "miami-assembly-release"
    _prepare_committed_fixture(repository)
    for name in (
        "test_miami_candidate_assembly.py",
        "test_miami_preparation.py",
        "test_miami_source_gate.py",
        "test_miami_binding.py",
        "test_guarded_fee_contract.py",
    ):
        shutil.copyfile(Path(__file__).parent / name, repository / "tests" / name)
    scripts = repository / "scripts"
    scripts.mkdir()
    shutil.copyfile(
        Path(__file__).resolve().parents[1] / "scripts/positive_ev_miami_half_hour_research.py",
        scripts / "positive_ev_miami_half_hour_research.py",
    )
    shutil.copyfile(
        Path(__file__).resolve().parents[1] / "scripts/positive_ev_miami_research.py",
        scripts / "positive_ev_miami_research.py",
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "--quiet", "-m", "Synthetic Miami actual-engine assembly fixture")
    environment = _fixture_git_env(repository)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(repository / "src"), str(repository / "tests"))
    )
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; "
            "from test_miami_candidate_assembly import _run_committed_miami_assembly; "
            "_run_committed_miami_assembly(Path.cwd())",
        ],
        cwd=repository,
        env=environment,
        text=True,
        capture_output=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1])["all12"]

