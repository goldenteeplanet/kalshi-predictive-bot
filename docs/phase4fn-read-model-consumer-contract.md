# Phase 4FN — Read-Model Consumer Contract

## Outcome

Phase 4FN adds a deterministic, artifact-only consumer for the documented Phase 4FM evidence
read-model wire format. It does not import the uncommitted Phase 4FM producer, open a database,
publish an artifact, or expose any execution control.

## Contract

| Property | Rule |
|---|---|
| Input | One JSON byte string or regular file supplied explicitly by the caller |
| Schema | Exact `phase4fm-evidence-read-model-v1` identity |
| Provenance | 64-character source database identity hash and optional previous-artifact hash |
| Integrity | Canonical SHA-256 payload and manifest hashes |
| Watermark | Non-empty `paper_pnl:` prefix by default |
| Freshness | Age is strictly less than the configured maximum; equality is stale |
| Resource bound | File size is checked before reading; default maximum is 1 MiB |
| Output | Frozen `ReadModelView` containing only validated fields |
| Failure | Stable `ReadModelContractError` reason; no partial view is returned |

The required `GUARDED_PAPER` evidence lane must be present and mapping-shaped. Unknown, missing,
or additional top-level fields fail closed so a producer change cannot silently broaden meaning.

## Measured evidence

Focused tests cover the valid path, empty and malformed input, the exact freshness boundary,
staleness, schema/watermark mismatch, missing lanes and fields, payload and manifest tampering,
pre-read byte limits, and an explicit production-mutation tripwire. The focused suite passed 10
tests in 31.29 seconds; the Phase 4FM/4FN compatibility regression passed 19 tests in 42.45
seconds. Ruff and mypy passed for the phase-owned Python files.

## Safety analysis

- The implementation imports only standard-library filesystem, JSON, hashing, and time modules.
- It has no SQLAlchemy/session/exchange imports and exposes no publisher or mutation callback.
- The only filesystem operation is a bounded read of a caller-selected path.
- The returned view is frozen; invalid evidence never becomes a partially trusted mapping.
- Production counts and order-204 lineage are checked outside this module with read-only queries.

## Rejected alternatives

- Importing the Phase 4FM producer was rejected because those prerequisite files are currently
  uncommitted and would make this phase non-reproducible from its commit.
- Database fallback was rejected because it would reintroduce the cold-path contention this
  workstream is intended to remove.
- Permissive schema evolution was rejected; Phase 4FO owns explicit compatibility policy.

## Removal

Remove `read_model_consumer.py`, its focused test, and this report. No database, service, runtime
artifact, configuration, or UI rollback is required.

## Next dependency

Phase 4FO should define an explicit schema compatibility matrix and use this exact-version
consumer as its fail-closed baseline.
