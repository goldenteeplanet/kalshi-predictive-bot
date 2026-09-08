"""Synthetic paired originals exercise actual scoring and leakage/cohort checks."""

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from test_overnight_provenance import artifact

from kalshi_predictor.data_sources.tournament import (
    CONSERVATIVE_FEE_MODEL,
    FEE_ROUNDING_SOURCE_URL,
    PairedForecast,
    conservative_single_fill_fees,
    evaluate_tournament,
)
from kalshi_predictor.overnight_paper.provenance import canonical_hash
from kalshi_predictor.overnight_paper.qualification import PUBLIC_BASE


def at(day, hour=1):
    return datetime(2026, 9, day, hour, tzinfo=UTC)


def model():
    return artifact(
        dict(
            name="synthetic",
            version="1",
            model_kind="fixed_heuristic",
            training_cutoff=None,
            frozen_at=at(1).isoformat(),
        )
    )


def fee_document():
    # Synthetic original exercises bindings; not an authenticated production doc.
    raw = b"Synthetic six-decimal trade-fee and cent-balance policy fixture."
    return artifact(
        dict(
            kind="fee-rounding-original-v1",
            source_url=FEE_ROUNDING_SOURCE_URL,
            source_version="UNVERSIONED_DOCUMENT_CAPTURE",
            received_at=at(1, 0).isoformat(),
            raw_payload_hex=raw.hex(),
            raw_payload_sha256=hashlib.sha256(raw).hexdigest(),
        )
    )


def execution_policy(**changes):
    return artifact(
        dict(
            kind="execution-policy-v1",
            committed_at=at(1).isoformat(),
            contracts=1,
            fee_model=CONSERVATIVE_FEE_MODEL,
            cost_assumption="CONSERVATIVE_CENT_BALANCE_SINGLE_FILL",
            balance_precision="0.01",
            starting_rounding_accumulator="0",
            rebate_assumption="0",
            fee_rounding_original_sha256=fee_document().sha256,
            fee_interpretation_version="ceil6dp-cent-buy-zero-accumulator-v1",
            max_quote_age_seconds=60,
            max_spread="0.10",
            slippage="0.01",
            uncertainty="0.01",
        )
        | changes
    )


def policy(**changes):
    row = dict(
        kind="paired-source-policy-v1",
        source_id="source-trial",
        model_artifact_sha256=model().sha256,
        execution_policy_sha256=execution_policy().sha256,
        committed_at=at(2).isoformat(),
        holdout_start=at(3, 0).isoformat(),
        holdout_end=at(8).isoformat(),
        minimum_independent_events=2,
        calibration_bin_count=4,
        minimum_brier_improvement=0.05,
        minimum_log_loss_improvement=0.05,
        minimum_ece_improvement=0.05,
        minimum_mean_net_ev_delta=0.05,
        minimum_mean_counterfactual_pnl_delta=0.05,
        opportunity_minimum_net_ev=0.01,
        maximum_source_on_ece=0.4,
    )
    return artifact(row | changes)


def pair(day, *, cluster=None, probability=0.75, result="yes"):
    clock = at(day)
    book = {"orderbook_fp": {"yes_dollars": [["0.45", "100"]], "no_dollars": [["0.50", "100"]]}}
    snapshot = artifact(
        dict(
            id=f"book-{day}",
            ticker=f"T-{day}",
            captured_at=clock.isoformat(),
            available_at=clock.isoformat(),
            clock_basis="public_rest_receipt",
            request_url=f"{PUBLIC_BASE}/markets/T-{day}/orderbook",
            provider_payload=book,
            provider_payload_sha256=canonical_hash(book),
        )
    )
    market_payload = {
        "market": dict(
            ticker=f"T-{day}",
            event_ticker=f"E-{day}",
            status="active",
            close_time=at(day, 2).isoformat(),
            series_ticker="SERIES",
            price_ranges=[dict(start="0.01", end="0.99", step="0.01")],
            volume_fp="100000",
            open_interest_fp="100000",
            liquidity_dollars="100000",
        )
    }
    series_payload = {"series": dict(ticker="SERIES", fee_type="quadratic", fee_multiplier="1")}

    def captured(kind, payload):
        return artifact(
            dict(
                kind=kind,
                request_url=(
                    f"{PUBLIC_BASE}/markets/T-{day}"
                    if kind == "market-original-v1"
                    else f"{PUBLIC_BASE}/series/SERIES"
                ),
                received_at=clock.isoformat(),
                available_at=clock.isoformat(),
                provider_payload=payload,
                provider_payload_sha256=canonical_hash(payload),
            )
        )

    market = captured("market-original-v1", market_payload)
    series = captured("series-original-v1", series_payload)
    rule = artifact(
        dict(
            ticker=f"T-{day}",
            event_id=f"E-{day}",
            rule_version="v1",
            available_at=clock.isoformat(),
            series_ticker="SERIES",
            market_original_sha256=market.sha256,
            series_original_sha256=series.sha256,
        )
    )
    anchor = artifact(
        dict(
            event_id=f"E-{day}",
            independent_event_id=cluster or f"DAY-{day}",
            ticker=f"T-{day}",
            snapshot_id=f"book-{day}",
            snapshot_sha256=snapshot.sha256,
            decision_at=clock.isoformat(),
            model_name="synthetic",
            model_version="1",
            model_kind="fixed_heuristic",
            training_cutoff=None,
            model_frozen_at=at(1).isoformat(),
            model_artifact_sha256=model().sha256,
            rule_version="v1",
            rule_sha256=rule.sha256,
            execution_policy_sha256=execution_policy().sha256,
            side="BUY_YES",
            executable_price=0.5,
            estimated_fee=0.02,
            trade_fee=0.0175,
            rounding_allowance=0.0025,
            slippage=0.01,
            uncertainty=0.01,
            event_window_start=clock.isoformat(),
            event_window_end=at(day, 2).isoformat(),
        )
    )
    source_payload = {"value": 42}
    source = artifact(
        dict(
            kind="source-original-v1",
            source_id="source-trial",
            available_at=clock.isoformat(),
            received_at=clock.isoformat(),
            provider_payload=source_payload,
            provider_payload_sha256=canonical_hash(source_payload),
        )
    )
    feature = artifact(
        dict(
            kind="feature-v1",
            source_id="source-trial",
            observed_at=(clock - timedelta(minutes=1)).isoformat(),
            available_at=clock.isoformat(),
            generated_at=clock.isoformat(),
            source_original_sha256=source.sha256,
            value=42,
        )
    )
    common = anchor.decode() | dict(
        kind="paired-forecast-v1",
        anchor_sha256=anchor.sha256,
        decision_id=canonical_hash(anchor.decode()),
        source_id="source-trial",
        generated_at=clock.isoformat(),
        recorded_at=clock.isoformat(),
    )
    off = artifact(common | dict(source_enabled=False, probability=0.45, feature_hashes=[]))
    on = artifact(
        common | dict(source_enabled=True, probability=probability, feature_hashes=[feature.sha256])
    )
    original = dict(
        kind="final-original-v1",
        status="final",
        result=result,
        ticker=f"T-{day}",
        event_id=f"E-{day}",
        final_at=at(day, 2).isoformat(),
        available_at=at(day, 3).isoformat(),
        received_at=at(day, 3).isoformat(),
    )
    outcome = artifact(
        dict(
            kind="outcome-v1",
            decision_id=canonical_hash(anchor.decode()),
            event_id=f"E-{day}",
            ticker=f"T-{day}",
            rule_version="v1",
            result=result,
            final_at=at(day, 2).isoformat(),
            available_at=at(day, 3).isoformat(),
            provider_payload=original,
            provider_payload_sha256=canonical_hash(original),
        )
    )
    return PairedForecast(
        anchor,
        off,
        on,
        outcome,
        (feature,),
        (snapshot, model(), rule, market, series, execution_policy(), fee_document(), source),
    )


def evaluate(pairs, frozen=None, **kwargs):
    return evaluate_tournament(policy=frozen or policy(), pairs=tuple(pairs), as_of=at(9), **kwargs)


def changed_market(original, changes, *, omit_series=False):
    old_market, old_rule = original.context_originals[3], original.context_originals[2]
    row = old_market.decode()
    row["provider_payload"]["market"].update(changes)
    if omit_series:
        del row["provider_payload"]["market"]["series_ticker"]
    row["provider_payload_sha256"] = canonical_hash(row["provider_payload"])
    market = artifact(row)
    rule = artifact(old_rule.decode() | dict(market_original_sha256=market.sha256))
    return rebind(original, dict(rule_sha256=rule.sha256), ((old_market, market), (old_rule, rule)))


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "closed"},
        {"status": "settled"},
        {"status": "initialized"},
        {"status": None},
        {"close_time": at(3).isoformat()},
        {"close_time": (at(3) - timedelta(seconds=1)).isoformat()},
        {"close_time": None},
        {"close_time": "2026-09-03T02:00:00"},
        {"series_ticker": "OTHER"},
        {"series_ticker": None},
    ],
)
def test_fresh_book_does_not_override_market_lifecycle_or_series(changes):
    changed = changed_market(pair(3), changes)
    assert evaluate([changed, pair(4)]).status == "INVALID_EVIDENCE"


@pytest.mark.parametrize("status", ["open", "active"])
def test_open_market_and_optional_absent_series_remain_supported(status):
    rows = [changed_market(pair(day), {"status": status}, omit_series=True) for day in (3, 4)]
    assert evaluate(rows).status == "POINT_ESTIMATES_MEET_POLICY_REVIEW_REQUIRED"


def test_actual_paired_scores_costs_and_counterfactual_opportunities():
    result = evaluate([pair(3), pair(4)])
    assert result.status == "POINT_ESTIMATES_MEET_POLICY_REVIEW_REQUIRED"
    assert result.independent_event_n == result.paired_decision_n == 2
    assert result.metrics["brier_improvement"] == pytest.approx(0.24)
    assert result.metrics["ece_improvement"] == pytest.approx(0.3)
    assert result.metrics["log_loss_improvement"] > 0
    assert result.metrics["opportunities_off"] == 0
    assert result.metrics["opportunities_on"] == 2
    assert result.metrics["mean_counterfactual_policy_pnl_delta"] == pytest.approx(0.47)
    assert result.actual_paper_pnl is result.actual_shadow_pnl is result.value_score is None
    assert all(value is None for value in result.measurements.values())
    assert result.purchase_verdict.startswith("UNAVAILABLE") and result.verified_hashes


def test_missing_evidence_never_becomes_fake_zero_metrics():
    for rows in ([], [pair(3)], [pair(3), replace(pair(4), outcome=None)]):
        result = evaluate(rows)
        assert result.status == "NOT_ENOUGH_DATA" and result.metrics is None
        assert result.value_score is None


def test_related_events_reduce_independent_n_and_exact_repeats_reject():
    result = evaluate([pair(3, cluster="same-day"), pair(4, cluster="same-day")])
    assert result.status == "NOT_ENOUGH_DATA"
    assert result.independent_event_n == 1 and len(result.purged_decision_ids) == 1
    repeated = pair(3)
    assert evaluate([repeated, repeated]).status == "INVALID_EVIDENCE"


@pytest.mark.parametrize(
    "key,value",
    [
        ("ticker", "different"),
        ("decision_id", "different"),
        ("snapshot_id", "different"),
        ("decision_at", "2026-09-03T01:01:00Z"),
        ("model_version", "different"),
        ("training_cutoff", "2026-09-04T00:00:00Z"),
        ("rule_version", "different"),
        ("executable_price", 0.01),
    ],
)
def test_on_off_exact_cohort_fields_are_required(key, value):
    changed = pair(3)
    on = artifact(changed.source_on.decode() | {key: value})
    assert evaluate([replace(changed, source_on=on), pair(4)]).status == "INVALID_EVIDENCE"


def test_feature_visibility_and_original_hash_are_rechecked():
    original = pair(3)
    future = artifact(original.features[0].decode() | dict(available_at=at(3, 2).isoformat()))
    on = artifact(original.source_on.decode() | dict(feature_hashes=[future.sha256]))
    assert (
        evaluate([replace(original, features=(future,), source_on=on), pair(4)]).status
        == "INVALID_EVIDENCE"
    )
    tampered = replace(original.features[0], payload=b"{}")
    assert evaluate([replace(original, features=(tampered,)), pair(4)]).status == "INVALID_EVIDENCE"
    assert evaluate([replace(original, context_originals=()), pair(4)]).status == "INVALID_EVIDENCE"


def test_source_enabled_marker_without_actual_ablation_is_rejected():
    original = pair(3)
    off = artifact(
        original.source_off.decode() | dict(feature_hashes=[original.features[0].sha256])
    )
    assert evaluate([replace(original, source_off=off), pair(4)]).status == "INVALID_EVIDENCE"


def test_holdout_must_be_precommitted_and_complete():
    rows = (pair(3), pair(4))
    assert evaluate(rows, policy(committed_at=at(5).isoformat())).status == "INVALID_EVIDENCE"
    incomplete = evaluate_tournament(policy=policy(), pairs=rows, as_of=at(5))
    assert incomplete.status == "NOT_ENOUGH_DATA" and incomplete.metrics is None
    assert "HOLDOUT_WINDOW_NOT_COMPLETE" in incomplete.blockers
    future_policy = evaluate_tournament(policy=policy(), pairs=(), as_of=at(1))
    assert future_policy.status == "INVALID_EVIDENCE"


def test_feature_available_after_forecast_before_decision_is_rejected():
    original = pair(3)
    on = artifact(
        original.source_on.decode() | dict(generated_at=(at(3) - timedelta(seconds=30)).isoformat())
    )
    assert evaluate([replace(original, source_on=on), pair(4)]).status == "INVALID_EVIDENCE"


def test_distinct_cluster_labels_do_not_make_overlapping_windows_independent():
    original = pair(4)
    anchor = artifact(original.anchor.decode() | dict(event_window_start=at(3).isoformat()))
    identity = dict(
        anchor_sha256=anchor.sha256,
        decision_id=canonical_hash(anchor.decode()),
        event_window_start=at(3).isoformat(),
    )
    changed = replace(
        original,
        anchor=anchor,
        source_off=artifact(original.source_off.decode() | identity),
        source_on=artifact(original.source_on.decode() | identity),
        outcome=artifact(original.outcome.decode() | dict(decision_id=identity["decision_id"])),
    )
    result = evaluate([pair(3), changed])
    assert result.status == "NOT_ENOUGH_DATA"
    assert result.independent_event_n == 1 and len(result.purged_decision_ids) == 1


def test_harmful_source_does_not_pass_or_suggest_purchase():
    result = evaluate(
        [pair(3, probability=0.9, result="no"), pair(4, probability=0.9, result="no")]
    )
    assert result.status == "POINT_ESTIMATES_FAIL_POLICY"
    assert result.metrics["brier_improvement"] < 0
    assert result.metrics["mean_counterfactual_policy_pnl_delta"] < 0
    assert result.value_score is None and result.purchase_verdict.startswith("UNAVAILABLE")


def test_measured_cost_reliability_remain_separate_and_zero_requests_unknown():
    measurements = artifact(
        dict(
            kind="provider-measurements-v1",
            source_id="source-trial",
            measured_at=at(9).isoformat(),
            monthly_cost_usd=25,
            latency_p95_ms=150,
            request_count=10,
            failure_count=2,
        )
    )
    result = evaluate([pair(3), pair(4)], measurements=measurements)
    assert result.measurements["failure_rate"] == 0.2
    assert result.measurements["monthly_cost_usd"] == 25
    assert result.measurements["coverage_fraction"] is None and result.value_score is None
    zero = artifact(measurements.decode() | dict(request_count=0, failure_count=0))
    assert evaluate([pair(3), pair(4)], measurements=zero).measurements["failure_rate"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"result": "no"},
        {"ticker": "OTHER"},
        {"event_id": "OTHER"},
        {"status": "open"},
        {"available_at": "2026-09-03T02:30:00Z"},
        {"received_at": "2026-09-03T02:30:00Z"},
    ],
)
def test_outcome_original_semantics_cannot_be_replaced_by_correct_hash(changes):
    original = pair(3)
    row = original.outcome.decode()
    payload = row["provider_payload"] | changes
    changed = artifact(
        row | dict(provider_payload=payload, provider_payload_sha256=canonical_hash(payload))
    )
    assert evaluate([replace(original, outcome=changed), pair(4)]).status == "INVALID_EVIDENCE"


def test_features_require_intact_source_originals():
    original = pair(3)
    assert (
        evaluate([replace(original, context_originals=original.context_originals[:-1])]).status
        == "INVALID_EVIDENCE"
    )
    corrupt = replace(original.context_originals[-1], payload=b"{}")
    assert (
        evaluate(
            [replace(original, context_originals=original.context_originals[:-1] + (corrupt,))]
        ).status
        == "INVALID_EVIDENCE"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"source_id": "other"},
        {"received_at": "2026-09-03T02:00:00Z"},
        {"provider_payload_sha256": "0" * 64},
    ],
)
def test_feature_source_identity_clock_and_payload_hash_are_bound(changes):
    original = pair(3)
    source = artifact(original.context_originals[-1].decode() | changes)
    feature = artifact(original.features[0].decode() | dict(source_original_sha256=source.sha256))
    on = artifact(original.source_on.decode() | dict(feature_hashes=[feature.sha256]))
    changed = replace(
        original,
        context_originals=original.context_originals[:-1] + (source,),
        features=(feature,),
        source_on=on,
    )
    assert evaluate([changed, pair(4)]).status == "INVALID_EVIDENCE"


def rebind(original, changes, replacements=()):
    """Rehash both variants too, so negatives exercise semantic validation."""
    anchor = artifact(original.anchor.decode() | changes)
    fields = anchor.decode() | dict(
        anchor_sha256=anchor.sha256, decision_id=canonical_hash(anchor.decode())
    )
    contexts = {row.sha256: row for row in original.context_originals}
    for old, new in replacements:
        del contexts[old.sha256]
        contexts[new.sha256] = new
    return replace(
        original,
        anchor=anchor,
        source_off=artifact(original.source_off.decode() | fields),
        source_on=artifact(original.source_on.decode() | fields),
        outcome=artifact(original.outcome.decode() | dict(decision_id=fields["decision_id"])),
        context_originals=tuple(contexts.values()),
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"executable_price": 0.49},
        {"estimated_fee": 0.01},
        {"estimated_fee": 0.0175},
        {"estimated_fee": 0},
        {"slippage": 0},
        {"uncertainty": 0},
        {"side": "BUY_NO"},
        {"side": "SELL_YES"},
    ],
)
def test_paired_cost_tampering_is_not_saved_by_consistent_anchor_hashes(changes):
    assert evaluate([rebind(pair(3), changes), pair(4)]).status == "INVALID_EVIDENCE"


@pytest.mark.parametrize(
    "mutation",
    [
        "stale",
        "future",
        "substituted",
        "wrong_request",
        "crossed",
        "thin",
        "missing",
        "off_tick",
        "wide",
    ],
)
def test_original_book_must_support_execution_at_decision_time(mutation):
    original = pair(3)
    previous = original.context_originals[0]
    row = previous.decode()
    if mutation == "stale":
        row["captured_at"] = (at(3) - timedelta(seconds=61)).isoformat()
    elif mutation == "future":
        row["captured_at"] = (at(3) + timedelta(seconds=1)).isoformat()
    elif mutation == "wrong_request":
        row["request_url"] = f"{PUBLIC_BASE}/markets/OTHER/orderbook"
    else:
        yes, no, depth = "0.45", "0.50", "100"
        if mutation == "substituted":
            no = "0.49"
        if mutation == "crossed":
            yes = "0.60"
        if mutation == "thin":
            depth = "0.5"
        if mutation == "off_tick":
            yes = "0.451"
        if mutation == "wide":
            yes = "0.20"
        payload = {"orderbook_fp": {"yes_dollars": [[yes, depth]], "no_dollars": [[no, depth]]}}
        if mutation == "missing":
            payload["orderbook_fp"]["no_dollars"] = []
        row.update(provider_payload=payload, provider_payload_sha256=canonical_hash(payload))
    snapshot = artifact(row)
    changed = rebind(original, {"snapshot_sha256": snapshot.sha256}, ((previous, snapshot),))
    assert evaluate([changed, pair(4)]).status == "INVALID_EVIDENCE"


@pytest.mark.parametrize(
    "mutation",
    [
        "fee_type",
        "multiplier",
        "series_id",
        "stale_liquidity",
        "future_liquidity",
        "tick_rules",
        "zero_liquidity",
    ],
)
def test_captured_fee_and_market_rules_cannot_be_substituted(mutation):
    original = pair(3)
    old_rule = original.context_originals[2]
    rule = old_rule.decode()
    series_change = mutation in {"fee_type", "multiplier", "series_id"}
    previous = original.context_originals[4 if series_change else 3]
    row = previous.decode()
    data = row["provider_payload"]["series" if series_change else "market"]
    if mutation == "fee_type":
        data["fee_type"] = "unsupported"
    if mutation == "multiplier":
        data["fee_multiplier"] = "2"
    if mutation == "series_id":
        data["ticker"] = "OTHER"
    if mutation == "stale_liquidity":
        row["received_at"] = (at(3) - timedelta(seconds=61)).isoformat()
    if mutation == "future_liquidity":
        row["available_at"] = (at(3) + timedelta(seconds=1)).isoformat()
    if mutation == "tick_rules":
        data["price_ranges"] = []
    if mutation == "zero_liquidity":
        data.update(volume_fp="0", open_interest_fp="0", liquidity_dollars="0")
    row["provider_payload_sha256"] = canonical_hash(row["provider_payload"])
    replacement = artifact(row)
    rule["series_original_sha256" if series_change else "market_original_sha256"] = (
        replacement.sha256
    )
    new_rule = artifact(rule)
    changed = rebind(
        original, {"rule_sha256": new_rule.sha256}, ((previous, replacement), (old_rule, new_rule))
    )
    assert evaluate([changed, pair(4)]).status == "INVALID_EVIDENCE"


def test_buy_no_uses_yes_bid_complement_and_selected_side_probability():
    fees = conservative_single_fill_fees(Decimal("0.55"), Decimal("1"))
    rows = [
        rebind(
            pair(day, probability=0.2, result="no"),
            dict(
                side="BUY_NO",
                executable_price="0.55",
                estimated_fee=str(fees["estimated_fee"]),
                trade_fee=str(fees["trade_fee"]),
                rounding_allowance=str(fees["rounding_allowance"]),
            ),
        )
        for day in (3, 4)
    ]
    result = evaluate(rows)
    assert result.status == "POINT_ESTIMATES_MEET_POLICY_REVIEW_REQUIRED"
    assert result.metrics["mean_net_ev_on"] == pytest.approx(0.8 - 0.55 - 0.02 - 0.01 - 0.01)
    assert result.actual_paper_pnl is None


@pytest.mark.parametrize(
    "changes",
    [
        {"max_quote_age_seconds": 61},
        {"contracts": 2},
        {"fee_model": "maker"},
        {"committed_at": at(3).isoformat()},
    ],
)
def test_execution_policy_cannot_be_relaxed_or_committed_after_tournament(changes):
    updated = execution_policy(**changes)
    original = pair(3)
    changed = rebind(
        original,
        {"execution_policy_sha256": updated.sha256},
        ((original.context_originals[5], updated),),
    )
    result = evaluate([changed], policy(execution_policy_sha256=updated.sha256))
    assert result.status == "INVALID_EVIDENCE"


@pytest.mark.parametrize(
    "price,multiplier,trade,rounding,total_fee,debit",
    [
        ("0.50", "1", "0.017500", "0.002500", "0.020000", "0.52"),
        ("0.055", "1", "0.003639", "0.001361", "0.005000", "0.06"),
        ("0.55", "1", "0.017325", "0.002675", "0.020000", "0.57"),
        ("0.50", "0", "0", "0", "0", "0.50"),
        ("0.055", "0", "0", "0.005", "0.005", "0.06"),
        ("0", "1", "0", "0", "0", "0"),
        ("1", "1", "0", "0", "0", "1"),
        ("0.999999", "1", "0.000001", "0", "0.000001", "1"),
    ],
)
def test_conservative_fee_accounts_for_balance_alignment(
    price, multiplier, trade, rounding, total_fee, debit
):
    fees = conservative_single_fill_fees(Decimal(price), Decimal(multiplier))
    assert fees == dict(
        trade_fee=Decimal(trade),
        rounding_allowance=Decimal(rounding),
        estimated_fee=Decimal(total_fee),
        total_debit=Decimal(debit),
    )
    assert fees["estimated_fee"] == fees["trade_fee"] + fees["rounding_allowance"]
    assert (Decimal(price) + fees["estimated_fee"]) % Decimal("0.01") == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"balance_precision": "0.0001"},
        {"starting_rounding_accumulator": "0.01"},
        {"rebate_assumption": "0.01"},
        {"cost_assumption": "ACTUAL_ACCOUNT_CERTIFIED"},
        {"fee_interpretation_version": "old-ceil4dp"},
    ],
)
def test_account_precision_or_rebate_cannot_be_assumed(changes):
    original = pair(3)
    updated = execution_policy(**changes)
    changed = rebind(
        original,
        {"execution_policy_sha256": updated.sha256},
        ((original.context_originals[5], updated),),
    )
    assert (
        evaluate([changed], policy(execution_policy_sha256=updated.sha256)).status
        == "INVALID_EVIDENCE"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"source_url": "https://other.invalid/fees"},
        {"raw_payload_sha256": "0" * 64},
        {"source_version": "INVENTED_OFFICIAL_VERSION"},
        {"received_at": at(2).isoformat()},
        {"raw_payload_hex": ""},
    ],
)
def test_original_fee_document_bound_to_precommitted_policy(changes):
    original = pair(3)
    changed_doc = artifact(fee_document().decode() | changes)
    updated = execution_policy(fee_rounding_original_sha256=changed_doc.sha256)
    changed = rebind(
        original,
        {"execution_policy_sha256": updated.sha256},
        ((original.context_originals[5], updated), (original.context_originals[6], changed_doc)),
    )
    assert (
        evaluate([changed], policy(execution_policy_sha256=updated.sha256)).status
        == "INVALID_EVIDENCE"
    )
