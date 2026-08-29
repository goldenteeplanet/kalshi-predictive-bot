# Phase 4LW — Verifier Dependency and Supply-Chain Readiness Gate

## Outcome

Phase 4LW validates explicitly supplied verifier-component attestations against exact package,
version, artifact, license, algorithm, runtime, scan, offline-wheel, provenance, build, and capability
requirements. It neither discovers nor installs dependencies.

## Readiness boundary

The reproducible fixture uses the already observed bundled `cryptography` 50.0.0 version as contract
data only; its hashes are synthetic test values. Fixture readiness may pass, but the current runtime's
production readiness remains `REFUSE` because no independently verified production attestation set
has been supplied. Even a self-declared `INDEPENDENT_PRODUCTION` component remains refused until the
separate Phase 4LX authenticity composition gate validates its external evidence.

## Reproducible gate evidence

The complete fixture returns fixture `PASS` and production `REFUSE` with readiness SHA-256
`3bebb7e00b1c38139e3ae04c11b516c4aa8c8328c8b3cc30f3e4177a8141762d`. A structurally valid but
self-declared production-scoped fixture also remains production `REFUSE`, with SHA-256
`d3f8671972c49b10d586a6f26fedf57668903009d38e41ef2806838078f3be89`.

## Safety and removal

Registry access, installation, network activity, key import, service control, and order capability
are absent. Remove the script, focused test, and report to roll back.

## Next phase

Phase 4LX — Independent-attestation authenticity and freshness composition gate.
