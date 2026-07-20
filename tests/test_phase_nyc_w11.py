from __future__ import annotations

from kalshi_predictor.phase_nyc_w11 import build_nyc_w11_preview


def w10(ready: bool):
    return {
        "execution_enabled": False,
        "feature_flag_changed": False,
        "summary": {"review_ready": ready, "automatic_action_taken": False},
    }


def test_current_blocked_w10_keeps_activation_inert() -> None:
    report = build_nyc_w11_preview(w10(False))
    assert report["status"] == "BLOCKED_BY_NYC_W10"
    assert report["activation_eligible"] is False
    assert report["feature_flag_changed"] is False
    assert report["activation_plan"]["commands_executable"] is False


def test_passing_w10_only_allows_separate_manual_review() -> None:
    report = build_nyc_w11_preview(w10(True))
    assert report["status"] == "PASSED_LOCAL_PREVIEW"
    assert report["activation_eligible"] is True
    assert report["automatic_activation_allowed"] is False
    assert report["deployment_requires_explicit_approval"] is True
    assert report["activation_plan"]["flag_after_preview"].endswith("=false")


def test_execution_enablement_blocks_preview() -> None:
    payload = w10(True)
    payload["execution_enabled"] = True
    report = build_nyc_w11_preview(payload)
    assert report["activation_eligible"] is False
    assert report["gates"]["source_execution_disabled"] is False


def test_automatic_w10_action_blocks_preview() -> None:
    payload = w10(True)
    payload["summary"]["automatic_action_taken"] = True
    report = build_nyc_w11_preview(payload)
    assert report["activation_eligible"] is False


def test_preview_is_deterministic() -> None:
    assert build_nyc_w11_preview(w10(True)) == build_nyc_w11_preview(w10(True))
