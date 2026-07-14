from dataclasses import dataclass


@dataclass(frozen=True)
class SchedulerStep:
    command: str
    every_minutes: int
    purpose: str


SCHEDULER_PROFILES: dict[str, tuple[SchedulerStep, ...]] = {
    "meta-watch": (
        SchedulerStep(
            "kalshi-bot build-meta-features --model-scope all",
            15,
            "Build meta features from local market, model, signal, and data-quality state.",
        ),
        SchedulerStep(
            "kalshi-bot build-meta-training --days 90",
            15,
            "Refresh settled-market training examples for meta model diagnostics.",
        ),
        SchedulerStep(
            "kalshi-bot forecast --model meta_model_v1",
            15,
            "Generate paper/demo meta_model_v1 forecasts.",
        ),
        SchedulerStep(
            "kalshi-bot forecast --model meta_ensemble_v1",
            15,
            "Generate paper/demo meta_ensemble_v1 forecasts.",
        ),
        SchedulerStep(
            "kalshi-bot meta-evaluate --days 90 --output reports/meta_evaluation.md",
            15,
            "Compare meta forecasts against ensemble_v2 and market_implied_v1.",
        ),
        SchedulerStep(
            "kalshi-bot meta-report --output reports/meta_report.md",
            15,
            "Write the main meta model report.",
        ),
        SchedulerStep(
            "kalshi-bot signal-report --output reports/signal_report.md",
            15,
            "Refresh Signal Marketplace ROI and confidence tracking.",
        ),
    ),
    "microstructure-watch": (
        SchedulerStep(
            "kalshi-bot collect-once --status open --limit 100 --max-pages 1",
            5,
            "Collect fresh public market snapshots and orderbooks.",
        ),
        SchedulerStep(
            "kalshi-bot build-microstructure-features --lookback-minutes 60",
            5,
            "Build read-only microstructure features from stored snapshots.",
        ),
        SchedulerStep(
            "kalshi-bot forecast --model microstructure_v1",
            5,
            "Generate microstructure_v1 paper/demo forecasts.",
        ),
        SchedulerStep(
            "kalshi-bot find-opportunities --model-name microstructure_v1 --limit 20",
            5,
            "Find microstructure opportunities for review.",
        ),
        SchedulerStep(
            "kalshi-bot signal-report --output reports/signal_report.md",
            5,
            "Refresh signal marketplace report.",
        ),
        SchedulerStep(
            "kalshi-bot microstructure-report --output reports/microstructure_report.md",
            5,
            "Write the microstructure dashboard report.",
        ),
    ),
    "crypto-watch": (
        SchedulerStep(
            (
                "kalshi-bot phase3bc-r5-unattended-start --refresh-open-markets "
                "--diagnose-snapshots --forecast-current-windows-only "
                "--skip-opportunity-report --ranking-repair --ranking-repair-limit 500 "
                "--near-money-only "
                "--market-limit 150 --market-max-pages 1 "
                "--near-money-per-symbol-limit 40 --near-money-window-limit 20 "
                "--snapshot-fetch-concurrency 2 "
                "--crypto-series-tickers KXBTC,KXETH,KXSOLE,KXXRP,KXDOGE "
                "--crypto-market-scan-limit 2500 --crypto-link-limit 500 "
                "--forecast-limit 1000 --opportunity-limit 500 --phase3bc-limit 1000 "
                "--cycles 32 --interval-minutes 15 --duration-hours 8 "
                "--timeout-grace-seconds 900"
            ),
            15,
            (
                "Refresh public crypto prices, bounded open Kalshi snapshots, crypto links, "
                "current-window crypto_v2 forecasts, rankings, Phase 3BC readiness, "
                "Phase 3BC-R4 diagnostics, and paper-only Phase 3M/3N risk preflight "
                "for clean fresh positive-EV rows, with PID/log/timeout guardrails."
            ),
        ),
    ),
    "sports-watch": (
        SchedulerStep(
            "kalshi-bot ingest-sports --league MLB --input-file data/mlb_latest.json",
            10,
            "Import latest manual/public-free sports data; repeat per configured league.",
        ),
        SchedulerStep(
            "kalshi-bot link-sports-markets --league ALL",
            10,
            "Refresh links between Kalshi sports markets and games.",
        ),
        SchedulerStep(
            "kalshi-bot build-sports-features --league ALL",
            10,
            "Build sports features and sports signal events.",
        ),
        SchedulerStep(
            "kalshi-bot forecast --model sports_v1",
            10,
            "Generate paper/demo sports forecasts for linked markets.",
        ),
        SchedulerStep(
            "kalshi-bot sports-opportunities --model-name sports_v1 --league ALL",
            10,
            "Write sports opportunity diagnostics for review.",
        ),
    ),
    "self-evaluation-nightly": (
        SchedulerStep(
            (
                "kalshi-bot self-evaluate --output reports/self_evaluation_journal.md "
                "--json-output reports/self_evaluation_journal.json"
            ),
            1440,
            "Write the read-only Phase 3P nightly self-evaluation journal.",
        ),
    ),
    "feature-discovery-nightly": (
        SchedulerStep(
            (
                "kalshi-bot feature-discovery-run --run-type INCREMENTAL "
                "--output reports/feature_discovery_report.md "
                "--json-output reports/feature_discovery_report.json"
            ),
            1440,
            "Run read-only Phase 3Q incremental feature discovery research.",
        ),
    ),
    "synthetic-markets-nightly": (
        SchedulerStep(
            (
                "kalshi-bot synthetic-markets-run --enable-research --mode shadow "
                "--input-file data/synthetic_markets_candidates.json "
                "--output reports/synthetic_markets_report.md "
                "--json-output reports/synthetic_markets_report.json"
            ),
            1440,
            "Run internal-only Phase 3R synthetic market research with no execution actions.",
        ),
    ),
    "rl-policy-nightly": (
        SchedulerStep(
            (
                "kalshi-bot rl-evaluate --enable-research "
                "--output reports/rl_policy_report.md "
                "--json-output reports/rl_policy_report.json"
            ),
            1440,
            "Run disabled-by-default Phase 3S offline policy evaluation research.",
        ),
    ),
    "paper-market-health-watch": (
        SchedulerStep(
            (
                "kalshi-bot phase3ay-unattended-start --cycles 1 "
                "--interval-seconds 0 --paged-markets --market-max-pages 1"
            ),
            5,
            (
                "Refresh exact-ticker settlement harvests, paper P&L, market collection, "
                "market coverage, sports placeholder watch, and the roadmap with PID/log guards."
            ),
        ),
    ),
}


def scheduler_plan(profile: str) -> tuple[SchedulerStep, ...]:
    try:
        return SCHEDULER_PROFILES[profile]
    except KeyError as exc:
        raise ValueError(f"Unknown scheduler profile: {profile}") from exc
