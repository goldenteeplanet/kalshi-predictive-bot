# Phase 4MD — Repair Authorization Envelope and Single-Use Token Model

## Outcome

Phase 4MD binds a short-lived HMAC-authenticated authorization envelope to the exact certified
repair-plan hash, incident identity, recovery epoch, trusted prefix and checkpoint, complete action
list, fail-closed invariant snapshot, issuer, audience, validity window, and unique nonce.

## Single-use boundary

Validation accepts an external consumed-nonce ledger and refuses a nonce already present. On a
valid unused token it emits a proposed hash-bound consumption receipt that explicitly requires
durable persistence before any future executor may act. The validator does not persist that receipt
or authorize execution, keeping issuance and validation capability-free and deterministic.

## Reproducible evidence

- Token ID: `0d3f593153f9eeedf1b70a8d528739734a49c787f1d50477c83aa25908075e22`
- Successful validation SHA-256:
  `3406b42aad35d427a9e5867475584e6dfe7018299866914e2ca90691aaa2b473`
- Proposed consumption-receipt SHA-256:
  `913ec6f927271dfd0c5a78fee657687fec649481d64a5a3b7b2cdff55b0f9d40`
- Replay-refusal SHA-256:
  `96b107a0938996748f36bb89c1eb94f8baee28b346dfe7814a6c234d4925715e`

## Safety and removal

The implementation is offline. It cannot execute a repair, edit checkpoints, persist a nonce,
write runtime state, control WSL or services, access the network, or create a live, demo, autopilot,
or paper order. Remove the script, focused test, and this report to roll back.

## Next phase

Phase 4ME — Atomic nonce-consumption ledger model and crash-consistency proof.
