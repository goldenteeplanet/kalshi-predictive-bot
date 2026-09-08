"""Append-only prospective lineage and precommitted chronological evaluation.

Pure builders return immutable hashed bytes for the single ledger writer to store.
They do not repair research databases, certify providers, or infer model skill
from provenance. Acceptance thresholds must be explicitly frozen before holdout.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from kalshi_predictor.evaluation.calibration import calibration_bins
from kalshi_predictor.evaluation.metrics import brier_score, log_loss
from kalshi_predictor.overnight_paper.provenance import (
    Artifact,
    canonical_hash,
    validate_fixed_heuristic_manifest,
    validate_source_visibility,
)
from kalshi_predictor.overnight_paper.provenance_gate import verify_complete_provenance
from kalshi_predictor.overnight_paper.source_health import aware


def _artifact(row: dict[str, Any]) -> Artifact:
    raw = json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return Artifact(hashlib.sha256(raw).hexdigest(), raw)


def _number(value: Any, *, low: float = 0, high: float = 1) -> float:
    result = float(value)
    if isinstance(value, bool) or not math.isfinite(result) or not low <= result <= high:
        raise ValueError("INVALID_EVALUATION_NUMBER")
    return result


def build_observation(
    *,
    provenance_args: dict[str, Any],
    independent_event_id: str,
    event_window_start: datetime,
    event_window_end: datetime,
    rule_artifact: Artifact,
    market_probability: float,
    executable_price: float,
    estimated_fee: float,
    slippage: float,
    uncertainty: float,
) -> Artifact:
    """Freeze verified decision-time originals, including the market baseline.

    Market probability must equal the independently captured snapshot field and
    all cost fields must equal the frozen decision. Book executability and the
    baseline's quote methodology remain the pricing adapter's responsibility.
    No outcome can be supplied while constructing this observation.
    """
    verified = verify_complete_provenance(**provenance_args)
    if not verified.passed:
        raise ValueError("OBSERVATION_PROVENANCE_INVALID:" + ",".join(verified.blockers))
    decision = provenance_args["decision"]
    context = provenance_args["context"]
    rule, snapshot = rule_artifact.decode(), context.artifacts["snapshot"].decode()
    at = aware(decision["decision_at"])
    start, end = aware(event_window_start), aware(event_window_end)
    if not independent_event_id.strip() or not start <= at < end:
        raise ValueError("INDEPENDENT_EVENT_WINDOW_REQUIRED")
    identity = {key: decision[key] for key in ("ticker", "event_id", "series", "rule_version")}
    if any(rule.get(key) != value for key, value in identity.items()):
        raise ValueError("DATASET_RULE_IDENTITY_MISMATCH")
    if decision.get("rule_artifact_sha256") != rule_artifact.sha256:
        raise ValueError("DATASET_RULE_HASH_MISMATCH")
    baseline = _number(market_probability)
    if baseline != _number(snapshot["market_implied_probability"]):
        raise ValueError("BASELINE_SNAPSHOT_MISMATCH")
    prices = {
        "executable_price": _number(executable_price),
        "estimated_fee": _number(estimated_fee),
        "slippage": _number(slippage),
        "uncertainty": _number(uncertainty),
    }
    for key, value in prices.items():
        if value != _number(decision[key]):
            raise ValueError("DATASET_COST_BINDING_MISMATCH:" + key)
    if decision.get("side") not in {"BUY_YES", "BUY_NO"}:
        raise ValueError("DATASET_SIDE_REQUIRED")
    return _artifact(
        {
            "kind": "observation-v1",
            "identity": identity,
            "decision_id": provenance_args["decision_id"],
            "decision": decision,
            "independent_event_id": independent_event_id,
            "event_window_start": start.isoformat(),
            "event_window_end": end.isoformat(),
            "market_probability": baseline,
            **prices,
            "originals": {
                key: {"sha256": value.sha256, "payload": value.decode()}
                for key, value in context.artifacts.items()
            },
            "sources": [
                {"sha256": value.sha256, "payload": value.decode()}
                for value in context.source_artifacts
            ],
            "features": {
                "sha256": context.features_artifact.sha256,
                "payload": context.features_artifact.decode(),
            },
            "training": [
                {"sha256": value.sha256, "payload": value.decode()}
                for value in context.training_artifacts
            ],
            "rule": {"sha256": rule_artifact.sha256, "payload": rule},
            "model_code_hex": context.model_code.hex(),
        }
    )


def join_outcome(*, observation: Artifact, outcome_artifact: Artifact) -> Artifact:
    """Append a later result by exact identity; never update the observation."""
    row, outcome = observation.decode(), outcome_artifact.decode()
    if row.get("kind") != "observation-v1":
        raise ValueError("OBSERVATION_REQUIRED")
    if any(outcome.get(key) != value for key, value in row["identity"].items()):
        raise ValueError("OUTCOME_IDENTITY_MISMATCH")
    if outcome.get("result") not in {"yes", "no"} or outcome.get("status") != "final":
        raise ValueError("FINAL_BINARY_RESULT_REQUIRED")
    if not outcome.get("source_url") or not outcome.get("provider_payload"):
        raise ValueError("ORIGINAL_OUTCOME_PROVIDER_REQUIRED")
    if canonical_hash(outcome["provider_payload"]) != outcome.get("provider_payload_sha256"):
        raise ValueError("OUTCOME_PROVIDER_HASH_MISMATCH")
    final, available = aware(outcome["final_at"]), aware(outcome["available_at"])
    if not aware(row["event_window_end"]) <= final <= available:
        raise ValueError("OUTCOME_VISIBILITY_INVALID")
    return _artifact(
        {
            "kind": "outcome-v1",
            "observation_sha256": observation.sha256,
            "decision_id": row["decision_id"],
            "outcome_sha256": outcome_artifact.sha256,
            "outcome": outcome,
        }
    )


def build_policy(
    *,
    committed_at: datetime,
    train_end: datetime,
    holdout_start: datetime,
    holdout_end: datetime,
    model_name: str,
    model_version: str,
    minimum_train_events: int,
    minimum_holdout_events: int,
    minimum_brier_improvement: float,
    minimum_log_loss_improvement: float,
    maximum_ece: float,
    minimum_mean_net_ev: float,
    minimum_mean_simulated_pnl: float,
    calibration_bin_count: int,
    model_artifact_sha256: str | None = None,
) -> Artifact:
    """No default thresholds: a reviewed acceptance policy is required."""
    train, committed, start, end = map(aware, (train_end, committed_at, holdout_start, holdout_end))
    if not train <= committed < start < end:
        raise ValueError("POLICY_NOT_PRECOMMITTED")
    if not model_name or not model_version:
        raise ValueError("POLICY_MODEL_REQUIRED")
    if model_artifact_sha256 is not None and not re.fullmatch(
        r"[0-9a-f]{64}", model_artifact_sha256
    ):
        raise ValueError("POLICY_MODEL_ARTIFACT_HASH_INVALID")
    for count in (minimum_train_events, minimum_holdout_events, calibration_bin_count):
        if type(count) is not int or count < 1:
            raise ValueError("POLICY_SAMPLE_REQUIREMENT_REQUIRED")
    return _artifact(
        {
            "kind": "policy-v1",
            "committed_at": committed.isoformat(),
            "train_end": train.isoformat(),
            "holdout_start": start.isoformat(),
            "holdout_end": end.isoformat(),
            "model_name": model_name,
            "model_version": model_version,
            "minimum_train_events": minimum_train_events,
            "minimum_holdout_events": minimum_holdout_events,
            "minimum_brier_improvement": _number(minimum_brier_improvement),
            "minimum_log_loss_improvement": _number(minimum_log_loss_improvement, high=100),
            "maximum_ece": _number(maximum_ece),
            "minimum_mean_net_ev": _number(minimum_mean_net_ev),
            "minimum_mean_simulated_pnl": _number(minimum_mean_simulated_pnl),
            "calibration_bin_count": calibration_bin_count,
            **(
                {"model_artifact_sha256": model_artifact_sha256}
                if model_artifact_sha256 is not None
                else {}
            ),
        }
    )


def append_record(
    records: tuple[Artifact, ...],
    record: Artifact,
    *,
    recorded_at: datetime,
) -> tuple[Artifact, ...]:
    """Single-writer append operation returning a new hash chain of bytes.

    The writer must atomically persist this chain; this function does not provide
    external timestamp attestation or multi-process ownership.
    """
    read_records(records)
    at = aware(recorded_at)
    row = record.decode()
    if records and at < aware(records[-1].decode()["recorded_at"]):
        raise ValueError("APPEND_CLOCK_REGRESSION")
    if any(item.decode()["record_sha256"] == record.sha256 for item in records):
        return records
    if row.get("kind") == "policy-v1" and aware(row["committed_at"]) != at:
        raise ValueError("POLICY_APPEND_TIME_MISMATCH")
    if row.get("kind") == "observation-v1" and not (
        0 <= (at - aware(row["decision"]["decision_at"])).total_seconds() <= 60
    ):
        raise ValueError("PROSPECTIVE_APPEND_DELAY")
    if row.get("kind") == "outcome-v1" and aware(row["outcome"]["available_at"]) > at:
        raise ValueError("OUTCOME_NOT_AVAILABLE_AT_APPEND")
    entry = _artifact(
        {
            "sequence": len(records),
            "previous_sha256": records[-1].sha256 if records else None,
            "record_sha256": record.sha256,
            "record": row,
            "recorded_at": at.isoformat(),
        }
    )
    return (*records, entry)


def read_records(records: tuple[Artifact, ...]) -> tuple[dict[str, Any], ...]:
    result = []
    previous = None
    previous_at = None
    for index, artifact in enumerate(records):
        row = artifact.decode()
        at = aware(row["recorded_at"])
        if (
            row["sequence"] != index
            or row["previous_sha256"] != previous
            or canonical_hash(row["record"]) != row["record_sha256"]
            or (previous_at is not None and at < previous_at)
        ):
            raise ValueError("DATASET_CHAIN_INVALID")
        result.append(row)
        previous, previous_at = artifact.sha256, at
    return tuple(result)


@dataclass(frozen=True)
class DatasetEvaluation:
    ready: bool
    blockers: tuple[str, ...]
    metrics: dict[str, float | int]
    retained_decision_ids: tuple[str, ...] = ()
    purged_decision_ids: tuple[str, ...] = ()


def evaluate_dataset(
    records: tuple[Artifact, ...],
    *,
    as_of: datetime | None = None,
) -> DatasetEvaluation:
    """Chronological independent-event holdout with separate forecast/cost scores.

    All overlapping event windows are conservatively purged, even for different
    tickers. One observation per independent event is selected chronologically.
    Returned P&L is hypothetical one-contract P&L, never realized ledger P&L.
    The evaluation train partition is development/evaluation evidence, distinct
    from a trained model's fitting artifacts. Fixed heuristics have no fitting
    artifacts but still require both independent evaluation partitions.
    """
    try:
        entries = read_records(records)
        policies = [item for item in entries if item["record"].get("kind") == "policy-v1"]
        if len(policies) != 1:
            raise ValueError("ONE_PRECOMMITTED_POLICY_REQUIRED")
        policy_entry = policies[0]
        policy = policy_entry["record"]
        if as_of is None:
            raise ValueError("EVALUATION_CLOCK_REQUIRED")
        evaluated_at = aware(as_of)
        if any(aware(entry["recorded_at"]) > evaluated_at for entry in entries):
            raise ValueError("DATASET_NOT_VISIBLE_AT_EVALUATION")
        # Revalidate policy semantics rather than trusting a correctly hashed JSON object.
        policy_args = {key: value for key, value in policy.items() if key != "kind"}
        if build_policy(**policy_args).sha256 != policy_entry["record_sha256"]:
            raise ValueError("POLICY_NONCANONICAL")
        if aware(policy_entry["recorded_at"]) != aware(policy["committed_at"]):
            raise ValueError("POLICY_APPEND_TIME_MISMATCH")
        observations = {
            item["record_sha256"]: item
            for item in entries
            if item["record"].get("kind") == "observation-v1"
        }
        outcomes = {}
        for item in entries:
            row = item["record"]
            if row.get("kind") != "outcome-v1":
                continue
            key = row["observation_sha256"]
            if key not in observations or key in outcomes:
                raise ValueError("DUPLICATE_OR_UNMATCHED_OUTCOME")
            original = _artifact(observations[key]["record"])
            if (
                join_outcome(
                    observation=original, outcome_artifact=_artifact(row["outcome"])
                ).sha256
                != item["record_sha256"]
            ):
                raise ValueError("OUTCOME_JOIN_MISMATCH")
            if observations[key]["sequence"] >= item["sequence"] or aware(
                row["outcome"]["available_at"]
            ) > aware(item["recorded_at"]):
                raise ValueError("OUTCOME_JOIN_ORDER_INVALID")
            outcomes[key] = row["outcome"]
        train, holdout, purged = [], [], []
        train_end = aware(policy["train_end"])
        start, end = aware(policy["holdout_start"]), aware(policy["holdout_end"])
        for sha, entry in sorted(
            observations.items(),
            key=lambda pair: (pair[1]["record"]["decision"]["decision_at"], pair[0]),
        ):
            row = entry["record"]
            _validate_stored_observation(row)
            decision = row["decision"]
            at = aware(decision["decision_at"])
            if not 0 <= (aware(entry["recorded_at"]) - at).total_seconds() <= 60:
                raise ValueError("PROSPECTIVE_APPEND_DELAY")
            if sha not in outcomes:
                continue
            outcome = outcomes[sha]
            if at <= train_end and aware(outcome["available_at"]) <= train_end:
                train.append((row, outcome))
            elif start <= at < end:
                model = row["originals"]["model"]["payload"]
                fixed = model.get("model_kind", "trained") == "fixed_heuristic"
                if fixed and (
                    aware(model["frozen_at"]) > aware(policy["committed_at"])
                    or aware(model["available_at"]) > aware(policy["committed_at"])
                    or policy.get("model_artifact_sha256") != row["originals"]["model"]["sha256"]
                ):
                    raise ValueError("FIXED_HEURISTIC_NOT_FROZEN_IN_POLICY")
                if (
                    entry["sequence"] <= policy_entry["sequence"]
                    or (not fixed and aware(decision["training_cutoff"]) > train_end)
                    or decision["model_name"] != policy["model_name"]
                    or decision["model_version"] != policy["model_version"]
                ):
                    raise ValueError("HOLDOUT_MODEL_OR_PRECOMMIT_MISMATCH")
                holdout.append((row, outcome))
        selected = []
        occupied = list(train)
        for row, outcome in holdout:
            original_training = [
                training for item in row["training"] for training in item["payload"]["records"]
            ]
            training_overlap = any(
                training["independent_event_id"] == row["independent_event_id"]
                or (
                    aware(row["event_window_start"]) < aware(training["event_window_end"])
                    and aware(training["event_window_start"]) < aware(row["event_window_end"])
                )
                for training in original_training
            )
            overlaps = any(
                other["independent_event_id"] == row["independent_event_id"]
                or (
                    aware(row["event_window_start"]) < aware(other["event_window_end"])
                    and aware(other["event_window_start"]) < aware(row["event_window_end"])
                )
                for other, _ in occupied
            )
            if overlaps or training_overlap:
                purged.append(row["decision_id"])
                continue
            selected.append((row, outcome))
            occupied.append((row, outcome))
        train_n = len({row["independent_event_id"] for row, _ in train})
        metrics: dict[str, float | int] = {
            "independent_train_events": train_n,
            "independent_holdout_events": len(selected),
            "purged_holdout_observations": len(purged),
            "unsettled_observations": len(observations) - len(outcomes),
        }
        blockers = []
        if evaluated_at < end:
            blockers.append("HOLDOUT_WINDOW_NOT_COMPLETE")
        if any(
            start <= aware(item["record"]["decision"]["decision_at"]) < end and sha not in outcomes
            for sha, item in observations.items()
        ):
            blockers.append("HOLDOUT_OUTCOMES_INCOMPLETE")
        if train_n < policy["minimum_train_events"]:
            blockers.append("INSUFFICIENT_INDEPENDENT_TRAINING_EVENTS")
        if len(selected) < policy["minimum_holdout_events"]:
            blockers.append("INSUFFICIENT_INDEPENDENT_HOLDOUT_EVENTS")
        if selected:
            y = [int(outcome["result"] == "yes") for _, outcome in selected]
            probability = [_number(row["decision"]["forecast_probability"]) for row, _ in selected]
            baseline = [_number(row["market_probability"]) for row, _ in selected]
            bins = calibration_bins(y, probability, n_bins=policy["calibration_bin_count"])
            ece = sum(
                item.count * abs(item.avg_predicted_probability - item.observed_frequency)
                for item in bins
            ) / len(selected)
            evs, pnls = [], []
            for (row, _), actual, prob in zip(selected, y, probability, strict=True):
                yes = row["decision"]["side"] == "BUY_YES"
                costs = row["executable_price"] + row["estimated_fee"] + row["slippage"]
                evs.append((prob if yes else 1 - prob) - costs - row["uncertainty"])
                pnls.append((actual if yes else 1 - actual) - costs)
            metrics.update(
                {
                    "brier": brier_score(y, probability),
                    "log_loss": log_loss(y, probability),
                    "ece": ece,
                    "baseline_brier": brier_score(y, baseline),
                    "baseline_log_loss": log_loss(y, baseline),
                    "mean_after_cost_predicted_ev": sum(evs) / len(evs),
                    "mean_hypothetical_one_contract_pnl": sum(pnls) / len(pnls),
                    "hypothetical_one_contract_pnl_sum": sum(pnls),
                }
            )
            comparisons = (
                (
                    metrics["baseline_brier"] - metrics["brier"]
                    >= policy["minimum_brier_improvement"],
                    "BRIER_ACCEPTANCE_FAILED",
                ),
                (
                    metrics["baseline_log_loss"] - metrics["log_loss"]
                    >= policy["minimum_log_loss_improvement"],
                    "LOG_LOSS_ACCEPTANCE_FAILED",
                ),
                (ece <= policy["maximum_ece"], "CALIBRATION_ACCEPTANCE_FAILED"),
                (
                    metrics["mean_after_cost_predicted_ev"] > policy["minimum_mean_net_ev"],
                    "AFTER_COST_EV_ACCEPTANCE_FAILED",
                ),
                (
                    metrics["mean_hypothetical_one_contract_pnl"]
                    > policy["minimum_mean_simulated_pnl"],
                    "SIMULATED_PNL_ACCEPTANCE_FAILED",
                ),
            )
            blockers.extend(reason for passed, reason in comparisons if not passed)
        return DatasetEvaluation(
            not blockers,
            tuple(blockers),
            metrics,
            tuple(row["decision_id"] for row, _ in selected),
            tuple(purged),
        )
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError) as exc:
        return DatasetEvaluation(False, (str(exc),), {})


def _validate_stored_observation(row: dict[str, Any]) -> None:
    """Replay stored hashes and clocks independently of builder invocation."""
    decision = row["decision"]
    at = aware(decision["decision_at"])
    if canonical_hash(decision) != row["decision_id"]:
        raise ValueError("STORED_DECISION_HASH_MISMATCH")
    objects = list(row["originals"].values()) + row["sources"] + row["training"]
    objects += [row["features"], row["rule"]]
    if any(canonical_hash(item["payload"]) != item["sha256"] for item in objects):
        raise ValueError("STORED_ORIGINAL_HASH_MISMATCH")
    if any(
        decision[key + "_artifact_sha256"] != item["sha256"]
        for key, item in row["originals"].items()
    ):
        raise ValueError("STORED_ARTIFACT_BINDING_MISMATCH")
    if any(
        row["identity"].get(key) != decision[key]
        or row["rule"]["payload"].get(key) != decision[key]
        for key in ("ticker", "event_id", "series", "rule_version")
    ):
        raise ValueError("STORED_IDENTITY_MISMATCH")
    if decision["rule_artifact_sha256"] != row["rule"]["sha256"]:
        raise ValueError("STORED_RULE_BINDING_MISMATCH")
    if not aware(row["event_window_start"]) <= at < aware(row["event_window_end"]):
        raise ValueError("STORED_EVENT_WINDOW_INVALID")
    model = row["originals"]["model"]["payload"]
    model_kind = model.get("model_kind", "trained")
    if model_kind not in {"trained", "fixed_heuristic"}:
        raise ValueError("UNSUPPORTED_MODEL_KIND")
    fixed = model_kind == "fixed_heuristic"
    if fixed:
        validate_fixed_heuristic_manifest(
            model=model,
            decision=decision,
            forecast=row["originals"]["forecast"]["payload"],
            config=row["originals"]["config"]["payload"],
            at=at,
            has_training_artifacts=bool(row["training"]),
        )
    cutoff = aware(model["frozen_at"] if fixed else model["training_cutoff"])
    if model["training_dataset_hashes"] != [item["sha256"] for item in row["training"]]:
        raise ValueError("STORED_TRAINING_BINDING_MISMATCH")
    source_hashes = [item["sha256"] for item in row["sources"]]
    if source_hashes != decision["source_hashes"]:
        raise ValueError("STORED_SOURCE_BINDING_MISMATCH")
    if (
        cutoff > at
        or model["version"] != decision["model_version"]
        or model["name"] != decision["model_name"]
        or (not fixed and cutoff != aware(decision["training_cutoff"]))
        or hashlib.sha256(bytes.fromhex(row["model_code_hex"])).hexdigest() != model["code_sha256"]
    ):
        raise ValueError("STORED_MODEL_LINEAGE_INVALID")
    for item in row["sources"]:
        source = item["payload"]
        try:
            validate_source_visibility(source, decision_at=at, now=at)
        except ValueError:
            raise ValueError("STORED_FUTURE_SOURCE") from None
    for item in row["training"]:
        for training in item["payload"]["records"]:
            if (
                not training.get("independent_event_id")
                or not aware(training["event_window_start"])
                < aware(training["event_window_end"])
                <= cutoff
            ):
                raise ValueError("TRAINING_INDEPENDENT_EVENT_LINEAGE_REQUIRED")
            if any(aware(training[key]) > cutoff for key in ("available_at", "label_available_at")):
                raise ValueError("STORED_FUTURE_TRAINING_LABEL")
    forecast, snapshot = (row["originals"][key]["payload"] for key in ("forecast", "snapshot"))
    if (
        forecast["model_artifact_sha256"] != row["originals"]["model"]["sha256"]
        or forecast["model_version"] != model["version"]
        or forecast["source_hashes"] != source_hashes
        or forecast["id"] != decision["forecast_id"]
        or snapshot["id"] != decision["snapshot_id"]
        or canonical_hash(snapshot["book"]) != decision["snapshot_book_hash"]
        or forecast["features_artifact_sha256"] != row["features"]["sha256"]
        or decision["features_artifact_sha256"] != row["features"]["sha256"]
    ):
        raise ValueError("STORED_FORECAST_BOOK_FEATURE_BINDING_MISMATCH")
    for feature in row["features"]["payload"]["records"]:
        if feature["source_sha256"] not in source_hashes or not aware(
            feature["observed_at"]
        ) <= aware(feature["available_at"]) <= aware(forecast["generated_at"]):
            raise ValueError("STORED_FEATURE_VISIBILITY_INVALID")
    if any(
        aware(value) > at
        for value in (
            forecast["generated_at"],
            forecast["available_at"],
            snapshot["captured_at"],
            snapshot["available_at"],
            row["features"]["payload"]["available_at"],
        )
    ):
        raise ValueError("STORED_FUTURE_FORECAST_BOOK_OR_FEATURE")
    if _number(row["market_probability"]) != _number(
        snapshot["market_implied_probability"]
    ) or _number(decision["forecast_probability"]) != _number(forecast["probability"]):
        raise ValueError("STORED_PROBABILITY_BINDING_MISMATCH")
    for key in ("executable_price", "estimated_fee", "slippage", "uncertainty"):
        if _number(row[key]) != _number(decision[key]):
            raise ValueError("STORED_COST_BINDING_MISMATCH")
