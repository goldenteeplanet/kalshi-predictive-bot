# Phase 4FU — Read-Model Staleness Escalation

## Outcome

Phase 4FU adds a deterministic classifier for hash-protected read-model freshness and progress
evidence. It distinguishes old snapshots from fresh snapshots whose watermark has stopped moving,
and it treats invalid lineage as a separate highest-priority failure.

## Contract and boundaries

| Status | Boundary | Action |
|---|---|---|
| `FRESH` | snapshot age below warning and progress age below stall | `NO_ACTION` |
| `WARNING` | snapshot age equal to or above warning, below stale | `MONITOR_CLOSELY` |
| `STALE` | snapshot age equal to or above stale | `ESCALATE_SOURCE_STALENESS` |
| `STALLED` | progress age equal to or above stall | `ESCALATE_RECONCILIATION` |
| `LINEAGE_FAILURE` | lineage flag false | `ESCALATE_CRITICAL` |

Precedence is lineage failure, stalled progress, stale snapshot, warning, then fresh. Thresholds
must satisfy `0 < warning < stale` and `stall > 0`. Future or naive timestamps fail closed.

## Inputs, identity, and output

The evidence artifact uses `phase4fu-read-model-staleness-v1` and binds snapshot/progress times,
lineage validity, source identity, and watermark with canonical SHA-256. The frozen output includes
status, action, both measured ages, reason, and evidence hash.

## Safety and verification

The classifier is pure and has no filesystem, database, network, exchange, service-control,
publication, or mutation interface. Tests cover fresh/warning, every exact escalation boundary,
precedence, empty/partial input, tampering, malformed/future timestamps, invalid thresholds, input
immutability, and absence of writer methods. The cumulative Phase 4FN–4FU regression passed 76
tests in 62.48 seconds. Ruff and mypy passed for the phase-owned files.

## Rejected alternatives

- A single age threshold was rejected because it conflates source freshness with no progress.
- Treating lineage failure as stale was rejected because tampering requires critical escalation.
- Automatic refresh or reconciliation was rejected; this phase classifies evidence only.

## Removal and next dependency

Remove the module, test, and report. No runtime rollback is required. Phase 4FV should present the
validated provenance, watermark, freshness, chain, and escalation state through an honest
read-only dashboard model.
