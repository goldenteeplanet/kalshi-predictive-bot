# Phase 4PA — Restart Observability and Alert Routing

## Outcome

Phase 4PA maps every Phase 4OZ expected refusal code to severity, stable deduplication key,
user-visible text, recovery owner, and escalation deadline. Failed recovery, invariant violations,
and the third repeated restart escalate immediately to critical. All 76 injected fault cases produce
dry-run alert artifacts.

Aggregation groups related alerts but preserves every constituent hash and suppresses none. Unknown
or extra codes, muted or invisible alerts, malformed hashes, contradictory fields, and any real
delivery attempt fail closed. This phase proves routing only and sends no notification.

## Verification evidence

- Targeted Phase 4OZ/4PA regression: `11 passed in 38.05s`
- Fault cases routed: `76`; alert artifacts produced: `76`
- Expected refusal codes mapped: `7`; unmapped codes: `0`
- Real notifications sent: `0`
- Fault campaign SHA-256: `f0cc9bc589a48eb2b0597c0ed4a608a9889ca4caa0997bc8889ab45225d4f253`
- Aggregate alert SHA-256: `057714a4e344c91865948ac25272d1f45c1c976b28aba5f0f20729580c690448`
- Coverage SHA-256: `064f891f76ff411bab41b07373521f7225a6db8fc80e819fae7a2dd87c2bb5f8`
- Verdict: `PASS`

## Safety and removal

Alert routing is offline, in-memory, and `DRY_RUN`; no notification or restart occurs. It cannot
create paper orders or enable demo, live, or autopilot execution. Remove the three Phase 4PA files
to roll back.

## Next phase

Phase 4PB — Alert-delivery acknowledgement, retry, and escalation-state-machine proof.
