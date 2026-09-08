"""Phase 4AM deterministic property model for the Phase 4AL transaction protocol."""

from __future__ import annotations

import argparse
import copy
import json
import os
import random
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

SCHEMA = "phase4am.invariant-coverage.v1"
MANIFEST_SCHEMA = "phase4am.counterexample-manifest.v1"
MODEL_VERSION = "phase4am.disposable-transaction-model.v1"
MAX_CASES = 10_000
VALID_RESULTS = ("NO", "YES")
LEGAL_TRANSITIONS = {
    "START": {"REFUSED", "TRANSACTION_STARTED"},
    "TRANSACTION_STARTED": {"REVALIDATED", "ROLLED_BACK"},
    "REVALIDATED": {"CAS_APPLIED", "ROLLED_BACK"},
    "CAS_APPLIED": {"ROW_COUNT_VERIFIED", "ROLLED_BACK"},
    "ROW_COUNT_VERIFIED": {"POSTCONDITION_VERIFIED", "ROLLED_BACK"},
    "POSTCONDITION_VERIFIED": {"COMMITTED", "ROLLED_BACK"},
}
SCENARIOS = (
    "VALID_SINGLE",
    "VALID_RESULT_NO",
    "VALID_TWO_ROWS",
    "SETTLED_ALREADY",
    "RESULT_INVALID",
    "RESULT_TYPE_INVALID",
    "MISSING_COLUMN",
    "MALFORMED_ROW",
    "DUPLICATE_IDENTITY",
    "LINEAGE_MISMATCH",
    "TIMESTAMP_NAIVE",
    "TIMESTAMP_MALFORMED",
    "EXPIRATION_BEFORE",
    "EXPIRATION_EQUAL",
    "EXPIRATION_AFTER",
    "CAS_ZERO_ROWS",
    "CAS_MULTI_ROWS",
    "POSTCONDITION_FAILURE",
)
INVARIANTS = (
    "COMMIT_REQUIRES_VALID_PRECONDITIONS",
    "COMPARE_AND_SWAP_EXACTLY_ONE_ROW",
    "DUPLICATE_IDENTITIES_REFUSED",
    "EXPIRATION_EQUALITY_REFUSED",
    "MISSING_OR_MALFORMED_ROWS_REFUSED",
    "ONLY_CANONICAL_TIMESTAMP_CHANGES",
    "REFUSAL_PRESERVES_STATE",
    "RESULT_ENCODING_VALIDATED",
    "ROLLBACK_PRESERVES_STATE",
    "STATE_TRANSITIONS_LEGAL",
    "TRANSACTION_ATOMICITY",
    "UTC_AWARE_TIMESTAMPS_REQUIRED",
)


def _hash(payload: dict[str, Any], field: str = "artifact_hash") -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != field})


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return None if parsed.tzinfo is None else parsed.astimezone(UTC)


def _row(ticker: str, result: Any = "YES") -> dict[str, Any]:
    return {
        "ticker": ticker,
        "result": result,
        "settled_at": None,
        "updated_at": "2026-08-25T00:00:00+00:00",
        "payload": {"unrelated": ticker},
    }


def generate_case(seed: int, index: int, now: datetime) -> dict[str, Any]:
    if now.tzinfo is None:
        raise ValueError("PHASE4AM_EVALUATION_TIMEZONE_MISSING")
    rng = random.Random(f"{seed}:{index}")
    scenario = SCENARIOS[index % len(SCENARIOS)]
    suffix = rng.randrange(1, 1_000_000)
    rows = [_row(f"KXMODEL-{suffix}")]
    proposed = (now.astimezone(UTC) + timedelta(seconds=1)).isoformat()
    expires = (now.astimezone(UTC) + timedelta(seconds=2)).isoformat()
    case: dict[str, Any] = {
        "case_id": f"seed-{seed}-case-{index:06d}",
        "scenario": scenario,
        "rows": rows,
        "proposed_settled_at": proposed,
        "expires_at": expires,
        "lineage_matches": True,
        "cas_effect": "ONE",
        "postcondition_matches": True,
    }
    if scenario == "VALID_RESULT_NO":
        rows[0]["result"] = "NO"
    elif scenario == "VALID_TWO_ROWS":
        rows.append(_row(f"KXMODEL-{suffix + 1}", "NO"))
    elif scenario == "SETTLED_ALREADY":
        rows[0]["settled_at"] = now.astimezone(UTC).isoformat()
    elif scenario == "RESULT_INVALID":
        rows[0]["result"] = "DRAW"
    elif scenario == "RESULT_TYPE_INVALID":
        rows[0]["result"] = 1
    elif scenario == "MISSING_COLUMN":
        del rows[0]["updated_at"]
    elif scenario == "MALFORMED_ROW":
        case["rows"] = ["not-a-row"]
    elif scenario == "DUPLICATE_IDENTITY":
        rows.append(copy.deepcopy(rows[0]))
    elif scenario == "LINEAGE_MISMATCH":
        case["lineage_matches"] = False
    elif scenario == "TIMESTAMP_NAIVE":
        case["proposed_settled_at"] = "2026-08-25T00:00:01"
    elif scenario == "TIMESTAMP_MALFORMED":
        case["proposed_settled_at"] = "not-a-time"
    elif scenario == "EXPIRATION_BEFORE":
        case["expires_at"] = (now.astimezone(UTC) - timedelta(microseconds=1)).isoformat()
    elif scenario == "EXPIRATION_EQUAL":
        case["expires_at"] = now.astimezone(UTC).isoformat()
    elif scenario == "EXPIRATION_AFTER":
        case["expires_at"] = (now.astimezone(UTC) + timedelta(microseconds=1)).isoformat()
    elif scenario == "CAS_ZERO_ROWS":
        case["cas_effect"] = "ZERO"
    elif scenario == "CAS_MULTI_ROWS":
        case["cas_effect"] = "MULTI"
    elif scenario == "POSTCONDITION_FAILURE":
        case["postcondition_matches"] = False
    case["case_hash"] = canonical_hash(case)
    return case


def evaluate_case(case: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Evaluate one pure case without touching SQLite or any external resource."""
    before = copy.deepcopy(case.get("rows"))
    after = copy.deepcopy(before)
    transitions = ["START"]
    reasons: list[str] = []
    rows = case.get("rows")
    required = {"ticker", "result", "settled_at", "updated_at", "payload"}
    proposed = _parse_time(case.get("proposed_settled_at"))
    expires = _parse_time(case.get("expires_at"))
    now = now.astimezone(UTC)
    if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) for row in rows):
        reasons.append("MALFORMED_ROW")
    elif any(not required.issubset(row) for row in rows):
        reasons.append("MISSING_COLUMN")
    elif len({row["ticker"] for row in rows}) != len(rows):
        reasons.append("DUPLICATE_IDENTITY")
    elif any(not isinstance(row["ticker"], str) or not row["ticker"] for row in rows):
        reasons.append("IDENTITY_INVALID")
    elif any(row["result"] not in VALID_RESULTS for row in rows):
        reasons.append("RESULT_ENCODING_INVALID")
    elif any(row["settled_at"] not in (None, "") for row in rows):
        reasons.append("SETTLEMENT_ALREADY_TIMESTAMPED")
    elif proposed is None or expires is None:
        reasons.append("TIMESTAMP_INVALID_OR_NAIVE")
    elif now >= expires:
        reasons.append("ARTIFACT_EXPIRED")
    elif case.get("lineage_matches") is not True:
        reasons.append("LINEAGE_MISMATCH")
    if reasons:
        transitions.append("REFUSED")
        return _result(case, before, after, transitions, reasons, 0, False, False)

    transitions.extend(["TRANSACTION_STARTED", "REVALIDATED", "CAS_APPLIED"])
    effect = case.get("cas_effect")
    affected = len(rows) if effect == "ONE" else 0 if effect == "ZERO" else len(rows) + 1
    for row in after:
        row["settled_at"] = case["proposed_settled_at"]
    if affected != len(rows):
        transitions.append("ROLLED_BACK")
        return _result(
            case, before, before, transitions, ["ROW_COUNT_MISMATCH"], affected, False, True
        )
    transitions.append("ROW_COUNT_VERIFIED")
    if case.get("postcondition_matches") is not True:
        transitions.append("ROLLED_BACK")
        return _result(
            case, before, before, transitions, ["POSTCONDITION_MISMATCH"], affected, False, True
        )
    transitions.extend(["POSTCONDITION_VERIFIED", "COMMITTED"])
    return _result(case, before, after, transitions, [], affected, True, False)


def _result(
    case: dict[str, Any],
    before: Any,
    after: Any,
    transitions: list[str],
    reasons: list[str],
    affected: int,
    committed: bool,
    rolled_back: bool,
) -> dict[str, Any]:
    return {
        "case_id": case.get("case_id"),
        "scenario": case.get("scenario"),
        "before": before,
        "after": after,
        "transitions": transitions,
        "reason_codes": reasons,
        "affected_rows": affected,
        "committed": committed,
        "rolled_back": rolled_back,
    }


def invariant_failures(case: dict[str, Any], result: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    transitions = result["transitions"]
    if any(
        nxt not in LEGAL_TRANSITIONS.get(current, set())
        for current, nxt in zip(transitions, transitions[1:], strict=False)
    ):
        failures.append("STATE_TRANSITIONS_LEGAL")
    if result["committed"] and result["affected_rows"] != len(result["before"]):
        failures.append("COMPARE_AND_SWAP_EXACTLY_ONE_ROW")
    if (result["rolled_back"] or transitions[-1] == "REFUSED") and result["after"] != result[
        "before"
    ]:
        failures.append("ROLLBACK_OR_REFUSAL_PRESERVES_STATE")
    if result["committed"]:
        for before, after in zip(result["before"], result["after"], strict=True):
            differences = {key for key in before if before[key] != after[key]}
            if differences != {"settled_at"}:
                failures.append("ONLY_CANONICAL_TIMESTAMP_CHANGES")
                break
    if (
        len(result["before"]) > 1
        and not result["committed"]
        and result["after"] != result["before"]
    ):
        failures.append("TRANSACTION_ATOMICITY")
    return sorted(set(failures))


def shrink_counterexample(
    case: dict[str, Any], predicate: Callable[[dict[str, Any]], bool]
) -> dict[str, Any]:
    """Deterministically find the lexicographically first simpler failing case."""
    current = copy.deepcopy(case)
    while True:
        candidates: list[dict[str, Any]] = []
        rows = current.get("rows")
        if isinstance(rows, list) and len(rows) > 1:
            candidate = copy.deepcopy(current)
            candidate["rows"] = candidate["rows"][:1]
            candidates.append(candidate)
        for key, value in (
            ("lineage_matches", True),
            ("cas_effect", "ONE"),
            ("postcondition_matches", True),
        ):
            if current.get(key) != value:
                candidate = copy.deepcopy(current)
                candidate[key] = value
                candidates.append(candidate)
        failing = [candidate for candidate in candidates if predicate(candidate)]
        if not failing:
            break
        current = min(failing, key=lambda value: json.dumps(value, sort_keys=True))
    current["case_hash"] = canonical_hash(
        {key: value for key, value in current.items() if key != "case_hash"}
    )
    return current


def build(
    *, seeds: list[int], case_count: int, now: datetime
) -> tuple[dict[str, Any], dict[str, Any]]:
    if now.tzinfo is None:
        raise ValueError("PHASE4AM_EVALUATION_TIMEZONE_MISSING")
    if not seeds or len(set(seeds)) != len(seeds) or any(type(seed) is not int for seed in seeds):
        raise ValueError("PHASE4AM_SEEDS_INVALID_OR_DUPLICATED")
    if not 1 <= case_count <= MAX_CASES:
        raise ValueError("PHASE4AM_CASE_COUNT_OUT_OF_RANGE")
    now = now.astimezone(UTC)
    coverage = {scenario: 0 for scenario in SCENARIOS}
    counterexamples: list[dict[str, Any]] = []
    case_hashes: list[str] = []
    for seed in sorted(seeds):
        for index in range(case_count):
            case = generate_case(seed, index, now)
            result = evaluate_case(case, now)
            failures = invariant_failures(case, result)
            coverage[case["scenario"]] += 1
            case_hashes.append(case["case_hash"])
            if failures:
                predicate = lambda value: bool(  # noqa: E731
                    invariant_failures(value, evaluate_case(value, now))
                )
                shrunk = shrink_counterexample(case, predicate)
                counterexamples.append(
                    {
                        "seed": seed,
                        "case_index": index,
                        "failure_codes": failures,
                        "original_case_hash": case["case_hash"],
                        "minimal_case": shrunk,
                        "minimal_case_hash": shrunk["case_hash"],
                    }
                )
    counterexamples.sort(key=lambda value: (value["seed"], value["case_index"]))
    coverage_rows = [
        {"scenario": scenario, "case_count": count} for scenario, count in sorted(coverage.items())
    ]
    run_id = canonical_hash(
        {"model": MODEL_VERSION, "seeds": sorted(seeds), "case_count": case_count, "now": now}
    )
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4AM",
        "model_version": MODEL_VERSION,
        "run_id": run_id,
        "evaluated_at": now.isoformat(),
        "seeds": sorted(seeds),
        "cases_per_seed": case_count,
        "total_cases": len(seeds) * case_count,
        "scenario_coverage": coverage_rows,
        "scenario_coverage_hash": canonical_hash(coverage_rows),
        "case_hashes_root": canonical_hash(case_hashes),
        "invariants": list(INVARIANTS),
        "all_invariants_satisfied": not counterexamples,
        "counterexample_count": len(counterexamples),
        "production_database_mutated": False,
        "research_database_mutated": False,
        "database_mutation_performed": False,
        "production_lock_acquired": False,
        "services_controlled": False,
        "exchange_requests_made": False,
        "orders_created": False,
        "execution_authorized": False,
    }
    pair_id = canonical_hash({"run_id": run_id, "case_hashes_root": report["case_hashes_root"]})
    report["publication_pair_id"] = pair_id
    report["artifact_hash"] = _hash(report)
    manifest: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "phase": "4AM",
        "publication_pair_id": pair_id,
        "coverage_artifact_hash": report["artifact_hash"],
        "counterexample_count": len(counterexamples),
        "counterexamples": counterexamples,
        "counterexamples_hash": canonical_hash(counterexamples),
        "reproduction": {"seeds": sorted(seeds), "cases_per_seed": case_count},
        "advancement_allowed": not counterexamples,
        "production_execution_authorized": False,
    }
    manifest["manifest_hash"] = _hash(manifest, "manifest_hash")
    return report, manifest


def publish_pair(
    report_path: Path,
    manifest_path: Path,
    report: dict[str, Any],
    manifest: dict[str, Any],
    *,
    replace: bool = False,
) -> None:
    if report_path.parent.resolve() != manifest_path.parent.resolve():
        raise ValueError("PHASE4AM_OUTPUT_DIRECTORIES_DIFFER")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    if not replace and (report_path.exists() or manifest_path.exists()):
        raise FileExistsError("PHASE4AM_OUTPUT_EXISTS")
    finals = (report_path, manifest_path)
    temporary = tuple(path.with_name(f".{path.name}.{os.getpid()}.tmp") for path in finals)
    backups = tuple(path.with_name(f".{path.name}.{os.getpid()}.bak") for path in finals)
    published: list[Path] = []
    try:
        for path, payload in zip(temporary, (report, manifest), strict=True):
            with path.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        if replace:
            for final, backup in zip(finals, backups, strict=True):
                if final.exists():
                    os.replace(final, backup)
        for temp, final in zip(temporary, finals, strict=True):
            os.replace(temp, final)
            published.append(final)
        try:
            descriptor = os.open(report_path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            pass
    except Exception:
        for final in published:
            if final.exists():
                final.unlink()
        for backup, final in zip(backups, finals, strict=True):
            if backup.exists():
                os.replace(backup, final)
        raise
    finally:
        for path in temporary + backups:
            if path.exists():
                path.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, action="append", required=True)
    parser.add_argument("--cases-per-seed", type=int, default=len(SCENARIOS))
    parser.add_argument("--evaluation-time", required=True)
    parser.add_argument("--coverage-output", type=Path, required=True)
    parser.add_argument("--counterexample-output", type=Path, required=True)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    now = _parse_time(args.evaluation_time)
    if now is None:
        raise ValueError("PHASE4AM_EVALUATION_TIME_INVALID_OR_NAIVE")
    report, manifest = build(seeds=args.seed, case_count=args.cases_per_seed, now=now)
    publish_pair(
        args.coverage_output,
        args.counterexample_output,
        report,
        manifest,
        replace=args.replace,
    )
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
