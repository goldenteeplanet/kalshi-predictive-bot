import shutil
import socket
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings


@dataclass(frozen=True)
class HealthCheck:
    name: str
    passed: bool
    severity: str
    detail: str


def run_health_checks(
    session: Session,
    *,
    settings: Settings | None = None,
    reports_dir: Path = Path("reports"),
    network_checker: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    resolved_settings = settings or get_settings()
    checks = [
        _db_check(session),
        _reports_check(reports_dir),
        _disk_check(resolved_settings),
        _deps_check(),
        _execution_safety_check(resolved_settings),
    ]
    if resolved_settings.overnight_require_market_data:
        checks.append(_network_check(network_checker or default_network_checker))
    ok = not any(check.severity == "ERROR" and not check.passed for check in checks)
    return {
        "ok": ok,
        "checks": [asdict(check) for check in checks],
        "errors": [
            asdict(check)
            for check in checks
            if check.severity == "ERROR" and not check.passed
        ],
        "warnings": [
            asdict(check)
            for check in checks
            if check.severity == "WARNING" and not check.passed
        ],
    }


def default_network_checker() -> bool:
    try:
        with socket.create_connection(("external-api.kalshi.com", 443), timeout=3):
            return True
    except OSError:
        return False


def _db_check(session: Session) -> HealthCheck:
    try:
        session.execute(text("select 1")).scalar_one()
    except Exception as exc:
        return HealthCheck("Database writable", False, "ERROR", str(exc))
    return HealthCheck("Database writable", True, "INFO", "Local database connection is healthy.")


def _reports_check(reports_dir: Path) -> HealthCheck:
    try:
        reports_dir.mkdir(parents=True, exist_ok=True)
        test_path = reports_dir / ".overnight_health_check.tmp"
        test_path.write_text("ok", encoding="utf-8")
        test_path.unlink(missing_ok=True)
    except Exception as exc:
        return HealthCheck("Reports directory writable", False, "ERROR", str(exc))
    return HealthCheck("Reports directory writable", True, "INFO", str(reports_dir))


def _disk_check(settings: Settings) -> HealthCheck:
    free_mb = shutil.disk_usage(Path.cwd()).free // (1024 * 1024)
    passed = free_mb >= settings.overnight_min_free_disk_mb
    return HealthCheck(
        "Free disk space",
        passed,
        "ERROR",
        f"{free_mb} MB free; required {settings.overnight_min_free_disk_mb} MB.",
    )


def _deps_check() -> HealthCheck:
    try:
        import fastapi  # noqa: F401
        import sqlalchemy  # noqa: F401
        import typer  # noqa: F401
    except Exception as exc:
        return HealthCheck("Python dependencies", False, "ERROR", str(exc))
    return HealthCheck("Python dependencies", True, "INFO", "Required packages import cleanly.")


def _execution_safety_check(settings: Settings) -> HealthCheck:
    if settings.kalshi_env.lower() != "demo":
        return HealthCheck(
            "Production execution disabled",
            False,
            "WARNING",
            f"KALSHI_ENV={settings.kalshi_env}; overnight execution will be avoided.",
        )
    if settings.execution_enabled:
        return HealthCheck(
            "Production execution disabled",
            False,
            "WARNING",
            "EXECUTION_ENABLED=true; overnight keeps demo execution disabled by default.",
        )
    return HealthCheck(
        "Production execution disabled",
        True,
        "INFO",
        "Demo environment and no production execution are configured.",
    )


def _network_check(network_checker: Callable[[], bool]) -> HealthCheck:
    passed = network_checker()
    return HealthCheck(
        "Public market data network",
        passed,
        "ERROR",
        "Kalshi public API is reachable."
        if passed
        else "Kalshi public API is not reachable and market data is required.",
    )
