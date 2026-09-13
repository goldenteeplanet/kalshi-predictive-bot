# Weather evidence review — 2026-09-08

Observation evidence collected successfully; trading readiness remains **BLOCKED**.

The status UI showed disabled execution, a completed observation rehearsal, a stale
status snapshot, and unreported forecast/roadmap evidence. A new diagnostic SQLite
database now retains seven public response bodies with receipt times and SHA-256
hashes, 24 NOAA hourly forecast periods, and three exact market/event/series lineage
records. This archive is deliberately not compatible with the trading database:
it has only `sources`, `forecasts`, and `settlement_lineage` tables. No candidate,
forecast certification, settlement value, portfolio, or order state was promoted.

## Captured results

The audited capture ran from 01:28:31 to 01:28:43 UTC on September 8, 2026.
All seven requests returned HTTP 200. The artifact directory is
`20260908T0130-weather-evidence` under the local `CodexPaperRehearsals` directory;
the actual acquisition times are in `result.json`, not inferred from its name.

Database SHA-256:
`f19780ecfb2c1d067f17900417375841699c032b06646dc1bf13ed68c172ec9c`.

The selected event was `KXTEMPNYCH-26SEP0722`, with markets ending
`T66.99`, `T67.99`, and `T68.99`. Each was active with more than 15 minutes
remaining at discovery. The fresh event catalog confirms series `KXTEMPNYCH`.
Rules explicitly name The Weather Company and station KNYC. The series also
announces a September 9 transition to Synoptic Data. This is captured source
lineage, not certification of the precise cutover time or final settlement value.

The station API identified KNYC and supplied coordinates 40.7833, -73.9667.
The NWS points response selected OKX grid 34,45. Its hourly forecast was generated
at 00:45:27 UTC; its underlying update was September 7 at 18:19:54 UTC.
All 24 archived periods are **FORECAST_STALE** against the existing 1,800-second
phase-3U freshness limit. Receipt time never replaces provider issue time.
The NWS grid forecast is analytical context, not the TWC station settlement value.
NWS documents this station/points/grid distinction in its
[API documentation](https://www.weather.gov/documentation/services-web-API).

The public TWC page was archived but did not establish a final KNYC value.
Contract terms are linked in the captured metadata; the PDF could not be verified
through the web reader. Contract methodology, finality/rounding, and the effective
source mapping remain uncertified. No settlement result was invented.

## Safety and verification

- Fixed maximums: 3 markets, 25 GETs, 1 MB per response, 15 seconds per request,
  570-second internal budget and 600-second parent watchdog. No retries, redirects,
  authenticated endpoints, application imports, scheduler, or trading services.
- A new unsynced directory is mandatory; existing and linked paths are refused.
  Child requests receive only a minimal system environment, excluding credentials.
- After creating the three-table schema, a SQLite authorizer permits only evidence
  inserts and reads/transactions. Order writes, updates, deletes, DDL and ATTACH
  are denied. Source hashes, exact table set, and integrity are audited at completion.
- 40 targeted tests passed (weather evidence and existing observation rehearsal).
  After tightening failure-stop behavior and portable path checks, all 14 weather
  evidence tests passed again. Ruff checks passed; no suppressions or runtime
  safety configuration changes. The full repository suite was not rerun for this
  standalone diagnostic collector.
- Previous observation database SHA-256 remained
  `b089290655ae30823da8c9b2d2c7ad7da11d8e140eabd663cd275d6b1e0879f7`.
  Other worktrees were not edited. No deployment or orders occurred.

## Repeat, stop, and rollback

Run `python -I scripts/local/weather_evidence_rehearsal.py --root <new-unsynced-directory>`
with the reviewed Python environment. Each run is one bounded evidence capture;
it does not start an observation loop or trading. Errors, identity/source conflict,
missing or invalid timestamps, and stale forecasts preserve blocked readiness.
An acquisition failure stops the dependent chain; a failed optional TWC page
fetch is recorded. No automatic retry or scope expansion follows a failed gate.

The UI should describe these as historical evidence, retaining acquisition times.
Do not refresh an artifact timestamp to disguise old provider data. The status
snapshot will naturally become stale after five minutes without a new review.
Its general database views still describe the earlier read-only observation DB;
weather evidence appears in the progress workstreams/roadmap and report entries.

Rollback requires no database restore: close the diagnostic collector (it exits
after one capture), retain its evidence archive, and restore the saved prior
status JSON if the UI presentation needs reverting. Never replace a trading or
observation DB with this diagnostic archive. Revert this isolated branch's commit
if removing the collector; preserve the other worktrees and audit artifacts.

## Next Codex phase

Review this isolated evidence branch. Certify exact KNYC settlement methodology,
finality, and the September 9 TWC-to-Synoptic cutover from authoritative public
sources. Perform one bounded fresh forecast capture into a new isolated database,
preserving provider issue/update times and all existing freshness limits. Only
after those evidence gates pass, propose the bounded observation-only 24-cycle
soak and recheck executable-book availability. Preserve all worktrees. Do not
deploy, enable trading, or create orders. Report unresolved blockers and the next phase.
