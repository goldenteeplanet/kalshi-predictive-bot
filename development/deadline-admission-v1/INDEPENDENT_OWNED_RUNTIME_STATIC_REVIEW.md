# Independent static review — scoped pre-run acceptance

No blocking finding for one bounded synthetic cloud fixture run under root's external resource and absolute deadline envelope. This is static review only: no execution, host call, provider call or source edit occurred.

Reviewed SHA256 pins:

- owned_runtime.py: `f39eacba4b64637a19e468c9cb7ffc39200610aca21f6d2801841c2ade2c2cc5`
- worker_entry.py: `11744c9ff16ebeb3945ef61d26511809260a833933b7ec8e01ffc90e6455a017`
- test_owned_runtime.py: `bfccce6274b0819e4ea4d6aa6324c4948bf3ed2aceb9a8c16a293407bd3c413b`

The worker parent preserves an exclusive intent before a fresh admission gate. Actual child entry gates again immediately before exec. The parent monitors both absolute UTC work cutoff and a monotonic runtime budget; scheduling delay does not extend the original cutoff. All process-group signals target the newly created session leader's group. WNOWAIT observes leader exit without reaping it, preserving its PID until unconditional group TERM then KILL. Cleanup executes in finally even when the leader exits first. Linux subreaper status allows adopted same-group descendants to be reaped after the leader. The six new cases cover normal completion, persistence delay, overrun, leader-first exit, TERM-resistant descendant and actual delayed child-entry refusal.

Limits to preserve in result interpretation:

- `workload_started=True` means an entry process was launched. It does not prove the substantive command executed; the entry can refuse with exit 78. Read child_returncode and marker evidence accordingly.
- Teardown waits include the grace sleep, a leader wait up to safety seconds, and a separate descendant-reap interval up to safety seconds. The single reserved safety margin is not a proven worst-case teardown bound. Scheduling and I/O can add delay; root's external absolute job envelope remains necessary. The tests can establish observed completion timing for their fixtures, not a universal guarantee.
- Source and command provenance, readonly placement and external cgroup limits are supplied by root. This runner itself does not hash executable source or authenticate custody/UTC.
- This is reviewed cooperative code. A descendant that creates another session/process group is outside this group-cleanup claim. stdout/stderr have no intrinsic byte ceiling and require external resource containment; these six fixture commands produce bounded output.
- The subreaper setting persists for the runner process, so root must keep that process dedicated to this job. Failure during cleanup leaves no success result; absence of a result requires reconciliation, not retry of the consumed identity.

The test module docstring still says three cases although six methods are present; this is cosmetic. Cloud result evidence is pending and must be recorded independently of this pre-run review.
