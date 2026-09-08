# Phase 4BD — Dependency and supply-chain integrity audit

Phase 4BD parses guarded source imports and audits only their declared non-standard dependencies from
a hash-protected local policy. Each component binds a locked version, lock-file hash, local
provenance JSON/hash, import name, executable hooks, generated files, and declared capabilities.

Lock, provenance, or version drift; unexpected hooks; mutation/network capability; declared but
unused dependencies; duplicate components; and unexplained imports are detected. All source, lock,
and provenance paths must be regular non-symlink files inside the repository root. The phase never
installs, upgrades, downloads, imports, or executes a dependency.

Outputs are `phase4bd.local-sbom.v1` and `phase4bd.dependency-risk-report.v1`. Focused tests cover
valid local provenance, lock/provenance/version drift, hooks, generated metadata, mutation/network
capabilities, unused dependencies, unexplained imports, duplication, path attacks, tampering,
timezone handling, and the absence of install/network/subprocess/database code paths.

