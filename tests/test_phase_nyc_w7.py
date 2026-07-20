from decimal import Decimal

from kalshi_predictor.weather.observation_shadow import evaluate_knyc_observation


EVIDENCE = {
    "station_id": "KNYC", "evidence_role": "NON_SETTLEMENT_POINT_OBSERVATION",
    "settlement_source": "the_weather_company", "target_utc_time": "2026-07-16T05:00:00Z",
    "offset_seconds": 540, "observation_temperature_f": "79.0",
}


def test_nyc_w7_disabled_flag_is_exact_rollback() -> None:
    result = evaluate_knyc_observation(
        baseline_probability=Decimal("0.40"), raw_strike=Decimal("78.99"),
        target_time="2026-07-16T05:00:00Z", evidence=EVIDENCE,
        max_adjustment=Decimal("0.10"), enabled=False,
    )
    assert result.passed and not result.applied
    assert result.applied_probability == Decimal("0.40")
    assert result.shadow_probability != result.applied_probability


def test_nyc_w7_rejects_settlement_provenance_conflation() -> None:
    evidence = {**EVIDENCE, "evidence_role": "SETTLEMENT_EVIDENCE"}
    result = evaluate_knyc_observation(
        baseline_probability=Decimal("0.40"), raw_strike=Decimal("78.99"),
        target_time="2026-07-16T05:00:00Z", evidence=evidence,
        max_adjustment=Decimal("0.10"), enabled=False,
    )
    assert not result.passed
    assert result.blocker == "PROVENANCE_ROLE_INVALID"
