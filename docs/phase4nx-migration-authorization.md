# Phase 4NX — Migration Authorization and Two-Person Approval

## Outcome

Phase 4NX binds an authorization request to the exact Phase 4NW plan, stage slice, source and target
policies, witnesses, placement evidence, checkpoint anchor, rollback points, time window, nonce,
requester, authorization epoch, and safety state. Two distinct authorized reviewers from independent
domains must sign the exact canonical request.

Verification rejects self or duplicate approval, signature reuse, unauthorized or revoked reviewers,
partial scope, stale/future approval, expiry, plan/policy/evidence/checkpoint/rollback drift, witness
substitution, replay, revocation, rollback, and failed-stage reuse. Remediation requires a new nonce,
epoch, and approvals. A successful grant is single-use and authorizes only the exact offline
simulation stage range.

## Verification evidence

- Focused Phase 4NX suite: `8 passed`
- Independently signed reviewers: `2`
- Exact authorized stage slice: `8` stages
- Bound plan SHA-256: `aeb77cf20b320d297065341b98c0452f9c9db3ad6ca67cbdf1be4d9cfa623acb`
- Request SHA-256: `e9275b99e7f2374be791d03e4e09ee436164dac866505b0a0bde06c2b1e7ff58`
- Grant SHA-256: `6d3a7119be388348796b33cf4431de2e9705b56d88a53c4c2711f3830e5a2d9f`
- Authorization verdict: `PASS`
- Verification SHA-256: `91496d62dc5137086f8395508cb9d7e156637c9cdec0e3ba4c3a17d77c59af73`

## Safety and removal

The authorization layer is offline and non-persistent. Its grant explicitly lacks infrastructure,
runtime, order, paper, demo, live, and autopilot capability. Remove the three phase files to roll
back.

## Next phase

Phase 4NY — Cutover ceremony simulator and operator error-injection proof.
