# Phase 4BM — Air-Gapped Acceptance Harness

Phase 4BM evaluates a hash-protected transcript produced entirely from synthetic fixtures or copied
artifacts. Its exact ordered checks cover lineage, disposable simulation, refusal classes,
rollback, determinism, and absence of network, production-path, and service dependencies.

The evaluator has no network, database, subprocess, or service-control imports. Missing, reordered,
failed, malformed, duplicated, or tampered evidence fails closed. A successful report is an offline
acceptance result only and contains `execution_authorized: false`.
