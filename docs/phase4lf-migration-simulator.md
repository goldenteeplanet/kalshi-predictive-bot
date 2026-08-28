# Phase 4LF — Evidence Migration Differential Simulator

## Outcome

Phase 4LF simulates deterministic v1-to-v1.1 evidence migrations for every Phase 4LE contract. It
preserves legacy evidence hashes as immutable identities, adds a separate migration binding, proves
idempotence, and requires an exact verification-only downgrade projection back to the source.

## Refusal coverage

The simulator refuses unsupported contracts or versions, malformed and conflicting extension
envelopes, required or immutable field drift, capability-bearing extensions, unknown top-level
fields, non-idempotence, lossy round trips, and downgrade projections that hide required v1.1
semantics. It never publishes migrated evidence.

## Safety and removal

All work is in memory. There is no database, filesystem publication, network, service, lock, or
trading capability. Remove the script, focused test, and report to roll back.

## Next phase

Phase 4LG — Evidence parser resource-bound and adversarial-input audit.
