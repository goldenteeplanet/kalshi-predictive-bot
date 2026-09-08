from dataclasses import replace
from datetime import UTC, datetime

import pytest

from kalshi_predictor.overnight_paper.source_health import SourceState, classify_source
from kalshi_predictor.overnight_paper.weather_rules import (
    WeatherSettlementRule,
    knyc_incomplete_registry,
    select_weather_rule,
)


def health(**overrides):
    args = dict(
        generated_at="2026-09-08T01:45:00Z",
        updated_at="2026-09-08T01:30:00Z",
        valid_from="2026-09-08T02:00:00Z",
        valid_to="2026-09-08T03:00:00Z",
        target_start="2026-09-08T02:00:00Z",
        target_end="2026-09-08T03:00:00Z",
        now=datetime(2026, 9, 8, 2, tzinfo=UTC),
        payload_hash="a",
    )
    return classify_source(**(args | overrides))


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({}, SourceState.NEW_FRESH_DATA),
        ({"previous_hash": "a"}, SourceState.UNCHANGED_AND_FRESH),
        ({"previous_hash": "a", "reused": True}, SourceState.REUSED_BUT_FRESH),
        ({"updated_at": "2026-09-08T01:29:59Z"}, SourceState.STALE),
        ({"generated_at": "2026-09-08T01:29:59Z"}, SourceState.STALE),
        ({"updated_at": None}, SourceState.SOURCE_ERROR),
        ({"generated_at": "2026-09-08T02:00:01Z"}, SourceState.SOURCE_ERROR),
        ({"updated_at": "2026-09-08T01:30:00"}, SourceState.SOURCE_ERROR),
        ({"target_end": "2026-09-08T03:00:01Z"}, SourceState.UNSUPPORTED),
        ({"valid_to": "2026-09-08T02:00:00Z"}, SourceState.SOURCE_ERROR),
        ({"max_age_seconds": 1801}, SourceState.SOURCE_ERROR),
        ({"payload_hash": ""}, SourceState.SOURCE_ERROR),
        ({"reused": True}, SourceState.SOURCE_ERROR),
        ({"supported": False}, SourceState.UNSUPPORTED),
        ({"error": "HTTP_503"}, SourceState.SOURCE_ERROR),
    ],
)
def test_source_health(overrides, expected):
    result = health(**overrides)
    assert result.state == expected
    assert result.role == "ANALYTICAL_SOURCE"
    assert result.eligible == (
        expected
        in {
            SourceState.NEW_FRESH_DATA,
            SourceState.REUSED_BUT_FRESH,
            SourceState.UNCHANGED_AND_FRESH,
        }
    )


def test_new_download_never_resets_provider_age():
    stale = health(payload_hash="new", updated_at="2026-09-07T18:19:54Z")
    assert stale.state == SourceState.STALE
    assert stale.provider_age_seconds > 1800


def rule(**overrides):
    values = dict(
        series="FIXTURE",
        provider="fixture provider",
        effective_from="2026-09-08T00:00:00Z",
        effective_to="2026-09-09T00:00:00Z",
        station="fixture station",
        metric="temperature",
        observation_semantics="fixture selection",
        precision="fixture",
        rounding="fixture",
        finality="fixture finality",
        conversion="fixture",
        missing_corrected_handling="fixture",
        network_sensor="fixture",
        first_event="fixture",
        evidence_urls=("https://example.org",),
        evidence_hashes=("fixture",),
    )
    return WeatherSettlementRule(**(values | overrides))


def select(rows, **overrides):
    return select_weather_rule(
        tuple(rows),
        **(
            {
                "series": "FIXTURE",
                "observation_time": "2026-09-08T23:59:59Z",
            }
            | overrides
        ),
    )


def test_rule_version_changes_with_methodology_and_is_reproducible():
    original = rule()
    assert original.version == rule().version
    assert original.version != replace(original, rounding="changed").version


def test_cutover_exclusive_end_and_exact_next_version():
    old = rule()
    new = rule(
        provider="next", effective_from=old.effective_to, effective_to="2026-09-10T00:00:00Z"
    )
    assert select([old, new]).provider == old.provider
    assert select([old, new], observation_time=old.effective_to).provider == "next"


def test_conflict_blocks_family_even_if_provider_preference_would_hide_it():
    a, b = rule(), rule(provider="other")
    assert select([a, b], provider=a.provider).blockers == ("SETTLEMENT_RULE_CONFLICT",)
    foreign = rule(series="OTHER", conflicts=("ambiguous",))
    assert select([a, foreign]).status == "CERTIFIED"


def test_incomplete_known_knyc_never_certifies_around_announced_date():
    rows = knyc_incomplete_registry("hash")
    for instant in ("2026-09-08T12:00:00Z", "2026-09-09T00:00:00Z", "2026-09-10T00:00:00Z"):
        result = select_weather_rule(rows, series="KXTEMPNYCH", observation_time=instant)
        assert result.status == "BLOCKED"
        assert result.blockers == ("CUTOVER_BOUNDARY_UNKNOWN",)


def test_missing_methodology_and_provider_conflict_fail_closed():
    assert select([rule(rounding=None)]).blockers == ("MISSING_ROUNDING",)
    assert select([rule()], provider="wrong").blockers == ("PROVIDER_CONFLICT",)
    assert select([rule(effective_to="2026-09-07T00:00:00Z")]).status == "BLOCKED"


def test_other_market_family_is_unsupported_not_weather_blocked():
    assert select(knyc_incomplete_registry("hash")).status == "UNSUPPORTED"
