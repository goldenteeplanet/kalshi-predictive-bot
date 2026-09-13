"""No sample count or fitted penalty can enter the support-only fallback."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from kalshi_predictor.crypto.uncertainty_contract import (
    UncertaintyStatus,
    binary_support_net_bounds,
    binary_support_uncertainty,
)


def test_support_bound_contains_every_binary_probability_and_is_sharp():
    for p in (Decimal(0), Decimal('.01'), Decimal('.5'), Decimal(1)):
        evidence, lower, upper = binary_support_net_bounds(
            selected_probability=p, executable_price=Decimal('.39'),
            fee=Decimal('.02'), snapshot_impact=Decimal('.004'), model='m',
            segment='SOL', assessed_at=datetime.now(UTC),
        )
        assert evidence.value == p
        assert evidence.status == UncertaintyStatus.CONSERVATIVE_BOUND
        assert evidence.independent_event_count is None
        assert not evidence.paper_support and not evidence.calibrated
        assert not evidence.execution_authority
        assert lower == Decimal('-.414') and upper == Decimal('.586')
        for q in (Decimal(0), Decimal('.001'), Decimal('.9'), Decimal(1)):
            assert lower <= q - Decimal('.414') <= upper
            assert p - q <= evidence.value


@pytest.mark.parametrize('p', ['NaN', 'Infinity', '-.01', '1.01'])
def test_invalid_probability_rejected(p):
    with pytest.raises(ValueError):
        binary_support_uncertainty(
            selected_probability=Decimal(p), model='m', segment='s',
            assessed_at=datetime.now(UTC),
        )


def test_naive_time_rejected():
    with pytest.raises(ValueError):
        binary_support_uncertainty(
            selected_probability=Decimal('.5'), model='m', segment='s',
            assessed_at=datetime(2026, 1, 1),
        )


@pytest.mark.parametrize('field', ['fee', 'snapshot_impact'])
def test_negative_cost_rejected(field):
    request = dict(
        selected_probability=Decimal('.5'), executable_price=Decimal('.4'),
        fee=Decimal('.02'), snapshot_impact=Decimal('0'), model='m',
        segment='s', assessed_at=datetime.now(UTC),
    )
    request[field] = Decimal('-.01')
    with pytest.raises(ValueError):
        binary_support_net_bounds(**request)
