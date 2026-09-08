# Phase 4BR — Stage-Level Latency Budget

Phase 4BR binds explicit budgets to every critical-path node and distinguishes hard deadlines,
soft budgets, expected operator waits, and external-source delays. Each row is linked to the Phase
4BP baseline and Phase 4BQ DAG through upstream hashes.

Compliance uses an inclusive exact boundary. Soft, wait, and external overruns remain measured
breaches; a hard-deadline overrun blocks advancement. The phase emits proposals and compliance
evidence only and never applies runtime configuration.
