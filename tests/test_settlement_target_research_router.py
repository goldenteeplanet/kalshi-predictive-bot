import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from kalshi_predictor.crypto.cf_settlement_windows import CFWindow, CFWindowRules
from kalshi_predictor.crypto.settlement_target import SettlementBenchmarkTarget
from kalshi_predictor.forecasting.crypto_research_router import (
    ResearchExecutionScenario,
    analyze_crypto_research,
)
from kalshi_predictor.forecasting.crypto_v3_independent import CryptoTarget, PriceObservation
from kalshi_predictor.forecasting.model_roles import ModelRole, research_model_role

NOW = datetime(2026, 9, 11, 10, tzinfo=UTC)
END = NOW + timedelta(minutes=5)


def target():
    raw = b"Synthetic rule bytes; not authoritative certification"
    rules = CFWindowRules(
        "KXBTC-E-T100",
        "BRTI",
        CFWindow(
            int(END.timestamp() * 1000) - 60000, int(END.timestamp() * 1000), True, False, 1000, 60
        ),
        None,
        2,
        "HALF_UP",
        "REJECT",
        "https://assets.kalshi.com/contract_terms/BTC.pdf",
        hashlib.sha256(raw).hexdigest(),
    )
    market = json.dumps(
        dict(
            market=dict(
                ticker=rules.market_ticker,
                event_ticker="KXBTC-E",
                market_type="binary",
                close_time=END.isoformat(),
            )
        )
    ).encode()
    return SettlementBenchmarkTarget(
        "BTC",
        "KXBTC-E",
        rules,
        "ABOVE",
        Decimal("100"),
        None,
        None,
        raw,
        NOW,
        market,
        NOW,
        None,
        "UNRESOLVED",
    )


def prices():
    return [
        PriceObservation(
            100 * (1 + (i % 5 - 2) * 0.001),
            NOW - timedelta(minutes=300 - i),
            NOW - timedelta(minutes=300 - i),
            "coinbase",
            "a" * 64,
            "BTC",
        )
        for i in range(301)
    ]


def test_declared_target_exact_window_and_unknown_finality_not_certification():
    t = target()
    p = t.validate(as_of=NOW)
    assert len(t.rules.closing.timestamps()) == 60
    assert p["target_variable"] == "CF_ARITHMETIC_WINDOW_AVERAGE"
    assert p["finality_deadline"] is None and not p["paper_eligible"]
    other = replace(
        t,
        rules=replace(
            t.rules, closing=replace(t.rules.closing, include_start=False, include_end=True)
        ),
    )
    assert other.rules.closing.timestamps() != t.rules.closing.timestamps()
    assert other.validate(as_of=NOW)["rules"] != p["rules"]


@pytest.mark.parametrize(
    "change",
    [
        lambda t: replace(t, rule_original=b"changed"),
        lambda t: replace(t, rule_received_at=NOW + timedelta(seconds=1)),
        lambda t: replace(t, market_received_at=NOW + timedelta(seconds=1)),
        lambda t: replace(t, threshold=True),
        lambda t: replace(t, rules=replace(t.rules, decimal_places=True)),
        lambda t: replace(t, rules=replace(t.rules, rounding="UNKNOWN")),
        lambda t: replace(
            t, rules=replace(t.rules, closing=replace(t.rules.closing, expected_ticks=59))
        ),
        lambda t: replace(t, event_ticker="OTHER"),
        lambda t: replace(t, finality_basis="CERTIFIED"),
        lambda t: replace(t, finality_deadline=NOW, finality_basis="DECLARED_UNCERTIFIED"),
        lambda t: replace(t, rule_received_at=NOW.replace(tzinfo=None)),
        lambda t: replace(t, market_original=t.market_original.replace(b'"binary"', b'"scalar"')),
        lambda t: replace(t, market_original=b'{"market":{},"market":{}}'),
    ],
)
def test_invalid_target_evidence_fails_closed(change):
    with pytest.raises(ValueError):
        change(target()).validate(as_of=NOW)


def test_average_window_cannot_be_backfilled_as_future_target():
    with pytest.raises(ValueError, match="PREWINDOW"):
        target().validate(as_of=END - timedelta(seconds=30))


def test_roles_do_not_promote_and_unknown_model_refuses():
    assert research_model_role("crypto_v2") is ModelRole.BASELINE
    assert research_model_role("crypto_v3_independent") is ModelRole.RESEARCH_CHALLENGER
    assert set(ModelRole) == {
        ModelRole.BASELINE,
        ModelRole.RESEARCH_CHALLENGER,
        ModelRole.PAPER_ELIGIBLE,
        ModelRole.PRODUCTION_APPROVED,
    }
    with pytest.raises(ValueError):
        research_model_role("unregistered")


def test_actual_v3_route_is_quote_independent_and_never_average_or_paper():
    t = target()
    proxy = CryptoTarget(
        "BTC", "ABOVE", END, threshold=100, benchmark="BRTI", rule_sha256=t.rules.rule_sha256
    )
    result = analyze_crypto_research(
        prices=prices(),
        proxy_target=proxy,
        decision_at=NOW,
        settlement_target=t,
        scenario=ResearchExecutionScenario(0.2, 0.3, 0.01, 0.01, 0.01, 0.1),
    ).decode()
    changed = analyze_crypto_research(
        prices=prices(),
        proxy_target=proxy,
        decision_at=NOW,
        settlement_target=t,
        scenario=ResearchExecutionScenario(0.7, 0.8, 0.02, 0.02, 0.02, 0.2),
    ).decode()
    assert result["forecast"]["probability"] == changed["forecast"]["probability"]
    assert result["scenario_comparisons"] != changed["scenario_comparisons"]
    assert result["model_role"] == "RESEARCH_CHALLENGER"
    assert not result["paper_eligible"] and not result["shadow_decision_written"]
    assert result["prediction_recorded_at"] is None
    assert result["first_blocker"] == "SETTLEMENT_ALIGNMENT"


def test_target_precision_cannot_be_lost_during_proxy_binding():
    t = replace(target(), threshold=Decimal("100.000000000000000001"))
    proxy = CryptoTarget(
        "BTC", "ABOVE", END, threshold=100, benchmark="BRTI", rule_sha256=t.rules.rule_sha256
    )
    with pytest.raises(ValueError, match="PROXY_STRIKE"):
        analyze_crypto_research(
            prices=prices(), proxy_target=proxy, decision_at=NOW, settlement_target=t
        )
