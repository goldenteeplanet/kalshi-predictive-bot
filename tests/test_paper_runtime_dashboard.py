"""Dashboard keeps recorded supervisor state separate from live process evidence."""

import hashlib
import json

import test_paper_supervisor as fixtures
from sqlalchemy import text

from kalshi_predictor.overnight_paper import dashboard

baseline_template = fixtures.baseline_template
prepared = fixtures.prepared
public_cycle = fixtures.public_cycle


def test_real_stopped_supervisor_is_displayed_read_only(prepared, public_cycle):
    result = fixtures.run(prepared)
    path = prepared["database_path"]
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    view = dashboard.snapshot(path)
    assert view["runtime_state"] == "STOPPED"
    assert view["runtime_last_reported_state"] == "STOPPED"
    assert view["runtime_generation"] == result.generation
    assert view["runtime_current_monitor_verified"] is False
    assert view["runtime_entries_reported"] is False
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    page = dashboard.render(view)
    assert "LIVE MARKET DATA" in page and "LOCAL PAPER ONLY" in page
    assert "Last Reported State" in page


def change_last(prepared, **changes):
    with prepared["session_factory"]() as session:
        key, raw = session.execute(text(
            "SELECT id,payload FROM overnight_sprint_cycles "
            "WHERE id LIKE 'runtime-health:%' ORDER BY id DESC LIMIT 1"
        )).one()
        event = json.loads(raw)
        event.update(changes)
        session.execute(text("UPDATE overnight_sprint_cycles SET payload=:raw WHERE id=:id"),
                        {"raw": json.dumps(event), "id": key})
        session.commit()


def test_live_process_with_saved_running_record_does_not_prove_monitor(prepared, public_cycle):
    fixtures.run(prepared)
    change_last(prepared, state="RUNNING")
    view = dashboard.snapshot(prepared["database_path"])
    assert view["runtime_process_state"] == "RUNNING"
    assert view["runtime_last_reported_state"] == "RUNNING"
    assert view["runtime_state"] == "UNVERIFIED"
    assert view["runtime_current_monitor_verified"] is False


def test_reused_pid_with_wrong_start_identity_is_stopped(prepared, public_cycle):
    fixtures.run(prepared)
    change_last(prepared, state="RUNNING", process_start_identity="different-incarnation")
    view = dashboard.snapshot(prepared["database_path"])
    assert view["runtime_process_state"] == "STOPPED"
    assert view["runtime_state"] == "STOPPED"


def test_wrong_ledger_health_is_never_presented_as_runtime_truth(prepared, public_cycle):
    fixtures.run(prepared)
    change_last(prepared, database_id="another-ledger")
    view = dashboard.snapshot(prepared["database_path"])
    assert view["paper_mode"] == "UNVERIFIED"
    assert view["runtime_current_monitor_verified"] is False
    assert view["open_positions"] is None
