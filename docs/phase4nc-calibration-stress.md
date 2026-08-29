# Phase 4NC — Calibration Stress and Tail-Bin Proof

## Outcome

Phase 4NC calculates weighted Brier score and reliability/resolution/uncertainty decomposition, log
loss, adaptive-bin expected and maximum calibration errors, Wilson intervals, and calibration
slope/intercept. Correlated event groups can be collapsed to equal group weight.

## Calibration boundary

Platt and isotonic calibration fits carry exact training-sample identity and are applied out of
sample. A deliberately test-fitted isotonic control is labeled leaky and never accepted. Sparse
tails and wide bin intervals refuse calibration claims even when aggregate scores are available.

## Reproducible evidence

- focused Phase 4NB–4NC suite: 20 passed
- calibration report SHA-256: `ed4d33b5f5dd4e840291e5083472f97d42e652c07960ffb51d8d96df0f19f46b`
- Brier score: `0.18963414634146342`
- reliability: `0.03643478584176086`
- resolution: `0.09125000000000001`
- uncertainty: `0.24437499999999998`
- tail observations: 8
- training-only/leaky-control comparison SHA-256: `efccc4aa6fc70f23a61ad765078cadb6e82cd5467a19781926f9b51044193911`
- sparse-tail refusal SHA-256: `5bcaffc794f850e8a648c246df0478fccbec11d70824689356037e9a00c74dcf`

## Safety and removal

All calculation is offline and non-persistent, with no network, runtime, paper, demo, live,
autopilot, or order capability. Remove the three phase files to roll back.

## Next phase

Phase 4ND — Market microstructure replay and pessimistic fill-envelope proof.
