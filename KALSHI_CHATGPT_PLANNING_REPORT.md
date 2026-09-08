# Kalshi bot: evidence review and planning handoff

Prepared September 8, 2026, approximately 01:43 UTC (September 7 in Chicago).
Purpose: import this document into ChatGPT to develop a phased engineering and
operational plan toward eventual live trading. This document does not authorize
deployment, activation, or any paper, demo, or live order.

## 1. Executive assessment

**The bot is not ready for live trading. The next observation-only soak should
not be started on the present evidence.** The project contains substantial market
data, forecasting, risk, paper-ledger, UI, and certification code, but code presence
and passing tests do not establish current operational readiness or profitability.

This review improved the weather evidence archive and fixed a freshness-reporting
defect. A new isolated database contains 24 NOAA forecast periods, three exact
market/event/series lineage records, and six hashed public responses. Every
forecast remains stale under the existing 30-minute limit. The current hourly
contracts identify TWC and KNYC. A Kalshi series notice announces a September 9
change to Synoptic, but precise effective-time and final settlement methodology
remain uncertified. No candidate or settlement was promoted into a trading DB.

The correct immediate objective is a reproducible, authoritative evidence chain.
Once that passes, subsequent stages can evaluate observation health, simulated
execution, economic performance, and operational safety. None is interchangeable
with the others. A successful capture is not a successful strategy.

## 2. Scope, boundaries, and terminology

Authorized in this task: read code and public sources; run bounded unauthenticated
GET requests; write diagnostic evidence to a new isolated SQLite database; test
and correct the evidence collector; prepare this report and update status evidence.

Prohibited in this task: deployment, service activation for trading, enabling
execution/autopilot, and creating paper, demo, or live orders. No authenticated
account endpoints were inspected. Account eligibility, balances, API permissions,
production hosting, and legal/tax suitability are therefore **not assessed**.

Terminology for future planning:

- **Observation-only:** collect and evaluate data; no orders of any kind.
- **Paper simulation:** local simulated orders/fills; requires a separate scope.
- **Demo exchange execution:** exchange sandbox requests; distinct from local paper.
- **Live execution:** real exchange orders and financial exposure; requires its own
  review, explicit authorization, and operating limits.

The current UI is a local read-only view. Its progress report describes isolated
rehearsal evidence. General database pages still read the earlier observation
database; they do not ingest this diagnostic archive. Global missing source or
certification badges are not evidence of a healthy production runtime.

## 3. Repository and integration state

Repository: https://github.com/goldenteeplanet/kalshi-predictive-bot

Reviewed branch: `evidence/weather-lineage-20260908`.
Review starting commit: `1dadee2fa1ca85301a1ab3a777de58a641d410bd`.
Remote main observed during this review:
`140e12f85f1c6f1aa76a25f91c8cda5d84a4093b`.

GitHub read-only checks confirmed:

| Change | Current recorded state | Merge commit |
| --- | --- | --- |
| PR #57, integrated code and cleanup | Merged September 8 at 00:32:39 UTC | `4b69da8d7b529ccf33eda3e4a087becd0240ae0d` |
| PR #58, bounded observation runner | Merged September 8 at 01:06:35 UTC | `140e12f85f1c6f1aa76a25f91c8cda5d84a4093b` |

The integration review records 49 initial branch tips, snapshot/equivalence
handling, lint cleanup, and historical hosted verification. Those results are
historical evidence, not a fresh full-suite certification of this review branch.
The detailed source inventory is in `docs/integration-20260907/input-manifest.json`.
The existing validation report identifies its own tested commit and environment.
Do not transfer a green result from one commit to a later commit without checks.

Other worktrees were enumerated but not reset, switched, pruned, merged, or edited.
Some registered worktrees have WSL-style paths; their registration does not justify
deleting them. Later active-task changes remain outside this evidence branch.

## 4. What exists and what is demonstrated

The README and source tree describe public market collection, raw JSON persistence,
forecasting, weather/crypto linking, feature/model evaluation, paper ledgers,
guardrails, a local UI, and readiness/certification modules. Treat that inventory
as implementation scope, not a complete functional audit of every subsystem.

| Area | Evidence available | Practical limitation |
| --- | --- | --- |
| Public market reads | Bounded successful unauthenticated captures | Current depth/quote health must be rechecked |
| Observation runner | Prior 5-cycle run; 3 markets; 15 snapshots; 21 GETs; zero orders | Does not establish 24 healthy decision cycles |
| Weather forecasts | 24 periods archived with two provider clocks | All stale; no approved current forecast |
| Identity | Three exact event/series joins and KNYC rule references | Not a verified final settlement value |
| Risk/execution controls | Existing disabled defaults; isolated scripts avoid execution imports | End-to-end production controls not certified here |
| Local UI | Read-only status and roadmap evidence displayed | Snapshot ages; many global feeds remain unreported |
| Model/strategy | Code and historical diagnostic machinery exist | No current after-cost, out-of-sample edge demonstrated here |
| Production operation | No deployment performed | Host state, recovery, monitoring, and credentials not audited |

The prior completed observation run reported `NO_EXECUTABLE_BOOK` for sampled
books lacking NO bids. That is a historical sample, not a claim that all Kalshi
markets lack liquidity now. This weather review did not fetch new books.

## 5. Authoritative settlement findings

### 5.1 Current contract identity

Fresh Kalshi public API responses link these three tickers to event
`KXTEMPNYCH-26SEP0722` and series `KXTEMPNYCH`:

- `KXTEMPNYCH-26SEP0722-T66.99`
- `KXTEMPNYCH-26SEP0722-T67.99`
- `KXTEMPNYCH-26SEP0722-T68.99`

The sampled rules name Central Park/New York City, KNYC, The Weather Company,
and the September 7, 2026 10 PM EDT temperature. They apply an above-threshold
comparison. The metadata gives close time 02:00 UTC and occurrence time 02:05 UTC.
Do not silently substitute either metadata timestamp for the rule's observation
time in a generic resolver; the discrepancy requires an explicit mapping.

Source: fresh response bodies in the new evidence database, obtained from the
[series endpoint](https://external-api.kalshi.com/trade-api/v2/series/KXTEMPNYCH),
the open-market catalog, and the exact event endpoint.

### 5.2 Broad methodology verified; precise implementation still incomplete

Kalshi's July 22 help article says hourly contracts use the temperature at the
specified time from the named station, with TWC as the source. It distinguishes
preliminary readings from final values and warns about rounding/conversion
differences. It describes an approximate 25–35 minute settlement delay after
close. Daily climate-report and daily standard-time rules should not be applied
to these hourly contracts merely because the station is the same.
[Official weather-market help](https://help.kalshi.com/en/articles/13823837-weather-markets).

This establishes the broad method, not a complete executable settlement algorithm.
We still need the exact observation selection policy, precision, conversion and
rounding order, correction/finality policy, missing-data fallback, and proof of
the final source value for a resolved example. The contract PDF was not verified
through the web reader. No substitute unofficial methodology was adopted.

### 5.3 September 9 cutover

The fresh series metadata announces a September 9 transition of KXTEMPNYCH,
KXTEMPCHIH, and KXTEMPLAXH to Synoptic, retaining TWC methodology until then.
**The existence of the notice is verified. Exact UTC cutover and affected-contract
mapping are not.** Do not assume midnight UTC, midnight New York time, or a
particular first event. The notice, current market rules, and series source field
must be retained together so conflicts block selection instead of being hidden.

The older general help article and broad August 27 partnership announcement
cannot override a later series-specific change notice. The partnership article
describes TWC's outcome-data role but does not specify this cutover.
[Kalshi announcement](https://news.kalshi.com/p/kalshi-weather-company-partnership).

Synoptic's public documentation describes station time-series queries, units,
quality-control options, and a required API token. It does not, by itself, prove
Kalshi's settlement selection algorithm or its source-transition schedule. No
token was sought or used, and no paid access was purchased.
[Synoptic time-series documentation](https://docs.synopticdata.com/services/time-series).

### 5.4 Questions that need authoritative answers

1. Which exact first KXTEMPNYCH event uses Synoptic, and what is the effective UTC time?
2. Does the provider change depend on contract listing, observation, close, or settlement time?
3. Which Synoptic station/sensor/network and field are authoritative for KNYC?
4. Is the selected value instantaneous, nearest-time, averaged, interpolated, or otherwise derived?
5. What tolerance, unit conversion, rounding, missing-data, and correction rules apply?
6. Where is the final value publicly verifiable, and when does it become final?
7. Which published rule controls if catalog metadata and event rules temporarily disagree?

These are suggested questions for the operator to resolve through published
contract documentation or Kalshi support. No message was sent on the user's behalf.

## 6. Fresh forecast capture and audit

New run: `20260908-review-refresh`, 01:39:39–01:39:48 UTC on September 8.
New database: `evidence.db`, diagnostic-only schema.

| Audit item | Result |
| --- | --- |
| Requests | 6 public GETs, all HTTP 200 |
| Source bodies | 6, receipt timestamps and SHA-256 retained |
| Forecast periods | 24 |
| Settlement-lineage records | 3 |
| Forecast status | 24 `FORECAST_STALE` |
| Database integrity | `ok` |
| Tables | `sources`, `forecasts`, `settlement_lineage` only |
| Orders created | 0 |
| Freshness limit | 1,800 seconds, unchanged |

Database SHA-256:
`de14dc4a0b578b97e12b83f354a0d60387ca24d98ef63e77980edd5301beb170`.

NWS station metadata supplied KNYC coordinates 40.7833, -73.9667; the points
response selected hourly grid OKX/34,45. The response was generated at 00:45:27 UTC;
its underlying update was September 7 at 18:19:54 UTC. The fresh download returned
the same forecast payload hash as the previous capture. Re-downloading old data
does not make its forecast fresh. A grid forecast is analytical context, not a
certified final station reading.

Limits retained: at most 3 markets, 25 GETs, 1 MB per response, 15 seconds per
request, 570 seconds internally, and a 600-second parent watchdog. No retries or
redirects. Market discovery requires active status and at least 15 minutes to close.
Existing or linked output directories and OneDrive paths are refused. The database
authorizer allows only evidence inserts and reads/transactions after schema setup.

The current review did not fetch the TWC landing page after the web reader rejected
it. In any case, an HTTP-200 landing page is not station/time/finality evidence.
The collector no longer performs that misleading optional page fetch.

## 7. Code review finding and validation

**Finding corrected:** the original collector computed top-level freshness from
`generatedAt` while recording `updateTime` only inside each row's JSON. A fresh
response-generation timestamp could therefore mask old underlying data in the
summary. Missing generation time could also fall back to the update time.

The revised collector requires both clocks, retains them separately, and reports
invalid, future, stale, or expired evidence conservatively. Neither timestamp is
replaced by receipt time. The numeric 1,800-second limit and all runtime trading
settings are unchanged. This only changes the standalone diagnostic collector;
the application forecasting pipeline has not been claimed fixed or certified.

Nineteen focused tests passed, including new cases for fresh generation with stale
updates, missing clocks, future updates, and the exact freshness boundary. Ruff
0.16.6 and CI-pinned Ruff 0.5.0 passed on changed code/tests. Earlier work recorded
40 targeted observation/evidence tests. The complete repository suite and hosted
CI were not rerun for this task. A future PR needs checks on its exact final SHA.

Remaining review limitations: no independent second reviewer; no full execution
audit; no authenticated provider verification; no proof of profitable forecasts;
no exhaustive malformed-source fuzzing. Diagnostic `ANALYTICAL_ONLY` is never a
trade-ready verdict. Readiness remains blocked regardless of collector exit success.

## 8. Blocker register and acceptance evidence

| ID | Blocker | Evidence needed to close it |
| --- | --- | --- |
| W1 | Forecast freshness | Both provider clocks valid and within existing limits; correct valid-time coverage |
| W2 | Settlement methodology | Versioned authoritative rule with station, observation selection, conversion, rounding and finality |
| W3 | Source cutover | Exact effective UTC time and first affected event, with conflict handling |
| W4 | Final source value | Resolved example reproducible from official value and contract rule |
| D1 | Diagnostic/application separation | Reviewed import/provenance contract; no accidental promotion of diagnostic rows |
| L1 | Executable liquidity | Fresh two-sided book and existing depth/spread/age/size gates satisfied |
| O1 | Healthy decision history | Required consecutive healthy cycles with real candidate and source-health evidence |
| M1 | Economic validity | Out-of-sample calibration and after-cost performance, leakage checks and uncertainty |
| R1 | Operational safety | Verified recovery, reconciliation, single-writer ownership and stop behavior |
| C1 | Release assurance | Exact-SHA review and required hosted checks; complete appropriate suite |
| A1 | Live authorization | Operator-approved scope, limits and account/operational prerequisites |

W1–W4 are currently open. This task does not propose starting a soak while those
evidence prerequisites remain unresolved. L1/O1 are also open; no artificial
candidate, false source-health report, or fabricated soak history should close them.

## 9. Conditional planning framework for ChatGPT

This is a dependency framework for discussion, not authorization or an executable
soak/deployment plan. Each phase must finish with evidence, unresolved issues,
rollback steps, and a next prompt. Do not estimate a live date from code volume.

| Phase | Objective and deliverable | Exit gate / dependency |
| --- | --- | --- |
| 0 | Establish one authoritative code/evidence baseline; retain other tasks | Exact SHA, artifact inventory, clean review scope |
| 1 | Resolve W1–W4; version market-specific source rules and provider clocks | Authoritative mapping and fresh reproducible evidence |
| 2 | Review application ingestion and decision provenance using offline fixtures | No source substitution, leakage, stale promotion, or unintended writes |
| 3 | Only then design an observation-only soak under existing gates | W1–W4 and ingestion gate closed; bounded scope reviewed |
| 4 | Evaluate observed decisions and model performance with costs | Adequate out-of-sample evidence; liquidity and economic gates defined and met |
| 5 | Separately authorize local paper lifecycle testing | Fill assumptions, fees, exposure accounting, reconciliation and stops verified |
| 6 | Audit production execution path and release operations without activation | Idempotency, partial fills, cancellations, restart/recovery, kill switch and exact-SHA CI evidence |
| 7 | Operator decision on any demo/live pilot | Explicit separate authorization and numerical limits; all preceding gates passed |
| 8 | If eventually authorized, monitor a constrained pilot before any expansion | Predeclared stop/rollback criteria and measured results; no automatic scaling |

The existing GH-4 code refers to 24 consecutive healthy cycles and source/candidate
quality gates. The short five-cycle rehearsal is not equivalent. GH-4 also checks
Kalshi streaming, crypto and weather source evidence; an NYC-only archive cannot
be claimed to satisfy the whole gate. Determine the intended supported scope and
review its requirements explicitly rather than suppressing unrelated failures.

Future numerical financial limits are intentionally **TBD by the operator**:
capital at risk, per-order size, aggregate and correlated exposure, loss/drawdown
limits, open-order count, allowed markets/hours, and manual intervention ownership.
Do not invent these values or infer live permission from a planning discussion.

## 10. Rollback, continuity, and report use

All new collection writes are confined to the new diagnostic archive. No shared
database restore is needed. Retain failed/stale captures for audit rather than
overwriting them. Revert the evidence-collector commit if necessary; do not alter
other worktrees or copy `evidence.db` over an application database. The previous
status snapshot is retained locally for presentation rollback.

The status UI is at `http://127.0.0.1:8765/system/progress` on this computer only.
ChatGPT elsewhere cannot access that URL or local repository paths. This document
is self-contained; upload the `.md` or `.txt` copy. The optional evidence ZIP also
contains result JSON, the diagnostic database and a hash manifest. No credentials,
account exports, private keys, or production environment files are included.

If ChatGPT needs deeper implementation review, provide selected source files or
a deliberately reviewed repository bundle later. This report does not include
the entire repository and should not be represented as a complete security audit.

## 11. Prompt to paste into ChatGPT with this report

> Use this report as a dated evidence baseline, not as proof of live readiness.
> Help me design a gated, multi-phase plan for the Kalshi bot. First distinguish
> verified facts, unverified assumptions, and external blockers. Prioritize exact
> settlement methodology/cutover and forecast freshness. For each phase specify
> dependencies, concrete code/data deliverables, tests, measurable exit criteria,
> stop conditions, rollback, and the Codex prompt that performs only that phase.
> Keep observation-only, local paper, demo exchange, and live execution distinct.
> Do not propose starting the soak before the evidence blockers close. Identify
> the operator decisions needed before any financial exposure. Do not assume
> profitability or authorize deployment, trading activation, or orders. Ask only
> the most consequential missing questions and preserve all other worktrees.

## 12. Immediate next Codex phase

Review the freshness fix and this evidence report. Resolve W2–W4 from authoritative
contract/source material: exact KNYC observation selection, rounding/finality,
and the first event affected by the Synoptic cutover. Preserve source documents,
hashes and effective times; fail closed on disagreement. After a new provider
issuance, perform one bounded forecast refresh in a new isolated database and
check both clocks against the unchanged limit. If authoritative evidence remains
unavailable, produce precise operator questions and keep the soak blocked.
Preserve worktrees; do not deploy, enable trading, or create orders. Report the
remaining gates and next phase.
