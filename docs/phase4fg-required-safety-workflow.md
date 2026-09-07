# Phase 4FG — Required Safety Check Workflow

`.github/workflows/phase4-required-safety.yml` is repository-local and non-deploying. It runs Ruff, focused Phase 4F tests, the complete Phase 4 suite, mutation scanners, a deterministic offline high-confidence credential scan, and artifact-lineage tests. The workflow has only `contents:read`, uses exact action SHAs and Python 3.11.9, references no environment or secret, and has no manual/scheduled or deployment trigger. Adding YAML does not make it a remotely required check; a future repository ruleset change remains separately approval-gated.
