# Phase 4NV — Witness Placement and Common-Mode Failure

## Outcome

Phase 4NV treats witness independence as an evidence-backed placement property across machine, WSL
distribution, host OS, storage, network, power, administrator, software build, signing authority,
and geography. It computes the largest pairwise-independent witness subset, detects hidden shared
dependencies, and rejects independence claims beyond the evidence.

Bounded outage and compromise enumeration re-evaluates the selected quorum's safety and liveness
for every domain/value. The audit rejects co-located quorum, shared storage, administrators, builds,
or signing authorities, insufficient diversity, undocumented dependencies, missing evidence, and
common-mode risk inconsistent with the claimed policy. Recommendations prioritize moving strict
trust domains first.

## Verification evidence

- Focused Phase 4NV suite: `8 passed`
- Placement domains: `10`
- Effective independent witnesses in the reference layout: `4`
- Single-domain outage/compromise cases: `80`
- Failure-enumeration SHA-256: `2f6a4e1fb0307742be6f4661dbb912c9af6b3351ae3bf98e023d3ba61317ad2b`
- Placement verdict: `PASS`
- Placement SHA-256: `51d745cb045f7a279cef1a5e2787bfba2891677c9cf8fada3653bd126075b363`

## Safety and removal

The analysis is offline and non-persistent and cannot alter infrastructure, access runtime services,
create orders, or enable paper, demo, live, or autopilot execution. Remove the three phase files to
roll back.

## Next phase

Phase 4NW — Witness placement migration simulation and staged cutover proof.
