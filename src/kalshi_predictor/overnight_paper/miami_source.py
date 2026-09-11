"""Pure canonical Miami input health; no qualification, model or settlement authority."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from kalshi_predictor.overnight_paper.miami_binding import INDEX, MiamiOriginal, _at, _decode
from kalshi_predictor.overnight_paper.provenance import Artifact
from kalshi_predictor.weather.miami_index import decode_miami_index


@dataclass(frozen=True)
class MiamiSourceHealth:
    state: str
    reasons: tuple[str, ...]
    checked_as_of: datetime
    origin_at: datetime
    target_at: datetime
    scope: str = "SOURCE_HEALTH_ONLY"
    source_healthy: bool = False
    index_sha256: str | None = None
    calibrations_sha256: str | None = None
    index_received_at: datetime | None = None
    calibrations_received_at: datetime | None = None
    available_at: datetime | None = None
    configuration_version: str | None = None
    origin_value_f: Decimal | None = None
    lag_value_f: Decimal | None = None
    origin_status: str | None = None
    lag_status: str | None = None
    origin_contributors: int | None = None
    lag_contributors: int | None = None
    origin_age_seconds: float | None = None
    index_receipt_age_seconds: float | None = None
    calibrations_receipt_age_seconds: float | None = None
    provider_generated_at: None = None
    provider_updated_at: None = None
    historical_public_availability: str = "UNKNOWN"
    settlement_rules_verified: bool = False
    model_calibration_verified: bool = False
    paper_eligible: bool = False
    execution_authority: bool = False


def _receipt(original: MiamiOriginal, evidence: Artifact, expected_url: str) -> None:
    if original.url != expected_url or len(evidence.payload) > 16_000:
        raise ValueError("MIAMI_SOURCE_ENDPOINT_OR_RECEIPT_BUDGET")
    receipt = _decode(evidence)
    if (
        receipt.get("url") != original.url
        or type(receipt.get("status")) is not int
        or receipt["status"] != 200
        or receipt.get("sha256") != original.artifact.sha256
        or _at(receipt["received_at"]) != _at(original.received_at)
        or ("method" in receipt and receipt["method"] != "GET")
    ):
        raise ValueError("MIAMI_SOURCE_ORIGINAL_RECEIPT_MISMATCH")
    # Earlier collector receipts omit method; this exact public endpoint's raw
    # bytes/receipt are retained, without claiming method was a provider field.
    if not 0 < len(original.artifact.payload) <= 4_000_000:
        raise ValueError("MIAMI_SOURCE_ORIGINAL_BYTES_BUDGET")
    original.artifact.decode()


def verify_miami_source(
    *,
    index: MiamiOriginal,
    calibrations: MiamiOriginal,
    index_receipt: Artifact,
    calibrations_receipt: Artifact,
    origin_at: datetime,
    target_at: datetime,
    model_input_as_of: datetime,
    decision_at: datetime,
    now: datetime,
    origin_grid_minutes: int = 60,
) -> MiamiSourceHealth:
    """Check original capture health at explicit clocks; perform no I/O.

    A retrospective checked_as_of is not the time this function was executed.
    Callers recording an audit must separately record their actual execution time.
    This does not certify that a frozen model used the input pair.
    """
    origin, target, cutoff, decision, current = map(
        _at, (origin_at, target_at, model_input_as_of, decision_at, now)
    )
    try:
        _receipt(index, index_receipt, INDEX + "?last_sec=7200")
        _receipt(calibrations, calibrations_receipt, INDEX + "/calibrations")
        local = origin.astimezone(ZoneInfo("America/New_York"))
        if type(origin_grid_minutes) is not int or origin_grid_minutes not in (30, 60):
            raise ValueError("MIAMI_SOURCE_ORIGIN_GRID_REQUIRED")
        if local.minute % origin_grid_minutes or local.second or local.microsecond:
            raise ValueError("MIAMI_SOURCE_EXACT_HOUR_ORIGIN")
        if target - origin not in (timedelta(minutes=30), timedelta(minutes=60)):
            raise ValueError("MIAMI_SOURCE_EXACT_HORIZON")
        if target.astimezone(ZoneInfo("America/New_York")).date() != local.date():
            raise ValueError("MIAMI_SOURCE_SAME_DAY_TARGET")
        received, cal_received = _at(index.received_at), _at(calibrations.received_at)
        if not max(received, cal_received) <= cutoff <= decision <= current or cutoff < origin:
            raise ValueError("MIAMI_SOURCE_CHRONOLOGY")
        capture = decode_miami_index(
            index.artifact.payload,
            calibrations.artifact.payload,
            index_received_at=received,
            calibrations_received_at=cal_received,
            index_units="fahrenheit",
        )
        if capture.latest_config_matches_last_point is not True:
            raise ValueError("MIAMI_SOURCE_CURRENT_CONFIGURATION_HEADER")
        points = {p.event_at: p for p in capture.points}
        a, b = points.get(origin), points.get(origin - timedelta(minutes=30))
        if a is None or b is None:
            raise ValueError("MIAMI_SOURCE_EXACT_ORIGIN_LAG_REQUIRED")
        if any(p.value_f is None or p.status not in {"normal", "degraded"} for p in (a, b)):
            raise ValueError("MIAMI_SOURCE_ORIGIN_LAG_UNAVAILABLE")
        if a.config_version != b.config_version or not all(
            p.configuration_published_by_event for p in (a, b)
        ):
            raise ValueError("MIAMI_SOURCE_ORIGIN_LAG_CONFIGURATION")
        stale = []
        if (current - origin).total_seconds() > 600:
            stale.append("MIAMI_SOURCE_ORIGIN_OLDER_THAN_TEN_MINUTES")
        if any((current - r).total_seconds() > 60 for r in (received, cal_received)):
            stale.append("MIAMI_SOURCE_RECEIPT_OLDER_THAN_SIXTY_SECONDS")
        if current >= target:
            stale.append("MIAMI_SOURCE_TARGET_EXPIRED")
        return MiamiSourceHealth(
            state="STALE" if stale else "FRESH",
            reasons=tuple(stale),
            checked_as_of=current,
            origin_at=origin,
            target_at=target,
            source_healthy=not stale,
            index_sha256=capture.index_sha256,
            calibrations_sha256=capture.calibrations_sha256,
            index_received_at=received,
            calibrations_received_at=cal_received,
            available_at=capture.available_at,
            configuration_version=a.config_version,
            origin_value_f=a.value_f,
            lag_value_f=b.value_f,
            origin_status=a.status,
            lag_status=b.status,
            origin_contributors=a.contributors,
            lag_contributors=b.contributors,
            origin_age_seconds=(current - origin).total_seconds(),
            index_receipt_age_seconds=(current - received).total_seconds(),
            calibrations_receipt_age_seconds=(current - cal_received).total_seconds(),
        )
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        return MiamiSourceHealth("INVALID", (str(exc),), current, origin, target)
