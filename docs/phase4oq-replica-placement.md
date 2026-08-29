# Phase 4OQ — Replica Placement and Correlated-Loss Resistance

## Outcome

Phase 4OQ binds every complete lineage replica to explicit host, filesystem, administrator, and
failure-domain placement plus a composite dependency hash. The audit requires diversity in every
dimension, unique replica identities and hidden-dependency fingerprints, exact trusted history,
and valid placement hashes.

It deterministically simulates loss of every individual placement value and specified multi-domain
combinations. A recovery claim is allowed only if complete surviving replicas still meet quorum in
every scenario. Shared dependencies, incomplete or wrong histories, tampering, duplicate scenarios,
invalid quorum, and any correlated loss below quorum fail closed.

## Verification evidence

- Ruff: passed.
- Focused Phase 4OP/4OQ regression: `13 passed in 34.50s`.
- Diverse replicas: `5`; simulated loss scenarios: `23`.
- Minimum complete survivors: `3` against quorum `2`.
- Placement verdict and recovery claim: `PASS`, `true`.
- Audit SHA-256: `c15d37c45e37710df065d40b5edc676c4dfee60d323aa12af56c5b89860dbed0`.

## Safety and removal

The placement and loss simulation is offline, in-memory, and non-persistent. It cannot create paper
orders or enable demo, live, or autopilot execution. Remove the three Phase 4OQ files to roll back.

## Next phase

Phase 4OR — Placement migration ceremony and continuous diversity-drift detection.
