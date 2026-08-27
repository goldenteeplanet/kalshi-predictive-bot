# Phase 4HL — Local Incident Journal Schema

## Outcome and measured evidence

Phase 4HL defines a deterministic, immutable schema for local incident lifecycle evidence. Entries bind
sequence, incident identity, timestamp, event type, severity, bounded single-line summary, evidence and
source hashes, previous-entry linkage, completeness, schema identity, locality, and denied operational
capabilities. This phase serializes entries in memory and performs no journal write or alert delivery.

Focused tests cover deterministic canonical serialization, every lifecycle event and severity, exact
text bounds, newline rejection, genesis/non-genesis links, malformed enums/hashes/numerics/booleans,
content/hash/schema/safety tampering, and forbidden file/control/mutation surfaces.

## Contract, provenance, freshness, and bounds

- Schema: `phase4hl-local-incident-journal-entry-v1`.
- Incident IDs are limited to 128 characters and summaries to 512 single-line characters.
- Evidence, source, previous-entry, and entry identities use lowercase SHA-256.
- Sequence 1 must link to the all-zero genesis hash; later entries may not.
- Supported lifecycle events cover detection, recovery attempt/outcome, restart schedule/cancellation,
  and post-boot verification/failure.
- Canonical JSON uses sorted keys and compact separators for reproducible hashing and serialization.

## Safety analysis, rejected alternatives, rollback, and next dependency

The schema performs no filesystem, notification, database, WSL, systemd, scheduler, or restart action.
Free-form multiline logs were rejected because they weaken bounds, canonicalization, and safe downstream
parsing. Entries explicitly deny recovery, service control, host restart, and execution authority.

Rollback is deletion of the implementation, focused test, and report. Phase 4HM should implement a
bounded append-only local writer over this schema using temporary fixtures until activation gates pass.
