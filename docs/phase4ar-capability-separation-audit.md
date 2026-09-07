# Phase 4AR — Capability separation audit

Phase 4AR parses guarded settlement source files without importing or executing them. It inventories
imports, CLI options, database-open modes, mutation statements, service/shell calls, exchange
surfaces, writer-lock use, environment mutation, and serialized callback execution.

Each source is assigned one explicit role: review, readiness, simulation, authorization validation,
or hypothetical execution. Review, readiness, and authorization-validation sources may not contain
writable database opens or settlement mutation SQL. Disposable simulation and future hypothetical
execution roles may contain those capabilities, but all roles fail separation if they contain
service control, shell/subprocess execution, exchange order entry, production writer-lock access,
environment-setting mutation, or serialized executable callbacks.

Network-capable dependencies are reported as capability evidence even when they do not alone fail
the separation verdict. Local imports between inventoried files become deterministic graph edges.
Evidence contains capability names and line numbers, never executable snippets or credentials.

Paired outputs are `phase4ar.capability-graph.v1` and
`phase4ar.separation-verdict.v1`. Source SHA-256 identities, sorted imports, CLI options, capability
rows, graph edges, violations, and hashes make the result reproducible. Advancement requires
`CAPABILITIES_SEPARATED` with an empty violation list.

The audit is read-only: it does not import scanned code, open databases, mutate artifacts, control
services, contact exchanges, acquire locks, or grant execution authority.
