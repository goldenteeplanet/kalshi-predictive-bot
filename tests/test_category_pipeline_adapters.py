from datetime import UTC, datetime, timedelta

import pytest

from kalshi_predictor.roadmap.category_adapters import (
    adapt_all_category_evidence,
    adapt_category_evidence,
)


def _complete_payload(**overrides):
    now = datetime.now(UTC)
    payload = {
        "source": {
            "name": "external-api",
            "kind": "external",
            "state": "READY",
            "published_at": (now - timedelta(minutes=3)).isoformat(),
            "available_at": (now - timedelta(minutes=2)).isoformat(),
            "ingested_at": (now - timedelta(minutes=1)).isoformat(),
        },
        "counts": {
            "active_markets": 2,
            "verified_links": 2,
            "fresh_snapshots": 2,
            "fresh_features": 2,
            "forecasts": 2,
            "rankings": 2,
            "opportunity_rows": 1,
            "risk_evidence_rows": 1,
            "complete_paper_traces": 1,
        },
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize("category", ["crypto", "weather"])
def test_external_live_v1_categories_normalize_as_live_eligible(category: str) -> None:
    evidence = adapt_category_evidence(category, _complete_payload())
    assert evidence.live_v1_allowed is True
    assert evidence.synthetic_or_manual_only is False
    assert evidence.deterministic_blockers == ()
    assert evidence.source_lineage["timestamp_order_valid"] is True


@pytest.mark.parametrize("category", ["sports", "economic", "news", "general"])
def test_non_v1_categories_are_normalized_but_not_live(category: str) -> None:
    evidence = adapt_category_evidence(category, _complete_payload())
    assert evidence.complete_paper_traces == 1
    assert evidence.live_v1_allowed is False
    assert evidence.synthetic_or_manual_only is False


@pytest.mark.parametrize("kind", ["manual", "file", "synthetic", "derived"])
def test_manual_and_synthetic_evidence_is_fail_closed_for_live(kind: str) -> None:
    payload = _complete_payload()
    payload["source"]["kind"] = kind
    evidence = adapt_category_evidence("crypto", payload)
    assert evidence.live_v1_allowed is False
    assert evidence.synthetic_or_manual_only is True
    assert "NON_EXTERNAL_EVIDENCE" in evidence.deterministic_blockers


def test_composite_is_always_paper_only() -> None:
    evidence = adapt_category_evidence("composite", _complete_payload())
    assert evidence.live_v1_allowed is False
    assert evidence.synthetic_or_manual_only is True
    assert evidence.deterministic_blockers[-2:] == (
        "NON_EXTERNAL_EVIDENCE",
        "COMPOSITE_PAPER_ONLY",
    )


def test_timestamp_order_is_enforced_with_deterministic_blockers() -> None:
    now = datetime.now(UTC)
    payload = _complete_payload()
    payload["source"].update(
        published_at=now.isoformat(),
        available_at=(now - timedelta(minutes=1)).isoformat(),
        ingested_at=(now - timedelta(minutes=2)).isoformat(),
    )
    evidence = adapt_category_evidence("weather", payload)
    assert evidence.live_v1_allowed is False
    assert evidence.deterministic_blockers[:2] == (
        "PUBLISHED_AFTER_AVAILABLE",
        "AVAILABLE_AFTER_INGESTED",
    )
    assert evidence.source_lineage["timestamp_order_valid"] is False


def test_missing_counts_and_times_emit_stable_funnel_blockers() -> None:
    evidence = adapt_category_evidence(
        "news", {"source_name": "rss", "source_state": "NO_DATA"}
    )
    assert evidence.deterministic_blockers[:3] == (
        "SOURCE_AVAILABLE_AT_MISSING",
        "INGESTED_AT_MISSING",
        "SOURCE_NO_DATA",
    )
    assert evidence.deterministic_blockers[3:] == (
        "NO_ACTIVE_MARKETS",
        "NO_VERIFIED_LINKS",
        "NO_FRESH_SNAPSHOTS",
        "NO_FRESH_FEATURES",
        "NO_FORECASTS",
        "NO_RANKINGS",
        "NO_OPPORTUNITIES",
        "NO_RISK_EVIDENCE",
        "NO_COMPLETE_PAPER_TRACE",
    )


def test_batch_adapter_uses_canonical_category_order_and_rejects_unknown() -> None:
    rows = adapt_all_category_evidence(
        {"news": _complete_payload(), "crypto": _complete_payload()}
    )
    assert [row.category for row in rows] == ["crypto", "news"]
    with pytest.raises(ValueError, match="Unknown category"):
        adapt_category_evidence("politics", _complete_payload())
