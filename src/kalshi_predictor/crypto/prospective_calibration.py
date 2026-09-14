"""Bounded prospective scoring and dependency grouping, with no paper authority.

Local recording clocks are chronology checks, not external timestamp attestations.
Dependency components prevent known pseudoreplication; disjoint components alone
never establish independence. Realized residuals are not probability error labels.
"""

from __future__ import annotations

import hashlib
import json
import random
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlsplit

from kalshi_predictor.crypto.research_shadow_evaluation import scores

METHODS = (
    "EMPIRICAL_RESIDUAL_QUANTILE",
    "CLUSTERED_BOOTSTRAP_RESIDUAL_BOUND",
    "CALIBRATION_ERROR_RESERVE",
    "ENSEMBLE_DISAGREEMENT_RESERVE",
    "HYBRID_CONSERVATIVE_RESERVE",
)
MAX_ROWS = 4096


def _criteria(plan: dict) -> dict | None:
    criteria = plan.get("validation_criteria")
    if criteria is None:
        return None
    if not isinstance(criteria, dict):
        raise ValueError("VALIDATION_CRITERIA_OBJECT_REQUIRED")
    if not _time(plan["window_start"]) < _time(criteria["split_at"]) < _time(plan["window_end"]):
        raise ValueError("CHRONOLOGICAL_HOLDOUT_SPLIT_REQUIRED")
    for key in ("quantile", "nominal_coverage"):
        value = Decimal(criteria[key])
        if not value.is_finite() or not 0 < value < 1:
            raise ValueError("INTERIOR_PREDECLARED_VALIDATION_LEVEL_REQUIRED")
    for key in ("max_coverage_gap", "max_stability_delta"):
        value = Decimal(criteria[key])
        if not value.is_finite() or not 0 <= value <= 1:
            raise ValueError("BOUNDED_VALIDATION_TOLERANCE_REQUIRED")
    for key, low, high in (
        ("min_independent_clusters", 2, MAX_ROWS),
        ("bootstrap_seed", 0, 2**32 - 1),
        ("bootstrap_draws", 2, 512),
    ):
        if type(criteria[key]) is not int or not low <= criteria[key] <= high:
            raise ValueError("BOUNDED_PREDECLARED_SAMPLE_POLICY_REQUIRED")
    return criteria


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _object(raw: bytes) -> dict:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 2_000_000:
        raise ValueError("BOUNDED_ORIGINAL_REQUIRED")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    result = json.loads(raw, object_pairs_hook=unique, parse_float=Decimal)
    if not isinstance(result, dict):
        raise ValueError("OBJECT_REQUIRED")
    return result


def _time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise ValueError("AWARE_TIME_REQUIRED")
    return result


def _hash(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError("SHA256_REQUIRED")
    return value


def prospective_decision(original: bytes, *, protocol: bytes, recorded_at: datetime) -> dict:
    """Validate a committed pre-outcome decision; preserve original hashes.

    Caller must durably store originals before target and independently verify
    source/book originals. This API cannot certify supplied provenance by itself.
    """
    row, plan = _object(original), _object(protocol)
    _criteria(plan)
    if (
        row.get("schema") != "prospective-calibration-decision-v1"
        or plan.get("schema") != "prospective-calibration-protocol-v1"
    ):
        raise ValueError("PROSPECTIVE_SCHEMA_REQUIRED")
    if recorded_at.utcoffset() is None:
        raise ValueError("AWARE_TIME_REQUIRED")
    declared, decision, target = (
        _time(plan["declared_at"]),
        _time(row["decision_time"]),
        _time(row["target_at"]),
    )
    if not declared <= decision <= recorded_at < target or not _time(
        plan["window_start"]
    ) <= target <= _time(plan["window_end"]):
        raise ValueError("PRETARGET_RECORDING_REQUIRED")
    if row["protocol_sha256"] != _sha(protocol):
        raise ValueError("PROTOCOL_BINDING_REQUIRED")
    for key in ("model", "segment"):
        if not isinstance(row.get(key), str) or not row[key] or row[key] != plan.get(key):
            raise ValueError("PREDECLARED_MODEL_SEGMENT_REQUIRED")
    for key in ("ticker", "event", "asset", "settlement_event", "rule_version"):
        if not isinstance(row.get(key), str) or not row[key]:
            raise ValueError("CONTRACT_AND_DEPENDENCY_IDENTITY_REQUIRED")
    for key in ("source_sha256", "book_sha256", "market_sha256"):
        _hash(row[key])
    if not _time(row["source_window_start"]) < _time(row["source_window_end"]) <= decision:
        raise ValueError("PREDECISION_SOURCE_WINDOW_REQUIRED")
    p = Decimal(row["p_yes"])
    if not p.is_finite() or not 0 <= p <= 1:
        raise ValueError("BINARY_FORECAST_REQUIRED")
    ensemble = row.get("ensemble_probabilities")
    if ensemble is not None:
        if not isinstance(ensemble, list) or not 2 <= len(ensemble) <= 32:
            raise ValueError("BOUNDED_REAL_ENSEMBLE_REQUIRED")
        if any(not Decimal(v).is_finite() or not 0 <= Decimal(v) <= 1 for v in ensemble):
            raise ValueError("BINARY_ENSEMBLE_FORECAST_REQUIRED")
    for key in ("executable_price", "fee", "snapshot_impact"):
        value = row.get(key)
        if value is not None and (not Decimal(value).is_finite() or Decimal(value) < 0):
            raise ValueError("NONNEGATIVE_KNOWN_COST_REQUIRED")
    if row.get("executable_price") is not None and not 0 < Decimal(row["executable_price"]) < 1:
        raise ValueError("INTERIOR_EXECUTABLE_PRICE_REQUIRED")
    if (
        plan.get("methods") != list(METHODS)
        or plan.get("selection_criterion") != "HELD_OUT_COVERAGE_STABILITY_PROSPECTIVE_VALIDITY"
    ):
        raise ValueError("PREDECLARED_COMPARISON_REQUIRED")
    return dict(
        row,
        decision_id=_sha(original),
        recorded_at=recorded_at.isoformat(),
        original_sha256=_sha(original),
        state="OPEN",
        external_timestamp_attestation=False,
        decision_original_json=original.decode("utf-8"),
        protocol_original_json=protocol.decode("utf-8"),
        uncertainty_status="UNCERTAINTY_RESEARCH_ONLY",
        paper_eligible=False,
    )


def _replay_decision(decision: dict) -> None:
    replay = prospective_decision(
        decision["decision_original_json"].encode("utf-8"),
        protocol=decision["protocol_original_json"].encode("utf-8"),
        recorded_at=_time(decision["recorded_at"]),
    )
    if replay != decision:
        raise ValueError("DECISION_ORIGINAL_REPLAY_MISMATCH")


def finalize_decision(
    decision: dict, *, official_original: bytes, official_receipt: bytes, as_of: datetime
) -> dict:
    """Score only final binary official outcomes, without recomputing forecasts."""
    _replay_decision(decision)
    original, receipt = _object(official_original), _object(official_receipt)
    market = original["market"]
    url = urlsplit(receipt["url"])
    ticker = decision["ticker"]
    if (
        url.scheme != "https"
        or url.netloc not in {"api.elections.kalshi.com", "external-api.kalshi.com"}
        or url.path != f"/trade-api/v2/markets/{ticker}"
        or url.query
        or url.fragment
        or receipt.get("method") != "GET"
        or receipt.get("http_status") != 200
        or receipt.get("original_complete") is not True
        or receipt.get("source_sha256") != _sha(official_original)
    ):
        raise ValueError("OFFICIAL_ORIGINAL_RECEIPT_REQUIRED")
    if market.get("ticker") != ticker or market.get("event_ticker") != decision["event"]:
        raise ValueError("OFFICIAL_CONTRACT_MISMATCH")
    if (
        market.get("status") != "finalized"
        or market.get("result") not in ("yes", "no")
        or market.get("is_provisional") not in (None, False)
    ):
        raise ValueError("STRICT_OFFICIAL_FINAL_REQUIRED")
    outcome = int(market["result"] == "yes")
    if Decimal(str(market.get("settlement_value_dollars"))) != outcome:
        raise ValueError("EXACT_BINARY_PAYOUT_REQUIRED")
    target = _time(decision["target_at"])
    received = _time(receipt["received_at"])
    if (
        as_of.utcoffset() is None
        or not _time(decision["recorded_at"])
        < target
        <= _time(market["settlement_ts"])
        <= received
        <= as_of
        or not target <= _time(receipt["requested_at"]) <= received
        or _time(market["close_time"]) != target
    ):
        raise ValueError("FINAL_OUTCOME_CHRONOLOGY_REQUIRED")
    p = Decimal(decision["p_yes"])
    return dict(
        decision_id=decision["decision_id"],
        outcome=outcome,
        residual=str(p - outcome),
        **{k: v for k, v in scores(p, outcome).items() if k not in ("outcome",)},
        official_sha256=_sha(official_original),
        receipt_sha256=_sha(official_receipt),
        official_original_json=official_original.decode("utf-8"),
        official_receipt_json=official_receipt.decode("utf-8"),
        evaluated_at=as_of.isoformat(),
        settlement_at=market["settlement_ts"],
        state="EVALUATED",
        paper_pnl=None,
    )


def calibration_dataset(decisions: list[dict], evaluations: list[dict]) -> dict:
    """Transitive dependency grouping; grouping count is explicitly not independent N."""
    if len(decisions) > MAX_ROWS or len(evaluations) > MAX_ROWS:
        raise ValueError("BOUNDED_DATASET_REQUIRED")
    indexed = {r["decision_id"]: r for r in decisions}
    scored = {r["decision_id"]: r for r in evaluations}
    if (
        len(indexed) != len(decisions)
        or len(scored) != len(evaluations)
        or not scored.keys() <= indexed.keys()
    ):
        raise ValueError("UNIQUE_BOUND_EVALUATIONS_REQUIRED")
    for row in decisions:
        _replay_decision(row)
    parents = list(range(len(decisions)))

    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    owner: dict[tuple, int] = {}
    for i, row in enumerate(decisions):
        # Same global target also groups assets sharing contemporaneous regimes.
        keys = [
            ("event", row["event"]),
            ("settlement", row["settlement_event"]),
            ("source", row["source_sha256"]),
            ("target", _time(row["target_at"])),
            ("asset_target", row["asset"], _time(row["target_at"])),
        ]
        for key in keys:
            if key in owner:
                parents[root(i)] = root(owner[key])
            owner[key] = i
    # Overlapping source windows within an asset imply shared information even
    # when capture files have different hashes. Sorted sweep keeps work bounded.
    by_asset: dict[str, list[int]] = {}
    for i, row in enumerate(decisions):
        by_asset.setdefault(row["asset"], []).append(i)
    for items in by_asset.values():
        items.sort(key=lambda i: _time(decisions[i]["source_window_start"]))
        previous = None
        latest = None
        for i in items:
            start, end = (
                _time(decisions[i][k]) for k in ("source_window_start", "source_window_end")
            )
            if previous is not None and latest is not None and start < latest:
                parents[root(i)] = root(previous)
            if latest is None or end > latest:
                previous, latest = i, end
    groups: dict[int, list[str]] = {}
    for i, row in enumerate(decisions):
        groups.setdefault(root(i), []).append(row["decision_id"])
    clusters = sorted(sorted(ids) for ids in groups.values())
    segment_rows: dict[str, list[dict]] = {}
    for identity, evaluation in scored.items():
        row = indexed[identity]
        replayed = finalize_decision(
            row,
            official_original=evaluation["official_original_json"].encode("utf-8"),
            official_receipt=evaluation["official_receipt_json"].encode("utf-8"),
            as_of=_time(evaluation["evaluated_at"]),
        )
        if replayed != evaluation:
            raise ValueError("EVALUATION_SCORE_MISMATCH")
        expected = scores(Decimal(row["p_yes"]), evaluation["outcome"])
        if (
            evaluation.get("state") != "EVALUATED"
            or any(evaluation.get(k) != v for k, v in expected.items())
            or evaluation.get("residual") != str(Decimal(row["p_yes"]) - evaluation["outcome"])
        ):
            raise ValueError("EVALUATION_SCORE_MISMATCH")
        segment_rows.setdefault(row["model"] + ":" + row["segment"], []).append(evaluation)
    comparisons = [
        dict(
            method=method,
            status="NOT_VALIDATED",
            reserve=None,
            blocker="PREDECLARED_INDEPENDENCE_AND_HELD_OUT_VALIDATION_REQUIRED",
        )
        for method in METHODS
    ]
    return dict(
        schema="prospective-calibration-dataset-v1",
        decision_n=len(decisions),
        contract_n=len({r["ticker"] for r in decisions}),
        event_n=len({r["event"] for r in decisions}),
        dependency_cluster_n=len(clusters),
        independent_cluster_n=None,
        independence_status="NOT_ESTABLISHED_BY_GROUPING",
        clusters=clusters,
        settled_decision_n=len(scored),
        segment_residuals=segment_rows,
        comparisons=comparisons,
        descriptive_comparison=_compare(indexed, scored, clusters),
        leading_method=None,
        calibrated_uncertainty=None,
        uncertainty_status="UNCERTAINTY_PRELIMINARY" if scored else "UNCERTAINTY_RESEARCH_ONLY",
        paper_eligible=False,
        execution_authority=False,
        fallback="WORST_CASE_SUPPORT_BOUND:BINARY_SUPPORT_V1",
    )


def _quantile(values: list[Decimal], level: Decimal) -> Decimal:
    # Nearest-rank empirical statistic, no interpolation or distribution claim.
    values = sorted(values)
    index = int((level * len(values)).to_integral_value(rounding="ROUND_CEILING")) - 1
    return values[max(0, index)]


def _compare(indexed: dict, scored: dict, clusters: list[list[str]]) -> list[dict]:
    """Five predeclared descriptive candidates; never a calibrated EV reserve.

    Group maximum residual represents an observed cluster, not an independent
    draw. Bootstrap is resampling sensitivity only until independence is reviewed.
    Heldout residual coverage cannot establish coverage of unobserved true q.
    """
    strata: dict[tuple, list[str]] = {}
    for identity in scored:
        row = indexed[identity]
        strata.setdefault((row["model"], row["segment"], row["protocol_sha256"]), []).append(
            identity
        )
    output = []
    for (model, segment, protocol_hash), identities in sorted(strata.items()):
        plan = _object(indexed[identities[0]]["protocol_original_json"].encode())
        criteria = _criteria(plan)
        info = dict(
            model=model,
            segment=segment,
            protocol_sha256=protocol_hash,
            status="DESCRIPTIVE_ONLY",
            paper_eligible=False,
            independence_review="MISSING",
            transportability_review="MISSING",
            interpretation="REALIZED_RESIDUAL_COVERAGE_IS_NOT_PROBABILITY_ERROR_COVERAGE",
        )
        if criteria is None:
            output.append(dict(info, blocker="PREDECLARED_VALIDATION_CRITERIA_MISSING", methods=[]))
            continue
        split = _time(criteria["split_at"])
        train: list[list[str]] = []
        holdout: list[list[str]] = []
        crossing = 0
        selected = set(identities)
        for group in clusters:
            members = [i for i in group if i in selected]
            if not members:
                continue
            # Classify using the full dependency component, including unscored
            # or other-segment records, to prevent split leakage.
            times = [_time(indexed[i]["target_at"]) for i in group]
            if min(times) < split <= max(times):
                crossing += 1
                continue
            (train if max(times) < split else holdout).append(members)
        if not train:
            output.append(
                dict(
                    info,
                    blocker="NO_PRESPLIT_SETTLED_CLUSTERS",
                    methods=[],
                    training_cluster_n=0,
                    holdout_cluster_n=len(holdout),
                    crossing_cluster_n=crossing,
                )
            )
            continue
        residuals = [max(Decimal(scored[i]["residual"]) for i in group) for group in train]
        quantile = Decimal(criteria["quantile"])
        a = max(Decimal(0), _quantile(residuals, quantile))
        rng = random.Random(criteria["bootstrap_seed"])
        boot = [
            sum((rng.choice(residuals) for _ in residuals), Decimal(0)) / len(residuals)
            for _ in range(criteria["bootstrap_draws"])
        ]
        b = max(Decimal(0), _quantile(boot, quantile))
        bins: dict[int, list[tuple[Decimal, Decimal, int]]] = {}
        dispersions = []
        ensemble_complete = True
        for group in train:
            weight = Decimal(1) / len(train) / len(group)
            for identity in group:
                row = indexed[identity]
                p = Decimal(row["p_yes"])
                bins.setdefault(min(int(p * 10), 9), []).append(
                    (weight, p, scored[identity]["outcome"])
                )
                ensemble = row.get("ensemble_probabilities")
                if ensemble is None:
                    ensemble_complete = False
                else:
                    values = [Decimal(v) for v in ensemble]
                    dispersions.append(max(values) - min(values))
        c = sum(
            (abs(sum((w * (p - y) for w, p, y in group), Decimal(0))) for group in bins.values()),
            Decimal(0),
        )
        d = max(dispersions) if ensemble_complete and dispersions else None
        e = max(a, b, c, d) if d is not None else None
        future = [max(Decimal(scored[i]["residual"]) for i in group) for group in holdout]
        methods = []
        for method, statistic in zip(METHODS, (a, b, c, d, e), strict=True):
            coverage = (
                Decimal(sum(r <= statistic for r in future)) / len(future)
                if statistic is not None and future
                else None
            )
            stability = (
                abs(_quantile(future, quantile) - _quantile(residuals, quantile))
                if future
                else None
            )
            methods.append(
                dict(
                    method=method,
                    descriptive_statistic=str(statistic) if statistic is not None else None,
                    statistic_is_reserve=False,
                    reserve=None,
                    heldout_realized_residual_coverage=str(coverage)
                    if coverage is not None
                    else None,
                    residual_quantile_shift=str(stability) if stability is not None else None,
                    descriptive_coverage_pass=(
                        coverage
                        >= Decimal(criteria["nominal_coverage"])
                        - Decimal(criteria["max_coverage_gap"])
                    )
                    if coverage is not None
                    else None,
                    descriptive_stability_pass=(
                        stability <= Decimal(criteria["max_stability_delta"])
                    )
                    if stability is not None
                    else None,
                    paper_eligible=False,
                    blocker="INDEPENDENCE_AND_PROBABILITY_CALIBRATION_NOT_CERTIFIED"
                    if statistic is not None
                    else "GENUINE_ENSEMBLE_EVIDENCE_MISSING",
                )
            )
        output.append(
            dict(
                info,
                criteria=criteria,
                methods=methods,
                training_cluster_n=len(train),
                holdout_cluster_n=len(holdout),
                crossing_cluster_n=crossing,
                independent_cluster_n=None,
                minimum_independent_support_pass=None,
                bootstrap_interpretation="DEPENDENCY_COMPONENT_RESAMPLING_SENSITIVITY_NOT_CONFIDENCE_BOUND",
                leading_method=None,
                selection_status="NO_METHOD_VALIDATED",
            )
        )
    return output
