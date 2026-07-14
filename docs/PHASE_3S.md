# Phase 3S Reinforcement Learning Layer

Phase 3S is an offline contextual-bandit research layer. It learns whether an already-defined opportunity should be `SKIP` or `PROCEED` based on decision-time context and finalized net economic reward.

It does not choose direction, set quantity, reserve risk, create orders, submit demo orders, submit live orders, or bypass Phase 3M or Phase 3N.

## Safety

- Disabled by default: `PHASE_3S_REINFORCEMENT_LEARNING_ENABLED=false`.
- Online exploration is disabled by default.
- Governed gate mode is disabled by default.
- `DISABLED` mode preserves existing behavior.
- `SHADOW` mode logs recommendations only.
- Phase 3S returns no contract quantity.
- Phase 3M remains dynamic sizing authority.
- Phase 3N remains final risk authority.

## Commands

```bash
kalshi-bot rl-status
kalshi-bot rl-dataset --enable-research --output reports/rl_dataset_report.md --json-output reports/rl_dataset_report.json
kalshi-bot rl-train --enable-research --output reports/rl_policy_report.md --json-output reports/rl_policy_report.json
kalshi-bot rl-evaluate --enable-research --output reports/rl_policy_report.md --json-output reports/rl_policy_report.json
kalshi-bot rl-shadow-report --output reports/rl_shadow_report.md
kalshi-bot rl-drift-report --output reports/rl_drift_report.md
kalshi-bot scheduler-plan --profile rl-policy-nightly
```

## Reward

The default reward is clipped net ROI:

```text
net_pnl / worst_case_capital_at_risk
```

`gross_pnl`, `net_pnl`, `total_cost`, denominator, raw reward, and clipped reward are stored separately. Invalid denominators produce unavailable rewards and are excluded from training.

## Evidence

Reports separate:

- `LIVE_REALIZED`
- `PAPER_SIMULATED`
- `NO_ACTION`
- `DOWNSTREAM_BLOCKED`

Skipped opportunities do not invent unchosen `PROCEED` rewards. Synthetic-only Phase 3R markets do not create ROI rewards.
