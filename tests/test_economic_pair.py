"""Real existing-model receipt, pure normalization and immutable replay negatives."""

from dataclasses import replace
from datetime import timedelta

import pytest
from test_api_tournament import execution_policy, fee_document, policy, rebind
from test_economic_execution import code_bundle, example  # noqa: F401

from kalshi_predictor.data_sources import economic_execution, economic_pair
from kalshi_predictor.data_sources.tournament import evaluate_tournament
from kalshi_predictor.overnight_paper.provenance import canonical_hash
from kalshi_predictor.overnight_paper.source_health import aware
from kalshi_predictor.utils.time import utc_now


@pytest.fixture
def paired(example):  # noqa: F811
    artifact = economic_execution._artifact
    # Synthetic quadratic metadata is explicit before model execution. Actual
    # current CPI fee type is tested separately and remains unsupported.
    market = example["market"].decode()
    market["body"]["market"].update(
        price_ranges=[dict(start="0.01", end="0.99", step="0.01")],
        volume_fp="100000",
        open_interest_fp="100000",
        liquidity_dollars="100000",
    )
    example["market"] = artifact(market)
    series = example["series"].decode()
    series["body"]["series"]["fee_type"] = "quadratic"
    example["series"] = artifact(series)
    committed = aware(example["frozen_procedure"].decode()["available_at"]) + timedelta(seconds=1)
    cost = execution_policy(committed_at=committed.isoformat(), side="BUY_YES")
    frozen = policy(
        kind=economic_pair.POLICY_KIND,
        source_id="FRED",
        model_artifact_sha256=example["frozen_model"].sha256,
        procedure_sha256=example["frozen_procedure"].sha256,
        execution_policy_sha256=cost.sha256,
        committed_at=committed.isoformat(),
        holdout_start=(committed + timedelta(seconds=1)).isoformat(),
        holdout_end=(committed + timedelta(days=10)).isoformat(),
    )
    receipt = economic_execution.execute_lagged_cpi_research(**example)
    arguments = dict(
        receipt=receipt,
        model=example["frozen_model"],
        procedure=example["frozen_procedure"],
        model_code=example["model_code"],
        source=example["source"],
        market=example["market"],
        event=example["event"],
        series=example["series"],
        book=example["book"],
        policy=frozen,
        execution_policy=cost,
        fee_original=fee_document(),
        decision_at=utc_now(),
        recorded_at=utc_now(),
    )
    return economic_pair.build_economic_pair(**arguments), arguments


def test_real_receipt_normalizes_without_model_database_or_network_calls(paired, monkeypatch):
    pair, args = paired

    def forbidden(*_, **__):
        raise AssertionError("Pure adapter must not execute model/HTTP/DB")

    monkeypatch.setattr(economic_execution.EconomicV1Forecaster, "forecast", forbidden)
    rebuilt = economic_pair.build_economic_pair(**args)
    assert rebuilt == pair
    result = evaluate_tournament(policy=args["policy"], pairs=(pair,), as_of=args["recorded_at"])
    assert result.status == "NOT_ENOUGH_DATA", result
    assert result.evidence_scope == economic_pair.EVIDENCE_SCOPE
    assert result.contrast_type == economic_pair.CONTRAST
    assert result.independent_event_n == 1 and result.metrics is None
    assert result.actual_paper_pnl is None
    assert pair.outcome is None
    assert pair.features[0].decode()["clock_basis"] == "LOCAL_RECEIPT_NOT_PROVIDER_PUBLICATION"


def test_rehashed_receipt_and_original_substitutions_rejected(paired):
    pair, args = paired

    def assess(value):
        return evaluate_tournament(policy=args["policy"], pairs=(value,), as_of=args["recorded_at"])

    old = args["receipt"]
    row = old.decode()
    changes = [
        dict(source_on_probability="0.9"),
        dict(source_off_probability="0.9"),
        dict(ephemeral_feature_id=999),
        dict(ephemeral_link_id=999),
        dict(ephemeral_snapshot_id=999),
        dict(dependencies_after={}),
        dict(settings_original={}),
        dict(source_received_at="2099-01-01T00:00:00Z"),
        dict(source_original_sha256="0" * 64),
        dict(book_envelope_sha256="0" * 64),
        dict(receipt_generated_at="2099-01-01T00:00:00Z"),
        dict(ephemeral_session=False),
        dict(fee_execution_verified=True),
        dict(runtime_certified=True),
    ]
    for changeset in changes:
        changed = economic_execution._artifact(row | changeset)
        altered = rebind(pair, {"execution_receipt_sha256": changed.sha256}, ((old, changed),))
        assert assess(altered).status == "INVALID_EVIDENCE", changeset
    for field in ("source_on", "source_off"):
        changed = economic_execution._artifact(
            getattr(pair, field).decode() | dict(probability="0.9")
        )
        assert assess(replace(pair, **{field: changed})).status == "INVALID_EVIDENCE"
    old_feature = pair.features[0]
    changed = economic_execution._artifact(old_feature.decode() | dict(value={"invented": True}))
    on = economic_execution._artifact(
        pair.source_on.decode() | dict(feature_hashes=[changed.sha256])
    )
    assert assess(replace(pair, features=(changed,), source_on=on)).status == "INVALID_EVIDENCE"


def test_generic_policy_cannot_masquerade_as_verified_economic_procedure(paired):
    pair, args = paired
    generic = economic_execution._artifact(
        args["policy"].decode() | dict(kind="paired-source-policy-v1")
    )
    result = evaluate_tournament(policy=generic, pairs=(pair,), as_of=args["recorded_at"])
    assert result.status == "INVALID_EVIDENCE"


def test_direct_verifier_refuses_decoded_context_substitution(paired):
    pair, args = paired
    anchor = pair.anchor.decode()
    contexts = {value.sha256: value.decode() for value in pair.context_originals}
    contexts[anchor["execution_receipt_sha256"]]["source_on_probability"] = "0.99"
    with pytest.raises(ValueError, match="DECODED_CONTEXT_SUBSTITUTION"):
        economic_pair.validate_economic_receipt(
            pair,
            anchor,
            contexts[anchor["snapshot_sha256"]],
            args["model"].decode(),
            contexts,
            args["policy"].decode(),
            args["decision_at"],
        )


@pytest.mark.parametrize("inflate_adjustment", [False, True])
def test_coordinated_rehashed_probability_inflation_is_not_execution_evidence(
    paired, inflate_adjustment
):
    pair, args = paired
    row = args["receipt"].decode()
    row["source_on_probability"] = "0.9"
    row["forecast_original"]["yes_probability"] = "0.9"
    row["forecast_original"]["feature_json"]["final_probability"] = "0.9"
    if inflate_adjustment:
        row["forecast_original"]["feature_json"]["adjustment"] = "0.425"
    row["forecast_sha256"] = canonical_hash(row["forecast_original"])
    receipt = economic_execution._artifact(row)
    changed = rebind(
        pair, {"execution_receipt_sha256": receipt.sha256}, ((args["receipt"], receipt),)
    )
    on = economic_execution._artifact(changed.source_on.decode() | {"probability": "0.9"})
    changed = replace(changed, source_on=on)
    anchor = changed.anchor.decode()
    contexts = {value.sha256: value.decode() for value in changed.context_originals}
    with pytest.raises(ValueError, match="EXISTING_ARITHMETIC_MISMATCH"):
        economic_pair.validate_economic_receipt(
            changed,
            anchor,
            contexts[anchor["snapshot_sha256"]],
            args["model"].decode(),
            contexts,
            args["policy"].decode(),
            args["decision_at"],
        )
    assert (
        evaluate_tournament(
            policy=args["policy"], pairs=(changed,), as_of=args["recorded_at"]
        ).status
        == "INVALID_EVIDENCE"
    )


def test_current_cpi_fee_type_remains_an_independent_cost_blocker(paired):
    pair, args = paired
    anchor = pair.anchor.decode()
    old_rule = next(x for x in pair.context_originals if x.sha256 == anchor["rule_sha256"])
    rule = old_rule.decode()
    old_series = next(
        x for x in pair.context_originals if x.sha256 == rule["series_original_sha256"]
    )
    wrapper = old_series.decode()
    wrapper["provider_payload"]["series"]["fee_type"] = "quadratic_with_maker_fees"
    from kalshi_predictor.overnight_paper.provenance import canonical_hash

    wrapper["provider_payload_sha256"] = canonical_hash(wrapper["provider_payload"])
    new_series = economic_execution._artifact(wrapper)
    new_rule = economic_execution._artifact(rule | dict(series_original_sha256=new_series.sha256))
    changed = rebind(
        pair, dict(rule_sha256=new_rule.sha256), ((old_series, new_series), (old_rule, new_rule))
    )
    # Test the existing cost verifier directly so this isn't merely rejection
    # of the independently inconsistent execution receipt after substitution.
    from kalshi_predictor.data_sources.tournament import _execution_context

    contexts = {x.sha256: x.decode() for x in changed.context_originals}
    with pytest.raises(ValueError):
        _execution_context(
            changed.anchor.decode(),
            contexts[anchor["snapshot_sha256"]],
            new_rule.decode(),
            contexts,
            args["policy"].decode(),
            args["decision_at"],
        )
