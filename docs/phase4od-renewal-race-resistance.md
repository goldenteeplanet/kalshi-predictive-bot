# Phase 4OD — Renewal Race, Clock Rollback, and Split-Brain Resistance

## Outcome

Phase 4OD adds deterministic offline renewal proposals and independent round adjudication. Every
proposal binds a witness, trusted UTC timestamp, prior watermark, prior proof, aggregate manifest,
settlement blocker, and safety state beneath a content hash.

Concurrent witnesses may submit the same child proof; identical children collapse to one canonical
renewal. Distinct valid children for the same parent are a split brain and fail closed with no
canonical selection. Clock regression, stale watermarks, duplicate witness identities, stale or
wrong parents, altered proposals, expired parents, empty rounds, and invalid renewals also refuse.
Proposal ordering cannot affect the adjudication artifact.

## Verification evidence

- Ruff: passed.
- Focused Phase 4OC/4OD regression: `13 passed in 26.87s`.
- Identical-child adjudication: `PASS`; canonical child:
  `5b6932a7696d7e4e530bbfb609dad926aaa0fcfff920b589c0d4474f29d79e0d`.
- Canonical adjudication SHA-256:
  `675ad46ad9f01fdc1cb2f3d00ca429b4ae17f52971d9361bb30c60663be00a0f`.
- Conflicting-child adjudication: `REFUSE` with `SPLIT_BRAIN_CHILDREN`.
- Split-brain adjudication SHA-256:
  `60bb7e1926e80690bef873344b8d6d2d6cdaedcce64be7787bd03698c3e0c876`.

## Safety and removal

The model is offline, in-memory, and non-persistent. It cannot mutate infrastructure or runtime
state and cannot create paper orders or enable demo, live, or autopilot execution. Remove the three
Phase 4OD files to roll back.

## Next phase

Phase 4OE — Quorum-backed trusted-time attestation and witness equivocation proof.
