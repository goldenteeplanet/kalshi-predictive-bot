# Refresh & Readiness Operator Runbook

## Safety boundary

The dashboard and API are read-only. They read generated JSON and JSONL artifacts and
do not open an exchange connection, create a paper order, reserve risk, or change any
trading configuration. No trading state is mutated by viewing or certifying the page.

## Normal operation

1. Open `/system/refresh-readiness`.
2. Confirm the source is `CURRENT` or that a cloud envelope is `VERIFIED_CLOUD`.
3. Confirm the cycle identifier, generation time, and deployment identity.
4. Review pipeline stages, the quality scorecard, changes, blockers, and incidents.
5. Treat paper readiness as blocked unless every existing safety gate passes.

Collect a checksummed cloud envelope without changing service state:

```bash
sudo -u kalshi scripts/cloud/collect-refresh-readiness-status.sh
```

Copy the resulting envelope to
`reports/phase_gh2/authoritative_cloud_status.json` in the review workspace. The UI
will reject missing required identity fields or any checksum mismatch.

## Degraded states

- `NO_SOURCE_DATA`: confirm the GH-2 timer and report path.
- `INVALID_SOURCE`: quarantine the malformed artifact and inspect the writer log.
- `STALE_INPUT`: check the timer, writer lock, and last successful completion.
- `VALID_ZERO_ACTIVE_MARKETS`: the cycle ran but found no active eligible markets.
- `NO_ELIGIBLE_ROWS`: active data existed but nothing reached the candidate manifest.
- Unverified cloud evidence: recollect and checksum the snapshot; never relabel it as
  authoritative manually.

## Incidents

Incidents deduplicate by code. Repeated observations increase the occurrence count.
When the triggering condition clears, the incident is marked resolved; its history is
retained. Do not lower edge, liquidity, freshness, or risk thresholds to clear alerts.

## Rollback

1. Stop serving the new route by reverting only the route and navigation registration.
2. Leave GH-2 collection and its safety settings unchanged.
3. Preserve `reports/phase_gh2/control_plane` for audit and diagnosis.
4. Restore the previous UI artifact, restart only the UI service, and verify existing
   `/system` and `/today` routes.
5. Confirm paper-order creation, live execution, and autopilot remain disabled.

## Rehearsal

Use fixtures to render missing, stale, valid-zero, degraded, and current states. Verify
the GET-only API matches the page data, the template hash matches the certified visual
baseline, responsive tables remain usable, and POST receives HTTP 405.
