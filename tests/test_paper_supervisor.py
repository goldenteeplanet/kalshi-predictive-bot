"""Real owner/SQLite/watcher cycles, with only outbound HTTP and sleeps intercepted."""

import json

import httpx
import pytest
import test_overnight_activation as fixtures
from sqlalchemy import text

from kalshi_predictor.overnight_paper import monitoring, settlement_runner, supervisor
from kalshi_predictor.overnight_paper.runtime_owner import acquire_runtime_owner

baseline_template = fixtures.baseline_template
prepared = fixtures.prepared


@pytest.fixture
def public_cycle(prepared, monkeypatch):
    requests = []
    real_client = httpx.Client

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        assert "authorization" not in request.headers
        assert not any("kalshi-access" in k for k in request.headers)
        shadow = prepared["shadow_payload"]
        return httpx.Response(
            200,
            json={
                "market": {
                    "ticker": shadow["ticker"],
                    "event_ticker": shadow["event_ticker"],
                    "series_ticker": shadow["series_ticker"],
                    "status": "active",
                    "close_time": shadow["close_time"],
                }
            },
        )

    monkeypatch.setattr(
        settlement_runner.httpx,
        "Client",
        lambda **kwargs: real_client(**{**kwargs, "transport": httpx.MockTransport(handler)}),
    )
    sleeps = []
    monkeypatch.setattr(supervisor.time, "sleep", sleeps.append)
    return requests, sleeps


def run(prepared, **kwargs):
    return supervisor.run_paper_supervisor(
        session_factory=prepared["session_factory"],
        database_path=prepared["database_path"],
        settings=prepared["settings"],
        code_sha="a" * 40,
        **kwargs,
    )


def test_disabled_entries_still_run_actual_watcher_and_end_stopped(prepared, public_cycle):
    result = run(prepared, cycles=2, entries_enabled=False)
    requests, sleeps = public_cycle
    assert len(requests) == 2 and sleeps == [60]
    assert result.cycles_completed == 2
    assert result.state == "STOPPED" and result.entries_enabled is False
    assert result.health_events[-1]["state"] == "STOPPED"
    assert len([e for e in result.health_events if e["state"] == "SETTLEMENT_MONITORING"]) == 2
    assert result.generation not in monitoring._ACTIVE
    with prepared["session_factory"]() as session:
        rows = (
            session.execute(
                text(
                    "SELECT payload FROM overnight_sprint_cycles "
                    "WHERE id LIKE 'runtime-health:%' ORDER BY id"
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == len(result.health_events)
        assert all(
            json.loads(r)["database_id"] == prepared["authorization"].database_id for r in rows
        )
        assert session.execute(text("SELECT count(*) FROM paper_orders")).scalar_one() == 0
    with acquire_runtime_owner(prepared["database_path"]):
        pass


def test_restart_uses_new_generation_without_overwriting_health(prepared, public_cycle):
    first = run(prepared)
    second = run(prepared)
    assert first.generation != second.generation
    with prepared["session_factory"]() as session:
        assert session.execute(
            text("SELECT count(*) FROM overnight_sprint_cycles WHERE id LIKE 'runtime-health:%'")
        ).scalar_one() == len(first.health_events) + len(second.health_events)


def test_no_tracked_markets_is_not_a_live_candidate_permit(prepared, public_cycle):
    with prepared["session_factory"]() as session:
        session.execute(text("DELETE FROM overnight_shadow"))
        session.commit()
    result = run(prepared)
    assert public_cycle[0] == []
    event = next(e for e in result.health_events if e["state"] == "SETTLEMENT_MONITORING")
    assert event["watcher_status"] == "NO_TRACKED_MARKETS"
    assert event["live_candidate_permits"] == 0
    assert result.admission_state is None


def test_recoverable_http_failure_disables_entry_but_next_cycle_runs(
    prepared, public_cycle, monkeypatch
):
    real = monitoring.run_settlement_cycles
    calls = []

    def once(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("offline fixture")
        return real(**kwargs)

    monkeypatch.setattr(monitoring, "run_settlement_cycles", once)
    result = run(prepared, cycles=2)
    assert len(calls) == 2 and result.cycles_completed == 1
    assert any(e["state"] == "DEGRADED" for e in result.health_events)
    assert result.state == "STOPPED" and not result.entries_enabled


def test_contending_owner_cannot_start_second_writer(prepared, public_cycle):
    with acquire_runtime_owner(prepared["database_path"]):
        with pytest.raises(ValueError, match="ALREADY_ACTIVE"):
            run(prepared)
    assert public_cycle[0] == []


@pytest.mark.parametrize("cycles", [0, 61, True])
def test_run_budget_rejects_before_monitor(prepared, public_cycle, cycles):
    with pytest.raises(ValueError, match="BOUNDED_CYCLES"):
        run(prepared, cycles=cycles)
    assert public_cycle[0] == []


@pytest.mark.parametrize(
    "final_state, blockers", [("PAPER_FILLED", ()), ("BLOCKED", ("MODEL_NOT_READY",))]
)
def test_candidate_admission_receives_actual_matching_monitor_permit(
    prepared, public_cycle, monkeypatch, final_state, blockers
):
    from kalshi_predictor.overnight_paper.coordinator import CoordinatorResult, PreparedCandidate

    calls = []

    def admission(**kwargs):
        calls.append(kwargs["entries_enabled"])
        if kwargs["entries_enabled"]:
            with prepared["session_factory"]() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                checked = monitoring.verify_monitoring(
                    session,
                    kwargs["monitoring_permit"],
                    database_path=prepared["database_path"],
                    database_id=prepared["authorization"].database_id,
                    code_sha="a" * 40,
                    shadow_id=prepared["shadow_id"],
                )
                assert checked.passed, checked.blockers
        return CoordinatorResult(
            final_state if kwargs["entries_enabled"] else "SHADOW_ONLY",
            prepared["qualification_args"]["decision_id"],
            prepared["shadow_id"],
            blockers=blockers if kwargs["entries_enabled"] else (),
        )

    # Admission mechanics have separate full lifecycle coverage; this asserts the
    # supervisor sequence and actual OS-owner/runner-produced permit, not a fake PASS.
    monkeypatch.setattr(supervisor, "admit_prepared_candidate", admission)
    candidate = PreparedCandidate(
        prepared["decision"], prepared["qualification_args"], prepared["shadow_payload"]
    )
    result = run(
        prepared,
        cycles=2,
        entries_enabled=True,
        candidate=candidate,
        authorization=prepared["authorization"],
        objective_bytes=prepared["objective_bytes"],
        release=prepared["release"],
    )
    assert calls == [False, True]
    assert len(public_cycle[0]) == 3
    assert result.admission_state == final_state
    admission_event = next(e for e in result.health_events if "order_id" in e)
    assert admission_event["blockers"] == list(blockers)
    with prepared["session_factory"]() as session:
        stored = (
            session.execute(
                text("SELECT payload FROM overnight_sprint_cycles WHERE id LIKE 'runtime-health:%'")
            )
            .scalars()
            .all()
        )
        assert admission_event in [json.loads(row) for row in stored]
    assert not result.entries_enabled
    assert all(
        not e["entries_enabled"] for e in result.health_events if e["state"] == "ENTRY_DISABLED"
    )


def test_existing_position_disables_entry_and_still_monitors(prepared, public_cycle):
    from kalshi_predictor.overnight_paper.activation import activate_local_paper

    activate_local_paper(**prepared)
    result = run(prepared, cycles=2, entries_enabled=True)
    assert len(public_cycle[0]) == 2
    disabled = [e for e in result.health_events if e["state"] == "ENTRY_DISABLED"]
    assert len(disabled) == 2
    assert all(e["reason"] == "EXPERIMENT_ALREADY_EXISTS" for e in disabled)
    with prepared["session_factory"]() as session:
        assert session.execute(text("SELECT count(*) FROM paper_orders")).scalar_one() == 1
    assert result.state == "STOPPED"


def test_unknown_database_fault_is_not_masked_by_final_health_write(
    prepared, public_cycle, monkeypatch
):
    from sqlalchemy.exc import OperationalError

    original = supervisor._verify_database
    calls = []

    def fail_after_start(*args):
        calls.append(1)
        if len(calls) > 1:
            raise OperationalError("fixture SQL", {}, RuntimeError("failure"))
        return original(*args)

    monkeypatch.setattr(supervisor, "_verify_database", fail_after_start)
    with pytest.raises(OperationalError):
        run(prepared)
    assert len(calls) == 2
    with prepared["session_factory"]() as session:
        rows = (
            session.execute(
                text("SELECT payload FROM overnight_sprint_cycles WHERE id LIKE 'runtime-health:%'")
            )
            .scalars()
            .all()
        )
        assert [json.loads(r)["state"] for r in rows] == ["RUNNING"]
    with acquire_runtime_owner(prepared["database_path"]):
        pass
