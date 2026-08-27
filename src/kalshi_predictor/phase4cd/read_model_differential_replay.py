from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from kalshi_predictor.phase4cd.read_model_provenance_dashboard import (
    validate_provenance_dashboard,
)

REPLAY_SCHEMA_VERSION = "phase4fw-read-model-differential-replay-v1"
ReplayStatus = Literal["MATCH", "DIVERGENCE"]


class DifferentialReplayError(ValueError):
    """Stable fail-closed differential replay rejection."""


@dataclass(frozen=True)
class DifferentialReplayResult:
    status: ReplayStatus
    compared_snapshots: int
    first_divergence_index: int | None
    primary_digest: str
    replay_digest: str
    evidence_hash: str
    execution_authorized: bool = False


def compare_dashboard_replays(
    primary: Sequence[Any],
    replay: Sequence[Any],
    *,
    max_snapshots: int = 128,
) -> DifferentialReplayResult:
    if max_snapshots <= 0:
        raise DifferentialReplayError("SNAPSHOT_BOUND_INVALID")
    if not primary or not replay:
        raise DifferentialReplayError("REPLAY_EMPTY")
    if len(primary) > max_snapshots or len(replay) > max_snapshots:
        raise DifferentialReplayError("SNAPSHOT_BOUND_EXCEEDED")
    if len(primary) != len(replay):
        raise DifferentialReplayError("REPLAY_LENGTH_MISMATCH")

    primary_rows = [_validated_row(row) for row in primary]
    replay_rows = [_validated_row(row) for row in replay]
    primary_digest = _hash(primary_rows)
    replay_digest = _hash(replay_rows)
    divergence = next(
        (
            index
            for index, pair in enumerate(zip(primary_rows, replay_rows, strict=True))
            if pair[0] != pair[1]
        ),
        None,
    )
    status: ReplayStatus = "MATCH" if divergence is None else "DIVERGENCE"
    evidence = {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "status": status,
        "compared_snapshots": len(primary_rows),
        "first_divergence_index": divergence,
        "primary_digest": primary_digest,
        "replay_digest": replay_digest,
        "execution_authorized": False,
    }
    return DifferentialReplayResult(
        status=status,
        compared_snapshots=len(primary_rows),
        first_divergence_index=divergence,
        primary_digest=primary_digest,
        replay_digest=replay_digest,
        evidence_hash=_hash(evidence),
    )


def validate_replay_result(payload: Any) -> None:
    if not isinstance(payload, DifferentialReplayResult):
        raise DifferentialReplayError("RESULT_TYPE_INVALID")
    if payload.execution_authorized is not False:
        raise DifferentialReplayError("REPLAY_SAFETY_BOUNDARY_INVALID")
    evidence = {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "status": payload.status,
        "compared_snapshots": payload.compared_snapshots,
        "first_divergence_index": payload.first_divergence_index,
        "primary_digest": payload.primary_digest,
        "replay_digest": payload.replay_digest,
        "execution_authorized": False,
    }
    if payload.evidence_hash != _hash(evidence):
        raise DifferentialReplayError("REPLAY_EVIDENCE_HASH_MISMATCH")
    if payload.status == "MATCH" and payload.primary_digest != payload.replay_digest:
        raise DifferentialReplayError("MATCH_DIGEST_INVALID")
    if payload.status == "DIVERGENCE" and payload.first_divergence_index is None:
        raise DifferentialReplayError("DIVERGENCE_INDEX_MISSING")


def _validated_row(row: Any) -> dict[str, Any]:
    try:
        validate_provenance_dashboard(row)
    except (TypeError, ValueError) as exc:
        raise DifferentialReplayError("DASHBOARD_INVALID") from exc
    return json.loads(json.dumps(row, sort_keys=True, separators=(",", ":")))


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
