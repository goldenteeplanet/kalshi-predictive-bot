# Phase 4NJ — Adversarial Backtest Reproducibility Bundle

## Outcome

Phase 4NJ packages canonical adversarial-backtest inputs, configuration, explicit seeds, scenario
identities, engine/schema fingerprints, expected outputs, per-artifact hashes, and an envelope hash.
The verifier checks the complete bundle before replaying every scenario through separate
scenario-by-scenario orchestration twice.

Missing or altered artifacts, unsupported schemas, model/version drift, scenario drift, changed
safety invariants, output mismatches, and nondeterminism all refuse verification explicitly. The
bundle is in-memory evidence; it does not write files or contact a runtime.

## Verification evidence

- Focused adversarial regression suite: `25 passed`
- Independently orchestrated scenarios: `17`
- Bundle SHA-256: `f27b80dbd6c1fb4310d6ac1da4e260edc25cf9abfa4cbf7e66ae7353608a7bf0`
- Engine source SHA-256: `87749bb05ceee518c87c971f2318420c18ffcd306d04e50f9d4a290a561d51dd`
- Replay output SHA-256: `3e6fdb86e76d41b085106db017bd68f1eb7b52d3508d603bc8eb9c2cffdc8107`
- Verification verdict: `PASS`
- Verification SHA-256: `591f1e1c07faa66dfee84bf9fe330f4dc1437ffabaa096be39c9d800ed89ad49`

## Safety and removal

Creation and verification are offline, deterministic, non-persistent, and unable to access the
network, control services, create orders, or enable paper, demo, live, or autopilot execution.
Remove the three phase files to roll back.

## Next phase

Phase 4NK — Cross-platform canonicalization and replay portability proof.
