# Phase 4ES — Approval Reuse Prohibition Audit

Phase 4ES binds a synthetic operator approval to exactly six decision terms: production
database identity, forecast artifact, limit price, quantity, risk artifact, and expiration
window. The approval binding is the canonical hash of the complete typed term envelope.

The audit requires one independent mutation probe for every term. Each probe must change
exactly its declared dimension, produce a different binding hash, and refuse approval reuse.
An exact replay must retain the original binding. Missing, duplicate, unchanged, multi-field,
malformed, noncanonical, or tampered probes fail closed.

This is an artifact-only proof. It neither records nor consumes a real approval, reads or
writes a database, creates an order, contacts an exchange, nor authorizes execution. Atomic
publication writes only the explicitly requested local report artifact.
