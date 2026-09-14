"""Replay committed model evaluation on the caller's locked paper ledger."""

import re
from datetime import datetime
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from kalshi_predictor.overnight_paper.dataset_store import load_dataset
from kalshi_predictor.overnight_paper.evaluation_dataset import evaluate_dataset, read_records
from kalshi_predictor.overnight_paper.provenance import Verification, canonical_hash
from kalshi_predictor.overnight_paper.source_health import aware


def verify_model_release(
    session: Session, decision_inputs: dict[str, Any], now: datetime
) -> Verification:
    """Read only; caller must hold its admission transaction throughout this check.

    An optional final observation may describe this exact decision, whose hash
    already commits to the preceding evaluated head. No other suffix is trusted.
    Thresholds are exclusively the dataset's prospectively committed policy.
    """
    scope = "SAME_LEDGER_MODEL_EVALUATION_RELEASE"
    try:
        if not session.in_transaction():
            raise ValueError("MODEL_RELEASE_TRANSACTION_REQUIRED")
        at = aware(decision_inputs["decision_at"])
        current = aware(now)
        if at > current:
            raise ValueError("MODEL_RELEASE_FUTURE_DECISION")
        model_hash = decision_inputs.get("model_artifact_sha256")
        head = decision_inputs.get("model_evaluation_head_sha256")
        if not isinstance(model_hash, str) or not re.fullmatch("[0-9a-f]{64}", model_hash):
            raise ValueError("MODEL_RELEASE_MODEL_HASH_REQUIRED")
        if not isinstance(head, str) or not re.fullmatch("[0-9a-f]{64}", head):
            raise ValueError("MODEL_EVALUATION_HEAD_REQUIRED")
        records = load_dataset(session, dataset="paper-release")
        entries = read_records(records)
        if not records:
            raise ValueError("MODEL_EVALUATION_DATASET_MISSING")
        bound_entries = entries
        if records[-1].sha256 != head:
            tail = entries[-1]["record"]
            if (
                len(records) < 2
                or records[-2].sha256 != head
                or tail.get("kind") != "observation-v1"
                or tail.get("decision_id") != canonical_hash(decision_inputs)
                or tail.get("decision") != decision_inputs
            ):
                raise ValueError("MODEL_EVALUATION_HEAD_MISMATCH")
            bound_entries = entries[:-1]
        if any(aware(entry["recorded_at"]) > at for entry in bound_entries):
            raise ValueError("MODEL_EVALUATION_NOT_AVAILABLE_AT_DECISION")
        policies = [entry for entry in entries if entry["record"].get("kind") == "policy-v1"]
        if len(policies) != 1:
            raise ValueError("MODEL_RELEASE_SINGLE_POLICY_REQUIRED")
        policy_entry = policies[0]
        policy = policy_entry["record"]
        if not policy.get("model_artifact_sha256"):
            raise ValueError("MODEL_RELEASE_POLICY_MODEL_HASH_REQUIRED")
        if policy["model_artifact_sha256"] != model_hash:
            raise ValueError("MODEL_RELEASE_POLICY_MODEL_HASH_MISMATCH")
        if any(
            not decision_inputs.get(key) or decision_inputs[key] != policy[key]
            for key in ("model_name", "model_version")
        ):
            raise ValueError("MODEL_RELEASE_MODEL_IDENTITY_MISMATCH")
        # Legacy trained-model evaluation binds name/version only. Release must
        # additionally bind the exact manifest in every holdout observation.
        start, end = aware(policy["holdout_start"]), aware(policy["holdout_end"])
        for entry in entries:
            row = entry["record"]
            if (
                row.get("kind") == "observation-v1"
                and start <= aware(row["decision"]["decision_at"]) < end
            ):
                if row["originals"]["model"]["sha256"] != model_hash:
                    raise ValueError("MODEL_RELEASE_HOLDOUT_MODEL_HASH_MISMATCH")
        if at < end:
            raise ValueError("HOLDOUT_WINDOW_NOT_COMPLETE")
        # Full chain replay also validates the permitted observation tail.
        evaluated = evaluate_dataset(records, as_of=current)
        if not evaluated.ready:
            return Verification(False, evaluated.blockers, scope=scope)
        return Verification(
            True,
            (),
            tuple(
                dict.fromkeys((model_hash, policy_entry["record_sha256"], head, records[-1].sha256))
            ),
            scope=scope,
            model_calibration_verified=True,
        )
    except SQLAlchemyError:
        return Verification(False, ("MODEL_EVALUATION_DATASET_READ_FAILED",), scope=scope)
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError) as exc:
        return Verification(False, (str(exc),), scope=scope)
