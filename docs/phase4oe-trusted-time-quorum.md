# Phase 4OE — Quorum-Backed Trusted Time and Witness Equivocation

## Outcome

Phase 4OE replaces reliance on one clock observation with independently hashed witness
attestations. A certificate requires an allowed-witness quorum, unique identities, a common round
and parent, an exact prior watermark, and clock dispersion no greater than 30 seconds. Its trusted
time is the deterministic lower median, so input ordering cannot change the result.

A witness that attests to multiple parent/time/watermark statements in one round is identified as
an equivocator and the entire certificate fails closed. Insufficient quorum, replayed identity,
unknown witnesses, wrong rounds or parents, rollback, stale watermarks, excessive dispersion,
tampering, settlement-blocker drift, and safety drift also refuse certification.

## Verification evidence

- Ruff: passed.
- Focused Phase 4OD/4OE regression: `12 passed in 29.19s`.
- Three-witness certificate: `PASS`; trusted time: `2026-08-29T12:00:10+00:00`.
- Certificate SHA-256: `0b53083c034b37833dda4dbc32531323cb080f1f127b1dce951d271f5ce09c37`.
- Equivocating-witness certificate: `REFUSE`; equivocator: `alpha`.
- Equivocation evidence SHA-256:
  `52a88e8557ccf57fdd07a3b2e2cf6188e38216333c256c0d2c994796dced8bbc`.

## Safety and removal

The attestation and certification model is offline, in-memory, and non-persistent. It cannot mutate
infrastructure or runtime state and cannot create paper orders or enable demo, live, or autopilot
execution. Remove the three Phase 4OE files to roll back.

## Next phase

Phase 4OF — Trusted-time quorum membership rotation and key-compromise containment.
