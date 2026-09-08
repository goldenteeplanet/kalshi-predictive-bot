# Phase 4OP — Independent Lineage Audit and Retention-Loss Recovery

## Outcome

Phase 4OP independently reconstructs baseline lineage from separately retained replica entries. It
does not call the primary lineage verifier. Exact fields, entry hashes, version sequence, parent
links, promotion references, performance-claim semantics, safety state, retention depth, and the
external trusted head are checked directly.

Recovery requires one unique quorum of allowed replicas holding an identical complete history. A
single damaged replica receives an in-memory exact replacement plan. Missing prefixes or interior
versions, inadequate retention, forged envelopes, identity replay, unknown replicas, stale heads,
ambiguous histories, and rollback masquerading as promotion fail closed and remain frozen.

## Verification evidence

- Ruff: passed.
- Focused Phase 4OO/4OP regression: `14 passed in 31.33s`.
- Two-replica recovery: `PASS`; canonical history SHA-256:
  `e56d87b7481383a2891439d30c832ec3de970031a98d1a2580d72f4fdc2b0067`.
- Preserved trusted head: `0a11680660e082153ab3b9fb80b15e2f19ffc47432157caddedd4134ccd72476`.
- Recovery state: `FROZEN`; capabilities: `false`.
- Recovery SHA-256: `f2e10de5ee00b1eb528af3987bca512d781e626c5ca132a4df70d41f17e8f221`.

## Safety and removal

Auditing and recovery planning are offline, in-memory, and non-persistent. They cannot create paper
orders or enable demo, live, or autopilot execution. Remove the three Phase 4OP files to roll back.

## Next phase

Phase 4OQ — Retention-replica placement diversity and correlated-loss resistance.
