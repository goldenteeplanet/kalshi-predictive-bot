"""One-contract observed-range stress estimate, never certified expected slippage."""

from decimal import Decimal

from kalshi_predictor.crypto.research_costs import CostComponent, EvidenceStatus, book_slippage


def one_contract_range_stress(
    *,
    executable_price: Decimal,
    depth: tuple[tuple[Decimal, Decimal], ...],
    recent_asks: tuple[Decimal, ...],
    evidence_hashes: tuple[str, ...],
    current_and_time_ordered: bool,
) -> CostComponent:
    """Depth consumption plus full observed range, including a jump to current ask.

    Receipt clocks and same-ticker/side binding must be validated by the caller.
    This does not certify immediate execution or model unobserved latency tails.
    A zero estimate from unchanged quotes is not a certified zero execution cost.
    """
    if recent_asks and recent_asks[-1] != executable_price:
        raise ValueError("CURRENT_PRICE_MUST_MATCH_LAST_QUOTE")
    base = book_slippage(
        executable_price=executable_price,
        depth=depth,
        recent_asks=recent_asks,
        evidence_hashes=evidence_hashes,
        current_and_time_ordered=current_and_time_ordered,
    )
    if base.value is None:
        return base
    previous_adverse = max(Decimal(0), max(recent_asks) - executable_price)
    observed_range = max(recent_asks) - min(recent_asks)
    return CostComponent(
        base.value - previous_adverse + observed_range,
        EvidenceStatus.ESTIMATED,
        "ONE_CONTRACT_DEPTH_PLUS_OBSERVED_ASK_RANGE_STRESS_V2",
        evidence_hashes,
        "DESCRIPTIVE_STRESS_NOT_EXPECTED_SLIPPAGE_OR_CERTIFIED_ZERO",
    )
