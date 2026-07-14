import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.learning.cycle import LearningCycleResult, LearningJobs, run_learning_cycle
from kalshi_predictor.learning.repository import complete_learning_run, create_learning_run


@dataclass(frozen=True)
class LearningSchedulerResult:
    run_id: int
    cycles: list[LearningCycleResult]
    stop_reason: str | None
    status: str


def run_learning_once(
    session: Session,
    *,
    settings: Settings | None = None,
    jobs: LearningJobs | None = None,
) -> LearningCycleResult:
    resolved_settings = settings or get_settings()
    run = create_learning_run(session, resolved_settings)
    if not resolved_settings.learning_mode:
        complete_learning_run(
            session,
            run,
            status="DISABLED",
            cycles_completed=0,
            paper_trades_created=0,
            settlements_synced=0,
            summary={"stop_reason": "LEARNING_MODE=false; cycle did not run."},
        )
        return LearningCycleResult(
            run_id=run.id,
            cycle_id=0,
            cycle_number=0,
            status="DISABLED",
            markets_scanned=0,
            forecasts_generated=0,
            opportunities_found=0,
            paper_trades_created=0,
            settlements_synced=0,
            settled_paper_trades_total=run.starting_settled_trades,
            errors=[],
            summary={"stop_reason": "LEARNING_MODE=false; cycle did not run."},
        )
    result = run_learning_cycle(
        session,
        run_id=run.id,
        cycle_number=1,
        settings=resolved_settings,
        jobs=jobs,
    )
    complete_learning_run(
        session,
        run,
        status=result.status,
        cycles_completed=1,
        paper_trades_created=result.paper_trades_created,
        settlements_synced=result.settlements_synced,
        summary={"cycles": [result.summary], "stop_reason": result.status},
    )
    return result


def run_learning_scheduler(
    session_factory: Callable[[], Session],
    *,
    settings: Settings | None = None,
    jobs: LearningJobs | None = None,
    max_cycles: int = 32,
    interval_minutes: int = 15,
    sleeper: Callable[[float], None] = time.sleep,
) -> LearningSchedulerResult:
    resolved_settings = settings or get_settings()
    cycles: list[LearningCycleResult] = []
    status = "COMPLETED"
    stop_reason: str | None = None

    with session_factory() as session:
        run = create_learning_run(session, resolved_settings)
        if not resolved_settings.learning_mode:
            stop_reason = "LEARNING_MODE=false; scheduler did not start."
            complete_learning_run(
                session,
                run,
                status="DISABLED",
                cycles_completed=0,
                paper_trades_created=0,
                settlements_synced=0,
                summary={"cycles": [], "stop_reason": stop_reason},
            )
            session.commit()
            return LearningSchedulerResult(
                run_id=run.id,
                cycles=[],
                stop_reason=stop_reason,
                status="DISABLED",
            )

        try:
            for cycle_number in range(1, max_cycles + 1):
                result = run_learning_cycle(
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
                if cycle_number >= max_cycles:
                    stop_reason = f"Reached max cycles ({max_cycles})."
                    break
                _sleep_between_cycles(interval_minutes, sleeper)
        except KeyboardInterrupt:
            stop_reason = "Interrupted by user."
            status = "INTERRUPTED"

        complete_learning_run(
            session,
            run,
            status=status,
            cycles_completed=len(cycles),
            paper_trades_created=sum(cycle.paper_trades_created for cycle in cycles),
            settlements_synced=sum(cycle.settlements_synced for cycle in cycles),
            summary=_run_summary(cycles, stop_reason=stop_reason),
        )
        session.commit()
        return LearningSchedulerResult(
            run_id=run.id,
            cycles=cycles,
            stop_reason=stop_reason,
            status=status,
        )


def _sleep_between_cycles(
    interval_minutes: int,
    sleeper: Callable[[float], None],
) -> None:
    seconds = max(0, interval_minutes * 60)
    if seconds:
        sleeper(seconds)


def _run_summary(
    cycles: list[LearningCycleResult],
    *,
    stop_reason: str | None,
) -> dict[str, Any]:
    return {
        "stop_reason": stop_reason,
        "cycles": [cycle.summary for cycle in cycles],
        "paper_trades_created": sum(cycle.paper_trades_created for cycle in cycles),
        "forecasts_generated": sum(cycle.forecasts_generated for cycle in cycles),
        "opportunities_found": sum(cycle.opportunities_found for cycle in cycles),
        "errors_count": sum(len(cycle.errors) for cycle in cycles),
    }

