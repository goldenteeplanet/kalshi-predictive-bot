from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from kalshi_predictor.crypto.cost_evidence import OriginalBook
from kalshi_predictor.crypto.full_cost_evidence import assess_full_cost_evidence

AT = datetime(2026, 9, 12, 12, tzinfo=UTC)
DECISION = dict(
    ticker="KXSOLE-TEST", event_id="KXSOLE-EVENT", series="KXSOLE", side="BUY_YES",
    executable_price=".5", selected_probability=".7", decision_at=AT.isoformat(),
    model_version="TEST", segment="TEST",
)


def assess(**kwargs):
    return assess_full_cost_evidence(**(dict(
        decision=DECISION, selected_probability=Decimal(".7"), executable_price=Decimal(".5"),
        side="YES",
    ) | kwargs))


def test_all_unknowns_are_explicit_and_gross_is_not_net():
    result = assess()
    assert result.gross_edge == Decimal(".2")
    assert result.full_net_ev is None
    assert result.shortfall_to_five_cents is None
    assert result.full_net_ev_status == "FULL_NET_EV_UNKNOWN"
    assert result.qualification_status == "RULE_BLOCKED"
    assert not result.clears_net_gate and not result.execution_authority
    assert {"RULE_UNCERTIFIED", "FEE_APPLICABILITY_UNKNOWN", "EXPECTED_SLIPPAGE_UNKNOWN",
            "CALIBRATION_BLOCKED"}.issubset(result.blockers)


def test_measured_zero_book_stress_does_not_fill_missing_expected_cost():
    raw = b'{"orderbook_fp":{"yes_dollars":[[".4","2"]],"no_dollars":[[".5","2"]]}}'
    url = "https://external-api.kalshi.com/trade-api/v2/markets/KXSOLE-TEST/orderbook"
    result = assess(books=(OriginalBook(url, raw, AT-timedelta(seconds=1)),
                           OriginalBook(url, raw, AT)))
    assert result.observed_book_stress.value == 0
    assert not result.observed_book_stress.paper_support
    assert result.full_net_ev is None


def test_passing_labels_in_decision_do_not_certify_costs_or_rules():
    result = assess(decision=DECISION | dict(
        rule_certified=True, fee_status="CERTIFIED", full_net_ev=".2", paper_eligible=True,
    ))
    assert result.full_net_ev is None and result.qualification_status == "RULE_BLOCKED"


@pytest.mark.parametrize("changes", [
    {"side": "NO"}, {"selected_probability": Decimal(".8")},
    {"executable_price": Decimal(".4")},
])
def test_economic_inputs_bind_exactly_to_decision(changes):
    with pytest.raises(ValueError, match="BINDING_MISMATCH"):
        assess(**changes)
