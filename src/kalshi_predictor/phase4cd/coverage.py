from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import (
    CalibrationObservation,
    CryptoFeatureLineage,
    Feature,
    Forecast,
    MarketSnapshot,
    ReplayDisposition,
    ResearchRun,
)


def replay_coverage_audit(session: Session, run_id: str | None = None) -> dict[str, Any]:
    run = (
        session.get(ResearchRun, run_id)
        if run_id
        else session.scalar(select(ResearchRun).order_by(ResearchRun.started_at.desc()).limit(1))
    )
    if run is None:
        return {"status": "NO_RESEARCH_RUN"}
    dispositions = list(
        session.scalars(select(ReplayDisposition).where(ReplayDisposition.run_id == run.run_id))
    )
    calibration = list(
        session.scalars(
            select(CalibrationObservation).where(CalibrationObservation.run_id == run.run_id)
        )
    )
    snapshot_n = feature_n = price_n = gross_n = net_n = 0
    for row in dispositions:
        forecast = session.get(Forecast, row.forecast_id)
        if forecast is None:
            continue
        has_snapshot = (
            session.scalar(
                select(MarketSnapshot.id)
                .where(
                    MarketSnapshot.ticker == forecast.ticker,
                    MarketSnapshot.captured_at <= forecast.forecasted_at,
                )
                .limit(1)
            )
            is not None
        )
        if has_snapshot:
            snapshot_n += 1
        generic_feature = (
            session.scalar(
                select(Feature.id)
                .where(
                    Feature.ticker == forecast.ticker,
                    Feature.generated_at <= forecast.forecasted_at,
                )
                .limit(1)
            )
            is not None
        )
        lineage = session.scalar(
            select(CryptoFeatureLineage).where(CryptoFeatureLineage.forecast_id == forecast.id)
        )
        verified_lineage = lineage is not None and lineage.verdict in {
            "FEATURE_LINEAGE_VERIFIED",
            "FEATURE_RECONSTRUCTABLE_POINT_IN_TIME",
        }
        if generic_feature or verified_lineage:
            feature_n += 1
        details = __import__("json").loads(row.details_json)
        if details.get("price") is not None:
            price_n += 1
        gross = Decimal(details.get("gross_edge", "0"))
        if gross > 0:
            gross_n += 1
        if row.disposition == "EVALUATED_EXECUTABLE":
            net_n += 1
    by_model: dict[str, list[CalibrationObservation]] = defaultdict(list)
    for observation in calibration:
        by_model[observation.model].append(observation)
    metrics = {}
    for model, rows in sorted(by_model.items()):
        metrics[model] = {
            "observations": len(rows),
            "independent_events": len({row.independent_event_id for row in rows}),
            "brier": str(
                sum((Decimal(r.brier_contribution) for r in rows), Decimal("0")) / len(rows)
            ),
            "log_loss": str(
                sum((Decimal(r.log_loss_contribution) for r in rows), Decimal("0")) / len(rows)
            ),
            "ece": _ece_calibration(rows),
        }
    counts = Counter(row.disposition for row in dispositions)
    lineage_counts = Counter(row.verdict for row in session.scalars(select(CryptoFeatureLineage)))
    return {
        "status": "RECONCILED" if len(dispositions) == run.processed else "MISMATCH",
        "run_id": run.run_id,
        "model": run.model,
        "processed": run.processed,
        "disposition_total": len(dispositions),
        "dispositions": dict(sorted(counts.items())),
        "calibration_observations": len(calibration),
        "snapshot_coverage": snapshot_n,
        "feature_coverage": feature_n,
        "executable_price_coverage": price_n,
        "positive_gross_edge": gross_n,
        "positive_net_edge": net_n,
        "calibration_metrics": metrics,
        "crypto_lineage_verdicts": dict(sorted(lineage_counts.items())),
    }


def _ece_calibration(rows: list[CalibrationObservation]) -> str:
    bins: dict[int, list[tuple[Decimal, int]]] = defaultdict(list)
    for observation in rows:
        probability = Decimal(observation.forecast_probability)
        outcome = 1 if observation.settlement_result.strip().lower() == "yes" else 0
        bins[min(int(probability * 10), 9)].append((probability, outcome))
    total = Decimal(len(rows))
    result = Decimal("0")
    for values in bins.values():
        confidence = sum((value[0] for value in values), Decimal("0")) / len(values)
        accuracy = Decimal(sum(value[1] for value in values)) / len(values)
        result += Decimal(len(values)) / total * abs(confidence - accuracy)
    return str(result)
