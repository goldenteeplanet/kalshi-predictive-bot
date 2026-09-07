from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import Engine, Table

from kalshi_predictor.data import schema
from kalshi_predictor.data.db import get_session_factory, make_engine, make_sqlite_read_only_engine

_RESEARCH_MODELS = {
    "ProspectiveCaptureRun": "prospective_capture_runs",
    "ProspectivePairedCapture": "prospective_paired_captures",
    "ProspectiveCaptureRejection": "prospective_capture_rejections",
    "ProspectivePairEvaluation": "prospective_pair_evaluations",
    "ProspectiveCaptureLease": "prospective_capture_leases",
    "ProspectiveCaptureAlert": "prospective_capture_alerts",
    "ProspectiveHealthSnapshot": "prospective_health_snapshots",
    "ProspectiveStatusLineage": "prospective_status_lineage",
    "ProspectiveSnapshotHandoff": "prospective_snapshot_handoffs",
    "ProspectiveHandoffCounter": "prospective_handoff_counters",
}


def _load_isolated_research_models(research_engine: Engine) -> None:
    """Reflect only sidecar tables when the deployed baseline predates Phase 4I."""
    for class_name, table_name in _RESEARCH_MODELS.items():
        if hasattr(schema, class_name):
            continue
        table = Table(table_name, schema.Base.metadata, autoload_with=research_engine)
        model = type(class_name, (schema.Base,), {"__table__": table})
        setattr(schema, class_name, model)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--research-db", type=Path, required=True)
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--owner-id", required=True)
    parser.add_argument("--batch-limit", type=int, default=50)
    args = parser.parse_args()
    research_engine = make_engine(f"sqlite:///{args.research_db}")
    _load_isolated_research_models(research_engine)
    from kalshi_predictor.phase4cd.operations import handoff_funnel, run_latest_handoff

    research_factory = get_session_factory(research_engine)
    source_factory = get_session_factory(
        make_sqlite_read_only_engine(f"sqlite:///{args.source_db}")
    )
    with research_factory() as research, source_factory() as source:
        payload = run_latest_handoff(
            research,
            source,
            owner_id=args.owner_id,
            batch_limit=args.batch_limit,
            artifact_path=args.artifact,
        )
        payload["funnel"] = handoff_funnel(research)
    print(json.dumps(payload, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
