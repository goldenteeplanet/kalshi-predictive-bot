# Phase 4JU — Least-privilege task identity audit

Phase 4JU accepts only a standard-user batch identity with no administrator membership, highest-privilege flag, interactive logon, or network dependency. Its exact capability set is limited to reading the supervisor artifact, writing supervisor-local state, and emitting a local alert.

Missing required rights fail; duplicate or additional rights—including explicit host-shutdown authority—are treated as tampered. Incomplete, malformed, or integrity-broken evidence fails closed. Identity and policy details are represented only by hashes.

`PASS` certifies the proposed identity boundary only. It grants no task activation, restart, service-control, or execution authority and performs no identity, policy, filesystem, Task Scheduler, process, or host mutation.
