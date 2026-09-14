import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from kalshi_predictor.overnight_paper.boundary import ExecutionMode
from kalshi_predictor.overnight_paper.qualification import (
    EvidenceReference,
    GateEvidence,
    Readiness,
    compute_net_ev,
    decision_fingerprint,
    qualify_candidate,
)
from kalshi_predictor.position_sizing.sizer import (
    DynamicPositionSizer,
    PositionSizingConfig,
    PositionSizingInput,
)


def ev(**overrides):
    values = dict(
        model_probability=Decimal("0.7"),
        executable_price=Decimal("0.5"),
        estimated_fee=Decimal("0.02"),
        slippage_allowance=Decimal("0.01"),
        uncertainty_buffer=Decimal("0.03"),
    )
    return compute_net_ev(**(values | overrides))


def qualify(**overrides):
    inputs = {"ticker": "KXBTC-T1", "category": "crypto"}
    values = dict(
        ticker="KXBTC-T1",
        category="crypto",
        decision_inputs=inputs,
        decision_id=decision_fingerprint(inputs),
        minimum_net_ev=Decimal("0.05"),
        ev=ev(),
        mode=ExecutionMode.LOCAL_PAPER,
    )
    return qualify_candidate(**(values | overrides))


def test_ev_subtracts_all_costs():
    assert ev().gross_edge == Decimal("0.2")
    assert ev().net_ev == Decimal("0.14")


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(model_probability=Decimal("NaN")),
        dict(executable_price=Decimal("1")),
        dict(estimated_fee=Decimal("-0.1")),
    ],
)
def test_ev_rejects_invalid_inputs(kwargs):
    with pytest.raises(ValueError):
        ev(**kwargs)


@pytest.mark.parametrize("cost", ["0.11", "0.12", "0.4"])
def test_net_ev_must_strictly_exceed_unchanged_minimum(cost):
    result = qualify(ev=ev(estimated_fee=Decimal(cost)))
    assert "POSITIVE_NET_EV" in result.blockers


def test_cannot_forge_net_ev_total():
    result = qualify(ev=replace(ev(), net_ev=Decimal("0.9")))
    assert "POSITIVE_NET_EV" in result.blockers


def test_no_evidence_fails_closed_including_writer_boundary():
    result = qualify()
    assert len(result.gates) == 12
    assert result.status == Readiness.PAPER_NOT_READY
    assert "NO_EXCHANGE_PATH" in result.blockers
    assert "PHASE_3M_NONZERO" in result.blockers
    assert "PHASE_3N_ALLOW" in result.blockers
    assert "IDEMPOTENT_LOCAL_DECISION" in result.blockers


def test_hashed_raw_public_pass_boolean_is_not_certification():
    payload = json.dumps({"passed": True}).encode()
    evidence = GateEvidence(
        3,
        "a",
        "weather",
        "T",
        "collector",
        EvidenceReference("public.json", hashlib.sha256(payload).hexdigest(), payload),
    )
    assert not evidence.verified()


def test_weather_blockers_do_not_leak_to_crypto():
    item = GateEvidence(
        3,
        "weather-decision",
        "weather",
        "WX",
        "rules",
        EvidenceReference("x", "0" * 64, b"{}"),
        ("TWC_CUTOVER",),
    )
    # A family registry must select exact ticker/category before passing evidence.
    # Foreign evidence cannot satisfy a gate, regardless of its status.
    result = qualify(evidence=(item,))
    assert result.status == Readiness.PAPER_NOT_READY
    assert "CERTIFIED_SETTLEMENT_RULE" in result.blockers
    assert "TWC_CUTOVER" not in result.blockers


def test_missing_phase3m_inputs_safe_fallback_not_accepted():
    size = DynamicPositionSizer(PositionSizingConfig()).decide(
        PositionSizingInput(
            confidence_score=None,
            opportunity_score=None,
            liquidity_score=None,
            current_drawdown_fraction=None,
            max_drawdown_fraction=None,
            historical_accuracy=None,
            historical_sample_size=None,
            decision_timestamp=datetime.now(UTC),
        )
    )
    assert "PHASE_3M_NONZERO" in qualify(phase3m=size).blockers


def test_decision_id_changes_with_snapshot_and_rejects_nan():
    assert decision_fingerprint({"snapshot_id": "one"}) != decision_fingerprint(
        {"snapshot_id": "two"}
    )
    with pytest.raises(ValueError):
        decision_fingerprint({"probability": float("nan")})


def test_existing_decision_cannot_create_again():
    result = qualify()
    assert (
        "IDEMPOTENT_LOCAL_DECISION"
        in qualify(existing_decision_ids=frozenset({result.decision_id})).blockers
    )


def test_phase3n_actual_engine_allow_required():
    from test_phase_3n_advanced_risk import _config, _request

    from kalshi_predictor.advanced_risk.engine import AdvancedRiskEngine

    size = DynamicPositionSizer(PositionSizingConfig()).decide(
        PositionSizingInput(
            confidence_score=0.55,
            opportunity_score=0.55,
            liquidity_score=0.8,
            current_drawdown_fraction=0.0,
            max_drawdown_fraction=0.2,
            historical_accuracy=0.55,
            historical_sample_size=30,
            decision_timestamp=datetime.now(UTC),
        )
    )
    allowed = AdvancedRiskEngine(_config()).decide(_request(phase_3m_contracts=1))
    assert allowed.action.value == "ALLOW"
    inputs = dict(
        ticker="KXBTC-T1",
        category="crypto",
        phase3m_hash=decision_fingerprint(size.as_dict()),
        phase3n_hash=decision_fingerprint(allowed.as_dict()),
    )
    result = qualify(
        phase3m=size,
        phase3n=allowed,
        decision_inputs=inputs,
        decision_id=decision_fingerprint(inputs),
    )
    assert "PHASE_3N_ALLOW" not in result.blockers
    reduced = AdvancedRiskEngine(_config(live_max_contracts=1)).decide(_request())
    assert reduced.action.value == "REDUCE"
    inputs["phase3n_hash"] = decision_fingerprint(reduced.as_dict())
    assert (
        "PHASE_3N_ALLOW" in qualify(phase3m=size, phase3n=reduced, decision_inputs=inputs).blockers
    )


def test_engine_output_must_match_shadow_bound_hash():
    from test_phase_3n_advanced_risk import _config, _request

    from kalshi_predictor.advanced_risk.engine import AdvancedRiskEngine

    allowed = AdvancedRiskEngine(_config()).decide(_request(phase_3m_contracts=1))
    assert "PHASE_3N_ALLOW" in qualify(phase3n=allowed).blockers


def structured_gate(
    gate=4,
    *,
    updated="2026-09-08T00:30:00Z",
    generated="2026-09-08T01:00:00Z",
    valid_to="2026-09-08T03:00:00Z",
    verifier=None,
    changes=None,
):
    base = "https://external-api.kalshi.com/trade-api/v2"
    ticker, event, series = "KXTEMPNYCH-TEST-T1", "KXTEMPNYCH-TEST", "KXTEMPNYCH"
    forecast_url = "https://api.weather.gov/gridpoints/OKX/34,45/forecast/hourly"
    bodies = {
        f"{base}/markets/{ticker}": {
            "market": {
                "ticker": ticker,
                "event_ticker": event,
                "status": "active",
                "close_time": "2026-09-08T02:00:00Z",
                "rules_primary": "The Weather Company (for coordinates KNYC)",
                "rules_secondary": None,
                "volume_fp": "20000",
                "open_interest_fp": "5000",
                "liquidity_dollars": "20000",
                "price_ranges": [{"start": "0", "end": "1", "step": "0.01"}],
            }
        },
        f"{base}/events/{event}": {"event": {"event_ticker": event, "series_ticker": series}},
        f"{base}/markets/{ticker}/orderbook": {
            "orderbook_fp": {
                "yes_dollars": [["0.4", "1000"]],
                "no_dollars": [["0.5", "1000"]],
            }
        },
        f"{base}/series/{series}": {
            "series": {
                "ticker": series,
                "category": "Climate and Weather",
                "contract_terms_url": "terms",
            }
        },
        "https://api.weather.gov/stations/KNYC": {
            "properties": {"stationIdentifier": "KNYC"},
            "geometry": {"coordinates": [-73.9667, 40.7833]},
        },
        "https://api.weather.gov/points/40.7833,-73.9667": {
            "properties": {"forecastHourly": forecast_url},
        },
        forecast_url: {
            "properties": {
                "generatedAt": generated,
                "updateTime": updated,
                "periods": [
                    {
                        "startTime": "2026-09-08T02:00:00Z",
                        "endTime": valid_to,
                        "temperature": 70,
                        "temperatureUnit": "F",
                    }
                ],
            }
        },
    }
    if changes:
        changes(bodies)
    sources = []
    for url, body in bodies.items():
        raw = json.dumps({"url": url, "received_at": "2026-09-08T01:00:00Z", "body": body}).encode()
        sources.append(EvidenceReference(url, hashlib.sha256(raw).hexdigest(), raw))
    inputs = dict(
        ticker=ticker,
        category="Climate and Weather",
        event_id=event,
        series=series,
        station="KNYC",
        settings={"opportunity_max_spread": "0.2"},
        config_hash=decision_fingerprint({"opportunity_max_spread": "0.2"}),
        side="BUY_YES",
        executable_price="0.5",
        source_hashes=[s.sha256 for s in sources],
        decision_at="2026-09-08T01:00:00Z",
        market_rules_hash=decision_fingerprint(
            {
                "primary": "The Weather Company (for coordinates KNYC)",
                "secondary": None,
                "contract_terms_url": "terms",
            }
        ),
    )
    verifiers = {
        1: "kalshi-public-market-v1",
        2: "kalshi-exact-catalog-identity-v1",
        4: "nws-hourly-both-clocks-v1",
        5: "kalshi-canonical-book-v1",
    }
    name = verifier or verifiers.get(gate, "fabricated-certification-v1")
    decision_id = decision_fingerprint(inputs)
    raw = json.dumps(
        dict(
            schema="overnight-paper-gate-v1",
            gate=gate,
            decision_id=decision_id,
            ticker=ticker,
            category=inputs["category"],
            verifier=name,
            verdict="PASS",
            sources=inputs["source_hashes"],
            validated_at=inputs["decision_at"],
            valid_until="2026-09-08T01:01:00Z",
        )
    ).encode()
    return GateEvidence(
        gate,
        decision_id,
        inputs["category"],
        ticker,
        name,
        EvidenceReference("semantic.json", hashlib.sha256(raw).hexdigest(), raw),
        sources=tuple(sources),
    ), inputs


@pytest.mark.parametrize("gate", [1, 2, 4, 5])
def test_registered_validators_recompute_structured_fixture_evidence(gate):
    item, inputs = structured_gate(gate)
    assert item.verified(inputs)
    assert not item.verified()  # No bound original decision is not enough.


@pytest.mark.parametrize(
    "changes",
    [
        {"updated": "2026-09-08T00:29:59Z"},
        {"generated": "2026-09-08T00:29:59Z"},
        {"updated": None},
        {"generated": "2026-09-08T01:00:01Z"},
        {"valid_to": "2026-09-08T02:00:00Z"},
        {"verifier": "unregistered-verifier"},
    ],
)
def test_fresh_report_cannot_certify_stale_missing_future_or_uncovered_source(changes):
    item, inputs = structured_gate(**changes)
    assert not item.verified(inputs)


def test_source_freshness_rechecked_at_activation_instead_of_trusting_ttl():
    item, inputs = structured_gate()
    assert item.verified(inputs)
    assert not item.verified(inputs, as_of=datetime(2026, 9, 8, 1, 0, 1, tzinfo=UTC))


@pytest.mark.parametrize("gate", [3, 9, 12])
def test_no_audited_rule_model_or_exchange_validator_means_fail_closed(gate):
    item, inputs = structured_gate(gate)
    assert not item.verified(inputs)


def test_original_source_hashes_must_be_bound_to_exact_decision():
    item, inputs = structured_gate()
    assert not item.verified(inputs | {"source_hashes": []})


def test_market_identity_conflict_is_not_hidden_by_valid_report():
    def change(bodies):
        for url, body in bodies.items():
            if "/events/" in url:
                body["event"]["series_ticker"] = "WRONG"

    item, inputs = structured_gate(2, changes=change)
    assert not item.verified(inputs)


def test_rule_text_change_requires_new_immutable_rule_hash():
    def change(bodies):
        for url, body in bodies.items():
            if "/markets/" in url and not url.endswith("/orderbook"):
                body["market"]["rules_primary"] = "different contract"

    item, inputs = structured_gate(2, changes=change)
    assert not item.verified(inputs)


def test_orderbook_gate_recomputes_executable_quote_not_report_pass():
    def change(bodies):
        for url, body in bodies.items():
            if url.endswith("/orderbook"):
                body["orderbook_fp"]["no_dollars"] = []

    item, inputs = structured_gate(5, changes=change)
    assert not item.verified(inputs)
