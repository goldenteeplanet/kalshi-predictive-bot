# Phase 4MN — Independent Handoff Verifier and Consensus Gate

## Outcome

Phase 4MN adds a second semantic verifier that does not import or call Phase 4ML verification logic.
After the shared bounded lexical preflight, it independently validates canonical JSON, schemas,
hashes, descriptors, safe paths, audience/time, commit bindings, evidence semantics, inventory,
risks, instructions, secret exclusion, and all-false capabilities.

## Consensus

The gate compares verdict plus package hash, manifest hash, component count, and safe-extraction plan
for a valid package. Invalid fuzz cases require unanimous refusal. It pins deterministic source-level
implementation identities and localizes the first differing semantic field; disagreement, stale
identity, exception, or missing fuzz coverage refuses certification.

## Reproducible evidence

- Independent implementation identity SHA-256:
  `8aaac79bbaf1f79d7f99db72556a87f419f9fa0993ae2a3617f4f3464e80cda4`
- Valid-package consensus SHA-256:
  `6e3cf03fb5174bf2ee3b96d4621cdc9d01b566e267a61d49ebcc603134a0ee8a`
- Fuzz consensus: 22 of 22 unanimous refusals, SHA-256
  `9563615dc326184e0022b654f7d59bf7dcc049d4815e45f5e9741109831c7874`.
- Valid package returned unanimous `PASS` with no differing semantic field.

## Safety and removal

Both paths are offline, bounded, read-only, and in memory. They never write or extract packages,
access the network, change runtime state, control WSL or services, or create any order. Remove the
script, focused test, and report to roll back.

## Next phase

Phase 4MO — Verifier disagreement mutation injection and adjudication protocol.
