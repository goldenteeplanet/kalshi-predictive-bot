from __future__ import annotations

import copy
from pathlib import Path

from scripts.local.phase4oa_aggregate_release_gate import build_release_candidate
from scripts.local.phase4ob_gate_mutation_resistance import (
    MUTATIONS,
    TARGET_FIELDS,
    independently_verify_candidate,
    mutate_candidate,
    run_mutation_campaign,
)
from tests.test_phase4oa_aggregate_release_gate import _checks

ROOT = Path(__file__).resolve().parents[1]


def _candidate():
    return build_release_candidate(ROOT, _checks())


def test_independent_verifier_accepts_only_trusted_complete_candidate() -> None:
    candidate = _candidate()
    result = independently_verify_candidate(
        candidate, trusted_manifest_sha256=candidate["manifest_sha256"], maximum_bytes=5_000_000
    )
    assert result["verdict"] == "PASS"


def test_all_mutations_are_deterministic_rejected_and_have_no_survivors() -> None:
    candidate = _candidate()
    result = run_mutation_campaign(
        candidate, trusted_manifest_sha256=candidate["manifest_sha256"], maximum_bytes=1_000_000
    )
    assert result["verdict"] == "PASS"
    assert result["mutation_count"] == len(MUTATIONS) == 26
    assert result["survivors"] == []
    assert set(result["target_fields"]) == TARGET_FIELDS
    assert result["verifier_coupled_to_primary"] is False


def test_recomputed_envelope_attack_still_fails_external_anchor() -> None:
    candidate = _candidate()
    mutated = mutate_candidate(candidate, "RECOMPUTED_ENVELOPE")
    assert mutated["manifest_sha256"] != candidate["manifest_sha256"]
    result = independently_verify_candidate(
        mutated, trusted_manifest_sha256=candidate["manifest_sha256"], maximum_bytes=5_000_000
    )
    assert "TRUSTED_MANIFEST_ANCHOR_MISMATCH" in result["errors"]


def test_incomplete_campaign_and_injected_verifier_survivor_refuse() -> None:
    candidate = _candidate()
    incomplete = run_mutation_campaign(
        candidate,
        trusted_manifest_sha256=candidate["manifest_sha256"],
        maximum_bytes=5_000_000,
        mutation_ids=MUTATIONS[:-1],
    )
    assert "MUTATION_COVERAGE_INCOMPLETE" in incomplete["errors"]

    def permissive(*args, **kwargs):
        return {"verdict": "PASS", "errors": [], "verification_sha256": "permissive"}

    survivor = run_mutation_campaign(
        candidate,
        trusted_manifest_sha256=candidate["manifest_sha256"],
        maximum_bytes=5_000_000,
        mutation_ids=("SAFETY_FLAG_ENABLE",),
        verifier=permissive,
    )
    assert "CERTIFICATION_BYPASS_SURVIVOR" in survivor["errors"]


def test_oversized_unknown_type_and_unicode_attacks_fail_closed() -> None:
    candidate = _candidate()
    oversized = mutate_candidate(candidate, "OVERSIZED_INPUT")
    assert (
        "CANDIDATE_RESOURCE_BOUND_EXCEEDED"
        in independently_verify_candidate(
            oversized, trusted_manifest_sha256=candidate["manifest_sha256"], maximum_bytes=1_000_000
        )["errors"]
    )
    for mutation in ("UNKNOWN_FIELD", "TYPE_CONFUSION", "UNICODE_COLLISION"):
        result = independently_verify_candidate(
            mutate_candidate(candidate, mutation),
            trusted_manifest_sha256=candidate["manifest_sha256"],
            maximum_bytes=5_000_000,
        )
        assert result["verdict"] == "REFUSE"


def test_campaign_is_input_preserving_and_has_no_execution_capability() -> None:
    candidate = _candidate()
    original = copy.deepcopy(candidate)
    result = run_mutation_campaign(
        candidate, trusted_manifest_sha256=candidate["manifest_sha256"], maximum_bytes=1_000_000
    )
    assert candidate == original
    safety = result["safety"]
    assert safety["offline_only"] is True
    assert all(value is False for key, value in safety.items() if key != "offline_only")
