from __future__ import annotations

import copy

from scripts.local.phase4mt_quorum_gossip import audit_gossip, evaluate_quorum, make_statement

IDENTITY = "a" * 64
HEAD = "b" * 64
STORE = "c" * 64
REGISTRY = {
    "witness-a": {"independence_group": "org-a", "revoked": False},
    "witness-b": {"independence_group": "org-b", "revoked": False},
    "witness-c": {"independence_group": "org-c", "revoked": False},
    "witness-a2": {"independence_group": "org-a", "revoked": False},
}


def _statement(witness, **overrides):
    values = {
        "witness_id": witness,
        "generation": 3,
        "head_record_sha256": HEAD,
        "store_sha256": STORE,
        "implementation_identity_sha256": IDENTITY,
        "observed_at": "2026-08-29T03:00:00Z",
    }
    values.update(overrides)
    return make_statement(**values)


def _evaluate(statements, registry=None, threshold=2):
    return evaluate_quorum(
        statements,
        registry or REGISTRY,
        threshold=threshold,
        expected_generation=3,
        expected_head_record_sha256=HEAD,
        expected_store_sha256=STORE,
        expected_implementation_identity_sha256=IDENTITY,
        evaluated_at="2026-08-29T03:05:00Z",
        max_age_seconds=600,
    )


def test_distinct_independent_fresh_witnesses_form_deterministic_quorum() -> None:
    statements = [_statement("witness-a"), _statement("witness-b")]
    first = _evaluate(statements)
    assert first == _evaluate(statements)
    assert first["verdict"] == "PASS"
    assert first["independent_count"] == 2
    assert first["quorum_attested"] is True


def test_duplicate_and_same_group_witnesses_do_not_inflate_quorum() -> None:
    duplicate = _statement("witness-a")
    assert "DUPLICATE_STATEMENT" in " ".join(
        _evaluate([duplicate, copy.deepcopy(duplicate)])["errors"]
    )
    colluding = _evaluate([_statement("witness-a"), _statement("witness-a2")])
    assert colluding["verdict"] == "REFUSE"
    assert "COLLUDING_OR_NONINDEPENDENT_WITNESSES" in colluding["errors"]


def test_stale_future_unknown_and_revoked_witnesses_refuse() -> None:
    assert "STATEMENT_STALE" in " ".join(
        _evaluate([_statement("witness-a", observed_at="2026-08-29T02:00:00Z")])["errors"]
    )
    assert "OBSERVATION_FROM_FUTURE" in " ".join(
        _evaluate([_statement("witness-a", observed_at="2026-08-29T04:00:00Z")])["errors"]
    )
    assert "WITNESS_UNKNOWN" in " ".join(_evaluate([_statement("unknown")])["errors"])
    registry = copy.deepcopy(REGISTRY)
    registry["witness-a"]["revoked"] = True
    assert "WITNESS_REVOKED" in " ".join(_evaluate([_statement("witness-a")], registry)["errors"])


def test_conflicting_head_identity_and_insufficient_quorum_refuse() -> None:
    conflict = _evaluate(
        [_statement("witness-a"), _statement("witness-b", head_record_sha256="d" * 64)]
    )
    assert conflict["verdict"] == "REFUSE"
    assert "HEAD_OR_IDENTITY_MISMATCH" in " ".join(conflict["errors"])
    identity = _evaluate(
        [_statement("witness-a"), _statement("witness-b", implementation_identity_sha256="e" * 64)]
    )
    assert "HEAD_OR_IDENTITY_MISMATCH" in " ".join(identity["errors"])
    assert "INSUFFICIENT_INDEPENDENT_QUORUM" in _evaluate([_statement("witness-a")])["errors"]


def test_single_witness_equivocation_is_explicitly_detected() -> None:
    result = _evaluate(
        [
            _statement("witness-a"),
            _statement("witness-a", head_record_sha256="d" * 64),
            _statement("witness-b"),
        ]
    )
    assert result["verdict"] == "REFUSE"
    assert "WITNESS_EQUIVOCATION" in result["errors"]
    assert result["equivocating_witnesses"] == ["witness-a"]


def test_partition_and_delayed_gossip_can_converge_without_conflict() -> None:
    rounds = [
        {"node-a": [HEAD], "node-b": []},
        {"node-a": [HEAD], "node-b": [HEAD]},
    ]
    result = audit_gossip(rounds, required_nodes=["node-a", "node-b"])
    assert result["verdict"] == "PASS"
    assert result["converged_single_head"] is True
    assert result["rounds"][0]["partitioned"] is True


def test_conflicting_gossip_never_auto_reconciles_even_after_exchange() -> None:
    other = "d" * 64
    rounds = [
        {"node-a": [HEAD], "node-b": [other]},
        {"node-a": [HEAD, other], "node-b": [HEAD, other]},
    ]
    result = audit_gossip(rounds, required_nodes=["node-a", "node-b"])
    assert result["verdict"] == "REFUSE"
    assert "CONFLICT_OBSERVED_NO_AUTOMATIC_RECONCILIATION" in result["errors"]
    assert result["reconciliation_authorized"] is False


def test_gossip_knowledge_rollback_and_nonconvergence_refuse() -> None:
    rollback = [
        {"node-a": [HEAD], "node-b": [HEAD]},
        {"node-a": [], "node-b": [HEAD]},
    ]
    result = audit_gossip(rollback, required_nodes=["node-a", "node-b"])
    assert result["verdict"] == "REFUSE"
    assert any("KNOWLEDGE_ROLLBACK" in error for error in result["errors"])
    assert "GOSSIP_NOT_CONVERGED" in result["errors"]


def test_quorum_and_gossip_grant_no_operational_capability() -> None:
    quorum = _evaluate([_statement("witness-a"), _statement("witness-b")])
    assert quorum["reconciliation_authorized"] is False
    safety = quorum["safety"]
    assert safety["simulation_only"] is True
    assert all(value is False for key, value in safety.items() if key != "simulation_only")
