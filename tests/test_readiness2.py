from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from kalshi_predictor.readiness2 import build_readiness2_preview


def fixture(tmp_path: Path):
    generated = datetime(2026, 7, 18, tzinfo=UTC)
    summary = tmp_path / "summary.md"
    summary.write_text(f"- Generated at: `{generated.isoformat()}`\n")
    blockers = tmp_path / "blockers.csv"
    blockers.write_text(
        "category,model_name,current_rows,paper_ready_rows,positive_ev_rows,first_blocker,blocker,blocker_count,next_action\n"
        "crypto,crypto_v2,10,0,1,SNAPSHOT_STALE,SNAPSHOT_STALE,8,refresh snapshots\n"
        "crypto,crypto_v2,10,0,1,SNAPSHOT_STALE,EV_NOT_POSITIVE,2,refresh snapshots\n"
        "weather,weather_v2,5,0,2,SNAPSHOT_MISSING,SNAPSHOT_MISSING,5,capture books\n"
    )
    return generated, blockers, summary


def test_exact_blockers_are_grouped_without_threshold_changes(tmp_path: Path) -> None:
    generated, blockers, summary = fixture(tmp_path)
    report = build_readiness2_preview(blockers, summary, as_of=generated + timedelta(hours=1))
    assert report["status"] == "PASSED_READ_ONLY_PREVIEW"
    assert report["observed"]["current_rows"] == 15
    assert report["observed"]["paper_ready_rows"] == 0
    assert [item["blocker"] for item in report["blocker_attribution"]] == [
        "SNAPSHOT_MISSING",
        "SNAPSHOT_STALE",
        "EV_NOT_POSITIVE",
    ]
    assert all(not item["threshold_change_required"] for item in report["blocker_attribution"])


def test_stale_readiness1_cannot_authorize_remediation(tmp_path: Path) -> None:
    generated, blockers, summary = fixture(tmp_path)
    report = build_readiness2_preview(blockers, summary, as_of=generated + timedelta(hours=25))
    assert report["status"] == "STALE_READINESS_1_EVIDENCE"
    assert report["decision"] == "RERUN_READINESS_1_BEFORE_REMEDIATION"
    assert report["guardrails"]["stale_evidence_can_authorize_activation"] is False


def test_report_is_deterministic(tmp_path: Path) -> None:
    generated, blockers, summary = fixture(tmp_path)
    first = build_readiness2_preview(blockers, summary, as_of=generated)
    second = build_readiness2_preview(blockers, summary, as_of=generated)
    assert first == second


def test_missing_generated_at_fails_visible(tmp_path: Path) -> None:
    _, blockers, summary = fixture(tmp_path)
    summary.write_text("missing timestamp")
    try:
        build_readiness2_preview(blockers, summary, as_of=datetime.now(UTC))
    except ValueError as exc:
        assert "generated-at" in str(exc)
    else:
        raise AssertionError("missing generated-at evidence must fail")


def test_future_generated_at_is_rejected(tmp_path: Path) -> None:
    generated, blockers, summary = fixture(tmp_path)
    try:
        build_readiness2_preview(blockers, summary, as_of=generated - timedelta(seconds=1))
    except ValueError as exc:
        assert "future" in str(exc)
    else:
        raise AssertionError("future evidence must fail closed")


def test_unknown_blocker_is_rejected_instead_of_generic_attribution(tmp_path: Path) -> None:
    generated, blockers, summary = fixture(tmp_path)
    text = blockers.read_text().replace("SNAPSHOT_STALE,8", "UNEXPLAINED,8", 1)
    blockers.write_text(text)
    try:
        build_readiness2_preview(blockers, summary, as_of=generated)
    except ValueError as exc:
        assert "not exact/recognized" in str(exc)
    else:
        raise AssertionError("unknown blockers must fail closed")


def test_conflicting_category_metrics_are_rejected(tmp_path: Path) -> None:
    generated, blockers, summary = fixture(tmp_path)
    text = blockers.read_text().replace(
        "crypto,crypto_v2,10,0,1,SNAPSHOT_STALE,EV_NOT_POSITIVE",
        "crypto,crypto_v2,11,0,1,SNAPSHOT_STALE,EV_NOT_POSITIVE",
    )
    blockers.write_text(text)
    try:
        build_readiness2_preview(blockers, summary, as_of=generated)
    except ValueError as exc:
        assert "metrics conflict" in str(exc)
    else:
        raise AssertionError("conflicting metrics must fail closed")
