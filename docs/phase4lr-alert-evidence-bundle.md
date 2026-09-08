# Phase 4LR — Alert Evidence Export and Offline Verification Bundle

## Outcome

Phase 4LR builds a deterministic metadata-only manifest across allowlisted Phase 4LL–4LQ artifacts.
It records canonical hashes, byte sizes, schema identities, and dependency edges in topological order;
artifact bodies remain separate and are required for independent offline reconstruction.

## Reproducible verification sample

The six-artifact synthetic chain used by the focused suite rebuilds with verdict `PASS`, manifest
SHA-256 `c1a377e7f6fc1a9e69783aa9db2f3d33c33328c955e67451ac9df88e00deb137`, and offline
verification SHA-256 `008e4afe0daaf07af87836ea298b8e695e3b12405795a7baa497b1f49ee5e09e`.

## Export constraints

Duplicate names or schemas, cycles, missing dependencies, broken hashes, unknown schemas, unsafe
names, excessive sizes or counts, credentials, database URLs, and mutable local runtime paths fail
closed. The verifier rebuilds the complete manifest and requires exact equality.

## Safety and removal

The builder and verifier operate on supplied values only. They cannot write exports, access files
beyond explicit CLI inputs, use a network or database, control services, or create orders. Remove
the script, focused test, and report to roll back.

## Next phase

Phase 4LS — Offline verifier mutation corpus and compatibility matrix.
