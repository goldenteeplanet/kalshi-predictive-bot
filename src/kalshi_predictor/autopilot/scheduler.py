import time
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from kalshi_predictor.autopilot.repository import complete_autopilot_run, create_autopilot_run
from kalshi_predictor.autopilot.runner import (
    AutopilotCycleResult,
    AutopilotExecutionClient,
    run_autopilot_cycle,
)
from kalshi_predictor.config import Settings, get_settings


@dataclass(frozen=True)
class AutopilotSchedulerResult:
    run_id: int
    cycles: list[AutopilotCycleResult]
    stop_reason: str | None


def run_autopilot_scheduler(
    session_factory: Callable[[], Session],
    *,
    settings: Settings | None = None,
    execution_client: AutopilotExecutionClient | None = None,
) -> AutopilotSchedulerResult:
    resolved_settings = settings or get_settings()
    cycles: list[AutopilotCycleResult] = []
    stop_reason: str | None = None

    with session_factory() as session:
        run = create_autopilot_run(session, resolved_settings)
        max_cycles = resolved_settings.autopilot_max_cycles
        cycle_number = 0
        while max_cycles == 0 or cycle_number < max_cycles:
            cycle_number += 1
            result = run_autopilot_cycle(
                session,
                run=run,
                cycle_number=cycle_number,
                settings=resolved_settings,
                execution_client=execution_client,
            )
            cycles.append(result)
            session.commit()
            if result.status == "BLOCKED":
                stop_reason = result.stop_reason or "Guardrails blocked the cycle."
                break
            if max_cycles and cycle_number >= max_cycles:
                stop_reason = f"Reached AUTOPILOT_MAX_CYCLES={max_cycles}."
                break
            time.sleep(max(1, resolved_settings.autopilot_interval_seconds))

        complete_autopilot_run(
            session,
            run,
            status=cycles[-1].status if cycles else "COMPLETED",
            cycles_completed=len(cycles),
            orders_attempted=sum(cycle.orders_attempted for cycle in cycles),
            orders_submitted=sum(cycle.orders_submitted for cycle in cycles),
            orders_blocked=sum(cycle.orders_blocked for cycle in cycles),
            stop_reason=stop_reason,
            summary={"cycles": [cycle.summary for cycle in cycles]},
        )
        session.commit()
        return AutopilotSchedulerResult(
            run_id=run.id,
            cycles=cycles,
            stop_reason=stop_reason,
        )
