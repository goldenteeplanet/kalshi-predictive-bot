# All-code integration review — 2026-09-07

Branch: `integration/all-code-20260907`.

PR #57 merged this integration into main at `4b69da8` on 2026-09-08, after all
13 hosted checks passed. It has not been deployed or used to run a trading service.
The sections below retain the original integration and validation history.

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

## Cleanup follow-up

The follow-up stayed on this integration branch and did not merge later changes
from active tasks or modify their working copies.

- Cleared the recorded 1,701 lint findings through formatting, import cleanup,
  equivalent Python 3.11 syntax, and removal of an unused timestamp assignment.
  The rule selection and 100-column limit remain unchanged; no lint suppressions
  were added. Explicit first-party classification for `kalshi_predictor` and
  third-party classification for Alembic make Ruff 0.5.0 and 0.16.6 agree.
- Wrapped long generated command strings while checking their syntax trees and
  literal values. The literal audit found only a wrapped help docstring and
  whitespace in one generated Python script; that script's AST was unchanged.
- Repaired CI constraints that prevented installation: corrected the nonexistent
  pandas-stubs version to `2.2.2.240603`, raised Rich to `13.8.0` for Typer 0.26,
  and selected Psycopg `3.1.4`, which has Python 3.11 binary wheels. Separate
  `psycopg` and `psycopg-binary` constraints replace the unsupported extras syntax.
- Consolidated four identical JSON digest implementations into a pure utility.
  Private compatibility aliases retain their `value` or `payload` keyword.
  Golden digest tests preserve Unicode serialization, key ordering, and rejection
  of unsupported values. The duplicate-helper detector remains enabled.
- Made two missing-history fixtures delete the entry referenced by the manifest,
  instead of depending on directory iteration order. Separate tests now verify
  that deleting the manifest also fails closed. Runtime history validation was
  not relaxed.
- Updated three older weather-provenance fixtures to use the `KXTEMPNYCH` scope
  required by the integrated `weather_v2` selector. Closed-market, stale-status,
  metadata-preservation, and unsupported-series checks remain exercised. No
  selector predicates were relaxed.
- Gave UI security tests a temporary database and explicit app settings, removing
  their dependence on a pre-existing local database. Header, request-ID, audit
  redaction, schema-hardening, and trusted-host assertions remain unchanged.
- Updated the exact-ticker fixture to preserve input order within one series,
  matching the integrated selector. Added coverage for fair-share selection
  across series, deduplication, and zero/oversized limits. Runtime ordering was
  not changed.

## Validation

The complete suite ran in four independent Linux checkouts of `c1c735c`, using
Python 3.11.9 and the repaired CI constraints. All **6,884 original cases** were
covered: **6,873 passed, 10 failed,
and 1 skipped**. The failures were the duplicate digest
inventory, two directory-order-dependent history fixtures, three weather
fixtures outside the supported model scope, three UI security tests that
depended on an existing local database, and an outdated exact-ticker ordering
expectation. All were corrected.

The history and digest fixes passed a combined **100-test regression run** at
`5de6bbc`, including all six added regression cases. Commit `0e5bc91`
passed **18 weather regressions**, including the three
failing provenance cases and unsupported-series checks. Commit `34270a4`
passed **19 security and progress UI regressions**. The final code
commit, `f476c12`, passed **4 ticker regressions**, including one
new check for fair-share limits across series. The final collection contains
**6,891 cases**. An XML-to-collection audit accounts for every
case across the full run and final regressions: **6,891 passed
and 0 skipped**. The optional SDK comparison skipped on
Linux passed separately with all six conformance checks in the dedicated Windows
Python 3.12.14 environment and Kalshi SDK 10.0.0. Those checks used public-data
fixtures, without credentials or live requests. This is combined coverage; the entire
suite was not rerun after the isolated fixes.

| Check | Result |
| --- | --- |
| Ruff 0.5.0, matching CI | Zero findings |
| Ruff 0.16.6 | Zero findings |
| Python compilation | Pass |
| Repository credential scan | Pass |
| Dependency consistency | Pass |
| Operator shell syntax | Pass |
| Final test collection | 6,891 cases |

Some parametrizations iterate over sets, so process-specific ordering caused 13
duplicate executions and 13 omissions in the initial split. The omitted cases all
passed in a supplemental batch. Coverage was checked by test identity, and the
launcher now sorts IDs before partitioning for future runs. An earlier launcher
import-path error was corrected before execution; it is not counted as a product
failure. Raw local logs are retained in `reports/integration-20260907` alongside the
workspace. Detailed counts, skip reasons, dependency versions, full-run failures,
and prior validation history are in [validation-summary.json](validation-summary.json).

The container reproduced CI's Python version and dependency constraints, using
Debian 12 instead of Ubuntu 24.04. Hosted CI and the additional Python-version
matrix have not run here. The branch is **ready for pull request review**; this
does not establish readiness to start a paper-trading service.

## Follow-up boundaries

PR follow-up: [pull request #57](https://github.com/goldenteeplanet/kalshi-predictive-bot/pull/57)
contains the hosted validation. Its first cumulative-safety run exposed three
recovery-certification failures because the checkout omitted historical commits.
The cumulative and full-suite jobs now fetch full history, retaining the existing
ancestry and blob checks. The local workflow and recovery-certification retest
passed all 24 cases. The PR's current checks are authoritative for its latest SHA.

A [bounded paper-readiness rehearsal](PAPER_REHEARSAL.md) is prepared with fresh
public-market evidence and an empty, unsynced database. It has not been started
and does not authorize paper or exchange order creation.

- Reconcile later work from concurrently running tasks as a separate, explicit input.
- Open the integration pull request and require hosted CI to pass before merging.
- Prepare a bounded paper-only readiness rehearsal before starting any service.
- Review historical archive scripts individually before adapting or executing them.

No live orders, external notifications, deployments, or production database
migrations were executed. Validation used disposable checkouts and test databases.
