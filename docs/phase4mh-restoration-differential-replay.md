# Phase 4MH — Restoration Differential Replay and State-Equivalence Certification

## Outcome

Phase 4MH compares direct Phase 4ME validation with Phase 4MF snapshot, Phase 4MG migration, and
retained-suffix restoration at every canonical cut point. It requires exact generation, head,
burned-nonce set, transaction/receipt state, replay-refusal decisions, and recovery classification.

## Differential localization

Byte-identical record replays are canonicalized consistently with Phase 4ME idempotency. Conflicting
replays remain invalid. Omitted valid suffix data reports the first different state field; reordered,
substituted, corrupted, or anchor-incompatible data reports the exact cut and restoration errors.

## Reproducible evidence

- Five-cut multi-transaction certification SHA-256:
  `cc02220fba8cbbb3f1050682f189770517065dd7feb3b417bd3b59bec117e4e8`
- Omitted-suffix localization SHA-256:
  `f02b9837cd7921921a6a298fac21b1fda1ce9b02450a3e4f484c09df458246e1`
- The omitted suffix localizes its first divergent field to `generation`.

## Safety and removal

Certification is read-only and in-memory. It does not persist snapshots, compact production data,
execute repairs, change runtime state, control WSL or services, access the network, or create any
order. Remove the script, focused test, and this report to roll back.

## Next phase

Phase 4MI — Differential-certification mutation coverage and minimization audit.
