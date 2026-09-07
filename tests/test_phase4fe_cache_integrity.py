import importlib.util
import json
from pathlib import Path

import pytest

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

S = Path(__file__).parents[1] / "scripts/local/phase4fe_cache_integrity.py"
X = importlib.util.spec_from_file_location("m", S)
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
            "partition_hash": "f" * 64,
            "components": {k: canonical_hash(k) for k in M.COMPONENTS},
            "restored_manifest": None,
        }
    )


def test_miss_falls_back_clean():
    r = M.build_report(payload())
    assert r["restore_status"] == "MISS" and not r["cache_usable"]


def test_exact_manifest_valid():
    v = payload()
    identity = canonical_hash({k: v["components"][k] for k in M.COMPONENTS})
    v["restored_manifest"] = {"cache_identity": identity, "payload_hash": "a" * 64}
    seal(v)
    assert M.build_report(v)["cache_usable"]


@pytest.mark.parametrize("component", M.COMPONENTS)
def test_every_component_changes_identity(component):
    a = payload()
    b = payload()
    b["components"][component] = "a" * 64
    seal(b)
    assert M.build_report(a)["cache_identity"] != M.build_report(b)["cache_identity"]


def test_stale_manifest_rejected():
    v = payload()
    v["restored_manifest"] = {"cache_identity": "b" * 64, "payload_hash": "c" * 64}
    seal(v)
    r = M.build_report(v)
    assert r["restore_status"] == "REJECTED_STALE_OR_POISONED" and not r["cache_usable"]


@pytest.mark.parametrize("component", M.COMPONENTS)
def test_missing_component_fails(component):
    v = payload()
    v["components"].pop(component)
    seal(v)
    with pytest.raises(ValueError, match="COMPONENTS"):
        M.build_report(v)


def test_bad_digest_fails():
    v = payload()
    v["components"]["platform"] = "bad"
    seal(v)
    with pytest.raises(ValueError, match="DIGEST"):
        M.build_report(v)


def test_tamper_fails():
    v = payload()
    v["components"]["platform"] = "a" * 64
    with pytest.raises(ValueError, match="HASH"):
        M.build_report(v)


@pytest.mark.parametrize(
    "field",
    [
        "cache_written",
        "ci_config_changed",
        "production_database_mutated",
        "services_controlled",
        "execution_authorized",
    ],
)
def test_no_mutation(field):
    assert M.build_report(payload())[field] is False


def test_atomic(tmp_path):
    p = tmp_path / "x"
    r = M.build_report(payload())
    M.publish(p, r)
    assert json.loads(p.read_text()) == r
