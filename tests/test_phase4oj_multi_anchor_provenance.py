from __future__ import annotations

import copy

from scripts.local.phase4oh_freeze_durability import append_event
from scripts.local.phase4oi_dual_copy_repair import create_anchor, create_copy
from scripts.local.phase4oj_multi_anchor_provenance import (
    attest_anchor,
    bind_recovery_provenance,
    certify_anchor_quorum,
)

AUTHORITIES = {"anchor-a", "anchor-b", "anchor-c"}


def _fixture():
    journal = append_event(
        [], event_type="FREEZE", evidence_sha256="a" * 64, evidence_verdict="PASS"
    )
    anchor = create_anchor(journal, generation=1)
    copies = [create_copy("copy-a", journal, None), create_copy("copy-b", journal, None)]
    return journal, anchor, copies


def _certificate(anchor, authorities=("anchor-a", "anchor-b"), **overrides):
    rows = [attest_anchor(anchor, authority_id=authority) for authority in authorities]
    values = {
        "allowed_authorities": AUTHORITIES,
        "quorum": 2,
        "minimum_generation": 1,
    }
    values.update(overrides)
    return certify_anchor_quorum(rows, **values)


def test_quorum_certificate_and_recovery_provenance_pass_frozen() -> None:
    _, anchor, copies = _fixture()
    certificate = _certificate(anchor)
    assert certificate["verdict"] == "PASS"
    result = bind_recovery_provenance(copies, anchor, certificate)
    assert result["verdict"] == "PASS"
    assert result["state"] == "FROZEN"
    assert result["capabilities_allowed"] is False


def test_insufficient_quorum_replay_unknown_authority_and_stale_generation_refuse() -> None:
    _, anchor, _ = _fixture()
    assert _certificate(anchor, ("anchor-a",))["verdict"] == "REFUSE"
    replay = _certificate(anchor, ("anchor-a", "anchor-a"))
    assert "ANCHOR_AUTHORITY_REPLAY" in replay["errors"]
    unknown = _certificate(anchor, ("anchor-a", "outsider"))
    assert "ANCHOR_AUTHORITY_NOT_ALLOWED" in unknown["errors"]
    stale = _certificate(anchor, minimum_generation=2)
    assert "ANCHOR_GENERATION_STALE" in stale["errors"]


def test_equivocation_and_mixed_history_fail_closed() -> None:
    journal, anchor, _ = _fixture()
    alternate_journal = append_event(
        journal, event_type="FREEZE", evidence_sha256="b" * 64, evidence_verdict="PASS"
    )
    alternate = create_anchor(alternate_journal, generation=2)
    rows = [
        attest_anchor(anchor, authority_id="anchor-a"),
        attest_anchor(alternate, authority_id="anchor-a"),
        attest_anchor(anchor, authority_id="anchor-b"),
    ]
    result = certify_anchor_quorum(
        rows, allowed_authorities=AUTHORITIES, quorum=2, minimum_generation=1
    )
    assert result["verdict"] == "REFUSE"
    assert "ANCHOR_EQUIVOCATION" in result["errors"]
    mixed = certify_anchor_quorum(
        [
            attest_anchor(anchor, authority_id="anchor-a"),
            attest_anchor(alternate, authority_id="anchor-b"),
        ],
        allowed_authorities=AUTHORITIES,
        quorum=2,
        minimum_generation=1,
    )
    assert "ANCHOR_QUORUM_NOT_UNIQUE" in mixed["errors"]


def test_tampering_and_wrong_provenance_binding_refuse() -> None:
    _, anchor, copies = _fixture()
    certificate = _certificate(anchor)
    tampered = copy.deepcopy(certificate)
    tampered["generation"] = 2
    result = bind_recovery_provenance(copies, anchor, tampered)
    assert result["verdict"] == "REFUSE"
    assert result["state"] == "FROZEN"
    assert "ANCHOR_CERTIFICATE_HASH_MISMATCH" in result["errors"]
    other = copy.deepcopy(anchor)
    other["anchor_sha256"] = "f" * 64
    result = bind_recovery_provenance(copies, other, certificate)
    assert "ANCHOR_CERTIFICATE_BINDING_MISMATCH" in result["errors"]


def test_certificate_and_provenance_are_order_independent_and_execution_free() -> None:
    _, anchor, copies = _fixture()
    rows = [
        attest_anchor(anchor, authority_id="anchor-b"),
        attest_anchor(anchor, authority_id="anchor-a"),
    ]
    kwargs = {"allowed_authorities": AUTHORITIES, "quorum": 2, "minimum_generation": 1}
    forward = certify_anchor_quorum(rows, **kwargs)
    reverse = certify_anchor_quorum(list(reversed(rows)), **kwargs)
    assert forward == reverse
    result = bind_recovery_provenance(copies, anchor, forward)
    assert result["safety"]["offline_only"] is True
    assert all(value is False for key, value in result["safety"].items() if key != "offline_only")
