# Phase 4JZ — Configuration signature validation

Phase 4JZ validates bounded canonical configuration bytes using an HMAC-SHA256 envelope tied to a trusted key identifier. Configuration hash, key ID, algorithm, signature, completeness, envelope integrity, and a 64 KiB default input bound are checked.

Configuration drift, signature mismatch, wrong keys, unknown algorithms, missing data, malformed fields, oversized input, envelope tampering, and decision tampering fail closed. The verification key is caller-supplied, never included in an artifact, and never persisted or returned.

`VALID` proves signature verification only. It grants no configuration-use, task-activation, restart, service-control, or execution authority. The validator is read-only and has no filesystem, secret-store, Task Scheduler, process, database, notification, or host-control surface.
