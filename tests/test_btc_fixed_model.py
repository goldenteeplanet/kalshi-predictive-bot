"""Stage-A synthetic arithmetic/alignment tests; no approvals or database sessions."""

import hashlib
import json
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import pytest
from test_coinbase_source_semantics import NOW, mutate_original, source

from kalshi_predictor.crypto.distribution_model import inputs_from_features, threshold_probability
from kalshi_predictor.crypto.features import calculate_crypto_features
from kalshi_predictor.data.schema import CryptoPrice
from kalshi_predictor.overnight_paper import btc_fixed_model as model


@pytest.mark.parametrize(
    "changes",
    [
        {"threshold": Decimal("70000.00")},
        {
            "comparator": "RANGE",
            "threshold": None,
            "lower": Decimal("69999.00"),
            "upper": Decimal("70001.00"),
        },
    ],
)
def test_decimal_strikes_have_exact_serializable_target_identity(changes):
    declared = target() | changes
    original_declared = deepcopy(declared)
    output = forecast(declared=declared)
    expected = {
        key: str(value) if isinstance(value, Decimal) else value for key, value in declared.items()
    }
    assert output["target"] == expected
    assert (
        output["target_sha256"]
        == hashlib.sha256(
            json.dumps(expected, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
    )
    assert declared == original_declared
    assert output["probability"] == forecast(declared=expected)["probability"]


def test_datetime_target_is_canonicalized_without_relabeling_clock():
    instant = NOW + timedelta(minutes=5)
    output = forecast(declared=target() | {"observation_at": instant})
    assert output["target"]["observation_at"] == instant.isoformat()
    assert output["target_sha256"] == forecast()["target_sha256"]


def original():
    value = source()
    mutate_original(
        value,
        "candles",
        lambda rows: [
            row[:4] + [close, row[5]]
            for row, close in zip(rows, (69999, 70001, 70000), strict=True)
        ],
    )
    return value


def target():
    return dict(
        ticker="KXBTCD-26SEP0802-T70000",
        event_id="KXBTCD-26SEP0802",
        series="KXBTCD",
        observation_at=(NOW + timedelta(minutes=5)).isoformat(),
        comparator="ABOVE",
        threshold="70000",
        lower=None,
        upper=None,
        rule_sha256=None,
    )


def forecast(value=None, declared=None, **kwargs):
    return model.forecast_btc_proxy(
        source=original() if value is None else value,
        target=target() if declared is None else declared,
        generated_at=NOW,
        decision_at=NOW,
        **kwargs,
    )


def test_shared_math_matches_original_calculation_and_diagnostic():
    from datetime import UTC, datetime

    from kalshi_predictor.overnight_paper.discovery import _crypto_diagnostic_inputs

    value = original()
    verified, features = model.verified_btc_features(value, computed_at=NOW)
    rows = verified["inputs"]["closed_candles"]
    prices = [
        CryptoPrice(
            symbol="BTC",
            source="coinbase_closed_1m_candles",
            observed_at=datetime.fromtimestamp(row[0] + 60, UTC),
            price_usd=str(row[4]),
            raw_json=json.dumps(row),
        )
        for row in rows
    ]
    expected = calculate_crypto_features(prices, window_minutes=1440)
    assert features == expected
    calculated = model.prepare_btc_inputs(
        value, horizon_end_at=target()["observation_at"], computed_at=NOW
    )
    expected_inputs = inputs_from_features(
        dict(expected, price=float(verified["inputs"]["spot"])), horizon_minutes=5
    )
    assert calculated.distribution == expected_inputs
    output = forecast(value)
    assert output["probability"] == str(
        threshold_probability(expected_inputs, comparator="ABOVE", threshold=70000)
    )
    ticker = json.loads(bytes.fromhex(value["body"]["ticker"]["payload_hex"]))
    discovery_input = dict(
        symbol="BTC",
        analytical_source=value,
        analytical_inputs=verified,
        features=features,
        ticker=ticker,
        ticker_evidence=value["body"]["ticker"],
        candles_evidence=value["body"]["candles"],
        latest_closed_candle_at=verified["inputs"]["candle_cutoff_at"],
        collected_at=NOW.isoformat(),
        closed_candle_count=len(rows),
    )
    diagnostic, _ = _crypto_diagnostic_inputs(discovery_input, target()["observation_at"], NOW)
    assert diagnostic == expected_inputs
    discovery_input["features"] = dict(features, price="1")
    with pytest.raises(ValueError, match="ORIGINAL_INPUT_MISMATCH"):
        _crypto_diagnostic_inputs(discovery_input, target()["observation_at"], NOW)


def test_identity_and_all_original_alignment_changes_are_visible():
    value = original()
    before = forecast(value)
    changed = deepcopy(value)
    mutate_original(changed, "ticker", lambda row: row | {"price": "70001"})
    after = forecast(changed)
    assert before["coinbase_input_sha256"] != after["coinbase_input_sha256"]
    assert before["source_hashes"] != after["source_hashes"]
    assert before["probability"] != after["probability"]
    assert before["model_spec_sha256"] == after["model_spec_sha256"]
    assert before["feature_record"]["value"]["spot"] == "70000.01"
    assert before["execution_authority"] is False
    assert "entrypoint" not in before["model"]
    assert before["execution_entrypoint"] == model.MODEL_ENTRYPOINT
    assert before["horizon_role"] == "DECLARED_OBSERVATION_TIME_UNVERIFIED"
    assert "PAYOFF_TIME_AND_RULE_SEMANTICS_UNVERIFIED" in before["blockers"]
    assert before["model"]["code_closure_verified"] is False
    assert "available_at" not in before  # Pure caller has not persisted/archived this output.


@pytest.mark.parametrize(
    "changes",
    [
        {"observation_at": NOW.isoformat()},
        {"observation_at": (NOW - timedelta(seconds=1)).isoformat()},
        {"observation_at": "2026-09-08T01:05:00"},
        {"observation_at": "unknown"},
        {"series": "KXDOGE"},
        {"event_id": "KXBTCD-OTHER"},
        {"comparator": "TOUCH"},
        {"threshold": "NaN"},
        {"threshold": "Infinity"},
        {"threshold": True},
        {"threshold": "0"},
        {"upper": "70001"},
        {"comparator": "RANGE", "threshold": None, "lower": "2", "upper": "1"},
        {"rule_sha256": "0" * 64},
    ],
)
def test_invalid_or_unavailable_target_terms_fail(changes):
    with pytest.raises((ValueError, TypeError)):
        forecast(declared=target() | changes)


def test_no_close_substitution_or_rule_hash_approval():
    declared = target()
    declared["close_time"] = declared.pop("observation_at")
    with pytest.raises(ValueError, match="EXPLICIT_TARGET"):
        forecast(declared=declared)
    raw = b"synthetic terms, not a policy"
    declared = target() | {"rule_sha256": hashlib.sha256(raw).hexdigest()}
    output = forecast(declared=declared, rule_original=raw)
    assert "PAYOFF_TIME_AND_RULE_SEMANTICS_UNVERIFIED" in output["blockers"]
    with pytest.raises(ValueError, match="ORIGINAL_BINDING"):
        forecast(declared=declared, rule_original=raw + b" changed")


def test_source_clock_and_zero_volatility_fail_without_fallback():
    with pytest.raises(ValueError, match="INSUFFICIENT_CANDLE_HISTORY"):
        forecast(source())
    value = original()
    value["body"]["ticker"]["received_at"] = (NOW + timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError):
        forecast(value)
    with pytest.raises(ValueError):
        model.forecast_btc_proxy(
            source=original(),
            target=target(),
            generated_at=NOW,
            decision_at=NOW + timedelta(seconds=61),
        )
    with pytest.raises(ValueError, match="EXECUTION_OR_TARGET_CLOCK"):
        model.forecast_btc_proxy(
            source=original(),
            target=target(),
            generated_at=NOW,
            decision_at=NOW - timedelta(seconds=1),
        )


def test_inclusive_comparators_are_explicit_continuous_proxy_only():
    assert (
        forecast()["probability"]
        == forecast(declared=target() | {"comparator": "AT_OR_ABOVE"})["probability"]
    )
    assert forecast()["model"]["payoff_approximation"] == "CONTINUOUS_TERMINAL_PRICE_PROXY"
