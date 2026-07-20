from pathlib import Path

from kalshi_predictor.config import Settings
from kalshi_predictor.phase_gh1l import apply_gh1l


def test_gh1l_blocks_before_opening_session(tmp_path: Path) -> None:
    def forbidden_session():
        raise AssertionError("session must not open while writer is active")
    result = apply_gh1l(
        session_factory=forbidden_session, settings=Settings(),
        gh1j_report=tmp_path / "missing-j.json", gh1k_report=tmp_path / "missing-k.json",
        writer_monitor_fn=lambda: {"safe_to_start_write": False, "current_writer_pid": 7},
    )
    assert result["status"] == "BLOCKED_ACTIVE_WRITER"
    assert result["database_writes"] == 0
