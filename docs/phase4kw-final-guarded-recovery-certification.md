# Phase 4KW: Final guarded-recovery certification

## Verdict

`CERTIFIED_GUARDED / NOT_ACTIVATED`. The 100-phase paper-only resilient-operations program is complete as implementation, evidence, simulation, rollback design, runbook, and guarded certification. It does not authorize or activate the supervisor, local alert delivery, service control, a Windows restart, trading, or execution. Activation requires a separate explicit operator decision.

## Phase ledger

For every entry below, the committed artifacts are the phase-owned implementation/proposal, deterministic `tests/test_phase<code>_*` test file, and `docs/phase<code>-*.md`; measured impact is the capability named in the authoritative phase matrix; residual risk is operational integration because the capability remains read-only/dry-run/not activated. Every phase passed its focused tests, Ruff, `git diff --check`, and the cumulative regression current at its commit.

- 4HB `a75e29e`; 4HC `28cb44a`; 4HD `596c4f4`; 4HE `de183dc`; 4HF `181e1fc`; 4HG `62bfa5d`; 4HH `2a9ef83`; 4HI `fcce372`; 4HJ `2ee11b3`; 4HK `836e5d0`; 4HL `83ec5a7`; 4HM `a597ac8`; 4HN `7b09663`; 4HO `49d2d90`; 4HP `8468d68`; 4HQ `0225cb4`; 4HR `358be6b`; 4HS `63f9c59`; 4HT `d38de2a`; 4HU `f28ccfc`; 4HV `101bc1a`; 4HW `d3c0313`; 4HX `644791d`; 4HY `a83d52d`; 4HZ `f23f266`.
- 4IA `7b0fa72`; 4IB `4158802`; 4IC `f19a0f2`; 4ID `b7c25a3`; 4IE `fdd6830`; 4IF `702fa4e`; 4IG `34a7e59`; 4IH `e4b03e3`; 4II `e4b3f35`; 4IJ `f40b9c2`; 4IK `2ac43a3`; 4IL `3cbbc37`; 4IM `7d7dc2f`; 4IN `dbf18a2`; 4IO `266cdfe`; 4IP `8261ddd`; 4IQ `38fd0ff`; 4IR `10ee285`; 4IS `3c38b2a`; 4IT `5fa4306`; 4IU `d8d7bf0`; 4IV `1175928`; 4IW `fef19d3`; 4IX `af61262`; 4IY `3e84fc5`; 4IZ `f92b8ec`.
- 4JA `ac55673`; 4JB `e8b2c81`; 4JC `2361590`; 4JD `eda1d1f`; 4JE `2d80f87`; 4JF `7353aeb`; 4JG `e4ef96e`; 4JH `bc0f030`; 4JI `91c2828`; 4JJ `be7221b`; 4JK `2aa5d63`; 4JL `f14c0eb`; 4JM `6137446`; 4JN `01ba59a`; 4JO `b646421`; 4JP `648f5be`; 4JQ `30b06b2`; 4JR `0b24485`; 4JS `573569a`; 4JT `a879117`; 4JU `176cde6`; 4JV `b1fe418`; 4JW `8846ecb`; 4JX `0791033`; 4JY `d6918b4`; 4JZ `653e02f`.
- 4KA `66a4f03`; 4KB `601e35c`; 4KC `d33b614`; 4KD `92efeb3`; 4KE `05fc647`; 4KF `32a4b9f`; 4KG `c70cf3b`; 4KH `575b8c5`; 4KI `29c4b29`; 4KJ `3e49092`; 4KK `2be4f4c`; 4KL `39ff29a`; 4KM `f449484`; 4KN `471694d`; 4KO `c0e5398`; 4KP `c1d5d7d`; 4KQ `7282437`; 4KR `046bac9`; 4KS `ba6b1e8`; 4KT `e125e5e`; 4KU `b0d8d61`; 4KV `74d9c1a`; 4KW `this phase4kw commit`.

The authoritative capability/impact description for each code is the phase matrix in `docs/phase4hb-4kw-resilient-operations-codex-goal.md`. Phase 4KW focused tests are recorded in `tests/test_phase4kw_final_guarded_recovery_certification.py`; final cumulative validation is recorded below.

## Final measured state and invariants

Read-only observations on 2026-08-28:

- WSL: Ubuntu running under WSL 2; `docker-desktop` stopped.
- Authoritative scheduler: `kalshi-fixed-rate-refresh.service` loaded, active, running, MainPID 307.
- Writer inventory: the authoritative refresh script at PID 307 was the sole matching production writer; the inspection command itself was the only additional matching shell line.
- Database opened with SQLite `-readonly` and `PRAGMA query_only=ON`.
- `paper_orders`: count/max 204/204.
- `position_sizing_decisions`: count/max 239/239.
- `advanced_risk_decisions`: count/max 239/239.
- Order 204: `KXRAINAUSM-26AUG-1`, forecast 523912, quantity 1, `FILLED`.
- Order 204 fill count/quantity: 1/1; maximum referenced forecast: 523912; Phase 3M/3N protected maxima remain 231/231 through the corresponding sizing/risk tables.
- Supervisor startup task: not installed or activated by this goal.
- Local alert delivery: not installed or activated; only capability evidence and previews exist.
- Restart adapter: not activated; no restart occurred. Cooldown history/budget are therefore not initialized by this work, and remaining restart authority is zero until separately activated and freshly evaluated.
- Remote: `origin` is configured, but no push was performed.

## Validation, rollback, and residual risk

Before Phase 4KW, 704 cumulative tests passed across 100 test files. Phase 4KW added fifteen focused tests; the final 101-file cumulative gate passed all 719 tests in 213.03 seconds. All edits remain confined to phase-owned commits; the pre-existing dirty inventory remains uncommitted and preserved.

No installation occurred, so rollback currently requires no system mutation. If activation is later authorized, use the reviewed Phase 4KB sequence in exact order: disable the proposed startup task; remove it; restore the previous signed configuration; preserve incident and restart history; verify task absence; verify supervisor absence. Never delete recovery evidence. The removal command must be generated from the verified task identity at activation time, previewed to the operator, and recorded before execution.

Residual risk is operational: real Windows toast delivery, startup behavior, component recovery, and non-forced restart have not been exercised on this host because the safety contract prohibits using real control actions merely to prove them. The certification establishes guarded design and simulation readiness, not trade readiness or live-restart authority.

## Recommended next Codex Goal

Run a separately authorized, operator-attended **local alerting and supervisor activation readiness** goal. It should re-verify current hashes and protected invariants, generate exact reversible Task Scheduler configuration/removal previews, activate local alerting first, observe a no-action soak period, and keep the real restart adapter disabled until a distinct approval is given. Do not include trading activation.
