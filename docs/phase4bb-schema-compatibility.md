# Phase 4BB — Schema compatibility and migration proof

Phase 4BB validates artifacts against a hash-bound exact-version policy catalog. Each policy declares
required and optional fields, JSON types, whether additional fields are permitted, canonical hash
field, and `RFC8785_SORTED_KEYS_V1` canonicalization. Unknown and forward versions, missing or added
fields, type changes, and canonical hash changes are reported deterministically.

When an unsupported version belongs to a known schema family, the tool emits only a
`CREATE_NEW_TEMPORARY_ARTIFACT` proposal targeting the newest explicitly supported version. It never
rewrites an input artifact or silently accepts forward incompatibility. Unknown families receive no
invented migration.

Outputs are `phase4bb.schema-compatibility-report.v1` and
`phase4bb.deterministic-migration-proposals.v1`, atomically published and non-authorizing. Focused
tests cover exact versions, unknown/forward versions, missing/added fields, required/optional type
changes, canonicalization and hash changes, duplicated or malformed policy, tampering, deterministic
proposal ordering, source immutability, and the artifact-only static surface.

