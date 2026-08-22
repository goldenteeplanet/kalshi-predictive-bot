# Canonical Wrapper Inventory

Generated: `2026-08-22T00:19:13.923317+00:00`
Status: `READY`

## Lane totals

| Lane | Commands | Tables | Artifacts | Writers | Counters |
|---|---:|---:|---:|---:|---:|
| GH2_OPERATIONAL_SOAK | 9 | 9 | 6 | 1 | 2 |
| CURRENT_MARKET_RESEARCH | 337 | 55 | 6 | 3 | 4 |
| HISTORICAL_REPLAY | 51 | 53 | 3 | 2 | 3 |
| GUARDED_PAPER_LEARNING | 36 | 25 | 3 | 2 | 4 |

## Phase 3BB weather wrapper chain

| Command | Writer callable | Transaction | Writer semantics | Disposition |
|---|---|---|---|---|
| `phase3bb-r2-weather-fast-lane` | `write_phase3bb_r2_weather_fast_lane_report` | commit | current-research database writer | compatibility wrapper retained |
| `phase3bb-weather-fast-lane` | `write_phase3bb_weather_fast_lane_report` | none | read-only diagnostic/report writer | compatibility wrapper retained |
| `phase3bb-r44-weather-catalog-hook-runtime-verification` | `write_phase3bb_r44_weather_catalog_hook_runtime_verification_report` | rollback | read-only diagnostic/report writer | compatibility wrapper retained |
| `phase3bb-r45-weather-freshness-to-ranking-impact` | `write_phase3bb_r45_weather_freshness_to_ranking_impact_report` | rollback | read-only diagnostic/report writer | compatibility wrapper retained |
| `phase3bb-r46-cloud-scheduler-weather-writer-gate-repair` | `write_phase3bb_r46_cloud_scheduler_weather_writer_gate_repair_report` | rollback | conditional external scheduler writer | compatibility wrapper retained |
| `phase3bb-r47-weather-current-window-series-discovery-linkability-repair` | `write_phase3bb_r47_weather_current_window_series_discovery_report` | rollback | read-only diagnostic/report writer | compatibility wrapper retained |
| `phase3bb-r48-weather-feature-refresh-runtime-verification` | `write_phase3bb_r48_weather_feature_refresh_runtime_verification_report` | rollback | read-only diagnostic/report writer | compatibility wrapper retained |
| `phase3bb-r49-weather-missing-link-apply-after-feature-refresh` | `write_phase3bb_r49_weather_missing_link_apply_after_feature_refresh_report` | rollback | read-only diagnostic/report writer | compatibility wrapper retained |
| `phase3bb-r50-weather-post-link-ranking-fast-lane-recheck` | `write_phase3bb_r50_weather_post_link_ranking_fast_lane_recheck_report` | rollback | read-only diagnostic/report writer | compatibility wrapper retained |
| `phase3bb-r51-weather-ranking-path-repair` | `write_phase3bb_r51_weather_ranking_path_repair_report` | rollback | read-only diagnostic/report writer | compatibility wrapper retained |
| `phase3bb-r52-weather-ev-fair-value-diagnostic` | `write_phase3bb_r52_weather_ev_fair_value_diagnostic_report` | rollback | read-only diagnostic/report writer | compatibility wrapper retained |
| `phase3bb-r53-weather-current-window-cadence-preview-narrowing-repair` | `write_phase3bb_r53_weather_current_window_cadence_report` | rollback | read-only diagnostic/report writer | compatibility wrapper retained |
| `phase3bb-r54-weather-missing-link-apply-deferral` | `write_phase3bb_r54_weather_missing_link_apply_deferral_report` | rollback | read-only diagnostic/report writer | compatibility wrapper retained |
| `phase3bb-r55-weather-ranking-path-retry` | `write_phase3bb_r55_weather_ranking_path_retry_report` | rollback | read-only diagnostic/report writer | compatibility wrapper retained |
| `phase3bb-r57-weather-selected-window-pipeline-speed-repair` | `write_phase3bb_r57_weather_selected_window_pipeline_report` | rollback | read-only diagnostic/report writer | compatibility wrapper retained |
| `phase3bb-r58-weather-selected-window-forecast-feature-alignment-repair` | `write_phase3bb_r58_weather_selected_window_alignment_report` | rollback | read-only diagnostic/report writer | compatibility wrapper retained |
| `phase3bb-r59-weather-catalog-refresh-r57-retry` | `write_phase3bb_r59_weather_catalog_refresh_r57_retry_report` | rollback | read-only diagnostic/report writer | compatibility wrapper retained |
| `phase3bb-r60-weather-next-window-lead-time-scheduler-repair` | `write_phase3bb_r60_weather_next_window_lead_time_report` | rollback | read-only diagnostic/report writer | compatibility wrapper retained |
| `phase3bb-r42-weather-fast-lane-post-unblock-verification` | `write_phase3bb_r42_weather_fast_lane_post_unblock_report` | rollback | read-only diagnostic/report writer | compatibility wrapper retained |
| `phase3bb-r43-weather-catalog-scheduler-hook` | `write_phase3bb_r43_weather_catalog_scheduler_hook_report` | rollback | conditional external scheduler writer | compatibility wrapper retained |

## Consolidated same-lane helpers

- `kalshi_predictor.phase3bb_weather_common` owns check, first_line, mark_executable, stdout, target_payload, write_probe_csv, write_rows_csv. Command names, writer callables, outputs, and transactions unchanged.

## Command consolidation gate

No Phase 3BB weather command was removed or aliased: each calls a distinct writer and retains a distinct artifact contract. Only byte-for-byte-equivalent same-lane helper implementations were consolidated.
