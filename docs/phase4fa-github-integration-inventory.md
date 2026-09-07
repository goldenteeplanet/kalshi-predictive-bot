# Phase 4FA — GitHub Integration Inventory

Observed read-only on 2026-08-26 for `goldenteeplanet/kalshi-predictive-bot`:

| Surface | Observation |
|---|---|
| Repository | Public, default branch `main` |
| Workflows | Five active local/remote workflows; PR/path and manual dispatch triggers |
| Workflow token | Default `read`; cannot approve pull-request reviews |
| Workflow declarations | All five declare only `contents: read` |
| Actions policy | Enabled, all actions allowed, SHA pinning not required |
| Action references | `actions/checkout@v4`, `setup-python@v5`, `upload-artifact@v4`; floating major tags |
| Webhooks | None |
| Environments | None |
| Actions secret metadata | No secret names |
| Dependabot secret metadata | No secret names |
| Main branch protection | Absent |
| Repository rulesets | None |
| Required checks | None; latest `main` had no check runs |
| CodeQL default setup | Not configured |
| Secret scanning | Disabled |
| Push protection | Disabled |
| Dependabot security updates | Disabled; alerts endpoint confirms disabled |
| GitHub Apps | Unknown: existing OAuth token type cannot enumerate installations |

Local workflow SHA-256 values at observation time:

- `gh2-paper-only-cycle.yml`: `f243c4557fcd9c042066f4ff30ac9cfcf0712e7f77d95e6d5f2e1532844667bf`
- `gh4-paper-activation-readiness.yml`: `a3318459b50e05420acfe357eba515565b9703e5db189d8e86104839e47a747f`
- `pmb28-offline-certification.yml`: `97f3227ba094cf3d908f68bfdc7135a441777c75855e10592fd23af6b382b0a9`
- `prov14b-r2c-offline-certification.yml`: `b2e9fba3f391833a145155acb85fb7dfa624e92825f9c7393b302e0a9703d7fe`
- `ui-obs2h-notification-pipeline.yml`: `7c128e1110eeedf743cdde3923eaf630c99e3f27215189537190f463057d0868`

The audit used only local reads and GitHub REST `GET` requests through the already-authenticated
CLI. It did not request broader scopes, read secret values, install or authorize an App, change
settings, create hooks or environments, or modify workflows. The App-installation limitation is
reported as unknown rather than incorrectly inferred as “none.”
