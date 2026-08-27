from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from kalshi_predictor.ui.dashboard_progressive_disclosure import (
    DashboardProgressiveDisclosure,
    validate_dashboard_progressive_disclosure,
)

CONTRACT_SCHEMA_VERSION = "phase4gp-dashboard-loading-state-contract-v1"
ContractStatus = Literal["SETTLED", "LOADING", "DEGRADED", "STALE"]
ObservedLoadState = Literal["NOT_STARTED", "LOADING", "LOADED", "FAILED"]
PanelLoadState = Literal[
    "DEFERRED", "UNAVAILABLE", "PENDING", "LOADING", "READY", "ERROR", "TIMED_OUT"
]


class DashboardLoadingStateContractError(ValueError):
    """Stable fail-closed dashboard loading-state error."""


@dataclass(frozen=True)
class DashboardPanelLoadObservation:
    panel_id: str
    state: ObservedLoadState
    elapsed_ms: int
    result_count: int | None
    source_identity_hash: str
    source_watermark: str
    evidence_age_seconds: int
    observation_hash: str


@dataclass(frozen=True)
class DashboardPanelLoadState:
    panel_id: str
    state: PanelLoadState
    result_count: int | None


@dataclass(frozen=True)
class DashboardLoadingStateContract:
    status: ContractStatus
    reasons: tuple[str, ...]
    disclosure_hash: str
    source_identity_hash: str
    source_watermark: str
    panel_count: int
    ready_panel_count: int
    max_load_ms: int
    observed_max_age_seconds: int
    max_evidence_age_seconds: int
    panels: tuple[DashboardPanelLoadState, ...]
    observations_hash: str
    contract_hash: str
    read_only: bool = True
    execution_authorized: bool = False


def make_dashboard_panel_load_observation(
    *,
    panel_id: str,
    state: ObservedLoadState,
    elapsed_ms: int,
    result_count: int | None,
    source_identity_hash: str,
    source_watermark: str,
    evidence_age_seconds: int,
) -> DashboardPanelLoadObservation:
    unsigned = {
        "panel_id": panel_id,
        "state": state,
        "elapsed_ms": elapsed_ms,
        "result_count": result_count,
        "source_identity_hash": source_identity_hash,
        "source_watermark": source_watermark,
        "evidence_age_seconds": evidence_age_seconds,
    }
    _validate_observation_fields(unsigned)
    return DashboardPanelLoadObservation(**unsigned, observation_hash=_hash(unsigned))


def build_dashboard_loading_state_contract(
    *,
    disclosure: Any,
    observations: Sequence[Any],
    max_observations: int = 32,
    max_load_ms: int = 2_000,
    max_evidence_age_seconds: int = 300,
) -> DashboardLoadingStateContract:
    for value in (max_observations, max_load_ms):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise DashboardLoadingStateContractError("LOADING_BOUND_INVALID")
    if (
        isinstance(max_evidence_age_seconds, bool)
        or not isinstance(max_evidence_age_seconds, int)
        or max_evidence_age_seconds < 0
    ):
        raise DashboardLoadingStateContractError("LOADING_BOUND_INVALID")
    try:
        validate_dashboard_progressive_disclosure(disclosure)
    except (TypeError, ValueError) as exc:
        raise DashboardLoadingStateContractError("DISCLOSURE_INPUT_INVALID") from exc
    if not isinstance(disclosure, DashboardProgressiveDisclosure):
        raise DashboardLoadingStateContractError("DISCLOSURE_INPUT_INVALID")
    if not observations:
        raise DashboardLoadingStateContractError("OBSERVATIONS_EMPTY")
    if len(observations) > max_observations:
        raise DashboardLoadingStateContractError("OBSERVATION_BOUND_EXCEEDED")

    validated = [_validated_observation(item) for item in observations]
    ids = [item.panel_id for item in validated]
    if len(set(ids)) != len(ids):
        raise DashboardLoadingStateContractError("OBSERVATION_DUPLICATE")
    disclosure_ids = [item.panel_id for item in disclosure.panels]
    if set(ids) != set(disclosure_ids):
        raise DashboardLoadingStateContractError("OBSERVATION_SET_INVALID")
    identities = {item.source_identity_hash for item in validated}
    watermarks = {item.source_watermark for item in validated}
    if identities != {disclosure.source_identity_hash} or watermarks != {
        disclosure.source_watermark
    }:
        raise DashboardLoadingStateContractError("OBSERVATION_LINEAGE_MISMATCH")

    by_id = {item.panel_id: item for item in validated}
    ordered = [by_id[panel_id] for panel_id in disclosure_ids]
    observed_age = max(
        disclosure.observed_max_age_seconds,
        *(item.evidence_age_seconds for item in ordered),
    )
    stale = disclosure.status == "STALE" or observed_age > max_evidence_age_seconds
    panel_states: list[DashboardPanelLoadState] = []
    for disclosed, observed in zip(disclosure.panels, ordered, strict=True):
        state = _resolve_state(
            disclosure_mode=disclosed.mode,
            observed=observed,
            stale=stale,
            max_load_ms=max_load_ms,
        )
        panel_states.append(
            DashboardPanelLoadState(
                panel_id=observed.panel_id,
                state=state,
                result_count=observed.result_count if state == "READY" else None,
            )
        )

    reasons: list[str] = []
    states = {item.state for item in panel_states}
    if stale:
        status: ContractStatus = "STALE"
        reasons.append("LOADING_EVIDENCE_STALE")
    elif states & {"ERROR", "TIMED_OUT"}:
        status = "DEGRADED"
        reasons.extend(
            sorted(
                f"PANEL_{item.state}:{item.panel_id}"
                for item in panel_states
                if item.state in {"ERROR", "TIMED_OUT"}
            )
        )
    elif states & {"PENDING", "LOADING"}:
        status = "LOADING"
    else:
        status = "SETTLED"

    observations_hash = _hash([asdict(item) for item in ordered])
    unsigned = {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "status": status,
        "reasons": sorted(reasons),
        "disclosure_hash": disclosure.disclosure_hash,
        "source_identity_hash": disclosure.source_identity_hash,
        "source_watermark": disclosure.source_watermark,
        "panel_count": len(panel_states),
        "ready_panel_count": sum(item.state == "READY" for item in panel_states),
        "max_load_ms": max_load_ms,
        "observed_max_age_seconds": observed_age,
        "max_evidence_age_seconds": max_evidence_age_seconds,
        "panels": [asdict(item) for item in panel_states],
        "observations_hash": observations_hash,
        "read_only": True,
        "execution_authorized": False,
    }
    return DashboardLoadingStateContract(
        status=status,
        reasons=tuple(unsigned["reasons"]),
        disclosure_hash=disclosure.disclosure_hash,
        source_identity_hash=disclosure.source_identity_hash,
        source_watermark=disclosure.source_watermark,
        panel_count=len(panel_states),
        ready_panel_count=unsigned["ready_panel_count"],
        max_load_ms=max_load_ms,
        observed_max_age_seconds=observed_age,
        max_evidence_age_seconds=max_evidence_age_seconds,
        panels=tuple(panel_states),
        observations_hash=observations_hash,
        contract_hash=_hash(unsigned),
    )


def validate_dashboard_loading_state_contract(contract: Any) -> None:
    if not isinstance(contract, DashboardLoadingStateContract):
        raise DashboardLoadingStateContractError("CONTRACT_RESULT_TYPE_INVALID")
    if contract.read_only is not True or contract.execution_authorized is not False:
        raise DashboardLoadingStateContractError("CONTRACT_SAFETY_BOUNDARY_INVALID")
    if contract.status in {"SETTLED", "LOADING"} and contract.reasons:
        raise DashboardLoadingStateContractError("CONTRACT_STATE_INVALID")
    if contract.status in {"DEGRADED", "STALE"} and not contract.reasons:
        raise DashboardLoadingStateContractError("CONTRACT_REASONS_MISSING")
    unsigned = asdict(contract)
    unsigned.pop("contract_hash")
    unsigned["schema_version"] = CONTRACT_SCHEMA_VERSION
    unsigned["reasons"] = sorted(unsigned["reasons"])
    unsigned["panels"] = [asdict(item) for item in contract.panels]
    if contract.contract_hash != _hash(unsigned):
        raise DashboardLoadingStateContractError("CONTRACT_HASH_MISMATCH")


def _resolve_state(
    *,
    disclosure_mode: str,
    observed: DashboardPanelLoadObservation,
    stale: bool,
    max_load_ms: int,
) -> PanelLoadState:
    if stale or disclosure_mode == "UNAVAILABLE":
        return "UNAVAILABLE"
    if disclosure_mode == "DEFERRED":
        if observed.state != "NOT_STARTED":
            raise DashboardLoadingStateContractError("DEFERRED_PANEL_ACTIVITY_INVALID")
        return "DEFERRED"
    if observed.state == "NOT_STARTED":
        return "PENDING"
    if observed.state == "FAILED":
        return "ERROR"
    if observed.state == "LOADING":
        return "TIMED_OUT" if observed.elapsed_ms > max_load_ms else "LOADING"
    return "READY"


def _validated_observation(value: Any) -> DashboardPanelLoadObservation:
    if not isinstance(value, DashboardPanelLoadObservation):
        raise DashboardLoadingStateContractError("OBSERVATION_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("observation_hash")
    _validate_observation_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise DashboardLoadingStateContractError("OBSERVATION_HASH_MISMATCH")
    return value


def _validate_observation_fields(payload: dict[str, Any]) -> None:
    for key in ("panel_id", "source_identity_hash", "source_watermark"):
        if not isinstance(payload[key], str) or not payload[key]:
            raise DashboardLoadingStateContractError("OBSERVATION_FIELD_INVALID")
    if payload["state"] not in {"NOT_STARTED", "LOADING", "LOADED", "FAILED"}:
        raise DashboardLoadingStateContractError("OBSERVATION_STATE_INVALID")
    for key in ("elapsed_ms", "evidence_age_seconds"):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise DashboardLoadingStateContractError("OBSERVATION_FIELD_INVALID")
    count = payload["result_count"]
    if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 0):
        raise DashboardLoadingStateContractError("OBSERVATION_FIELD_INVALID")
    if payload["state"] == "LOADED" and count is None:
        raise DashboardLoadingStateContractError("LOADED_RESULT_COUNT_MISSING")
    if payload["state"] != "LOADED" and count is not None:
        raise DashboardLoadingStateContractError("NON_LOADED_RESULT_COUNT_INVALID")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
