from __future__ import annotations

from typer.testing import CliRunner

from kalshi_predictor import phase3bb_r3_free_source_inventory as inventory
from kalshi_predictor.cli import app


def _row(category: str, score: int, blocker: str = "PAPER_GATE_NOT_OPEN") -> dict[str, object]:
    return {
        "category": category,
        "score": score,
        "top_blocker": blocker,
        "next_implementation_step": f"build {category}",
    }


def test_economic_sources_defer_tradingeconomics() -> None:
    family = inventory.source_family_for_category("economic")

    assert "FRED" in family["free_source_options"]
    assert "BLS" in family["official_source_options"]
    assert family["paid_deferred_sources"] == "TradingEconomics=DEFERRED"


def test_selector_prefers_best_noncrypto_over_crypto() -> None:
    selected = inventory.select_best_noncrypto_category(
        [
            _row("crypto", 150, "BACKGROUND_WAIT_FOR_EXECUTABLE_BOOK"),
            _row("sports", 12, "UNSUPPORTED_KXMVE_COMPOSITES_PARKED"),
            _row("weather", 80, "EV_NOT_POSITIVE"),
        ]
    )

    assert selected["category"] == "weather"
    assert selected["crypto_selected"] is False


def test_acceptance_requires_noncrypto_selection_when_available() -> None:
    rows = [
        _row("weather", 40),
        _row("crypto", 100, "BACKGROUND_WAIT_FOR_EXECUTABLE_BOOK"),
    ]
    selected = inventory.select_best_noncrypto_category(rows)
    acceptance = inventory._acceptance(rows, selected)

    assert acceptance["one_best_noncrypto_category_selected"] is True
    assert acceptance["crypto_not_selected_unless_every_noncrypto_path_worse"] is True
    assert acceptance["no_live_demo_or_paper_orders"] is True


def test_category_score_penalizes_background_crypto() -> None:
    base = {
        "active_markets": 100,
        "parsed_markets": 100,
        "linked_markets": 100,
        "source_ready_rows": 100,
        "forecast_ready_rows": 100,
        "ranking_ready_rows": 100,
        "paper_gate_ready_rows": 0,
        "top_blocker": "PAPER_GATE_NOT_OPEN",
    }

    weather = inventory.category_score({"category": "weather", **base})
    crypto = inventory.category_score(
        {"category": "crypto", **base, "top_blocker": "BACKGROUND_WAIT_FOR_EXECUTABLE_BOOK"}
    )

    assert weather > crypto


def test_phase3bb_r3_cli_help_exposes_command() -> None:
    result = CliRunner().invoke(app, ["phase3bb-r3-free-source-inventory", "--help"])

    assert result.exit_code == 0
    assert "phase3bb-r3-free-source-inventory" in result.output
