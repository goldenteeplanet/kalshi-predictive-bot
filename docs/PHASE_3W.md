# Phase 3W End-to-End Integration Certification

Phase 3W adds a system-certification audit layer across Phase 1 through Phase 3V.

It does not authorize live trading. It does not enable production execution, demo
execution, order creation, funding, credentials, or live feature flags.

## Commands

```bash
kalshi-bot system-certification-status
kalshi-bot system-certification-run --enable-audit --output-dir reports/system_certification
kalshi-bot system-certification-report --enable-audit --output-dir reports/system_certification
```

## Default Result

The default audit run is expected to return `SYSTEM_INCOMPLETE` until golden replay,
runtime observation, domain scenarios, database restore, and human approval evidence are
captured. `SYSTEM_INCOMPLETE` is the safe result when required evidence is unavailable.

## Artifacts

The audit writes:

- `repo_map.md`
- `phase_capability_inventory.json`
- `connection_graph.json`
- `order_write_path_inventory.json`
- `runtime_access_statement.md`
- `initial_gap_report.md`
- `system_certification_report.json`
- `system_certification_report.md`

## Safety

`SAFE_REPAIR` is disabled by default and requires `PHASE_3W_SAFE_REPAIR_ENABLED=true`.
Safe repair must not change model logic, risk thresholds, capital limits, credentials, live
flags, human approvals, or historical truth.

