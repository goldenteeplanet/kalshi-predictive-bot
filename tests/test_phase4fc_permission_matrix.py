import importlib.util
import json
from pathlib import Path

import pytest

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

S = Path(__file__).parents[1] / "scripts/local/phase4fc_permission_matrix.py"
X = importlib.util.spec_from_file_location("p", S)
assert X and X.loader
M = importlib.util.module_from_spec(X)
X.loader.exec_module(M)


def seal(v):
    v.pop("artifact_hash", None)
    v["artifact_hash"] = canonical_hash(v)
    return v


def payload():
    return seal(
        {
            "schema": M.SCHEMA,
            "recommendation_hash": "a" * 64,
            "roles": {k: dict(v) for k, v in M.MAXIMUM.items()},
        }
    )


def test_valid_matrix_is_deterministic():
    r = M.build_report(payload())
    assert list(r["roles"]) == list(M.ROLES)
    assert not r["administration_granted"]


@pytest.mark.parametrize("role", M.ROLES)
def test_each_role_is_required(role):
    v = payload()
    v["roles"].pop(role)
    seal(v)
    with pytest.raises(ValueError, match="ROLES"):
        M.build_report(v)


@pytest.mark.parametrize("permission", sorted(M.FORBIDDEN))
def test_broad_permissions_are_refused(permission):
    v = payload()
    v["roles"]["ci"][permission] = "read"
    seal(v)
    with pytest.raises(ValueError, match="BROAD_PERMISSION_REFUSED"):
        M.build_report(v)


@pytest.mark.parametrize("role", M.ROLES)
def test_unknown_permission_is_refused(role):
    v = payload()
    v["roles"][role]["issues"] = "read"
    seal(v)
    with pytest.raises(ValueError, match="PERMISSION_INVALID"):
        M.build_report(v)


def test_excess_permission_is_refused():
    v = payload()
    v["roles"]["ci"]["contents"] = "write"
    seal(v)
    with pytest.raises(ValueError, match="EXCESS"):
        M.build_report(v)


def test_tampering_fails_closed():
    v = payload()
    v["roles"]["ci"] = {"contents": "none"}
    with pytest.raises(ValueError, match="HASH"):
        M.build_report(v)


@pytest.mark.parametrize(
    "field",
    [
        "organization_scope_granted",
        "administration_granted",
        "settings_changed",
        "apps_authorized",
        "production_database_mutated",
        "services_controlled",
        "exchange_requests_made",
        "execution_authorized",
    ],
)
def test_no_privileged_mutation(field):
    assert M.build_report(payload())[field] is False


def test_atomic(tmp_path):
    r = M.build_report(payload())
    p = tmp_path / "x.json"
    M.publish(p, r)
    assert json.loads(p.read_text()) == r


def test_no_connected_surface():
    s = S.read_text()
    assert "requests." not in s and "subprocess" not in s and "/home/james" not in s
