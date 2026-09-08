"""All twelve real gates on committed synthetic evidence, never production readiness.

The child imports an isolated copy of the actual implementation. Its test-only
source review manifest is committed before verification; the production checkout
is never represented as clean or changed by this test. No verifier is replaced.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repository), *arguments],
        text=True,
        stderr=subprocess.STDOUT,
        timeout=120,
    ).strip()


def _prepare_committed_fixture(repository: Path) -> None:
    original = Path(__file__).resolve().parents[1]
    shutil.copytree(
        original / "src", repository / "src", ignore=shutil.ignore_patterns("__pycache__")
    )
    test_dir = repository / "tests"
    test_dir.mkdir()
    for name in (
        "test_paper_release_all_gates.py",
        "test_overnight_provenance.py",
        "test_overnight_qualification.py",
        "test_paper_release_provenance.py",
        "test_paper_release_rules_timing.py",
        "test_phase_3n_advanced_risk.py",
    ):
        shutil.copyfile(original / "tests" / name, test_dir / name)
    path = repository / "src/kalshi_predictor/overnight_paper/provenance.py"
    source = path.read_text(encoding="utf-8")
    declaration = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "AUDITED_BOUNDARY_SHA256"
    )
    reviewed = ast.literal_eval(declaration.value)
    for module in (
        "kalshi_predictor.overnight_paper.coordinator",
        "kalshi_predictor.overnight_paper.boundary_gate",
        "kalshi_predictor.memory.repository",
    ):
        reviewed[module] = ""
    for module in reviewed:
        raw = (repository / "src" / (module.replace(".", "/") + ".py")).read_bytes()
        reviewed[module] = hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()
    lines = source.splitlines(keepends=True)
    assert declaration.end_lineno is not None
    lines[declaration.lineno - 1 : declaration.end_lineno] = [
        "AUDITED_BOUNDARY_SHA256: dict[str, str] = " + repr(reviewed) + "\n"
    ]
    path.write_text("".join(lines), encoding="utf-8")
    (repository / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.name", "Synthetic gate fixture")
    _git(repository, "config", "user.email", "fixture@example.invalid")
    _git(repository, "config", "core.autocrlf", "false")
    _git(repository, "config", "commit.gpgsign", "false")
    _git(repository, "add", ".")
    _git(repository, "commit", "--quiet", "-m", "Commit synthetic actual-code gate fixture")
    assert not _git(repository, "status", "--porcelain")


def _all_gate_inputs(repository: Path):
    from test_overnight_provenance import artifact
    from test_overnight_qualification import structured_gate
    from test_paper_release_provenance import complete_inputs
    from test_paper_release_rules_timing import fixture
    from test_phase_3n_advanced_risk import _config, _request

    from kalshi_predictor.advanced_risk.engine import AdvancedRiskEngine
    from kalshi_predictor.overnight_paper import rule_verifier
    from kalshi_predictor.overnight_paper.gate_context import QualificationContext
    from kalshi_predictor.overnight_paper.provenance import canonical_hash

    args = complete_inputs()
    original = args["context"]
    public, market_inputs = structured_gate()
    inputs = args["decision"] | market_inputs
    now, stamp = args["now"], inputs["decision_at"]
    sha = _git(repository, "rev-parse", "HEAD")
    sources = []
    for reference in public.sources:
        value = json.loads(reference.payload)
        if value["url"].endswith("/orderbook"):
            value["body"]["orderbook_fp"]["yes_dollars"] = [["0.49", "1000"]]
        value.update(provider_updated_at=stamp, provider_generated_at=stamp, available_at=stamp)
        if "forecast/hourly" in value["url"]:
            value["provider_updated_at"] = value["body"]["properties"]["updateTime"]
            value["provider_generated_at"] = value["body"]["properties"]["generatedAt"]
        sources.append(artifact(value))
    source_hashes = [value.sha256 for value in sources]
    settings = dict(
        opportunity_max_spread="0.2",
        execution_enabled=False,
        execution_dry_run=True,
        execution_kill_switch=True,
        execution_gateway_mode="disabled",
        autopilot_enabled=False,
        autopilot_dry_run=True,
        learning_mode=False,
    )
    _, policy, document = fixture()
    policy = replace(
        policy,
        ticker=inputs["ticker"],
        event_id=inputs["event_id"],
        series=inputs["series"],
        observation_time=inputs["close_time"],
    )
    # Synthetic reviewed policy data is the only injected dependency. The actual
    # verifier still validates every field, document hash and timing relation.
    rule_verifier.CERTIFIED_RULE_POLICIES = (policy,)
    request = _request(phase_3m_contracts=args["phase3m"].proposed_contracts)
    request = replace(
        request,
        decision_timestamp=now,
        instrument_id=inputs["ticker"],
        category_id=inputs["category"],
        model_id=inputs["model_version"],
        correlation_group_id=inputs["event_id"],
        entry_price=Decimal(inputs["executable_price"]),
        estimated_round_trip_fees=Decimal("0.01"),
        estimated_slippage_per_contract=Decimal("0.01"),
        gap_or_tail_buffer_per_contract=Decimal("0.01"),
        portfolio_snapshot=replace(request.portfolio_snapshot, captured_at=now),
        market_snapshot=replace(
            request.market_snapshot,
            captured_at=now,
            bid_price=Decimal("0.49"),
            ask_price=Decimal("0.5"),
        ),
        edge_statistics=replace(request.edge_statistics, statistics_as_of=now - timedelta(days=1)),
    )
    risk = AdvancedRiskEngine(_config()).decide(request)
    assert risk.action.value == "ALLOW", risk.hard_blocks
    common = {key: inputs[key] for key in ("ticker", "event_id", "series")}
    features = original.features_artifact.decode()
    features.update(common, source_hashes=source_hashes)
    weather = next(value for value in sources if "forecast/hourly" in value.decode()["url"])
    features["records"][0].update(
        name="synthetic_temperature", value=70, source_sha256=weather.sha256
    )
    feature_artifact = artifact(features)
    rows = {key: value.decode() for key, value in original.artifacts.items()}
    rows["forecast"].update(
        common,
        source_hashes=source_hashes,
        code_sha=sha,
        rule_version=policy.version,
        features_artifact_sha256=feature_artifact.sha256,
    )
    book = next(
        value.decode()["body"] for value in sources if value.decode()["url"].endswith("/orderbook")
    )
    rows["snapshot"].update(common, book=book)
    rows["config"] = settings
    rows["phase3n"] = risk.as_dict()
    artifacts = {key: artifact(value) for key, value in rows.items()}
    inputs.update(
        source_hashes=source_hashes,
        settings=settings,
        config_hash=canonical_hash(settings),
        code_sha=sha,
        rule_version=policy.version,
        settlement_rule=asdict(policy),
        observation_time=policy.observation_time,
        market_close_time=policy.observation_time,
        market_open_time=(now - timedelta(hours=1)).isoformat(),
        expected_settlement_time=(now + timedelta(hours=1, seconds=60)).isoformat(),
        settlement_deadline=(now + timedelta(hours=1, seconds=120)).isoformat(),
        final_settlement_time=None,
        snapshot_book_hash=canonical_hash(book),
        phase3n_hash=canonical_hash(risk.as_dict()),
        features_artifact_sha256=feature_artifact.sha256,
    )
    inputs["settlement_rule"]["amendments"] = []
    inputs["feature_timestamps"] = [
        {key: value for key, value in record.items() if key != "value"}
        for record in features["records"]
    ]
    inputs["source_timestamps"] = [
        {
            "sha256": value.sha256,
            **{
                key: value.decode()[key]
                for key in (
                    "provider_updated_at",
                    "provider_generated_at",
                    "available_at",
                    "received_at",
                )
            },
        }
        for value in sources
    ]
    inputs.update({key + "_artifact_sha256": value.sha256 for key, value in artifacts.items()})
    context = QualificationContext(
        repository,
        (document,),
        replace(
            original,
            artifacts=artifacts,
            source_artifacts=tuple(sources),
            features_artifact=feature_artifact,
            expected_code_sha=sha,
            expected_rule_version=policy.version,
        ),
        args["phase3m"],
        risk,
    )
    return inputs, context, now


def _run_all_gates(repository: Path) -> None:
    from test_overnight_provenance import artifact

    from kalshi_predictor.overnight_paper import provenance
    from kalshi_predictor.overnight_paper.boundary import ExecutionMode
    from kalshi_predictor.overnight_paper.boundary_gate import verify_coordinator_boundary
    from kalshi_predictor.overnight_paper.qualification import (
        SEMANTIC_VERIFIERS,
        EvidenceReference,
        GateEvidence,
        Readiness,
        compute_net_ev,
        decision_fingerprint,
        qualify_candidate,
    )

    inputs, context, now = _all_gate_inputs(repository)
    for module in provenance.AUDITED_BOUNDARY_SHA256:
        importlib.import_module(module)
    boundary = verify_coordinator_boundary(
        repository=repository, code_sha=inputs["code_sha"], settings=inputs["settings"]
    )
    assert boundary.passed, boundary.blockers
    sources = tuple(
        EvidenceReference(value.decode()["url"], value.sha256, value.payload)
        for value in context.provenance.source_artifacts
    )
    decision_id = decision_fingerprint(inputs)
    evidence = []
    for gate, verifier in SEMANTIC_VERIFIERS.items():
        report = artifact(
            dict(
                schema="overnight-paper-gate-v1",
                gate=gate,
                decision_id=decision_id,
                ticker=inputs["ticker"],
                category=inputs["category"],
                verifier=verifier,
                verdict="PASS",
                validated_at=now.isoformat(),
                valid_until=(now + timedelta(seconds=60)).isoformat(),
                sources=[source.sha256 for source in sources],
            )
        )
        evidence.append(
            GateEvidence(
                gate,
                decision_id,
                inputs["category"],
                inputs["ticker"],
                verifier,
                EvidenceReference("computed-report", report.sha256, report.payload),
                sources=sources,
                context=context,
            )
        )
    result = qualify_candidate(
        ticker=inputs["ticker"],
        category=inputs["category"],
        decision_inputs=inputs,
        decision_id=decision_id,
        evidence=tuple(evidence),
        ev=compute_net_ev(
            model_probability=Decimal(inputs["forecast_probability"]),
            executable_price=Decimal(inputs["executable_price"]),
            estimated_fee=Decimal("0.01"),
            slippage_allowance=Decimal("0.01"),
            uncertainty_buffer=Decimal("0.01"),
        ),
        minimum_net_ev=Decimal("0.05"),
        phase3m=context.phase3m,
        phase3n=context.phase3n,
        mode=ExecutionMode.LOCAL_PAPER,
    )
    assert result.status == Readiness.PAPER_ELIGIBLE, result.blockers
    assert len(result.gates) == 12 and all(passed for _, passed in result.gates)

    def rejected(gate, changes, *, checked_context=context, checked_sources=sources):
        changed = inputs | changes
        original = next(item for item in evidence if item.gate == gate)
        payload = json.loads(original.reference.payload)
        payload.update(
            decision_id=decision_fingerprint(changed),
            sources=[source.sha256 for source in checked_sources],
        )
        report = artifact(payload)
        checked = replace(
            original,
            decision_id=payload["decision_id"],
            reference=EvidenceReference("recomputed-report", report.sha256, report.payload),
            sources=checked_sources,
            context=checked_context,
        )
        assert not checked.verified(changed, as_of=now), (gate, changes)

    rejected(3, {}, checked_context=replace(context, rule_documents=()))
    rejected(3, {"rule_version": "other-rule"})
    rejected(2, {"event_id": "OTHER-EVENT"})
    rejected(9, {"model_version": "other-model"})
    rejected(9, {"snapshot_book_hash": "0" * 64})
    rejected(9, {"source_timestamps": []})
    rejected(12, {"settings": inputs["settings"] | {"execution_enabled": True}})
    stale_sources = []
    for source in sources:
        payload = json.loads(source.payload)
        if "forecast/hourly" in payload["url"]:
            payload["body"]["properties"]["updateTime"] = "2026-09-07T00:00:00Z"
        original = artifact(payload)
        stale_sources.append(EvidenceReference(source.artifact, original.sha256, original.payload))
    rejected(
        4,
        {"source_hashes": [source.sha256 for source in stale_sources]},
        checked_sources=tuple(stale_sources),
    )
    assert not _git(repository, "status", "--porcelain")
    print(json.dumps({"sha": inputs["code_sha"], "gates": result.gates, "synthetic_only": True}))


def test_all_twelve_gates_pass_real_verifiers_on_committed_synthetic_fixture(tmp_path):
    repository = tmp_path / "committed-fixture"
    _prepare_committed_fixture(repository)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(repository / "src"), str(repository / "tests"))
    )
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; from test_paper_release_all_gates import _run_all_gates; "
            "_run_all_gates(Path.cwd())",
        ],
        cwd=repository,
        env=environment,
        text=True,
        capture_output=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert report["synthetic_only"] and len(report["gates"]) == 12
    assert all(passed for _, passed in report["gates"])
