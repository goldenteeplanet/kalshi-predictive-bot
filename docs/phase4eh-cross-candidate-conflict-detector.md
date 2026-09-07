# Phase 4EH — Cross-Candidate Conflict Detector

Phase 4EH detects candidate sets that cannot simultaneously satisfy exposure, expected
loss, drawdown, liquidity, or group-concentration limits. It exhaustively examines bounded
candidate subsets and emits only inclusion-minimal conflicts, so multi-way conflicts are
found even when all individual candidates and pairs pass.

All constraint arithmetic uses exact Decimal values. Equality with a limit passes; any
excess conflicts. Candidate and reason ordering are deterministic, and concentration is
aggregated only within its named group. Mixed snapshot lineage, invalid limits or values,
duplicate identities, oversized inputs, schema drift, and hash tampering fail closed.

The detector is artifact-only: it reserves no capital, creates no risk decision or order,
and publishes atomically without database, service, or exchange access.
