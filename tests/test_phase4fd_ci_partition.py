import importlib.util
import json
from pathlib import Path

import pytest
from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

S = Path(__file__).parents[1] / "scripts/local/phase4fd_ci_partition.py"
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
            "permission_hash": "a" * 64,
            "shard_count": 2,
            "tests": [
                {"node_id": "a", "path": "tests/a.py", "duration_ms": 100, "dependencies": []},
                {"node_id": "b", "path": "tests/b.py", "duration_ms": 80, "dependencies": ["a"]},
                {"node_id": "c", "path": "tests/c.py", "duration_ms": 30, "dependencies": []},
            ],
        }
    )


def test_partition_is_balanced_and_full_suite_preserved():
    r = M.build_report(payload())
    assert sorted(x for s in r["shards"] for x in s["tests"]) == ["a", "b", "c"]
    assert r["full_merge_suite"] == ["a", "b", "c"]


def test_order_independent():
    a = payload()
    b = payload()
    b["tests"].reverse()
    seal(b)
    assert M.build_report(a)["shards"] == M.build_report(b)["shards"]


@pytest.mark.parametrize("count", [0, 33, True])
def test_invalid_shard_count(count):
    v = payload()
    v["shard_count"] = count
    seal(v)
    with pytest.raises(ValueError, match="SHARD_COUNT"):
        M.build_report(v)


def test_unknown_dependency_fails_closed():
    v = payload()
    v["tests"][0]["dependencies"] = ["z"]
    seal(v)
    with pytest.raises(ValueError, match="UNKNOWN_DEPENDENCY"):
        M.build_report(v)


def test_duplicate_fails_closed():
    v = payload()
    v["tests"].append(dict(v["tests"][0]))
    seal(v)
    with pytest.raises(ValueError, match="DUPLICATE"):
        M.build_report(v)


@pytest.mark.parametrize("duration", [-1, 1.2, True])
def test_bad_duration(duration):
    v = payload()
    v["tests"][0]["duration_ms"] = duration
    seal(v)
    with pytest.raises(ValueError, match="DURATION"):
        M.build_report(v)


def test_tamper():
    v = payload()
    v["shard_count"] = 3
    with pytest.raises(ValueError, match="HASH"):
        M.build_report(v)


@pytest.mark.parametrize(
    "field",
    [
        "tests_omitted",
        "ci_config_changed",
        "production_database_mutated",
        "services_controlled",
        "execution_authorized",
    ],
)
def test_safe(field):
    assert M.build_report(payload())[field] is False


def test_atomic(tmp_path):
    p = tmp_path / "x"
    r = M.build_report(payload())
    M.publish(p, r)
    assert json.loads(p.read_text()) == r
