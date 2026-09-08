"""Code-owned scoped typing policy for the guarded paper release path.

P17/P18 permit relevant checks while global debt stays separately disclosed.
Targets derive from the reviewed callable/service closure, not supplied reports.
"""

import hashlib
import re
from pathlib import Path
from typing import Any

POLICY_VERSION = "paper-release-typing-v1"
TYPING_TARGETS: tuple[str, ...] = (
    'src/kalshi_predictor/advanced_risk/engine.py',
    'src/kalshi_predictor/advanced_risk/repository.py',
    'src/kalshi_predictor/advanced_risk/service.py',
    'src/kalshi_predictor/autopilot/repository.py',
    'src/kalshi_predictor/config.py',
    'src/kalshi_predictor/crypto/assets.py',
    'src/kalshi_predictor/crypto/providers.py',
    'src/kalshi_predictor/crypto/semantics.py',
    'src/kalshi_predictor/data/repositories.py',
    'src/kalshi_predictor/data/schema.py',
    'src/kalshi_predictor/evaluation/calibration.py',
    'src/kalshi_predictor/evaluation/metrics.py',
    'src/kalshi_predictor/forecasting/base.py',
    'src/kalshi_predictor/forecasting/skip_log.py',
    'src/kalshi_predictor/forecasting/sports_v1.py',
    'src/kalshi_predictor/forecasting/weather_v2.py',
    'src/kalshi_predictor/kalshi/client.py',
    'src/kalshi_predictor/kalshi/orderbook.py',
    'src/kalshi_predictor/kalshi/protocol_math.py',
    'src/kalshi_predictor/learning/duplicates.py',
    'src/kalshi_predictor/memory/capture.py',
    'src/kalshi_predictor/memory/contracts.py',
    'src/kalshi_predictor/memory/repository.py',
    'src/kalshi_predictor/opportunities/payout_scoring.py',
    'src/kalshi_predictor/opportunities/scanner.py',
    'src/kalshi_predictor/opportunities/scoring.py',
    'src/kalshi_predictor/overnight_paper/acquisition.py',
    'src/kalshi_predictor/overnight_paper/activation.py',
    'src/kalshi_predictor/overnight_paper/books.py',
    'src/kalshi_predictor/overnight_paper/boundary.py',
    'src/kalshi_predictor/overnight_paper/boundary_gate.py',
    'src/kalshi_predictor/overnight_paper/candidate_assembly.py',
    'src/kalshi_predictor/overnight_paper/cli.py',
    'src/kalshi_predictor/overnight_paper/coordinator.py',
    'src/kalshi_predictor/overnight_paper/dashboard.py',
    'src/kalshi_predictor/overnight_paper/dataset_store.py',
    'src/kalshi_predictor/overnight_paper/discovery.py',
    'src/kalshi_predictor/overnight_paper/evaluation_dataset.py',
    'src/kalshi_predictor/overnight_paper/gate_context.py',
    'src/kalshi_predictor/overnight_paper/model_release.py',
    'src/kalshi_predictor/overnight_paper/monitoring.py',
    'src/kalshi_predictor/overnight_paper/preparation.py',
    'src/kalshi_predictor/overnight_paper/preparation_runner.py',
    'src/kalshi_predictor/overnight_paper/provenance.py',
    'src/kalshi_predictor/overnight_paper/provenance_gate.py',
    'src/kalshi_predictor/overnight_paper/qualification.py',
    'src/kalshi_predictor/overnight_paper/qualified_scan.py',
    'src/kalshi_predictor/overnight_paper/release_typing.py',
    'src/kalshi_predictor/overnight_paper/rule_verifier.py',
    'src/kalshi_predictor/overnight_paper/runtime_liveness.py',
    'src/kalshi_predictor/overnight_paper/runtime_owner.py',
    'src/kalshi_predictor/overnight_paper/settlement.py',
    'src/kalshi_predictor/overnight_paper/settlement_runner.py',
    'src/kalshi_predictor/overnight_paper/source_health.py',
    'src/kalshi_predictor/overnight_paper/store.py',
    'src/kalshi_predictor/overnight_paper/supervisor.py',
    'src/kalshi_predictor/overnight_paper/timing.py',
    'src/kalshi_predictor/overnight_paper/watcher.py',
    'src/kalshi_predictor/overnight_paper/weather_driver.py',
    'src/kalshi_predictor/paper/ledger.py',
    'src/kalshi_predictor/paper/models.py',
    'src/kalshi_predictor/paper/pnl.py',
    'src/kalshi_predictor/paper/simulator.py',
    'src/kalshi_predictor/position_sizing/repository.py',
    'src/kalshi_predictor/position_sizing/service.py',
    'src/kalshi_predictor/position_sizing/sizer.py',
    'src/kalshi_predictor/provenance/dual_write.py',
    'src/kalshi_predictor/reinforcement_learning/contracts.py',
    'src/kalshi_predictor/runtime_stage_heartbeat.py',
    'src/kalshi_predictor/signals/attribution.py',
    'src/kalshi_predictor/signals/registry.py',
    'src/kalshi_predictor/signals/signal_types.py',
    'src/kalshi_predictor/signals/skip_log.py',
    'src/kalshi_predictor/sports/repository.py',
    'src/kalshi_predictor/system_certification/migration_diagnostics.py',
    'src/kalshi_predictor/tournament/ranking.py',
    'src/kalshi_predictor/utils/decimals.py',
    'src/kalshi_predictor/utils/time.py',
    'src/kalshi_predictor/weather/features.py',
    'src/kalshi_predictor/weather/ingestion.py',
    'src/kalshi_predictor/weather/linker.py',
    'src/kalshi_predictor/weather/monthly_rain.py',
    'src/kalshi_predictor/weather/observation_shadow.py',
    'src/kalshi_predictor/weather/providers.py',
    'src/kalshi_predictor/weather/repository.py',
    'src/kalshi_predictor/weather/station_observations.py',
    'src/kalshi_predictor/weather/temperature_contracts.py',
    'src/kalshi_predictor/weather/temperature_probability.py',
)


def typing_command() -> str:
    return "mypy --follow-imports=silent " + " ".join(TYPING_TARGETS)


def _source_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def typing_manifest(repository: Path) -> dict[str, Any]:
    """Original source/config binding; this does not claim a check has run."""
    return {
        "version": POLICY_VERSION,
        "targets": list(TYPING_TARGETS),
        "source_sha256": {name: _source_hash(repository / name) for name in TYPING_TARGETS},
        "config_sha256": {
            name: _source_hash(repository / name)
            for name in ("pyproject.toml", "mypy.ini", ".mypy.ini", "setup.cfg")
            if (repository / name).is_file()
        },
    }


def verify_typing_evidence(repository: Path, report: dict[str, Any], output: bytes) -> None:
    """Require actual successful output and either full or exact reviewed scope."""
    decoded = output.decode("utf-8")
    match = re.search(r"Success: no issues found in ([1-9][0-9]*) source files?", decoded)
    if match is None or re.search(r"\berror:|Found [1-9][0-9]* errors?", decoded):
        raise ValueError("MYPY_EVIDENCE_NOT_GREEN")
    if report.get("mypy_command") == "mypy src":
        return
    if report.get("mypy_command") != typing_command():
        raise ValueError("MYPY_REVIEWED_COMMAND_REQUIRED")
    if report.get("mypy_policy") != typing_manifest(repository):
        raise ValueError("MYPY_REVIEWED_SOURCE_SCOPE_REQUIRED")
    if int(match.group(1)) != len(TYPING_TARGETS):
        raise ValueError("MYPY_CHECKED_TARGET_COUNT_MISMATCH")
    if report.get("typing_scope") != "PAPER_RELEASE_PATH_ONLY":
        raise ValueError("MYPY_SCOPED_RESULT_MUST_BE_LABELED")
