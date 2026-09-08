from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4ca_catalog_delta_planner.py"
    spec = importlib.util.spec_from_file_location("phase4ca_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(module, market_id, revision):
    return {
        "market_id": market_id,
        "revision": revision,
        "content_hash": module.canonical_hash([market_id, revision]),
    }


def _state(module, *, cycle=3, force=False, previous=None, discovered=None):
    state = {
        "schema": module.INPUT_SCHEMA,
        "incremental_cycles_since_full": cycle,
        "force_full_reconciliation": force,
        "previous_catalog": (
            previous if previous is not None else [_row(module, "A", 1), _row(module, "B", 1)]
        ),
        "discovered_catalog": (
            discovered
            if discovered is not None
            else [_row(module, "A", 1), _row(module, "B", 2), _row(module, "C", 1)]
        ),
    }
    state["artifact_hash"] = module._hash(state)
    return state


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_incremental_plan_is_deterministic_and_avoids_unchanged_refetches():
    module = _module()
    plan = module.build_plan(_state(module))
    assert plan == module.build_plan(_state(module))
    assert plan["mode"] == "INCREMENTAL"
    assert plan["fetch_market_ids"] == ["B", "C"]
    assert plan["unchanged_market_ids"] == ["A"]
    assert plan["avoided_refetch_count"] == 1
    assert plan["execution_authorized"] is False


@pytest.mark.parametrize("reason", ("forced", "periodic", "initial"))
def test_full_reconciliation_checkpoints_fetch_all_discovered(reason: str):
    module = _module()
    kwargs = {}
    if reason == "forced":
        kwargs["force"] = True
    elif reason == "periodic":
        kwargs["cycle"] = module.MAX_INCREMENTAL_CYCLES
    else:
        kwargs["previous"] = []
    plan = module.build_plan(_state(module, **kwargs))
    assert plan["mode"] == "FULL_RECONCILIATION"
    assert plan["fetch_market_ids"] == ["A", "B", "C"]
    assert plan["next_incremental_cycles_since_full"] == 0


def test_removed_markets_are_explicit_and_sorted():
    module = _module()
    previous = [_row(module, "Z", 1), _row(module, "A", 1)]
    discovered = [_row(module, "A", 1)]
    plan = module.build_plan(_state(module, previous=previous, discovered=discovered))
    assert plan["removed_market_ids"] == ["Z"]


@pytest.mark.parametrize("catalog", ("previous_catalog", "discovered_catalog"))
def test_duplicate_and_malformed_catalog_entries_fail_closed(catalog: str):
    module = _module()
    for mutation in (
        lambda rows: rows.append(dict(rows[0])),
        lambda rows: rows[0].update(revision=True),
        lambda rows: rows[0].update(content_hash="bad"),
        lambda rows: rows[0].update(extra=True),
    ):
        state = _state(module)
        mutation(state[catalog])
        _rehash(module, state)
        with pytest.raises(ValueError):
            module.build_plan(state)


@pytest.mark.parametrize("cycle", (-1, True, "1"))
def test_invalid_cycle_fails_closed(cycle):
    module = _module()
    state = _state(module, cycle=cycle)
    with pytest.raises(ValueError, match="CYCLE"):
        module.build_plan(state)


def test_outer_tampering_fails_closed():
    module = _module()
    state = _state(module)
    state["extra"] = True
    with pytest.raises(ValueError, match="HASH"):
        module.build_plan(state)


def test_atomic_publication_round_trip(tmp_path: Path):
    module = _module()
    plan = module.build_plan(_state(module))
    output = tmp_path / "plan.json"
    module.publish(output, plan)
    assert json.loads(output.read_text()) == plan
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_database_service_network_or_exchange_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4ca_catalog_delta_planner.py"
    ).read_text()
    for token in (
        "sqlite3",
        "subprocess",
        "requests",
        "systemctl",
        "exchange_client",
        "/home/james",
    ):
        assert token not in source
