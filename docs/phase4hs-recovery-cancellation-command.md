# Phase 4HS — Recovery cancellation command

## Purpose

Phase 4HS defines a deterministic, fail-closed command for revoking an existing recovery authority. The command is a pure validation contract: it does not contact services, alter persisted state, restart a host, or authorize trading.

## Contract

The authority state is bound to SHA-256 incident and authorization identifiers and protected by a deterministic state hash. A cancellation command binds that same incident and authorization, an operator identity hash, a reason code, issue and expiry times, and a completeness marker. The full command is protected by its own deterministic hash.

The default maximum command lifetime is 300 seconds. A command is valid at its exact expiry second and stale one second later. Commands issued in the future or with negative or excessive lifetimes are denied.

The only successful transition is `active -> cancelled`:

- `CANCELLED` validates and applies that transition in the returned decision.
- `ALREADY_CANCELLED` is an idempotent no-op.
- `STALE`, `INCOMPLETE`, `TAMPERED`, and `DENIED` never apply a transition.

Every result reports `authority_active_after=false`. This contract cannot create or restore recovery authority.

## Safety boundary

All outputs keep recovery, service control, host restart, and execution authorization false. The evaluator is in-memory and has no filesystem, database, network, subprocess, order, restart, reboot, or shutdown surface. Applying a validated cancellation to an external authority store remains a separate, explicitly gated future concern.

## Verification

The phase tests cover deterministic cancellation, idempotence, exact time boundaries, incomplete and mismatched bindings, malformed inputs, state/command/result tampering, safety-flag tampering, and absence of operational side effects.
