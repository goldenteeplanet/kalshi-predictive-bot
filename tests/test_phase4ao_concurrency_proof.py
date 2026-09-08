from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4ao_concurrency_proof.py"
    spec = importlib.util.spec_from_file_location("phase4ao_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
FIRST = "2026-08-25T12:00:01+00:00"
SECOND = "2026-08-25T12:00:02+00:00"


def _fixture(tmp_path: Path):
    module = _module()
    protected = tmp_path / "protected"
    protected.mkdir()
    production = protected / "production.db"
    con = sqlite3.connect(production)
    con.execute("CREATE TABLE sentinel (id INTEGER PRIMARY KEY, value TEXT)")
    con.execute("INSERT INTO sentinel VALUES (1, 'unchanged')")
    con.commit()
    con.close()
    template = tmp_path / "template" / "disposable.db"
    template.parent.mkdir()
    con = sqlite3.connect(template)
    con.execute(
        "CREATE TABLE phase4al_disposable_marker "
        "(id INTEGER PRIMARY KEY, marker_schema TEXT, disposable INTEGER)"
    )
    con.execute("INSERT INTO phase4al_disposable_marker VALUES (1, ?, 1)", (module.MARKER_SCHEMA,))
    con.execute(
        "CREATE TABLE settlements "
        "(ticker TEXT PRIMARY KEY, result TEXT, settled_at TEXT, updated_at TEXT, payload TEXT)"
    )
    con.execute(
        "INSERT INTO settlements VALUES (?, ?, NULL, ?, ?)",
        ("KXCONCURRENT-1", "YES", "2026-08-25T00:00:00+00:00", "preserve"),
    )
    con.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY, value TEXT)")
    con.execute("INSERT INTO unrelated VALUES (1, 'unchanged')")
    con.commit()
    con.close()
    return module, production, template, tmp_path / "work"


def _simulate(fixture, scenario):
    module, production, template, work = fixture
    return module.simulate(
        production,
        template,
        work,
        ticker="KXCONCURRENT-1",
        first_timestamp=FIRST,
        second_timestamp=SECOND,
        scenario=scenario,
        now=NOW,
    )


@pytest.mark.parametrize("scenario", _module().SCENARIOS)
def test_every_contention_scenario_allows_at_most_one_cas(tmp_path: Path, scenario: str):
    fixture = _fixture(tmp_path)
    before = fixture[1].read_bytes()
    result = _simulate(fixture, scenario)
    assert result["successful_compare_and_swaps"] == 1
    assert result["final_timestamp"] == FIRST
    assert result["lost_update_prevented"] is True
    assert result["unrelated_state_preserved"] is True
    assert result["production_metadata_unchanged"] is True
    assert fixture[1].read_bytes() == before
    assert not list(fixture[3].iterdir())


def test_duplicate_attempt_is_refused_before_second_cas(tmp_path: Path):
    result = _simulate(_fixture(tmp_path), "DUPLICATE_ATTEMPT")
    assert result["duplicate_attempt_refusals"] == 1
    assert result["events"][-1]["event"] == "DUPLICATE_ATTEMPT_REFUSED"


def test_stale_reader_observes_zero_row_compare_and_swap(tmp_path: Path):
    result = _simulate(_fixture(tmp_path), "STALE_READER")
    assert result["stale_reads"] == 1
    assert result["events"][-1] == {"actor": "B", "event": "STALE_CAS", "affected": 0}


@pytest.mark.parametrize("scenario", ["LOCK_CONTENTION", "BUSY_TIMEOUT"])
def test_lock_contention_and_busy_timeout_fail_closed(tmp_path: Path, scenario: str):
    result = _simulate(_fixture(tmp_path), scenario)
    assert result["lock_refusals"] == 1
    assert any(event["event"] == "BUSY_REFUSAL" for event in result["events"])
    assert result["retry_count"] == (1 if scenario == "LOCK_CONTENTION" else 0)


def test_apparent_timeout_replay_does_not_mutate_twice(tmp_path: Path):
    result = _simulate(_fixture(tmp_path), "REPLAY_AFTER_APPARENT_TIMEOUT")
    assert result["apparent_timeout_simulated"] is True
    assert result["retry_count"] == 1
    assert result["events"][-1]["affected"] == 0


def test_campaign_is_complete_hash_valid_and_deterministic(tmp_path: Path):
    fixture = _fixture(tmp_path)
    module, production, template, work = fixture
    kwargs = {
        "ticker": "KXCONCURRENT-1",
        "first_timestamp": FIRST,
        "second_timestamp": SECOND,
        "now": NOW,
    }
    first = module.build(production, template, work, **kwargs)
    second = module.build(production, template, work, **kwargs)
    assert first == second
    report, proof = first
    assert report["all_scenarios_prevented_lost_updates"] is True
    assert report["maximum_successful_cas_per_scenario"] == 1
    assert proof["at_most_one_cas_succeeded"] is True
    assert proof["verified_scenario_count"] == len(module.SCENARIOS)
    assert report["artifact_hash"] == module._hash(report)
    assert proof["manifest_hash"] == module._hash(proof, "manifest_hash")


def test_same_path_hardlink_protected_root_and_marker_fail_closed(tmp_path: Path):
    module, production, template, work = _fixture(tmp_path)
    with pytest.raises(ValueError, match="PATH_OVERLAP"):
        module._validate_isolation(production, production, work)
    hardlink = tmp_path / "production-hardlink.db"
    try:
        os.link(production, hardlink)
    except OSError:
        pass
    else:
        with pytest.raises(ValueError, match="HARD_LINK"):
            module._validate_isolation(production, hardlink, work)
    protected_work = production.parent / "nested"
    protected_work.mkdir()
    with pytest.raises(ValueError, match="PATH_OVERLAP"):
        module._validate_isolation(production, template, protected_work)
    con = sqlite3.connect(template)
    con.execute("UPDATE phase4al_disposable_marker SET disposable=0")
    con.commit()
    con.close()
    with pytest.raises(ValueError, match="MARKER_INVALID"):
        module._validate_isolation(production, template, work)


def test_unknown_scenario_and_naive_time_fail_closed(tmp_path: Path):
    fixture = _fixture(tmp_path)
    module, production, template, work = fixture
    kwargs = {
        "ticker": "KXCONCURRENT-1",
        "first_timestamp": FIRST,
        "second_timestamp": SECOND,
        "now": NOW,
    }
    with pytest.raises(ValueError, match="SCENARIO_INVALID"):
        module.simulate(production, template, work, scenario="UNKNOWN", **kwargs)
    kwargs["now"] = datetime(2026, 8, 25)
    with pytest.raises(ValueError, match="TIMEZONE_MISSING"):
        module.simulate(production, template, work, scenario="TWO_VALID_CANDIDATES", **kwargs)


def test_artifacts_are_non_authorizing_and_contain_no_production_controls(tmp_path: Path):
    fixture = _fixture(tmp_path)
    report, proof = fixture[0].build(
        fixture[1],
        fixture[2],
        fixture[3],
        ticker="KXCONCURRENT-1",
        first_timestamp=FIRST,
        second_timestamp=SECOND,
        now=NOW,
    )
    rendered = json.dumps([report, proof], sort_keys=True).lower()
    assert 'execution_authorized": true' not in rendered
    assert 'production_database_mutated": true' not in rendered
    assert "systemctl" not in rendered
    assert "exchange" not in rendered or 'exchange_requests_made": false' in rendered
