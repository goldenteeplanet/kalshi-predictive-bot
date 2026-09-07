from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

import pytest
from kalshi_predictor.phase4cd.read_model_watermark import (
    WatermarkError,
    build_watermark,
    evaluate_watermark,
)

NOW = datetime(2026, 8, 27, tzinfo=UTC)


def test_initial_and_progress_transitions() -> None:
    first = _watermark(10, NOW)
    initial = _evaluate(first, previous=None, now=NOW)
    assert initial.transition == "INITIAL"
    assert initial.delta is None
    progress = _evaluate(
        _watermark(12, NOW + timedelta(seconds=1)),
        previous=first,
        now=NOW + timedelta(seconds=1),
    )
    assert progress.transition == "PROGRESSED"
    assert progress.delta == 2


def test_unchanged_is_not_progress() -> None:
    previous = _watermark(10, NOW)
    result = _evaluate(
        _watermark(10, NOW + timedelta(seconds=1)),
        previous=previous,
        now=NOW + timedelta(seconds=1),
    )
    assert result.transition == "UNCHANGED"
    assert result.delta == 0


def test_empty_or_partial_evidence_fails_closed() -> None:
    with pytest.raises(WatermarkError, match="WATERMARK_FIELDS_INVALID"):
        _evaluate({}, previous=None, now=NOW)
    partial = _watermark(10, NOW)
    partial.pop("source_identity_hash")
    with pytest.raises(WatermarkError, match="WATERMARK_FIELDS_INVALID"):
        _evaluate(partial, previous=None, now=NOW)


def test_exact_staleness_boundary_fails_closed() -> None:
    with pytest.raises(WatermarkError, match="CURRENT_STALE"):
        _evaluate(_watermark(10, NOW), previous=None, now=NOW + timedelta(seconds=30))


def test_tampering_and_malformed_canonical_form_fail_closed() -> None:
    tampered = _watermark(10, NOW)
    tampered["sequence"] = 11
    with pytest.raises(WatermarkError, match="WATERMARK_CANONICAL_FORM_INVALID"):
        _evaluate(tampered, previous=None, now=NOW)
    malformed = _watermark(10, NOW)
    malformed["watermark"] = "paper_pnl:not-an-int"
    with pytest.raises(WatermarkError, match="WATERMARK_CANONICAL_FORM_INVALID"):
        _evaluate(malformed, previous=None, now=NOW)


@pytest.mark.parametrize(
    ("sequence", "reason"),
    [
        (9, "SEQUENCE_REGRESSED"),
        (16, "FORWARD_STEP_EXCEEDED"),
    ],
)
def test_sequence_boundaries_fail_closed(sequence: int, reason: str) -> None:
    with pytest.raises(WatermarkError, match=reason):
        _evaluate(
            _watermark(sequence, NOW + timedelta(seconds=1)),
            previous=_watermark(10, NOW),
            now=NOW + timedelta(seconds=1),
        )


def test_source_and_identity_changes_fail_closed() -> None:
    previous = _watermark(10, NOW)
    changed_source = build_watermark(
        source="forecasts",
        sequence=11,
        observed_at=NOW + timedelta(seconds=1),
        source_identity_hash="a" * 64,
    )
    with pytest.raises(WatermarkError, match="SOURCE_CHANGED"):
        _evaluate(changed_source, previous=previous, now=NOW + timedelta(seconds=1))
    changed_identity = _watermark(11, NOW + timedelta(seconds=1), identity="b" * 64)
    with pytest.raises(WatermarkError, match="SOURCE_IDENTITY_CHANGED"):
        _evaluate(changed_identity, previous=previous, now=NOW + timedelta(seconds=1))


def test_observation_order_and_future_time_fail_closed() -> None:
    previous = _watermark(10, NOW + timedelta(seconds=2))
    with pytest.raises(WatermarkError, match="OBSERVATION_ORDER_INVALID"):
        _evaluate(
            _watermark(11, NOW + timedelta(seconds=1)),
            previous=previous,
            now=NOW + timedelta(seconds=2),
        )
    with pytest.raises(WatermarkError, match="CURRENT_FUTURE_DATED"):
        _evaluate(_watermark(11, NOW + timedelta(seconds=1)), previous=None, now=NOW)


def test_copied_input_is_not_mutated_and_no_writer_surface_exists() -> None:
    current = _watermark(10, NOW)
    original = copy.deepcopy(current)
    _evaluate(current, previous=None, now=NOW)
    assert current == original
    assert "execute" not in dir(evaluate_watermark)
    assert "commit" not in dir(evaluate_watermark)


def _evaluate(current: dict, *, previous: dict | None, now: datetime):
    return evaluate_watermark(
        current,
        previous=previous,
        now=now,
        max_age_seconds=30,
        max_forward_step=5,
    )


def _watermark(sequence: int, observed_at: datetime, *, identity: str = "a" * 64) -> dict:
    return build_watermark(
        source="paper_pnl",
        sequence=sequence,
        observed_at=observed_at,
        source_identity_hash=identity,
    )
