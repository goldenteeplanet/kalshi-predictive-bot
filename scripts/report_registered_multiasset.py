"""Read-only registered-cohort artifact report. No collectors or HTTP calls."""

import argparse
import importlib.util
import json
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path

FROZEN = Path("/opt/kalshi-multiasset-frozen-20260912")
CONTROL = Path("/mnt/kalshi-backup-02/multiasset-control-20260912")
ANALYSIS = Path("/mnt/kalshi-backup-02/multiasset-analysis-protocol-20260912")
TOOLS = Path("/mnt/kalshi-backup-02/multiasset-report-tools-20260912")
EVIDENCE = TOOLS / "multiasset_tournament_evidence.py"


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


def main():
    sys.path.insert(0, str(FROZEN / "src"))
    from kalshi_predictor.crypto.multiasset_capture import (
        at,
        digest,
        encode,
        persist,
        source_manifest,
    )
    from kalshi_predictor.crypto.multiasset_outcomes import verify_capture

    signal.alarm(120)
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if (
        any(p.is_symlink() for p in (args.output, *args.output.parents))
        or args.output.parent
        != Path("/mnt/kalshi-backup-02/multiasset-tournament-reports-20260912")
        or not args.output.name.startswith("multiasset-tournament-report-")
    ):
        raise ValueError("DEDICATED_UNLINKED_REPORT_OUTPUT_REQUIRED")
    args.output.mkdir(exist_ok=False)
    now = datetime.now(UTC)
    runner = module("registered_capture_reader", FROZEN / "scripts/multiasset_cohort_runner.py")
    tools_manifest = json.loads(runner.trusted(TOOLS / "manifest.json"))
    for name, expected in tools_manifest["files"].items():
        if Path(name).name != name or digest(runner.trusted(TOOLS / name)) != expected:
            raise ValueError("REGISTERED_REPORT_TOOLS_CHANGED")
    if Path(__file__).resolve() != (TOOLS / "report_registered_multiasset.py").resolve():
        raise ValueError("FROZEN_REPORT_WRITER_REQUIRED")
    registration_raw = runner.trusted(CONTROL / "registration.json")
    if (
        digest(registration_raw)
        != "797ee2601abac5c58264b7e0234366b3f45cad9f7ff5ecfff4ac7109711b7ca2"
    ):
        raise ValueError("EXACT_COHORT_REGISTRATION_REQUIRED")
    protocol_raw = runner.trusted(ANALYSIS / "protocol.json")
    if digest(protocol_raw) != "401f551e87c2fd0610dab13d8e7e2188867678d58ef40b23834d758679716b3f":
        raise ValueError("EXACT_PREREGISTERED_ANALYSIS_REQUIRED")
    protocol = json.loads(protocol_raw)
    engine_raw = runner.trusted(ANALYSIS / "multiasset_tournament.original.py")
    if digest(engine_raw) != protocol["source_files"]["multiasset_tournament.original.py"]:
        raise ValueError("PREREGISTERED_ANALYSIS_ENGINE_CHANGED")
    engine = module(
        "preregistered_tournament_engine", ANALYSIS / "multiasset_tournament.original.py"
    )
    evidence_raw = EVIDENCE.read_bytes()
    evidence = module("tournament_evidence_reader", EVIDENCE)
    start_manifest = source_manifest()
    slots = []
    rows = []
    economics = []
    captured = 0
    evaluated = 0
    hashes = {
        "registration.json": digest(registration_raw),
        "analysis-protocol.json": digest(protocol_raw),
        "analysis-engine.py": digest(engine_raw),
        "evidence-reader.py": digest(evidence_raw),
        "report-writer.py": digest(Path(__file__).read_bytes()),
    }
    persist(
        args.output / "reservation.json",
        encode({"at": now.isoformat(), "max_provider_requests": 0, "source_hashes": hashes}),
    )
    for number in range(30):
        plan, item, registration = runner.registered(CONTROL, number)
        if plan["source_manifest"] != start_manifest:
            raise ValueError("FROZEN_SOURCE_CHANGED")
        root = Path(item["capture_path"])
        outcome = root.with_name(root.name + "-outcome")
        status = {
            "slot": number,
            "asset": item["symbol"],
            "event": item["event"],
            "target": item["target_at"],
        }
        if not root.exists():
            status["capture"] = (
                "ARTIFACT_ABSENT_NOT_YET_DUE"
                if now < at(item["capture_at"])
                else "ARTIFACT_ABSENT_JOB_STATUS_NOT_INFERRED"
            )
        elif (root / "failure.json").exists() or (
            CONTROL / f"slot-{number}.pin-failure.json"
        ).exists():
            status["capture"] = "FAILURE_MARKER_PRESENT"
        elif (
            not (root / "completion.json").exists()
            or not (CONTROL / f"slot-{number}.pin-receipt.json").exists()
        ):
            status["capture"] = "ARTIFACTS_INCOMPLETE_JOB_STATUS_NOT_INFERRED"
        else:
            try:
                pin = runner.trusted(CONTROL / f"slot-{number}.pin.json")
                pin_receipt = runner.trusted(CONTROL / f"slot-{number}.pin-receipt.json")
                _, decisions, completion_sha = verify_capture(
                    root, pin, pin_receipt, item["plan_sha256"]
                )
                captured += 1
                status.update(
                    capture="VERIFIED_PROSPECTIVE_CAPTURE",
                    decisions=len(decisions),
                    capture_completion_sha256=completion_sha,
                )
                if not (outcome / "completion.json").exists():
                    status["outcome"] = (
                        "FAILURE_MARKER_PRESENT"
                        if (outcome / "failure.json").exists()
                        else "NO_COMPLETED_OUTCOME_ARTIFACT"
                    )
                else:
                    result = evidence.verify_outcomes(
                        root, outcome, pin, pin_receipt, item["plan_sha256"], as_of=now
                    )
                    evaluated += 1
                    rows.extend(result["rows"])
                    economics.extend(result["economics"])
                    status.update(
                        outcome="VERIFIED_OFFICIAL_EVALUATION",
                        score_rows=result["verified_score_rows"],
                        outcome_completion_sha256=result["outcome_completion_sha256"],
                    )
            except Exception as error:
                status["verification_error"] = type(error).__name__ + ":" + str(error)
        slots.append(status)
    report = {
        "at": datetime.now(UTC).isoformat(),
        "as_of": now.isoformat(),
        "scope": "ARTIFACT_AUDIT_NOT_SERVICE_STATE",
        "source_hashes": hashes,
        "slots": slots,
        "verified_captures": captured,
        "verified_evaluated_events": evaluated,
        "registered_event_count": 30,
        "minimum_capture_target_met": captured >= 20,
        "by_asset": {
            asset: {
                "captures": sum(
                    s.get("capture") == "VERIFIED_PROSPECTIVE_CAPTURE" and s["asset"] == asset
                    for s in slots
                ),
                "evaluated": sum(
                    s.get("outcome") == "VERIFIED_OFFICIAL_EVALUATION" and s["asset"] == asset
                    for s in slots
                ),
            }
            for asset in ("BTC", "ETH", "SOL", "XRP", "DOGE")
        },
        "tournament": engine.aggregate(rows),
        "economics": economics,
        "positive_gross_side_cases": sum(e["gross_positive"] for e in economics),
        "positive_full_net_cases": 0,
        "full_net_unknown_cases": len(economics),
        "formal_near_miss_count": None,
        "gross_only_screen_count": sum(e["gross_only_screen_within_5c_of_gate"] for e in economics),
        "mission_paper_created": 0,
        "network_requests": 0,
        "execution_authority": False,
        "limitations": (
            "Gross counts are correlated side/model/rule cases, not independent events. "
            "Current registered costs are unknown. "
            "No completed artifact is inferred from wall clock alone."
        ),
    }
    if source_manifest() != start_manifest or EVIDENCE.read_bytes() != evidence_raw:
        raise ValueError("ANALYSIS_SOURCE_CHANGED_DURING_REPORT")
    persist(args.output / "report.json", encode(report))
    persist(
        args.output / "completion.json",
        encode(
            {
                "at": datetime.now(UTC).isoformat(),
                "report_sha256": digest((args.output / "report.json").read_bytes()),
                "status": "READ_ONLY_REPORT_COMPLETE",
                "report_scope": report["scope"],
            }
        ),
    )
    print(
        json.dumps(
            {k: v for k, v in report.items() if k not in ("slots", "tournament", "economics")},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
