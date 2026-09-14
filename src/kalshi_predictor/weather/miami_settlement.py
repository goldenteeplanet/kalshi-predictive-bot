"""Conditional MIAWINDEX 'at' selection, not a certified settlement adapter.

Appendix A and Appendix B sections 7-8 govern selection. The public minute
decoder does not supply first-publication evidence; callers cannot substitute
event timestamps or download receipts for that missing administrator evidence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from kalshi_predictor.weather.miami_index import IndexPoint, MiamiIndexCapture

TERMS_SHA256 = "f7acb398f739ef0b4745e9ae4cbf19b9834676ca4316a81af5c7e3c2c4d20a87"


@dataclass(frozen=True)
class FirstPublicationEvidence:
    """Normalized administrator audit claim; requires independent adapter validation.

    This is NOT a claimed schema of the public endpoint. Evidence bytes/hash bind
    what a caller supplied; they do not attest truth, completeness, or firstness.
    """

    event_at: datetime
    published_at: datetime
    point_sha256: str
    index_sha256: str
    kind: str
    evidence_raw: bytes
    evidence_sha256: str


@dataclass(frozen=True)
class MiamiSettlementSelection:
    status: str
    target_at: datetime
    publication_cutoff: datetime
    selected_event_at: datetime | None
    value_f: Decimal | None
    selected_published_at: datetime | None
    selected_status: str | None
    configuration_version: str | None
    age_seconds: int | None
    index_sha256: str
    calibration_sha256: str
    publication_evidence_hashes: tuple[str, ...]
    terms_sha256: str = TERMS_SHA256
    coverage_minutes: int = 0
    publication_adapter_verified: bool = False
    settlement_certified: bool = False
    execution_authority: bool = False


def _at(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("MIAMI_SELECTION_AWARE_CLOCK_REQUIRED")
    return value.astimezone(UTC)


def point_fingerprint(point: IndexPoint) -> str:
    value = {
        "event_at": _at(point.event_at).isoformat(),
        "value_f": str(point.value_f) if point.value_f is not None else None,
        "status": point.status,
        "contributors": point.contributors,
        "config_version": point.config_version,
        "configuration_published_by_event": point.configuration_published_by_event,
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def select_miami_at_value(
    capture: MiamiIndexCapture,
    *,
    publications: tuple[FirstPublicationEvidence, ...],
    target_at: datetime,
    publication_cutoff: datetime,
    evaluated_at: datetime,
    contract_kind: str,
    terms_raw: bytes,
) -> MiamiSettlementSelection:
    """Select latest eligible minute in inclusive [S-60min,S] without filling.

    Success remains conditional on independently validating administrator audit
    claims. A later publication is not invalidated by the five-minute timing rule:
    it must be available by the explicit cutoff. Restatements are never selected.
    Require continuous evidence from S back to the first eligible minute only.
    A no-value result requires all 61 minutes; irrelevant older points are ignored.
    """
    target, cutoff, evaluated = map(_at, (target_at, publication_cutoff, evaluated_at))
    if (
        contract_kind != "AT" or type(terms_raw) is not bytes or not 0 < len(terms_raw) <= 4_000_000
        or hashlib.sha256(terms_raw).hexdigest() != TERMS_SHA256
    ):
        raise ValueError("MIAMI_SELECTION_SUPPORTED_TERMS_REQUIRED")
    if (
        target.second
        or target.microsecond
        or not target + timedelta(minutes=5) <= cutoff <= evaluated
    ):
        raise ValueError("MIAMI_SELECTION_TARGET_OR_CUTOFF_INVALID")
    index_received, calibration_received = map(
        _at,
        (capture.index_received_at, capture.calibrations_received_at),
    )
    if (
        capture.index_units != "fahrenheit"
        or _at(capture.available_at) != max(index_received, calibration_received)
        or max(index_received, calibration_received) > evaluated
        or any(
            len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha)
            for sha in (capture.index_sha256, capture.calibrations_sha256)
        )
        or len(capture.points) > 11000
        or not 0 < len(publications) <= 61
    ):
        raise ValueError("MIAMI_SELECTION_UNITS_OR_PUBLICATION_EVIDENCE_REQUIRED")
    expected = {target - timedelta(minutes=i) for i in range(61)}
    points: dict[datetime, IndexPoint] = {}
    previous = None
    for point in capture.points:
        at = _at(point.event_at)
        if at.second or at.microsecond or at > index_received or (previous and at <= previous):
            raise ValueError("MIAMI_SELECTION_POINT_ORDER_INVALID")
        previous = at
        if at in expected:
            points[at] = point
    evidence = {}
    for publication in publications:
        at = _at(publication.event_at)
        if at in evidence or at not in expected:
            raise ValueError("MIAMI_SELECTION_DUPLICATE_OR_EXTRA_PUBLICATION")
        evidence[at] = publication
    selected = None
    inspected = []
    for offset in range(61):
        at = target - timedelta(minutes=offset)
        if at not in points:
            raise ValueError("MIAMI_SELECTION_COMPLETE_MINUTE_COVERAGE_REQUIRED")
        if at not in evidence:
            raise ValueError("MIAMI_SELECTION_PUBLICATION_EVIDENCE_REQUIRED")
        point = points[at]
        publication = evidence[at]
        published = _at(publication.published_at)
        if (
            publication.kind != "INITIAL_PUBLICATION"
            or publication.index_sha256 != capture.index_sha256
            or publication.point_sha256 != point_fingerprint(point)
            or not 0 < len(publication.evidence_raw) <= 16000
            or hashlib.sha256(publication.evidence_raw).hexdigest() != publication.evidence_sha256
        ):
            raise ValueError("MIAMI_SELECTION_INITIAL_PUBLICATION_BINDING_REQUIRED")
        if not at + timedelta(minutes=5) <= published <= min(cutoff, index_received):
            raise ValueError("MIAMI_SELECTION_PUBLICATION_OUTSIDE_AS_OF_WINDOW")
        inspected.append(publication.evidence_sha256)
        if not point.config_version or not point.configuration_published_by_event:
            raise ValueError("MIAMI_SELECTION_CONFIGURATION_UNPROVEN")
        if point.status == "unavailable":
            if point.value_f is not None or (
                point.contributors is not None
                and (type(point.contributors) is not int or not 0 <= point.contributors < 4)
            ):
                raise ValueError("MIAMI_SELECTION_UNAVAILABLE_VALUE_OR_QUORUM_INVALID")
        elif point.status in {"normal", "degraded"}:
            if (
                not isinstance(point.value_f, Decimal)
                or not point.value_f.is_finite()
                or point.value_f != point.value_f.quantize(Decimal(".01"))
                or type(point.contributors) is not int
                or not 4 <= point.contributors <= 5
                or (point.status == "normal" and point.contributors != 5)
            ):
                raise ValueError("MIAMI_SELECTION_CANONICAL_VALUE_OR_QUORUM_INVALID")
        else:
            raise ValueError("MIAMI_SELECTION_UNKNOWN_OR_INCOMPLETE_MINUTE")
        if point.status in {"normal", "degraded"}:
            selected = at
            break
    chosen = points[selected] if selected is not None else None
    return MiamiSettlementSelection(
        status=(
            "CONDITIONAL_SELECTION_NOT_CERTIFIED"
            if selected is not None
            else "NO_CANONICAL_POINT_RULE_7_1_REQUIRED"
        ),
        target_at=target,
        publication_cutoff=cutoff,
        selected_event_at=selected,
        value_f=chosen.value_f if chosen else None,
        selected_published_at=evidence[selected].published_at if selected else None,
        selected_status=chosen.status if chosen else None,
        configuration_version=chosen.config_version if chosen else None,
        age_seconds=int((target - selected).total_seconds()) if selected else None,
        index_sha256=capture.index_sha256,
        calibration_sha256=capture.calibrations_sha256,
        publication_evidence_hashes=tuple(inspected),
        coverage_minutes=len(inspected),
    )
