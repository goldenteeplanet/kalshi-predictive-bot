# Phase 4JL — Disposable recovery sandbox

Phase 4JL provisions a bounded recovery-simulation fixture beneath an explicit allowed temporary root. A sandbox requires a test-host marker, disposable mode, complete evidence, and a fixture identity distinct from production. Dry-run is the default and performs no mutation.

Explicit fixture mode creates one new sandbox directory and one canonical manifest; it refuses existing targets, symlinks, path escapes, relative paths, production-identity collisions, partial requests, and tampering. Tests write only under pytest temporary directories.

The manifest is read-only simulation evidence. No production database, service, WSL instance, notification provider, process executor, or restart mechanism is accessible, and receipts grant no operational authority. Cleanup remains the temporary-fixture owner's responsibility rather than a recursive deletion surface in this module.
