"""Honest fixed-model lineage without fabricated fitting data or evaluation bypass."""

import hashlib
from dataclasses import replace

import pytest
from test_overnight_provenance import artifact
from test_paper_release_dataset import observation, outcome, policy, stamp
from test_paper_release_provenance import complete_inputs

from kalshi_predictor.overnight_paper.evaluation_dataset import (
    append_record,
    build_observation,
    evaluate_dataset,
)
from kalshi_predictor.overnight_paper.provenance import canonical_hash
from kalshi_predictor.overnight_paper.provenance_gate import verify_complete_provenance


def fixed_rows(decision, rows, *, frozen_at=None, parameters=None):
    model = rows["model"]
    if parameters is not None:
        rows["config"] = parameters
    frozen = frozen_at or stamp(7).isoformat()
    model.update(
        model_kind="fixed_heuristic",
        training_cutoff=None,
        training_dataset_hashes=[],
        created_at=stamp(7, 0).isoformat(),
        frozen_at=frozen,
        available_at=frozen,
        parameters=rows["config"],
        parameters_sha256=canonical_hash(rows["config"]),
    )
    for row in (decision, rows["forecast"]):
        row.update(
            model_kind="fixed_heuristic",
            training_cutoff=None,
            model_frozen_at=frozen,
            model_parameters_sha256=model["parameters_sha256"],
            model_code_sha256=model["code_sha256"],
        )
    rows["forecast"]["model_artifact_sha256"] = artifact(model).sha256
    artifacts = {key: artifact(row) for key, row in rows.items()}
    decision["config_hash"] = canonical_hash(rows["config"])
    decision.update({key + "_artifact_sha256": value.sha256 for key, value in artifacts.items()})
    return artifacts


def fixed_inputs(**changes):
    args = complete_inputs()
    context = args["context"]
    rows = {key: value.decode() for key, value in context.artifacts.items()}
    artifacts = fixed_rows(args["decision"], rows, **changes)
    args["context"] = replace(context, artifacts=artifacts, training_artifacts=())
    args["decision_id"] = canonical_hash(args["decision"])
    return args


def fixed_observation(day, *, code_swap=False, **changes):
    row = observation(day).decode()
    rows = {key: value["payload"] for key, value in row["originals"].items()}
    if code_swap:
        code = bytes.fromhex(row["model_code_hex"]) + b"# different frozen code\n"
        row["model_code_hex"] = code.hex()
        rows["model"]["code_sha256"] = hashlib.sha256(code).hexdigest()
    artifacts = fixed_rows(row["decision"], rows, **changes)
    row["originals"] = {
        key: {"sha256": value.sha256, "payload": value.decode()} for key, value in artifacts.items()
    }
    row["decision_id"] = canonical_hash(row["decision"])
    row["training"] = []
    return artifact(row)


def fixed_dataset(
    *,
    model_swap=False,
    missing_model_policy=False,
    late_freeze=False,
    pending=False,
    code_swap=False,
):
    train = fixed_observation(8)
    chain = append_record((), train, recorded_at=stamp(8))
    chain = append_record(chain, outcome(train), recorded_at=stamp(8, 2))
    holdout = fixed_observation(
        10, **({"frozen_at": stamp(9, 2).isoformat()} if late_freeze else {})
    )
    manifest_hash = holdout.decode()["originals"]["model"]["sha256"]
    frozen_policy = policy(
        **({} if missing_model_policy else {"model_artifact_sha256": manifest_hash})
    )
    chain = append_record(chain, frozen_policy, recorded_at=stamp(9))
    for day in (10, 11):
        obs = (
            holdout
            if day == 10
            else fixed_observation(
                11,
                code_swap=code_swap,
                **({"parameters": {"minimum_edge": "0.06"}} if model_swap else {}),
            )
        )
        chain = append_record(chain, obs, recorded_at=stamp(day))
        if not pending:
            chain = append_record(chain, outcome(obs), recorded_at=stamp(day, 2))
    return chain


def test_explicit_fixed_model_passes_lineage_without_fictional_training():
    args = fixed_inputs()
    result = verify_complete_provenance(**args)
    assert result.passed, result.blockers
    assert args["decision"]["training_cutoff"] is None
    assert args["context"].training_artifacts == ()
    assert not result.model_calibration_verified


def test_trained_default_still_requires_genuine_training():
    args = complete_inputs()
    args["context"] = replace(args["context"], training_artifacts=())
    assert not verify_complete_provenance(**args).passed


def test_fixed_model_builds_real_prospective_observation_without_training_artifacts():
    args = fixed_inputs()
    decision, context = args["decision"], args["context"]
    snapshot = context.artifacts["snapshot"].decode()
    snapshot["market_implied_probability"] = 0.5
    snapshot_artifact = artifact(snapshot)
    rule = artifact(
        {key: decision[key] for key in ("ticker", "event_id", "series", "rule_version")}
    )
    decision.update(
        snapshot_artifact_sha256=snapshot_artifact.sha256,
        rule_artifact_sha256=rule.sha256,
        executable_price=0.4,
        estimated_fee=0.01,
        slippage=0.01,
        uncertainty=0.01,
        side="BUY_YES",
    )
    args["decision_id"] = canonical_hash(decision)
    args["context"] = replace(
        context, artifacts=context.artifacts | {"snapshot": snapshot_artifact}
    )
    row = build_observation(
        provenance_args=args,
        independent_event_id="E",
        event_window_start=stamp(8),
        event_window_end=stamp(8, 2),
        rule_artifact=rule,
        market_probability=0.5,
        executable_price=0.4,
        estimated_fee=0.01,
        slippage=0.01,
        uncertainty=0.01,
    ).decode()
    assert row["training"] == []
    assert row["decision"]["training_cutoff"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("training_cutoff", "2026-09-06T00:00:00Z"),
        ("model_kind", "trained"),
        ("model_parameters_sha256", "0" * 64),
        ("model_frozen_at", "2026-09-06T00:00:00Z"),
    ],
)
def test_rehashed_decision_cannot_misstate_fixed_manifest(field, value):
    args = fixed_inputs()
    args["decision"][field] = value
    args["decision_id"] = canonical_hash(args["decision"])
    assert not verify_complete_provenance(**args).passed


def test_fixed_model_cannot_attach_fictional_training_records():
    args = fixed_inputs()
    args["context"] = replace(
        args["context"], training_artifacts=complete_inputs()["context"].training_artifacts
    )
    assert verify_complete_provenance(**args).blockers == (
        "FIXED_HEURISTIC_CANNOT_CLAIM_TRAINING_ARTIFACTS",
    )


def test_future_freeze_rejected_even_when_every_hash_is_bound():
    result = verify_complete_provenance(**fixed_inputs(frozen_at=stamp(9).isoformat()))
    assert result.blockers == ("FIXED_HEURISTIC_FREEZE_VISIBILITY_INVALID",)


def test_fixed_model_requires_full_precommitted_evaluation():
    result = evaluate_dataset(fixed_dataset(), as_of=stamp(14))
    assert result.ready, result.blockers
    assert result.metrics["independent_train_events"] == 1
    assert result.metrics["independent_holdout_events"] == 2
    pending = evaluate_dataset(fixed_dataset(pending=True), as_of=stamp(14))
    assert not pending.ready
    assert "INSUFFICIENT_INDEPENDENT_HOLDOUT_EVENTS" in pending.blockers
    assert "HOLDOUT_OUTCOMES_INCOMPLETE" in pending.blockers


@pytest.mark.parametrize(
    "change", ["model_swap", "missing_model_policy", "late_freeze", "code_swap"]
)
def test_holdout_requires_exact_code_parameters_manifest_frozen_before_policy(change):
    result = evaluate_dataset(fixed_dataset(**{change: True}), as_of=stamp(14))
    assert not result.ready
    assert result.blockers == ("FIXED_HEURISTIC_NOT_FROZEN_IN_POLICY",)
