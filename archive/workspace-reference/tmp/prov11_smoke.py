from pathlib import Path
from tempfile import TemporaryDirectory
import os

from tests.test_phase_prov11 import (
    test_prov11_bounded_diagnostics_verify_exact_chain,
    test_prov11_dashboard_preview_is_flag_guarded,
    test_prov11_detects_digest_tampering_and_execution_guard,
    test_prov12_drift_alerts_detect_stale_and_missing_ranking,
    test_prov12_exact_market_trace_is_complete_and_read_only,
    test_prov12_routes_are_separately_flag_guarded,
)

class MonkeyPatch:
    def __init__(self):
        self._undo = []

    def setattr(self, target, name, value):
        original = getattr(target, name)
        self._undo.append(lambda: setattr(target, name, original))
        setattr(target, name, value)

    def chdir(self, path):
        original = os.getcwd()
        self._undo.append(lambda: os.chdir(original))
        os.chdir(path)

    def undo(self):
        for action in reversed(self._undo):
            action()


for test in (
    test_prov11_bounded_diagnostics_verify_exact_chain,
    test_prov11_detects_digest_tampering_and_execution_guard,
    test_prov11_dashboard_preview_is_flag_guarded,
    test_prov12_exact_market_trace_is_complete_and_read_only,
    test_prov12_drift_alerts_detect_stale_and_missing_ranking,
    test_prov12_routes_are_separately_flag_guarded,
):
    with TemporaryDirectory(prefix="prov11-") as directory:
        monkeypatch = MonkeyPatch()
        try:
            test(Path(directory), monkeypatch)
        finally:
            monkeypatch.undo()
    print(f"PASS {test.__name__}")
