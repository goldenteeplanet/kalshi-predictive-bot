from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

from scripts.local.phase4oc_evidence_freshness import issue_freshness_proof
from scripts.local.phase4od_renewal_race_resistance import (
    adjudicate_renewal_round,
    propose_renewal,
)

MANIFEST = "c" * 64
START = datetime(2026, 8, 29, 12, tzinfo=UTC)


def _prior():
    return issue_freshness_proof(aggregate_manifest_sha256=MANIFEST, observed_at=START)


def _proposal(prior, witness="witness-a", when=START + timedelta(hours=1)):
    return propose_renewal(
        prior,
        witness_id=witness,
        trusted_manifest_sha256=MANIFEST,
        trusted_time=when,
        watermark=START,
    )


def test_identical_concurrent_children_collapse_to_one_canonical_renewal() -> None:
    prior = _prior()
    proposals = [_proposal(prior, "witness-a"), _proposal(prior, "witness-b")]
    result = adjudicate_renewal_round(
        prior,
        proposals,
        trusted_manifest_sha256=MANIFEST,
        evaluated_at=START + timedelta(hours=1),
        prior_watermark=START,
    )
    assert result["verdict"] == "PASS"
    assert len(result["child_proof_sha256s"]) == 1
    assert result["canonical_renewal"]["sequence"] == 2


def test_conflicting_concurrent_children_fail_closed_as_split_brain() -> None:
    prior = _prior()
    proposals = [
        _proposal(prior, "witness-a", START + timedelta(hours=1)),
        _proposal(prior, "witness-b", START + timedelta(hours=1, seconds=1)),
    ]
    result = adjudicate_renewal_round(
        prior,
        proposals,
        trusted_manifest_sha256=MANIFEST,
        evaluated_at=START + timedelta(hours=1, minutes=1),
        prior_watermark=START,
    )
    assert result["verdict"] == "REFUSE"
    assert "SPLIT_BRAIN_CHILDREN" in result["errors"]
    assert result["canonical_renewal"] is None


def test_witness_and_adjudicator_clock_rollback_refuse() -> None:
    prior = _prior()
    rolled_back = propose_renewal(
        prior,
        witness_id="witness-a",
        trusted_manifest_sha256=MANIFEST,
        trusted_time=START - timedelta(seconds=1),
        watermark=START,
    )
    assert rolled_back["verdict"] == "REFUSE"
    assert "TRUSTED_CLOCK_ROLLBACK" in rolled_back["errors"]
    result = adjudicate_renewal_round(
        prior,
        [_proposal(prior)],
        trusted_manifest_sha256=MANIFEST,
        evaluated_at=START - timedelta(seconds=1),
        prior_watermark=START,
    )
    assert result["verdict"] == "REFUSE"
    assert "ADJUDICATOR_CLOCK_ROLLBACK" in result["errors"]


def test_stale_watermark_duplicate_witness_wrong_parent_and_tamper_refuse() -> None:
    prior = _prior()
    proposal = _proposal(prior)
    stale = copy.deepcopy(proposal)
    stale["watermark"] = (START - timedelta(seconds=1)).isoformat()
    tampered = copy.deepcopy(proposal)
    tampered["prior_proof_sha256"] = "d" * 64
    for proposals, expected in (
        ([proposal, proposal], "DUPLICATE_WITNESS"),
        ([stale], "PROPOSAL_HASH_MISMATCH"),
        ([tampered], "STALE_OR_WRONG_PARENT"),
    ):
        result = adjudicate_renewal_round(
            prior,
            proposals,
            trusted_manifest_sha256=MANIFEST,
            evaluated_at=START + timedelta(hours=1),
            prior_watermark=START,
        )
        assert result["verdict"] == "REFUSE"
        assert expected in result["errors"]


def test_expired_prior_and_empty_round_cannot_produce_canonical_child() -> None:
    prior = _prior()
    expired = propose_renewal(
        prior,
        witness_id="witness-a",
        trusted_manifest_sha256=MANIFEST,
        trusted_time=START + timedelta(hours=6),
        watermark=START,
    )
    assert expired["verdict"] == "REFUSE"
    assert "PRIOR_NOT_RENEWABLE" in expired["errors"]
    result = adjudicate_renewal_round(
        prior,
        [],
        trusted_manifest_sha256=MANIFEST,
        evaluated_at=START + timedelta(hours=1),
        prior_watermark=START,
    )
    assert result["canonical_renewal"] is None
    assert "RENEWAL_PROPOSALS_EMPTY" in result["errors"]


def test_round_is_deterministic_order_independent_input_preserving_and_safe() -> None:
    prior = _prior()
    proposals = [_proposal(prior, "witness-b"), _proposal(prior, "witness-a")]
    original = copy.deepcopy((prior, proposals))
    kwargs = {
        "trusted_manifest_sha256": MANIFEST,
        "evaluated_at": START + timedelta(hours=1),
        "prior_watermark": START,
    }
    forward = adjudicate_renewal_round(prior, proposals, **kwargs)
    reverse = adjudicate_renewal_round(prior, list(reversed(proposals)), **kwargs)
    assert forward == reverse
    assert (prior, proposals) == original
    assert forward["safety"]["offline_only"] is True
    assert all(value is False for key, value in forward["safety"].items() if key != "offline_only")
