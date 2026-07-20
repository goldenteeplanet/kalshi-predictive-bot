from kalshi_predictor.ui.cloud_deployment_preflight import certify_cloud_preflight


def healthy_capture() -> dict:
    return {
        "execution_enabled": False,
        "ui_read_only": True,
        "service_active": True,
        "loopback_only": True,
        "root_health_http": 200,
        "writer_clear": True,
        "db_locks_clear": True,
        "new_oom": False,
        "memory_max": "infinity",
        "root_available_gib": 16,
        "backup_available_gib": 6,
        "project_world_writable": True,
        "reports_world_writable": True,
    }


def test_healthy_capture_passes_without_authorizing_deployment() -> None:
    result = certify_cloud_preflight(healthy_capture())
    assert result.passed
    assert result.payload["deployment_performed"] is False
    assert result.payload["next_phase_requires_explicit_approval"] is True


def test_safety_flag_failure_blocks() -> None:
    capture = healthy_capture()
    capture["execution_enabled"] = True
    result = certify_cloud_preflight(capture)
    assert not result.passed
    assert "execution_enabled" in result.payload["blockers"]


def test_hardening_findings_are_warnings_not_hidden() -> None:
    warnings = certify_cloud_preflight(healthy_capture()).payload["warnings"]
    assert "PROJECT_WORLD_WRITABLE" in warnings
    assert "REPORTS_WORLD_WRITABLE" in warnings
    assert "SERVICE_MEMORY_LIMIT_UNBOUNDED" in warnings
    assert "BACKUP_FREE_SPACE_BELOW_10_GIB" in warnings


def test_capture_digest_is_deterministic() -> None:
    first = certify_cloud_preflight(healthy_capture()).payload["capture_sha256"]
    second = certify_cloud_preflight(healthy_capture()).payload["capture_sha256"]
    assert first == second
