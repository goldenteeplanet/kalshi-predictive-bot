import ast
from pathlib import Path

ROOT = Path(__file__).parents[1] / "src" / "kalshi_predictor"

ALLOWED_IMPORTERS = {
    "kalshi_predictor.current_research_common": {
        "phase3ag_crypto.py",
        "phase3ar.py",
        "phase3bb_r45_weather_freshness_to_ranking_impact.py",
        "phase3bc.py",
        "phase3bc_r3.py",
        "phase3bc_r4.py",
        "phase3bc_r5.py",
        "phase3bc_r5_alignment.py",
        "phase3bc_r6.py",
        "phase3bc_r7.py",
        "phase3bc_r16.py",
        "phase3bc_r17.py",
    },
    "kalshi_predictor.historical_replay_common": {
        "backtesting/engine.py",
        "crypto/backtest.py",
        "phase3aa_r2.py",
        "phase3aa_r3.py",
        "phase3aa_r4.py",
        "phase3aa_r5.py",
        "phase3aa_r6.py",
        "tournament/engine.py",
        "weather/backtest.py",
    },
    "kalshi_predictor.guarded_paper_common": {
        "learning/cycle.py",
        "learning/diagnostics.py",
        "phase3bb_r33_cloud_paper_only_operations_readiness.py",
    },
    "kalshi_predictor.gh2_soak_common": {
        "live_readiness/contracts.py",
        "phase_gh2.py",
        "roadmap/runtime_reports.py",
        "system_certification/contracts.py",
    },
}


def _canonical_helper_importers() -> dict[str, set[str]]:
    importers = {module: set() for module in ALLOWED_IMPORTERS}
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in importers:
                importers[node.module].add(relative)
    return importers


def test_canonical_helpers_have_only_reviewed_same_lane_importers() -> None:
    assert _canonical_helper_importers() == ALLOWED_IMPORTERS


def test_canonical_helpers_do_not_import_other_lane_helpers() -> None:
    helper_files = {f"{module.rsplit('.', 1)[1]}.py" for module in ALLOWED_IMPORTERS}
    importers = _canonical_helper_importers()

    assert not any(
        importer in helper_files
        for module_importers in importers.values()
        for importer in module_importers
    )
