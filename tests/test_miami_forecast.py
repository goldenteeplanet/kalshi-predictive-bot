from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from kalshi_predictor.weather.miami_forecast import empirical_probability, forecast_miami_prior_day
from kalshi_predictor.weather.miami_index import IndexPoint, MiamiIndexCapture

ORIGIN = datetime(2026, 9, 10, 21, tzinfo=UTC)
AS_OF = ORIGIN + timedelta(minutes=5)


def point(at, value):
    return IndexPoint(at, Decimal(str(value)), "normal", 5, "config", False, True)


def capture():
    points = [point(ORIGIN - timedelta(minutes=30), 79), point(ORIGIN, 80)]
    for day, increment in ((3, 1), (2, 2), (1, 3)):
        at = ORIGIN - timedelta(days=day)
        points.extend(
            [
                point(at, 70),
                point(at + timedelta(minutes=30), 70 + increment),
                point(at + timedelta(minutes=60), 70 + increment),
            ]
        )
    return MiamiIndexCapture(
        tuple(sorted(points, key=lambda p: p.event_at)),
        "a" * 64,
        "b" * 64,
        AS_OF - timedelta(seconds=1),
        AS_OF - timedelta(seconds=2),
        AS_OF - timedelta(seconds=1),
        "config",
        (),
        "fahrenheit",
        True,
    )


def forecast(c=None, **kwargs):
    args = dict(origin_at=ORIGIN, model_input_as_of=AS_OF, horizon_minutes=60)
    args.update(kwargs)
    return forecast_miami_prior_day([capture() if c is None else c], **args)


def test_preserves_fixed_formulas_exact_horizon_and_provenance():
    result = forecast()
    assert result["models"]["persistence"]["mean_f"] == 80
    assert result["models"]["fixed_30min_linear_trend"]["mean_f"] == 82
    assert result["models"]["prior_day_increment_empirical"]["samples_f"] == [81, 82, 83]
    assert result["models"]["prior_day_increment_empirical"]["mean_f"] == 82
    assert datetime.fromisoformat(result["target_at"]) - ORIGIN == timedelta(minutes=60)
    assert result["origin_at"] != result["model_input_as_of"]
    assert result["source_hashes"] == ["a" * 64]
    assert all(datetime.fromisoformat(t["target_at"]) < ORIGIN for t in result["training"])
    assert result["training_day_groups"] == 3
    assert not result["calibrated"] and not result["paper_eligible"]
    assert not result["execution_authority"]


def test_post_origin_values_never_enter_fixed_prediction():
    original = capture()
    later = point(ORIGIN + timedelta(minutes=1), 120)
    amended = replace(original, points=original.points + (later,))
    assert forecast(amended)["models"] == forecast(original)["models"]
    assert all(
        datetime.fromisoformat(p["event_at"]) <= ORIGIN for p in forecast(amended)["lineage"]
    )


@pytest.mark.parametrize(
    "kwargs,error",
    [
        ({"model_input_as_of": ORIGIN + timedelta(minutes=11)}, "OLDER_THAN_TEN"),
        ({"model_input_as_of": ORIGIN - timedelta(seconds=1)}, "ORIGIN_FUTURE"),
        ({"origin_at": ORIGIN + timedelta(minutes=1)}, "EXACT_LOCAL_HOUR"),
        ({"horizon_minutes": 15}, "FIXED_HORIZON"),
    ],
)
def test_clock_and_horizon_guards(kwargs, error):
    with pytest.raises(ValueError, match=error):
        forecast(**kwargs)


def test_future_receipt_is_not_relabelled_historical_availability():
    original = capture()
    with pytest.raises(ValueError, match="SOURCE_NOT_AVAILABLE"):
        forecast(
            replace(
                original,
                index_received_at=AS_OF + timedelta(seconds=1),
                available_at=AS_OF + timedelta(seconds=1),
            )
        )


@pytest.mark.parametrize("defect", ["missing", "config", "unpublished"])
def test_no_gapfill_or_invalid_training_window(defect):
    original = capture()
    at = ORIGIN - timedelta(days=1) + timedelta(minutes=60)
    points = list(original.points)
    index = next(i for i, p in enumerate(points) if p.event_at == at)
    if defect == "missing":
        points.pop(index)
    elif defect == "config":
        points[index] = replace(points[index], config_version="other")
    else:
        points[index] = replace(points[index], configuration_published_by_event=False)
    with pytest.raises(ValueError, match="INSUFFICIENT_PRIOR_DAY"):
        forecast(replace(original, points=tuple(points)))


def test_conflicting_history_is_not_silently_selected():
    original = capture()
    changed = replace(
        original,
        points=(replace(original.points[0], value_f=Decimal("99")),),
        index_sha256="c" * 64,
    )
    with pytest.raises(ValueError, match="CONFLICTING_OVERLAPPING"):
        forecast_miami_prior_day(
            [original, changed], origin_at=ORIGIN, model_input_as_of=AS_OF, horizon_minutes=60
        )


def test_detail_completeness_variation_does_not_invent_numerical_conflict():
    original = capture()
    detailed = replace(
        original,
        points=tuple(replace(p, station_observation_times_complete=True) for p in original.points),
        index_sha256="c" * 64,
    )
    result = forecast_miami_prior_day(
        [original, detailed], origin_at=ORIGIN, model_input_as_of=AS_OF, horizon_minutes=30
    )
    assert result["models"]["prior_day_increment_empirical"]["samples_f"] == [81, 82, 83]
    assert result["lineage"][0]["source_hashes"] == ["a" * 64, "c" * 64]


def test_empirical_exact_boundary_partition():
    samples = [80.0, 81.0, 82.0]
    assert empirical_probability(samples, comparator="ABOVE", threshold_f=81) == 1 / 3
    assert empirical_probability(samples, comparator="AT_OR_ABOVE", threshold_f=81) == 2 / 3
    assert empirical_probability(samples, comparator="RANGE", lower_f=81, upper_f=82) == 1 / 3
    below = empirical_probability(samples, comparator="BELOW", threshold_f=81)
    above = empirical_probability(samples, comparator="AT_OR_ABOVE", threshold_f=81)
    assert below + above == 1


@pytest.mark.parametrize("samples", [[80.0, float("nan"), 82.0], [80.0, 81.0], [True, 81.0, 82.0]])
def test_empirical_bad_inputs(samples):
    with pytest.raises(ValueError):
        empirical_probability(samples, comparator="ABOVE", threshold_f=81)


def test_decimal_sample_preserves_exact_strict_threshold_tie():
    original = capture()
    amended = []
    for p in original.points:
        if p.event_at == ORIGIN:
            p = replace(p, value_f=Decimal("84.79"))
        elif p.event_at.date() < ORIGIN.date() and p.event_at.minute == 0 and p.event_at.hour == 22:
            p = replace(p, value_f=Decimal("70.20"))
        amended.append(p)
    result = forecast(replace(original, points=tuple(amended)))
    samples = result["models"]["prior_day_increment_empirical"]["samples_f"]
    assert samples == [84.99] * 3
    assert empirical_probability(samples, comparator="ABOVE", threshold_f=84.99) == 0
    assert empirical_probability(samples, comparator="AT_OR_ABOVE", threshold_f=84.99) == 1


@pytest.mark.parametrize("contributors", [True, 4, 6])
def test_direct_capture_cannot_bypass_normal_quorum(contributors):
    original = capture()
    changed = tuple(
        replace(p, contributors=contributors) if p.event_at == ORIGIN else p
        for p in original.points
    )
    with pytest.raises(ValueError, match="ORIGIN_LAG"):
        forecast(replace(original, points=changed))


def test_boolean_range_bound_rejected():
    with pytest.raises(ValueError, match="INVALID_RANGE"):
        empirical_probability([1.0, 2.0, 3.0], comparator="RANGE", lower_f=True, upper_f=2.0)


def shifted_capture(delta):
    c = capture()
    return replace(c, points=tuple(replace(p, event_at=p.event_at + delta) for p in c.points),
        index_received_at=c.index_received_at + delta,
        calibrations_received_at=c.calibrations_received_at + delta,
        available_at=c.available_at + delta)


def test_grid30_exact_minute_matching_and_separate_model_identity():
    from kalshi_predictor.weather.miami_half_hour_forecast import forecast_miami_prior_day_grid30

    delta = timedelta(hours=2, minutes=30)
    c = shifted_capture(delta)
    result = forecast_miami_prior_day_grid30([c], origin_at=ORIGIN + delta,
        model_input_as_of=AS_OF + delta, horizon_minutes=60)
    assert result["models"]["prior_day_increment_empirical"]["samples_f"] == [81, 82, 83]
    assert result["target_at"] == "2026-09-11T00:30:00+00:00"
    assert result["model"] == "miami_prior_day_increment_grid30_v1"
    assert all(datetime.fromisoformat(t["origin_at"]).minute == 30 for t in result["training"])
    with pytest.raises(ValueError, match="EXACT_LOCAL_HOUR"):
        forecast_miami_prior_day([c], origin_at=ORIGIN + delta,
            model_input_as_of=AS_OF + delta, horizon_minutes=60)


def test_grid30_missing_exact_training_endpoint_is_not_filled():
    from kalshi_predictor.weather.miami_half_hour_forecast import forecast_miami_prior_day_grid30

    delta = timedelta(minutes=30)
    c = shifted_capture(delta)
    missing = ORIGIN + delta - timedelta(days=1) + timedelta(minutes=60)
    c = replace(c, points=tuple(p for p in c.points if p.event_at != missing))
    with pytest.raises(ValueError, match="INSUFFICIENT_PRIOR_DAY"):
        forecast_miami_prior_day_grid30([c], origin_at=ORIGIN + delta,
            model_input_as_of=AS_OF + delta, horizon_minutes=60)


def test_grid30_rejects_off_grid_and_local_date_boundary():
    from kalshi_predictor.weather.miami_half_hour_forecast import forecast_miami_prior_day_grid30

    for delta, error in ((timedelta(minutes=15), "HALF_HOUR_GRID"),
                         (timedelta(hours=6, minutes=30), "SAME_DAY")):
        c = shifted_capture(delta)
        with pytest.raises(ValueError, match=error):
            forecast_miami_prior_day_grid30([c], origin_at=ORIGIN + delta,
                model_input_as_of=AS_OF + delta, horizon_minutes=60)


def test_grid30_on_hour_has_identical_hourly_numerics():
    from kalshi_predictor.weather.miami_half_hour_forecast import forecast_miami_prior_day_grid30

    hourly = forecast()
    grid = forecast_miami_prior_day_grid30([capture()], origin_at=ORIGIN,
        model_input_as_of=AS_OF, horizon_minutes=60)
    assert grid["models"] == hourly["models"]
    assert grid["training"] == hourly["training"]
    assert hourly["model"] == "miami_prior_day_increment_v1"
