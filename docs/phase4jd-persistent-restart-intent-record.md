# Phase 4JD — Persistent restart intent record

Phase 4JD adds a bounded, append-only JSONL record for guarded restart intents. Each record binds the incident, denial reason, evidence bundle, eligibility decision, warning, cancellation token, attempt counter, and a mandatory pending post-boot verification marker into a hash chain.

The writer requires an absolute `.jsonl` target strictly beneath an explicitly supplied Windows-side root. Dry-run is the default. Real append mode uses append-only OS semantics and an `fsync`; tests mutate only temporary directories. Broken chains, partial records, path escapes, missing safety markers, malformed data, bounds violations, and tampering fail closed.

The receipt records whether persistence occurred and grants no restart, service-control, or execution authority. Deployment must place the journal on the Windows host outside WSL; this phase does not install or activate that configuration.
