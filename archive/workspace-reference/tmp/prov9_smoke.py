from pathlib import Path
from tempfile import TemporaryDirectory

from tests.test_phase_2_6_opportunities import (
    test_latest_forecasts_pushes_exact_ticker_scope_into_query,
)
from tests.test_phase3bb_r8_unified_paper_gate import (
    test_phase3bb_r8_crypto_linked_row_gets_source_missing_blocker,
    test_phase3bb_r8_evidence_queries_are_constant_for_repeated_exact_keys,
    test_phase3bb_r8_recent_decision_window_is_bounded_and_conservative,
    test_phase3bb_r8_weather_row_gets_feature_missing_after_source_snapshot,
)


tests = (
    test_latest_forecasts_pushes_exact_ticker_scope_into_query,
    test_phase3bb_r8_evidence_queries_are_constant_for_repeated_exact_keys,
    test_phase3bb_r8_recent_decision_window_is_bounded_and_conservative,
    test_phase3bb_r8_crypto_linked_row_gets_source_missing_blocker,
    test_phase3bb_r8_weather_row_gets_feature_missing_after_source_snapshot,
)
for test in tests:
    with TemporaryDirectory(prefix="prov9-") as directory:
        test(Path(directory))
    print(f"PASS {test.__name__}")
