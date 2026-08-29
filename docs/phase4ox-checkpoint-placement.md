# Phase 4OX — Checkpoint Placement and Correlated Interruption Recovery

## Outcome

Phase 4OX places five checkpoint replicas across independent host, filesystem, administrator,
power, runtime, and failure domains. It evaluates process crash, WSL shutdown, host restart,
filesystem loss, correlated power/zone loss, and an explicitly unsurvivable catastrophic outage at
every one of seven renewal interruption prefixes.

All survivable cases must recover a quorum-valid chain and converge to one uninterrupted
orchestration hash; catastrophic cases must refuse. Hidden shared dependencies, placement hash
tampering, dimension convergence, or incorrect survivability claims revoke recovery certification.

## Verification evidence

- Ruff: passed.
- Focused Phase 4OW/4OX regression: `12 passed in 40.57s`.
- Interruption prefixes: `7`; scenarios: `6`; total cases: `42`.
- Expected recoveries: `35`; expected catastrophic refusals: `7`.
- Unique converged orchestration SHA-256:
  `199cd825cb673fcbd1ea5b2136627a08fb998b10934c30a28491ca178ae79ed7`.
- Matrix verdict: `PASS`; SHA-256:
  `26a3fdd9669b743f17b9e1c5c35003815a38f884d798ad4c6c659872312ddf75`.

## Safety and removal

The interruption matrix is offline, in-memory, and does not stop WSL or any service. It cannot
create paper orders or enable demo, live, or autopilot execution. Remove the three Phase 4OX files
to roll back.

## Next phase

Phase 4OY — WSL restart rehearsal model and service-invariant recovery certification.
