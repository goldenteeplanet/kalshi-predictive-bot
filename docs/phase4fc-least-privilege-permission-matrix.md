# Phase 4FC — Least-Privilege GitHub Permission Matrix

| Identity | Maximum repository grants | Explicitly omitted |
|---|---|---|
| CI | `contents:read`, `checks:write` | deployments, packages, secrets, administration |
| Code scanning | `contents:read`, `security-events:write` | administration and organization scope |
| Dependency updates | `contents:write`, `pull-requests:write` | workflows, administration, organization scope |
| Notifications | `actions:read`, `checks:read`, `contents:read` | repository writes and secret administration |
| Codex review | `contents:read`, `pull-requests:write` | administration, organization access, environments, secrets |

The validator rejects undeclared grants, grants above the per-role ceiling, and all administration, organization-member, organization-secret, Actions-secret, and environment permissions. Separate job identities are preferred; write grants are never inherited by read-only jobs. This phase configures nothing remotely and authorizes no App.
