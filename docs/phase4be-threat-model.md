# Phase 4BE — Threat model and adversarial review

Phase 4BE defines fourteen ordered threat classes: artifact forgery, hash substitution, path
confusion, symlink/hard-link attacks, TOCTOU, clock manipulation, stale approvals, replay, partial
publication, database replacement, malicious environment variables, command injection, service
impersonation/alternate writers, and compromised disposable markers.

Each class has mandatory named controls, evidence hashes, deterministic adversarial test outcome,
severity, and residual risk. Missing controls, non-passing tests, invalid evidence, or unresolved
high-severity residual risk blocks advancement. Coverage and ordering are exact, preventing a threat
from being silently omitted or renamed.

Outputs are `phase4be.formal-threat-model.v1` and
`phase4be.adversarial-risk-report.v1`. Focused tests independently remove controls from all fourteen
classes and cover failed evidence, high residual risk, missing/reordered/unknown threats, duplicated
controls, invalid hashes/risk levels, tampering, timezone handling, and the non-executing static
surface.

