import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.overnight.cycle import (
    OvernightCycleResult,
    OvernightJobs,
    run_overnight_cycle,
)
from kalshi_predictor.overnight.repository import (
    complete_overnight_run,
    create_overnight_run,
)


@dataclass(frozen=True)
class OvernightSchedulerResult:
    run_id: int
    cycles: list[OvernightCycleResult]
    stop_reason: str | None
    status: str


def run_overnight_once(
    session: Session,
    *,
    settings: Settings | None = None,
    jobs: OvernightJobs | None = None,
) -> OvernightCycleResult:
    resolved_settings = settings or get_settings()
    run = create_overnight_run(session, resolved_settings)
    result = run_overnight_cycle(
        session,
        run_id=run.id,
        cycle_number=1,
        settings=resolved_settings,
        jobs=jobs,
    )
    complete_overnight_run(
        session,
        run,
        status=result.status,
        cycles_completed=1,
        errors_count=len(result.errors),
        summary={"cycles": [result.summary], "stop_reason": result.status},
    )
    return result


def run_overnight_scheduler(
    session_factory: Callable[[], Session],
    *,
    settings: Settings | None = None,
    jobs: OvernightJobs | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> OvernightSchedulerResult:
    resolved_settings = settings or get_settings()
    cycles: list[OvernightCycleResult] = []
    stop_reason: str | None = None
    status = "COMPLETED"

    with session_factory() as session:
        run = create_overnight_run(session, resolved_settings)
        if not resolved_settings.overnight_enabled:
            stop_reason = "OVERNIGHT_ENABLED=false; scheduler did not start."
            complete_overnight_run(
                session,
                run,
                status="DISABLED",
                cycles_completed=0,
                errors_count=0,
                summary={"cycles": [], "stop_reason": stop_reason},
            )
            session.commit()
            return OvernightSchedulerResult(
                run_id=run.id,
                cycles=[],
                stop_reason=stop_reason,
                status="DISABLED",
            )

        try:
            for cycle_number in range(1, resolved_settings.overnight_max_cycles + 1):
                result = run_overnight_cycle(
                    session,
                    run_id=run.id,
                    cycle_number=cycle_number,
                    settings=resolved_settings,
                    jobs=jobs,
                )
                cycles.append(result)
                session.commit()
                if result.errors:
                    status = "COMPLETED_WITH_ERRORS"
                    if resolved_settings.overnight_stop_on_error:
                        stop_reason = "Stopped because OVERNIGHT_STOP_ON_ERROR=true."
                        status = "ERROR"
                        break
                if cycle_number >= resolved_settings.overnight_max_cycles:
                    stop_reason = (
                        f"Reached OVERNIGHT_MAX_CYCLES={resolved_settings.overnight_max_cycles}."
                    )
                    break
                _sleep_between_cycles(resolved_settings, sleeper)
        except KeyboardInterrupt:
            stop_reason = "Interrupted by user."
            status = "INTERRUPTED"

        if not cycles and status == "COMPLETED":
            stop_reason = "No cycles were requested."
        complete_overnight_run(
            session,
            run,
            status=status,
            cycles_completed=len(cycles),
            errors_count=sum(len(cycle.errors) for cycle in cycles),
            summary=_run_summary(cycles, stop_reason=stop_reason),
        )
        session.commit()
        return OvernightSchedulerResult(
            run_id=run.id,
            cycles=cycles,
            stop_reason=stop_reason,
            status=status,
        )


def _sleep_between_cycles(settings: Settings, sleeper: Callable[[float], None]) -> None:
    seconds = max(0, settings.overnight_interval_minutes * 60)
    if seconds:
        sleeper(seconds)


def _run_summary(
    cycles: list[OvernightCycleResult],
    *,
    stop_reason: str | None,
) -> dict[str, Any]:
    return {
        "stop_reason": stop_reason,
        "cycles": [cycle.summary for cycle in cycles],
        "paper_orders_created": sum(cycle.paper_orders_created for cycle in cycles),
        "forecasts_inserted": sum(cycle.forecasts_inserted for cycle in cycles),
        "opportunities_detected": sum(cycle.opportunities_detected for cycle in cycles),
        "errors_count": sum(len(cycle.errors) for cycle in cycles),
    }
