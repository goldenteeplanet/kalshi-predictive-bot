"""Pure, bounded, original-preserving journal export; no calibration promotion.

Every supplied journal row is validated. All final evaluations are included;
pending shadows are explicitly inventoried. No outcome/score selection exists.
Local journal clocks and supplied receipts are not external timestamp attestation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from kalshi_predictor.crypto.prospective_calibration import calibration_dataset
from kalshi_predictor.crypto.settlement_target import _json
from kalshi_predictor.overnight_paper.current_research_store import _read_envelope, _shadow


@dataclass(frozen=True)
class JournalOriginal:
    journal_id: str
    captured_at: str
    payload: bytes


@dataclass(frozen=True)
class ProspectiveEvidenceExport:
    """Immutable bytes suitable for exclusive artifact publication by a caller."""

    manifest: bytes
    dataset: bytes
    originals: tuple[tuple[str, bytes], ...]

    @property
    def manifest_sha256(self) -> str:
        return _sha(self.manifest)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _encode(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("AWARE_EXPORT_CLOCK_REQUIRED")
    return parsed


def export_prospective_evidence(
    records: tuple[JournalOriginal, ...],
    *,
    as_of: datetime,
    attached_originals: tuple[tuple[str, bytes], ...] = (),
) -> ProspectiveEvidenceExport:
    """Replay raw journal, pretarget decisions, protocols and final receipts.

    The caller supplies a complete bounded journal snapshot. The manifest binds
    precisely that snapshot, not a claim that a caller omitted no database rows.
    Optional source/book/market originals are hash checked; absent originals are
    reported as provenance gaps, never replaced by reconstructed/fabricated data.
    """
    if (
        type(records) is not tuple
        or len(records) > 10000
        or type(attached_originals) is not tuple
        or len(attached_originals) > 10000
        or as_of.utcoffset() is None
    ):
        raise ValueError("BOUNDED_EXPORT_INPUT_REQUIRED")
    blobs: dict[str, bytes] = {}
    blob_bytes = 0

    def preserve(raw: bytes) -> str:
        nonlocal blob_bytes
        if type(raw) is not bytes or not 0 < len(raw) <= 8_000_000:
            raise ValueError("BOUNDED_EXPORT_ORIGINAL_REQUIRED")
        identity = _sha(raw)
        if identity not in blobs:
            blob_bytes += len(raw)
            if blob_bytes > 64_000_000:
                raise ValueError("EXPORT_TOTAL_ORIGINAL_BYTES_EXCEEDED")
            blobs[identity] = raw
        return identity

    for expected, raw in attached_originals:
        if preserve(raw) != expected:
            raise ValueError("ATTACHED_ORIGINAL_HASH_MISMATCH")
    envelopes = []
    seen_ids: set[str] = set()
    journal_bytes = 0
    for row in records:
        if type(row) is not JournalOriginal or type(row.payload) is not bytes:
            raise ValueError("EXACT_RAW_JOURNAL_RECORD_REQUIRED")
        journal_bytes += len(row.payload)
        if journal_bytes > 32_000_000:
            raise ValueError("EXPORT_JOURNAL_BYTES_EXCEEDED")
        if row.journal_id in seen_ids:
            raise ValueError("DUPLICATE_JOURNAL_ROW")
        seen_ids.add(row.journal_id)
        if _at(row.captured_at) > as_of:
            raise ValueError("JOURNAL_RECORD_AFTER_EXPORT")
        preserve(row.payload)
        _json(row.payload)  # Reject duplicate keys/nonfinite JSON before existing replay.
        envelope = _read_envelope(row.journal_id, row.captured_at, row.payload.decode("utf-8"))
        envelopes.append((row, envelope))
    envelopes.sort(key=lambda pair: (_at(pair[0].captured_at), pair[0].journal_id))
    shadows: dict[str, tuple[JournalOriginal, dict[str, Any]]] = {}
    evaluations: dict[str, tuple[JournalOriginal, dict[str, Any]]] = {}
    observations: list[dict[str, Any]] = []
    for row, envelope in envelopes:
        kind, record = envelope["record_kind"], envelope["record"]
        if kind == "PROSPECTIVE_SHADOW":
            decision = _shadow(record)
            identity = decision["decision_id"]
            if identity in shadows or envelope["identity"] != identity:
                raise ValueError("DUPLICATE_OR_MISBOUND_SHADOW")
            shadows[identity] = (row, decision)
        elif kind == "EVALUATION":
            identity = record["decision"]["decision_id"]
            if identity in evaluations or envelope["identity"] != identity:
                raise ValueError("DUPLICATE_OR_MISBOUND_EVALUATION")
            evaluations[identity] = (row, record)
        elif kind == "SHADOW_OBSERVATION":
            observations.append(dict(journal=row, envelope=envelope))
    if len(shadows) > 4096:
        raise ValueError("BOUNDED_EXPORT_SHADOWS_REQUIRED")
    for identity, (row, payload) in evaluations.items():
        previous = shadows.get(identity)
        if (
            previous is None
            or previous[1] != payload["decision"]
            or not _at(previous[0].captured_at) < _at(row.captured_at)
        ):
            raise ValueError("PRIOR_EXACT_SHADOW_REQUIRED")
    observed_states: set[tuple[str, str]] = set()
    for item in observations:
        row, envelope = item["journal"], item["envelope"]
        record = envelope["record"]
        identity, state = record["decision_id"], record["state"]
        previous = shadows.get(identity)
        market = _json(record["official_original_json"].encode())["market"]
        if (
            previous is None
            or not _at(previous[0].captured_at) < _at(row.captured_at)
            or market.get("ticker") != previous[1]["ticker"]
            or market.get("event_ticker") != previous[1]["event"]
            or _at(record["observed_at"]) < _at(previous[1]["target_at"])
            or envelope["identity"] != identity + ":" + state
            or (identity, state) in observed_states
        ):
            raise ValueError("OBSERVATION_SHADOW_BINDING_INVALID")
        observed_states.add((identity, state))
        preserve(record["official_original_json"].encode())
        preserve(record["official_receipt_json"].encode())
    # Existing implementation replays decision originals and official FINAL
    # originals/receipts, recomputes scores, rejects premature/provisional results.
    summary = calibration_dataset(
        [value[1] for value in shadows.values()],
        [value[1]["evaluation"] for value in evaluations.values()],
    )
    cluster_for = {
        identity: _sha(_encode(members)) for members in summary["clusters"] for identity in members
    }
    included, exclusions, missing_inputs = [], [], set()
    inventory = []
    for identity, (journal, decision) in sorted(shadows.items()):
        refs = {
            "decision_original": preserve(decision["decision_original_json"].encode()),
            "protocol_original": preserve(decision["protocol_original_json"].encode()),
            "shadow_journal_original": _sha(journal.payload),
        }
        source_refs = {
            key: decision[key] for key in ("source_sha256", "book_sha256", "market_sha256")
        }
        gaps = sorted({value for value in source_refs.values() if value not in blobs})
        missing_inputs.update(gaps)
        item = dict(
            decision_id=identity,
            ticker=decision["ticker"],
            event=decision["event"],
            asset=decision["asset"],
            model=decision["model"],
            segment=decision["segment"],
            rule_version=decision["rule_version"],
            decision_time=decision["decision_time"],
            recorded_at=journal.captured_at,
            target_at=decision["target_at"],
            dependency_cluster_id=cluster_for[identity],
            source_references=source_refs,
            missing_original_hashes=gaps,
            original_references=refs,
        )
        inventory.append(item)
        if identity not in evaluations:
            exclusions.append(
                dict(decision_id=identity, reason="OFFICIAL_FINAL_EVALUATION_NOT_AVAILABLE")
            )
            continue
        evaluation_journal, payload = evaluations[identity]
        evaluation = payload["evaluation"]
        final_refs = {
            "official_original": preserve(evaluation["official_original_json"].encode()),
            "official_receipt": preserve(evaluation["official_receipt_json"].encode()),
            "evaluation_journal_original": _sha(evaluation_journal.payload),
        }
        included.append(
            dict(
                item,
                evaluation_recorded_at=evaluation_journal.captured_at,
                original_references=refs | final_refs,
                probability=decision["p_yes"],
                outcome=evaluation["outcome"],
                residual=evaluation["residual"],
                brier=evaluation["brier"],
                log_loss=evaluation["log_loss"],
                settlement_at=evaluation["settlement_at"],
                evaluated_at=evaluation["evaluated_at"],
            )
        )
    dataset = _encode(
        dict(
            schema="prospective-original-evidence-dataset-v1",
            rows=included,
            shadow_inventory=inventory,
            exclusions=exclusions,
            independent_cluster_n=None,
            candidate_applicability=None,
            paper_eligible=False,
            execution_authority=False,
        )
    )
    preserve(dataset)
    manifest = _encode(
        dict(
            schema="prospective-original-evidence-export-v1",
            as_of=as_of.isoformat(),
            dataset_sha256=_sha(dataset),
            selection="ALL_VALID_FINAL_EVALUATIONS_NO_SCORE_FILTER",
            supplied_snapshot_completeness="CALLER_SNAPSHOT_BOUND_NOT_DATABASE_COMPLETENESS_ATTESTATION",
            journal=[
                dict(journal_id=r.journal_id, captured_at=r.captured_at, sha256=_sha(r.payload))
                for r, _ in envelopes
            ],
            decision_n=len(shadows),
            evaluated_n=len(included),
            excluded_n=len(exclusions),
            contract_n=summary["contract_n"],
            event_n=summary["event_n"],
            dependency_cluster_n=summary["dependency_cluster_n"],
            clusters=summary["clusters"],
            independent_cluster_n=None,
            candidate_applicability=None,
            missing_input_original_hashes=sorted(missing_inputs),
            original_input_coverage="PARTIAL"
            if missing_inputs
            else "REFERENCED_BYTES_PRESENT_NOT_SOURCE_AUTHENTICATION",
            calibration_policy_issued=False,
            external_timestamp_attestation=False,
            paper_eligible=False,
            execution_authority=False,
            originals=[dict(sha256=key, size=len(value)) for key, value in sorted(blobs.items())],
        )
    )
    return ProspectiveEvidenceExport(manifest, dataset, tuple(sorted(blobs.items())))
