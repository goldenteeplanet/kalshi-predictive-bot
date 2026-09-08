# Phase 4NI — Rare-Joint Scenario Coverage and Mutation

## Outcome

Phase 4NI defines market, model, data, execution, portfolio, settlement, infrastructure, and
operator factors and generates bounded singleton, pairwise, three-way, and targeted higher-order
scenarios. Impossible combinations are excluded and every scenario retains factor/category
provenance and deterministic identity.

## Mutation and coverage boundary

Remove, invert, delay, correlate, duplicate, and amplify mutations are parent-bound. Coverage tracks
factors, pairs, triples, critical interactions, and actual refusal paths from the Phase 4MZ, 4ND,
4NF, 4NG, and 4NH offline models. Uncovered critical interactions or unexpected mutation survivors
refuse certification. Failing cases can be minimized without discarding provenance.

## Safety and removal

Generation and execution remain offline and non-persistent, with no order, network, runtime, paper,
demo, live, or autopilot capability. Remove the three phase files to roll back.

## Verification evidence

- Focused regression suite: `19 passed`
- Bounded generated scenarios: `86`
- Generation SHA-256: `1503fc48c968a6cc8256f6aaead73480464019d4c9f2247ebf5a8ea31365dbe4`
- Coverage verdict: `PASS`
- Coverage SHA-256: `d39f4779db425c91adcd6dde687d7e1f2912ba84bb5641ee2f7243af1ace7566`
- Observed refusal paths: `backtest`, `microstructure`, `portfolio`
- Minimized joint failure: `STALE_BOOK`
- Minimization SHA-256: `028ec71670aa96ba97089e7c3ec56557869e5bd91aca269587812cda35f63c18`

## Next phase

Phase 4NJ — Adversarial backtest reproducibility bundle and independent replay proof.
