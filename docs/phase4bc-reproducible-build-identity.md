# Phase 4BC — Reproducible build identity

Phase 4BC creates a source-level identity for the offline verifier and disposable sandbox prototype.
It binds repository-relative source hashes, dependency-lock hashes, Python implementation/version,
platform/compiler identity, sorted build options, test file hashes, discovered artifact-schema
versions, and a safety capability manifest.

Inputs must be regular non-symlink files inside the declared repository root. Missing, duplicate,
outside-root, or symlinked files fail closed. The capability scan refuses service control, exchange
clients, production writer locks, and literal production runtime paths. A disposable sandbox may be
represented, but the identity always states that no production executor exists.

Outputs are `phase4bc.reproducible-build-identity.v1` and
`phase4bc.build-reproducibility-proof.v1`. Identical inputs produce byte-for-byte equal payloads;
changes to source, locks, tests, options, interpreter, or platform change the identity. The phase
publishes no binary and performs no deployment.

Focused tests cover deterministic ordering, every bound input class, schema discovery,
interpreter/platform inclusion, symlink/outside/duplicate/missing files, invalid options, prohibited
capabilities, and the real Phase 4AT/4BA guarded sources.

