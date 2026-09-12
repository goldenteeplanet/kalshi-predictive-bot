from dataclasses import replace

import pytest

from kalshi_predictor.crypto.settlement_rule_version import CryptoSettlementRuleVersion


def rule():
    return CryptoSettlementRuleVersion(
        family="KXSOLE",
        benchmark_id="SOLUSD_RTI",
        sample_frequency_ms=1000,
        sample_count=60,
        start_offset_ms=-60000,
        end_offset_ms=0,
        include_start=True,
        include_end=False,
        sample_precision=None,
        sample_rounding=None,
        average_precision=None,
        final_precision="0.0001",
        final_rounding="HALF_EVEN",
        tie_breaking=None,
        missing_sample_behavior=None,
        amendment_handling=None,
        finality=None,
        effective_from=None,
        effective_until=None,
        authority_version=None,
        field_evidence=(),
    )


def test_hypothesis_identity_is_not_certification():
    first = rule()
    other = replace(first, include_start=False, include_end=True)
    assert first.version_id != other.version_id
    assert first.status == "UNCERTIFIED"
    assert "sample_precision" in first.unresolved_fields
    assert first.bind(family="KXSOLE", benchmark_id="SOLUSD_RTI") == first.version_id
    with pytest.raises(ValueError, match="IDENTITY"):
        first.bind(family="KXBTC", benchmark_id="BRTI")


def test_inconsistent_grid_and_evidence_rejected():
    with pytest.raises(ValueError, match="COUNT"):
        replace(rule(), include_end=True)
    with pytest.raises(ValueError, match="UNIQUE"):
        replace(rule(), field_evidence=(("missing", "a" * 64),))
    with pytest.raises(ValueError, match="AWARE"):
        replace(rule(), effective_from="2026-09-12T01:00:00")
