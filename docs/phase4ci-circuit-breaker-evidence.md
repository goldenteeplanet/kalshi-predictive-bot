# Phase 4CI — Circuit-Breaker Evidence Protocol

Phase 4CI implements an offline, artifact-only circuit-breaker evidence model. It never calls a provider, sleeps, changes a runtime setting, or authorizes execution.

## Model

- `DEGRADED_API` identifies a transient run of throttling, timeout, or unavailability evidence.
- `OPEN_API` requires the configured consecutive API-failure threshold.
- `OPEN_DATA_QUALITY` independently requires consecutive `INVALID` evidence. `UNKNOWN` never counts as invalid.
- `RECOVERING` requires an explicit run of observations where the API is `OK` and quality is `VALID`.
- `CLOSED` is restored only at the configured recovery boundary.

Inputs require a canonical hash, exact schema, integer thresholds from 1 through 100, UTC whole-second timestamps in strict chronological order, and closed outcome vocabularies. Reports contain every state decision, transition reason, source hash, and their own canonical hash. Publication uses a same-directory temporary file, file flush and `fsync`, and atomic replacement.

## Safety boundary

The command reads one supplied JSON artifact and may write only the requested status artifact. It has no database, network, exchange-client, service-control, wall-clock, or retry capability. Every report fixes `execution_authorized=false` and `production_records_created=0`.
