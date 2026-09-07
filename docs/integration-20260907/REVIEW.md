# All-code integration review — 2026-09-07

Branch: `integration/all-code-20260907`.

This is a local integration branch for review. It has not been pushed, merged into
main, deployed, or used to run a trading service.

## Included work

- All 49 branch tips from the initial inventory are ancestors of this branch.
- 31 branches were proven equivalent to existing patches or squash-merge trees.
  Their ancestry was recorded without replacing newer code with older versions.
- The refreshed `origin/main` tip, `1d28ddc`, was merged with the local recovery work.
- All fetched remote tips are included. The final remote audit added the 17-commit
  runtime-stabilization branch and recorded the exact squash-equivalent shadow
  preflight branch. Dynamic weather selection and strict explicit ticker validation
  were preserved while adding a skipped-status artifact for an empty dynamic scope.
- Four immutable worktree snapshots preserve the original inputs: 505 paths in the
  main working copy, two orchestrator paths, one weather-shadow path with only
  line-ending differences, and five operator-console paths.
- Previously unmerged opportunity diagnostics, weather-alpha validation,
  executable-book gating, market-rollover handling, and operator reports are included.
- 69 loose trading-project scripts are retained under `archive/workspace-reference`.
  They are historical reference material, not installed or executed application code.
  An unrelated personal-document utility was excluded.

The original working copies were not switched, reset, stashed, or committed by this
review. Snapshot commits were created with temporary Git indexes. Other tasks have
continued changing the source working copy during the review; changes after the
named snapshots are outside this integration's scope.

Exact source commits, file hashes, snapshot paths, and equivalence evidence are in
[input-manifest.json](input-manifest.json).

## Merge decisions

- Kept the newer forecast-to-ranking identity validation and terminal-settlement
  checks, including the distinction between placeholders and completed settlements.
- Combined the remote preflight savepoint rollback with dynamic weather selection
  and local cache handling. Historical hard-coded August ticker selection was not
  reintroduced. The scheduler's configurable database path is retained.
- Added market-rollover catalog ingestion and richer readiness diagnostics while
  retaining bounded scheduler execution, fair market-family allocation, batched
  weather-source selection, and existing blocker names.
- Kept the current operator navigation and added the pending operator report commands.

## Review findings corrected

1. **Conflicting CLI registrations.** Four command names had incompatible handlers.
   The original `runtime-origin` report options work again; deployment-manifest
   inspection is available as `runtime-deployment-origin`. The three report-only
   sports-artifact variants now use `-artifacts` suffixes. Their imports are aliased
   so they no longer replace the database-backed report implementations. A source
   test checks for duplicate command registrations.
2. **Broken CI test selection.** The safety workflow referenced a nonexistent test
   file. It now runs the existing reconciliation-attribution and reconciliation-plan
   tests. A regression test checks that every referenced test path resolves.
3. **Credential-scan false positives.** Public operator confirmation phrases and
   deliberate fake credential test inputs are explicitly allowlisted at their
   source lines. Credential detection remains enabled.
4. **Stale UI test expectation.** An older shell test expected a previous cache-busting
   asset version. It now checks the version used by the integrated evidence UI.
5. **Lost executable file mode.** The fixed-rate refresh service directly executes
   its shell script. The remote script's executable bit was restored after resolving
   the content conflict, avoiding a permission-denied failure on a fresh checkout.
6. **Watcher cleanup crash on Windows.** Loading the current process as a native
   library with `ctypes.CDLL(None)` raises `TypeError` on Windows. Optional heap
   trimming now runs only on Linux; session cleanup and engine disposal still run
   on every platform. The POSIX process-probe tests explicitly exercise that code
   path, and the migration test uses Alembic's actual graph head instead of an older
   hard-coded revision.
7. **Concurrent Windows artifact publication.** Transient sharing violations can
   prevent replacement while readers hold the current artifact open. Publication
   now retries only those Windows errors for at most five seconds, retaining the
   old file until atomic replacement succeeds. It never unlinks the current artifact
   or falls back to a partial overwrite.
8. **Checkout-byte certification mismatch.** Git's Windows newline conversion
   changed the bytes that certification compares against committed blobs. Text
   checkouts now use LF via `.gitattributes`, and the 75 certified source paths were
   normalized and their index metadata refreshed. Hash checks remain unchanged.
9. **Scheduler shard starvation.** Choosing a shard from epoch-second parity does
   not alternate when the interval is even. A cycle counter now alternates the
   two bounded capture groups independently of clock parity.

## Validation

The Windows integration regression checks passed **41 tests**, and the changed
settlement, watcher, model-repair, migration, and deployment checks passed **70 tests**.
All **6,884 tests in the final branch collected successfully**. Python compilation and the repository credential scan
passed, and the dedicated Windows environment has no broken package requirements.
The initial Linux focused run completed with **105 passed and one failure**, caused by the stale
UI asset-version assertion corrected above. The attempted full Linux suite was
interrupted when the WSL environment disappeared and subsequently returned a
connection failure (`Wsl/Service/0x8007274c`). It is not a completed full-suite result.

The broader Phase 4 run completed with **4,604 passed, two skipped, and seven failed**.
The seven failures exposed the Windows publication and newline-certification issues
described above. A subsequent targeted run passed **71 tests**, including the four
previously failing concurrent-reader cases and the additional remote runtime changes.
Final certification retest results are recorded in the validation summary.
All **13 certification tests passed**, including the three previously failing
cases and the clean-clone audit. Thus every failure found in the broader Phase 4
run has a passing targeted retest. The entire suite was not rerun after those fixes.

Detailed results and the tested code commit are in
[validation-summary.json](validation-summary.json). The Windows checks used Python
3.12.14; they did not reproduce the workflow's exact Python 3.11.9 environment.

The source-wide lint check reports **1,701 findings**, predominantly existing
line-length and import-order debt. No undefined-name or duplicate-definition
findings remain in that scan. The branch is therefore not CI-green, and the new
workflow's lint job remains a known blocker. This integration does not claim a
line-by-line audit of every historical module or production readiness.
All lint findings are listed in [lint-findings.json](lint-findings.json).

## Follow-up boundaries

- Reconcile later work from concurrently running tasks as a separate, explicit input.
- Clear the recorded lint debt and complete an uninterrupted full-suite run before
  treating this integration as a release candidate.
- Review historical archive scripts individually before adapting or executing them.

No live orders, notifications, cloud deployment scripts, or database migrations were
executed as part of this review.
