# Phase 4GJ — Evidence Cache Cold-Start Seeding Proposal

## Outcome and measured evidence

Phase 4GJ proposes a deterministic bounded cold-start seed set from intact Phase 4GI provenance and
hash-protected candidate descriptors. Focused tests cover valid order independence, empty and partial
inputs, exact age/count/byte boundaries, stale and oversized candidates, malformed, duplicate,
tampered, lineage-mismatched inputs, priority omission, and safety-boundary enforcement. Ruff and
pytest evidence is reproducible from committed files.

## Contract, provenance, freshness, and bounds

- Schema: `phase4gj-evidence-cache-cold-start-seeding-proposal-v1`.
- At most 128 candidates are inspected; defaults select at most eight entries and 256 KiB.
- Candidates are ordered by descending priority, ascending evidence age, then key. Selection never
  exceeds count or byte limits; exact limits remain eligible.
- Intact provenance and candidates through 300 seconds old are required. Any stale evidence rejects
  the entire proposal; a set with no fitting candidate is also rejected explicitly.
- Canonical SHA-256 binds integrity evidence, every candidate, selected content hashes, source
  lineage, limits, decision, reasons, and `execution_authorized=false`.

## Safety, alternatives, rollback, and next dependency

The proposer cannot fetch payloads, populate or mutate a cache, run SQL, publish artifacts, control
services, or contact an exchange. Automatic seeding and partial use of stale candidates were rejected
because they are stateful or hide freshness failure. UI integration is omitted because seeding is an
opt-in control-plane proposal.

Rollback is deletion of the module, focused test, and report. Phase 4GK should independently review
the proposed seed manifest before any implementation or cache write is considered.
