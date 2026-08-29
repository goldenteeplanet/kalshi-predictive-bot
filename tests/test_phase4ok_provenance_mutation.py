from __future__ import annotations

import copy

from scripts.local.phase4oh_freeze_durability import append_event
from scripts.local.phase4oi_dual_copy_repair import create_anchor, create_copy, reconcile_copies
from scripts.local.phase4oj_multi_anchor_provenance import (
    attest_anchor,
    bind_recovery_provenance,
    certify_anchor_quorum,
)
from scripts.local.phase4ok_provenance_mutation import (
    MUTATIONS,
    independently_verify_provenance,
    mutate,
    run_campaign,
)


def _fixture():
    journal = append_event(
        [], event_type="FREEZE", evidence_sha256="a" * 64, evidence_verdict="PASS"
    )
    anchor = create_anchor(journal, generation=1)
    copies = [create_copy("a", journal, None), create_copy("b", journal, None)]
    reconciliation = reconcile_copies(copies, anchor)
    rows = [attest_anchor(anchor, authority_id=name) for name in ("anchor-a", "anchor-b")]
    certificate = certify_anchor_quorum(
        rows,
        allowed_authorities={"anchor-a", "anchor-b", "anchor-c"},
        quorum=2,
        minimum_generation=1,
    )
    provenance = bind_recovery_provenance(copies, anchor, certificate)
    kwargs = {
        "trusted_certificate_sha256": certificate["certificate_sha256"],
        "trusted_anchor_sha256": anchor["anchor_sha256"],
        "trusted_reconciliation_sha256": reconciliation["reconciliation_sha256"],
        "trusted_canonical_copy_sha256": reconciliation["canonical_copy_sha256"],
        "maximum_bytes": 1_000_000,
    }
    return provenance, certificate, kwargs


def test_valid_envelope_passes_independent_verifier() -> None:
    provenance, certificate, kwargs = _fixture()
    assert independently_verify_provenance(provenance, certificate, **kwargs)["verdict"] == "PASS"


def test_all_mutations_are_deterministically_rejected_with_zero_survivors() -> None:
    provenance, certificate, kwargs = _fixture()
    result = run_campaign(provenance, certificate, **kwargs)
    assert result["verdict"] == "PASS"
    assert result["mutation_count"] == len(MUTATIONS) == 18
    assert result["survivors"] == []
    assert result["independent_of_primary_acceptance"] is True
    assert all(row["verdict"] == "REFUSE" and row["deterministic"] for row in result["results"])


def test_external_anchors_defeat_recomputed_envelopes() -> None:
    provenance, certificate, kwargs = _fixture()
    mutated_p, mutated_c = mutate(provenance, certificate, "SOURCE_ANCHOR_DRIFT")
    result = independently_verify_provenance(mutated_p, mutated_c, **kwargs)
    assert "EXTERNAL_TRUST_ANCHOR_MISMATCH" in result["errors"]


def test_resource_identity_ordering_and_capability_attacks_refuse() -> None:
    provenance, certificate, kwargs = _fixture()
    for mutation_id in (
        "OVERSIZED_INPUT",
        "AUTHORITY_IDENTITY_DUPLICATE",
        "ATTESTATION_REORDER",
        "CAPABILITY_ENABLE",
    ):
        result = independently_verify_provenance(
            *mutate(provenance, certificate, mutation_id), **kwargs
        )
        assert result["verdict"] == "REFUSE"


def test_campaign_is_input_preserving_and_execution_free() -> None:
    provenance, certificate, kwargs = _fixture()
    original = copy.deepcopy((provenance, certificate))
    result = run_campaign(provenance, certificate, **kwargs)
    assert (provenance, certificate) == original
    assert result["safety"]["offline_only"] is True
    assert all(value is False for key, value in result["safety"].items() if key != "offline_only")
